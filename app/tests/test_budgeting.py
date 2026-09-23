from datetime import date

import pytest

from app.billpay import add_vendor, create_bill, schedule_bill_payment
from app.budgeting import budget_vs_actual, create_budget, populate_from_history, set_budget_line
from app.ledger import LedgerError, get_account
from app.onboarding import fund_account, onboard_company
from app.reports import add_months
from app.settlement import run_settlement
from app.simclock import set_sim_date


@pytest.fixture()
def setup(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="seed")
    return company, admin


def test_create_budget_requires_name(db, setup):
    company, admin = setup
    with pytest.raises(ValueError):
        create_budget(db, company_id=company.id, name="  ", created_by=admin)


def test_set_budget_line_only_revenue_or_expense(db, setup):
    company, admin = setup
    budget = create_budget(db, company_id=company.id, name="FY26", created_by=admin)
    checking = get_account(db, company.id, "1000")
    with pytest.raises(LedgerError, match="revenue or expense"):
        set_budget_line(db, budget=budget, category=checking, period=date(2026, 1, 1), amount_cents=100)


def test_set_budget_line_upserts_by_month(db, setup):
    company, admin = setup
    budget = create_budget(db, company_id=company.id, name="FY26", created_by=admin)
    software = get_account(db, company.id, "5010")
    set_budget_line(db, budget=budget, category=software, period=date(2026, 3, 15), amount_cents=10_000)
    line2 = set_budget_line(db, budget=budget, category=software, period=date(2026, 3, 1), amount_cents=12_000)  # same month
    assert len(budget.lines) == 1
    assert line2.amount_cents == 12_000
    assert line2.period == date(2026, 3, 1)  # normalized to the 1st


def test_negative_budget_amount_rejected(db, setup):
    company, admin = setup
    budget = create_budget(db, company_id=company.id, name="FY26", created_by=admin)
    software = get_account(db, company.id, "5010")
    with pytest.raises(ValueError):
        set_budget_line(db, budget=budget, category=software, period=date(2026, 1, 1), amount_cents=-1)


def test_budget_vs_actual_favorable_sign_differs_by_type(db, setup):
    company, admin = setup
    budget = create_budget(db, company_id=company.id, name="FY26", created_by=admin)
    revenue = get_account(db, company.id, "4000")
    software = get_account(db, company.id, "5010")
    set_budget_line(db, budget=budget, category=revenue, period=date(2026, 1, 1), amount_cents=100_000)
    set_budget_line(db, budget=budget, category=software, period=date(2026, 1, 1), amount_cents=5_000)

    from app.invoicing import record_invoice_payment, send_invoice

    set_sim_date(db, date(2026, 1, 10))
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=120_000, memo="", due_date=date(2026, 2, 1), created_by=admin
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)

    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="wire")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=software,
        amount_cents=8_000,
        memo="",
        due_date=date(2026, 1, 15),
        created_by=admin,
    )
    schedule_bill_payment(db, bill, admin)
    run_settlement(db, company.id)

    report = budget_vs_actual(db, company.id, budget.id, date(2026, 1, 1), date(2026, 1, 31))
    rev_row = next(r for r in report["rows"] if r["code"] == "4000")
    sw_row = next(r for r in report["rows"] if r["code"] == "5010")
    assert rev_row["variance_cents"] == 20_000 and rev_row["favorable"] is True  # beat revenue target
    assert sw_row["variance_cents"] == -3_000 and sw_row["favorable"] is False  # overspent budget
    assert sw_row["variance_pct"] == pytest.approx(-0.6)


def test_budget_vs_actual_unknown_budget_rejected(db, setup):
    company, admin = setup
    with pytest.raises(LedgerError):
        budget_vs_actual(db, company.id, 99999, date(2026, 1, 1), date(2026, 1, 31))


def test_populate_from_history_uses_trailing_average(db, setup):
    company, admin = setup
    software = get_account(db, company.id, "5010")
    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="wire")
    set_sim_date(db, date(2026, 1, 10))
    for month, amt in ((date(2026, 1, 10), 10_000), (date(2026, 2, 10), 20_000), (date(2026, 3, 10), 30_000)):
        set_sim_date(db, month)
        bill = create_bill(
            db, company_id=company.id, vendor=vendor, category=software, amount_cents=amt, memo="", due_date=month, created_by=admin
        )
        schedule_bill_payment(db, bill, admin)
    run_settlement(db, company.id)

    budget = create_budget(db, company_id=company.id, name="Auto", created_by=admin)
    created = populate_from_history(db, budget=budget, months=2, lookback_months=3)
    assert len(created) == 2  # only the one active category with nonzero history
    avg = (10_000 + 20_000 + 30_000) // 3
    for line in created:
        assert line.amount_cents == avg
    forward_months = {line.period for line in created}
    assert forward_months == {add_months(date(2026, 3, 1), 1), add_months(date(2026, 3, 1), 2)}


def test_populate_from_history_skips_categories_with_no_activity(db, setup):
    company, admin = setup
    budget = create_budget(db, company_id=company.id, name="Auto", created_by=admin)
    created = populate_from_history(db, budget=budget, months=3)
    assert created == []
