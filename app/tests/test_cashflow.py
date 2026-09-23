from datetime import date

import pytest

from app.billpay import add_vendor, create_bill, schedule_bill_payment
from app.invoicing import record_invoice_payment, send_invoice
from app.ledger import get_account
from app.onboarding import fund_account, onboard_company
from app.reports import add_months, burn_and_runway, cash_flow_statement, month_bounds, monthly_series, trailing_months
from app.settlement import run_settlement
from app.simclock import advance_days, set_sim_date


def test_month_bounds():
    assert month_bounds(date(2026, 2, 15)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds(date(2024, 2, 10)) == (date(2024, 2, 1), date(2024, 2, 29))  # leap year


def test_add_months_wraps_year():
    assert add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)


def test_trailing_months_oldest_first():
    months = trailing_months(date(2026, 3, 15), 3)
    assert months == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]


@pytest.fixture()
def scenario(db):
    """Month 1: fund + pay a bill. Month 2: send + collect an invoice. Deterministic sim clock."""
    set_sim_date(db, date(2026, 1, 5))
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="seed")

    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="wire")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=50_000,
        memo="",
        due_date=date(2026, 1, 20),
        created_by=admin,
    )
    schedule_bill_payment(db, bill, admin)
    run_settlement(db, company.id)

    advance_days(db, 30)  # into February
    inv = send_invoice(
        db, company_id=company.id, customer_name="Cust", amount_cents=200_000, memo="", due_date=date(2026, 3, 1), created_by=admin
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)
    run_settlement(db, company.id)
    return company, admin


def test_cash_flow_statement_ties_to_cash(db, scenario):
    company, admin = scenario
    cf = cash_flow_statement(db, company.id, date(2026, 1, 1), date(2026, 2, 28))
    assert cf["ties_to_cash"] is True
    assert cf["cash_end_cents"] - cf["cash_start_cents"] == cf["net_change_cents"]


def test_cash_flow_statement_classifies_ap_and_ar_as_operating(db, scenario):
    company, admin = scenario
    cf_jan = cash_flow_statement(db, company.id, *month_bounds(date(2026, 1, 15)))
    # Bill accrued (Debit expense/Credit AP) then paid same period: AP nets to 0 change, expense hits net income.
    assert cf_jan["operating_total_cents"] == -50_000
    assert cf_jan["investing"] == []
    assert cf_jan["financing_total_cents"] == 1_000_000  # the Jan-5 funding also lands in this month

    cf_feb = cash_flow_statement(db, company.id, *month_bounds(date(2026, 2, 15)))
    # Invoice sent AND collected same month: net income +200,000, AR nets to 0 change.
    assert cf_feb["operating_total_cents"] == 200_000


def test_cash_flow_statement_funding_is_financing(db, scenario):
    company, admin = scenario
    cf = cash_flow_statement(db, company.id, date(2026, 1, 1), date(2026, 1, 4))  # before the funding txn settles same-day on Jan 5
    assert cf["financing_total_cents"] == 0
    cf2 = cash_flow_statement(db, company.id, date(2026, 1, 1), date(2026, 1, 6))
    assert cf2["financing_total_cents"] == 1_000_000  # Paid-in Capital


def test_monthly_series_matches_income_statement(db, scenario):
    company, admin = scenario
    series = monthly_series(db, company.id, trailing_months(date(2026, 2, 15), 2))
    assert series[0]["month"] == date(2026, 1, 1) and series[0]["expenses_cents"] == 50_000
    assert series[1]["month"] == date(2026, 2, 1) and series[1]["revenue_cents"] == 200_000
    assert series[1]["ending_cash_cents"] > series[0]["ending_cash_cents"]


def test_burn_and_runway_positive_cash_flow_month_has_zero_burn(db, scenario):
    company, admin = scenario
    r = burn_and_runway(db, company.id, as_of=date(2026, 3, 1), trailing=1)
    assert r["months_used"] == [date(2026, 2, 1).isoformat()]
    assert r["avg_monthly_operating_cash_flow_cents"] == 200_000
    assert r["burn_cents"] == 0
    assert r["runway_months"] is None


def test_burn_and_runway_negative_cash_flow_month_computes_runway(db, scenario):
    company, admin = scenario
    r = burn_and_runway(db, company.id, as_of=date(2026, 2, 1), trailing=1)  # only January is "complete"
    assert r["months_used"] == [date(2026, 1, 1).isoformat()]
    assert r["avg_monthly_operating_cash_flow_cents"] == -50_000
    assert r["burn_cents"] == 50_000
    assert r["runway_months"] == pytest.approx(r["cash_on_hand_cents"] / 50_000)


def test_burn_and_runway_uses_only_complete_months(db, scenario):
    company, admin = scenario
    # as_of is mid-February: the "trailing 1" complete month must be January, not February (incomplete).
    r = burn_and_runway(db, company.id, as_of=date(2026, 2, 15), trailing=1)
    assert r["months_used"] == [date(2026, 1, 1).isoformat()]
