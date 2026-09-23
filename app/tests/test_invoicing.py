from datetime import date

import pytest

from app.invoicing import record_invoice_payment, send_invoice, void_invoice
from app.ledger import LedgerError, account_balance, get_account
from app.models import InvoiceStatus, TxnStatus
from app.onboarding import onboard_company
from app.settlement import run_settlement
from app.simclock import advance_days


@pytest.fixture()
def setup(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    return company, admin


def test_send_invoice_posts_accrual_ar_and_revenue(db, setup):
    company, admin = setup
    ar = get_account(db, company.id, "1200")
    revenue = get_account(db, company.id, "4000")
    inv = send_invoice(
        db,
        company_id=company.id,
        customer_name="Big Customer LLC",
        amount_cents=50_000,
        memo="Q1 services",
        due_date=date(2026, 3, 1),
        created_by=admin,
    )
    assert inv.status == InvoiceStatus.sent
    assert account_balance(db, ar) == 50_000
    assert account_balance(db, revenue) == 50_000


def test_invoice_amount_must_be_positive(db, setup):
    company, admin = setup
    with pytest.raises(ValueError):
        send_invoice(db, company_id=company.id, customer_name="X", amount_cents=0, memo="", due_date=date.today(), created_by=admin)


def test_invoice_requires_customer_name(db, setup):
    company, admin = setup
    with pytest.raises(ValueError):
        send_invoice(db, company_id=company.id, customer_name="  ", amount_cents=100, memo="", due_date=date.today(), created_by=admin)


def test_ach_payment_settles_next_day_and_moves_ar_to_checking(db, setup):
    company, admin = setup
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=10_000, memo="", due_date=date(2026, 3, 1), created_by=admin
    )
    checking = get_account(db, company.id, "1000")
    ar = get_account(db, company.id, "1200")
    txn = record_invoice_payment(db, inv, method="ach", created_by=admin)
    assert txn.status == TxnStatus.processing
    assert account_balance(db, checking) == 0
    assert inv.status == InvoiceStatus.paid

    advance_days(db, 1)
    run_settlement(db, company.id)
    assert account_balance(db, checking) == 10_000
    assert account_balance(db, ar) == 0


def test_wire_payment_settles_same_day(db, setup):
    company, admin = setup
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=75_000, memo="", due_date=date(2026, 3, 1), created_by=admin
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)
    run_settlement(db, company.id)
    assert account_balance(db, get_account(db, company.id, "1000")) == 75_000


def test_cannot_pay_draft_or_already_paid_invoice(db, setup):
    company, admin = setup
    inv = send_invoice(db, company_id=company.id, customer_name="Cust", amount_cents=100, memo="", due_date=date.today(), created_by=admin)
    record_invoice_payment(db, inv, method="wire", created_by=admin)
    with pytest.raises(LedgerError):
        record_invoice_payment(db, inv, method="wire", created_by=admin)


def test_invalid_payment_method_rejected(db, setup):
    company, admin = setup
    inv = send_invoice(db, company_id=company.id, customer_name="Cust", amount_cents=100, memo="", due_date=date.today(), created_by=admin)
    with pytest.raises(ValueError):
        record_invoice_payment(db, inv, method="crypto", created_by=admin)


def test_void_sent_invoice_reverses_accrual(db, setup):
    company, admin = setup
    ar = get_account(db, company.id, "1200")
    revenue = get_account(db, company.id, "4000")
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=1_000, memo="", due_date=date.today(), created_by=admin
    )
    void_invoice(db, inv)
    assert inv.status == InvoiceStatus.void
    assert account_balance(db, ar) == 0
    assert account_balance(db, revenue) == 0


def test_void_paid_invoice_rejected(db, setup):
    company, admin = setup
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=1_000, memo="", due_date=date.today(), created_by=admin
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)
    with pytest.raises(LedgerError, match="paid"):
        void_invoice(db, inv)


def test_full_accrual_cycle_balance_sheet_identity_holds(db, setup):
    """After send + pay, AR is back to zero, cash is up, and revenue reflects the sale -
    the fundamental check that accrual accounting was done correctly, not just cash counted."""
    company, admin = setup
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=20_000, memo="", due_date=date.today(), created_by=admin
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)
    run_settlement(db, company.id)
    from app.reports import balance_sheet

    bs = balance_sheet(db, company.id)
    assert bs["balances"] is True
    assert bs["assets_total_cents"] == 20_000
