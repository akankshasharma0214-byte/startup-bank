import pytest

from app.cards import CardError, authorize_charge, check_limit, issue_card, set_card_status
from app.ledger import account_balance, get_account
from app.models import CardStatus, LimitPeriod, Role
from app.onboarding import fund_account, onboard_company
from app.simclock import advance_days


@pytest.fixture()
def setup(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="seed")
    from app.models import User

    employee = User(company_id=company.id, name="Emp", email="e@a.com", role=Role.employee, password_hash="x")
    db.add(employee)
    db.commit()
    return company, admin, employee


def test_issue_card_generates_last4_and_defaults_active(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Emp Travel Card")
    assert len(card.last4) == 4 and card.last4.isdigit()
    assert card.status == CardStatus.active


def test_issue_card_requires_bank_account(db, setup):
    company, admin, employee = setup
    software = get_account(db, company.id, "5010")
    with pytest.raises(CardError, match="bank account"):
        issue_card(db, company_id=company.id, account=software, holder=employee, label="bad")


def test_issue_card_limit_amount_required_with_period(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    with pytest.raises(CardError, match="limit"):
        issue_card(db, company_id=company.id, account=checking, holder=employee, label="x", limit_period=LimitPeriod.daily)


def test_authorize_charge_creates_uncategorized_pending_txn_and_reduces_available(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Card")
    balance_before = account_balance(db, checking)
    txn = authorize_charge(db, card=card, amount_cents=2_500, merchant="Random Vendor")
    assert txn.category_id == get_account(db, company.id, "5900").id
    assert account_balance(db, checking) == balance_before  # not posted yet, only authorized

    from app.settlement import available_balance_delta

    assert available_balance_delta(db, checking.id, company.id) == -2_500


def test_authorize_charge_auto_categorizes_known_merchant(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Card")
    txn = authorize_charge(db, card=card, amount_cents=1_200, merchant="AWS")
    assert txn.category_id == get_account(db, company.id, "5010").id


def test_frozen_card_cannot_charge(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Card")
    set_card_status(db, card, CardStatus.frozen)
    with pytest.raises(CardError, match="frozen"):
        authorize_charge(db, card=card, amount_cents=100, merchant="Anywhere")


def test_closed_card_cannot_be_reactivated(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Card")
    set_card_status(db, card, CardStatus.closed)
    with pytest.raises(CardError, match="closed"):
        set_card_status(db, card, CardStatus.active)


def test_per_transaction_limit_enforced(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(
        db,
        company_id=company.id,
        account=checking,
        holder=employee,
        label="Card",
        limit_cents=5_000,
        limit_period=LimitPeriod.per_transaction,
    )
    authorize_charge(db, card=card, amount_cents=5_000, merchant="OK")  # exactly at limit is fine
    with pytest.raises(CardError, match="exceeds"):
        authorize_charge(db, card=card, amount_cents=5_001, merchant="Too much")


def test_daily_limit_accumulates_across_transactions(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(
        db, company_id=company.id, account=checking, holder=employee, label="Card", limit_cents=10_000, limit_period=LimitPeriod.daily
    )
    authorize_charge(db, card=card, amount_cents=6_000, merchant="A")
    authorize_charge(db, card=card, amount_cents=4_000, merchant="B")  # exactly hits limit
    with pytest.raises(CardError, match="daily"):
        authorize_charge(db, card=card, amount_cents=1, merchant="C")


def test_daily_limit_resets_next_day(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(
        db, company_id=company.id, account=checking, holder=employee, label="Card", limit_cents=10_000, limit_period=LimitPeriod.daily
    )
    authorize_charge(db, card=card, amount_cents=10_000, merchant="A")
    with pytest.raises(CardError):
        authorize_charge(db, card=card, amount_cents=1, merchant="B")
    advance_days(db, 1)
    authorize_charge(db, card=card, amount_cents=10_000, merchant="C")  # new day, fresh limit


def test_monthly_limit_uses_month_start(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(
        db, company_id=company.id, account=checking, holder=employee, label="Card", limit_cents=1_000, limit_period=LimitPeriod.monthly
    )
    check_limit(db, card, 1_000)  # should not raise
    with pytest.raises(CardError):
        check_limit(db, card, 1_001)


def test_no_limit_never_blocks(db, setup):
    company, admin, employee = setup
    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=employee, label="Card")
    authorize_charge(db, card=card, amount_cents=999_999, merchant="Huge purchase")
