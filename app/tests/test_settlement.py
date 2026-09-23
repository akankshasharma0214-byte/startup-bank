from datetime import date

import pytest

from app.ledger import account_balance, get_account
from app.models import Direction, TxnStatus, TxnType
from app.onboarding import fund_account, onboard_company
from app.settlement import available_balance_delta, run_settlement
from app.simclock import advance_days, sim_today
from app.transactions import create_transaction


@pytest.fixture()
def company(db):
    c, _ = onboard_company(
        db, name="Acme", legal_name="Acme Inc", ein="0", industry="x", admin_name="A", admin_email="a@acme.com", admin_password="x"
    )
    fund_account(db, company_id=c.id, amount_cents=1_000_000, memo="seed")
    return c


def test_pending_transaction_does_not_post_until_settle_date(db, company):
    checking = get_account(db, company.id, "1000")
    revenue = get_account(db, company.id, "4900")
    today = sim_today(db)
    txn = create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.debit,
        amount_cents=50_000,
        counterparty_account=revenue,
        txn_type=TxnType.wire_in,
        description="test",
        settle_date=today,
        created_date=today,
    )
    db.commit()
    balance_before = account_balance(db, checking)
    settled = run_settlement(db, company.id)
    assert txn in settled
    assert account_balance(db, checking) == balance_before + 50_000
    assert txn.status == TxnStatus.settled
    assert txn.journal_entry_id is not None


def test_future_settle_date_stays_pending(db, company):
    checking = get_account(db, company.id, "1000")
    revenue = get_account(db, company.id, "4900")
    today = sim_today(db)
    future = date(today.year + 1, 1, 1)
    create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.debit,
        amount_cents=1_000,
        counterparty_account=revenue,
        txn_type=TxnType.wire_in,
        description="future",
        settle_date=future,
        created_date=today,
    )
    db.commit()
    balance_before = account_balance(db, checking)
    settled = run_settlement(db, company.id)
    assert settled == []
    assert account_balance(db, checking) == balance_before


def test_advancing_sim_clock_settles_pending_ach(db, company):
    checking = get_account(db, company.id, "1000")
    revenue = get_account(db, company.id, "4900")
    today = sim_today(db)
    tomorrow = date.fromordinal(today.toordinal() + 1)
    create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.debit,
        amount_cents=2_500,
        counterparty_account=revenue,
        txn_type=TxnType.wire_in,
        description="ach",
        settle_date=tomorrow,
        created_date=today,
    )
    db.commit()
    assert run_settlement(db, company.id) == []
    advance_days(db, 1)
    settled = run_settlement(db, company.id)
    assert len(settled) == 1
    assert account_balance(db, checking) == 1_002_500


def test_settlement_is_idempotent(db, company):
    checking = get_account(db, company.id, "1000")
    revenue = get_account(db, company.id, "4900")
    today = sim_today(db)
    create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.debit,
        amount_cents=100,
        counterparty_account=revenue,
        txn_type=TxnType.wire_in,
        description="x",
        settle_date=today,
        created_date=today,
    )
    db.commit()
    run_settlement(db, company.id)
    bal_once = account_balance(db, checking)
    run_settlement(db, company.id)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == bal_once


def test_available_balance_delta_reflects_pending_only(db, company):
    checking = get_account(db, company.id, "1000")
    ap = get_account(db, company.id, "2000")
    today = sim_today(db)
    tomorrow = date.fromordinal(today.toordinal() + 1)
    create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.credit,
        amount_cents=10_000,
        counterparty_account=ap,
        txn_type=TxnType.bill_payment,
        description="pending bill",
        settle_date=tomorrow,
        created_date=today,
    )
    db.commit()
    assert available_balance_delta(db, checking.id, company.id) == -10_000
    advance_days(db, 1)
    run_settlement(db, company.id)
    assert available_balance_delta(db, checking.id, company.id) == 0


def test_settlement_scoped_to_company(db, company):
    from app.onboarding import onboard_company as ob

    other, _ = ob(
        db, name="Other", legal_name="Other", ein="1", industry="x", admin_name="B", admin_email="b@other.com", admin_password="x"
    )
    checking_other = get_account(db, other.id, "1000")
    revenue_other = get_account(db, other.id, "4900")
    today = sim_today(db)
    create_transaction(
        db,
        company_id=other.id,
        account=checking_other,
        direction=Direction.debit,
        amount_cents=999,
        counterparty_account=revenue_other,
        txn_type=TxnType.wire_in,
        description="x",
        settle_date=today,
        created_date=today,
    )
    db.commit()
    settled = run_settlement(db, company.id)  # settling company A must not touch company B's txn
    assert settled == []
    assert run_settlement(db, other.id) != []
