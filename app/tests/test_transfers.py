import pytest

from app.ledger import LedgerError, account_balance, get_account
from app.onboarding import fund_account, onboard_company
from app.settlement import run_settlement
from app.simclock import advance_days
from app.transfers import internal_transfer, receive_money, send_money


@pytest.fixture()
def setup(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="seed")
    return company, admin


def test_internal_transfer_settles_instantly_and_moves_cash(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    savings = get_account(db, company.id, "1010")
    internal_transfer(db, company_id=company.id, from_account=checking, to_account=savings, amount_cents=300_000)
    run_settlement(db, company.id)  # instant settle_date, but run to actually post
    assert account_balance(db, checking) == 700_000
    assert account_balance(db, savings) == 300_000


def test_internal_transfer_same_account_rejected(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    with pytest.raises(LedgerError, match="differ"):
        internal_transfer(db, company_id=company.id, from_account=checking, to_account=checking, amount_cents=100)


def test_internal_transfer_requires_two_bank_accounts(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    software = get_account(db, company.id, "5010")
    with pytest.raises(LedgerError, match="bank accounts"):
        internal_transfer(db, company_id=company.id, from_account=checking, to_account=software, amount_cents=100)


def test_send_money_ach_pending_then_settles_and_hits_owner_draws(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    draws = get_account(db, company.id, "3100")
    before = account_balance(db, checking)
    send_money(db, company_id=company.id, from_account=checking, amount_cents=40_000, counterparty="Jane Founder", method="ach")
    assert account_balance(db, checking) == before
    advance_days(db, 1)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before - 40_000
    assert account_balance(db, draws) == -40_000  # draws reduce equity: debit-normal, so signed negative here


def test_send_money_wire_settles_same_day(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    before = account_balance(db, checking)
    send_money(db, company_id=company.id, from_account=checking, amount_cents=1_000, counterparty="Vendor", method="wire")
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before - 1_000


def test_receive_money_credits_other_income(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    other_income = get_account(db, company.id, "4900")
    before = account_balance(db, checking)
    receive_money(db, company_id=company.id, to_account=checking, amount_cents=15_000, counterparty="Grant Program", method="wire")
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before + 15_000
    assert account_balance(db, other_income) == 15_000


def test_invalid_method_rejected(db, setup):
    company, admin = setup
    checking = get_account(db, company.id, "1000")
    with pytest.raises(ValueError):
        send_money(db, company_id=company.id, from_account=checking, amount_cents=100, counterparty="X", method="cash")
    with pytest.raises(ValueError):
        receive_money(db, company_id=company.id, to_account=checking, amount_cents=100, counterparty="X", method="cash")
