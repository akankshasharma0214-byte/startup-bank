from datetime import date, timedelta

import pytest

from app.billpay import add_vendor, approve_bill, create_bill, schedule_bill_payment, void_bill
from app.ledger import LedgerError, account_balance, get_account
from app.models import BillStatus, Role, TxnStatus, User
from app.onboarding import fund_account, onboard_company
from app.settlement import run_settlement
from app.simclock import advance_days


@pytest.fixture()
def setup(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="seed")
    bookkeeper = User(company_id=company.id, name="B", email="b@a.com", role=Role.bookkeeper, password_hash="x")
    db.add(bookkeeper)
    db.commit()
    return company, admin, bookkeeper


def test_add_vendor_validates_payment_method(db, setup):
    company, admin, _ = setup
    with pytest.raises(ValueError):
        add_vendor(db, company_id=company.id, name="V", payment_method="bitcoin")
    v = add_vendor(db, company_id=company.id, name="AWS", payment_method="ach")
    assert v.payment_method == "ach"


def test_admin_created_bill_is_auto_approved(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="AWS")
    category = get_account(db, company.id, "5010")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=category,
        amount_cents=10_000,
        memo="hosting",
        due_date=date(2026, 2, 1),
        created_by=admin,
    )
    assert bill.status == BillStatus.scheduled
    assert bill.approved_by_id == admin.id
    assert account_balance(db, category) == 10_000  # accrual posted immediately
    assert account_balance(db, get_account(db, company.id, "2000")) == 10_000


def test_bookkeeper_created_bill_needs_approval(db, setup):
    company, admin, bookkeeper = setup
    vendor = add_vendor(db, company_id=company.id, name="AWS")
    category = get_account(db, company.id, "5010")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=category,
        amount_cents=10_000,
        memo="hosting",
        due_date=date(2026, 2, 1),
        created_by=bookkeeper,
    )
    assert bill.status == BillStatus.pending_approval
    with pytest.raises(LedgerError, match="before payment"):
        schedule_bill_payment(db, bill, bookkeeper)


def test_non_admin_cannot_approve(db, setup):
    company, admin, bookkeeper = setup
    vendor = add_vendor(db, company_id=company.id, name="AWS")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=100,
        memo="",
        due_date=date(2026, 2, 1),
        created_by=bookkeeper,
    )
    with pytest.raises(PermissionError):
        approve_bill(db, bill, bookkeeper)


def test_approve_then_pay_settles_and_moves_ap_to_checking(db, setup):
    company, admin, bookkeeper = setup
    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="ach")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=25_000,
        memo="",
        due_date=date(2026, 2, 1),
        created_by=bookkeeper,
    )
    approve_bill(db, bill, admin)
    assert bill.status == BillStatus.scheduled

    checking = get_account(db, company.id, "1000")
    ap = get_account(db, company.id, "2000")
    balance_before = account_balance(db, checking)
    txn = schedule_bill_payment(db, bill, admin)
    assert bill.status == BillStatus.paid
    assert txn.status == TxnStatus.processing
    assert account_balance(db, checking) == balance_before  # ACH: not settled yet

    advance_days(db, 1)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == balance_before - 25_000
    assert account_balance(db, ap) == 0  # AP cleared


def test_wire_bill_payment_settles_same_day(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="Landlord", payment_method="wire")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5050"),
        amount_cents=500_00,
        memo="rent",
        due_date=date(2026, 2, 1),
        created_by=admin,
    )
    checking = get_account(db, company.id, "1000")
    before = account_balance(db, checking)
    schedule_bill_payment(db, bill, admin)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before - 500_00


def test_check_bill_payment_settles_after_three_business_days(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="Local Vendor", payment_method="check")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5040"),
        amount_cents=1_000,
        memo="",
        due_date=date(2026, 2, 1),
        created_by=admin,
    )
    checking = get_account(db, company.id, "1000")
    before = account_balance(db, checking)
    schedule_bill_payment(db, bill, admin)
    advance_days(db, 2)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before  # not yet, checks take ~3 business days
    advance_days(db, 2)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == before - 1_000


def test_cannot_pay_unapproved_bill(db, setup):
    company, admin, bookkeeper = setup
    vendor = add_vendor(db, company_id=company.id, name="V")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=100,
        memo="",
        due_date=date.today(),
        created_by=bookkeeper,
    )
    with pytest.raises(LedgerError):
        schedule_bill_payment(db, bill, admin)


def test_double_pay_rejected(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="V")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=100,
        memo="",
        due_date=date.today(),
        created_by=admin,
    )
    schedule_bill_payment(db, bill, admin)
    with pytest.raises(LedgerError):
        schedule_bill_payment(db, bill, admin)


def test_void_unapproved_bill_reverses_accrual(db, setup):
    company, admin, bookkeeper = setup
    vendor = add_vendor(db, company_id=company.id, name="V")
    category = get_account(db, company.id, "5010")
    bill = create_bill(
        db, company_id=company.id, vendor=vendor, category=category, amount_cents=100, memo="", due_date=date.today(), created_by=bookkeeper
    )
    assert account_balance(db, category) == 100
    void_bill(db, bill)
    assert bill.status == BillStatus.void
    assert account_balance(db, category) == 0
    assert account_balance(db, get_account(db, company.id, "2000")) == 0


def test_void_paid_bill_rejected(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="V")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=100,
        memo="",
        due_date=date.today(),
        created_by=admin,
    )
    schedule_bill_payment(db, bill, admin)
    with pytest.raises(LedgerError, match="paid"):
        void_bill(db, bill)


def test_bill_amount_must_be_positive(db, setup):
    company, admin, _ = setup
    vendor = add_vendor(db, company_id=company.id, name="V")
    with pytest.raises(ValueError):
        create_bill(
            db,
            company_id=company.id,
            vendor=vendor,
            category=get_account(db, company.id, "5010"),
            amount_cents=0,
            memo="",
            due_date=date.today() + timedelta(days=1),
            created_by=admin,
        )
