from datetime import date

import pytest

from app.billpay import add_vendor, create_bill, schedule_bill_payment
from app.forecasting import (
    build_forecast,
    build_forecast_for_scenario,
    clear_override,
    compute_default_assumptions,
    create_scenario,
    get_overrides,
    set_override,
)
from app.invoicing import record_invoice_payment, send_invoice
from app.ledger import get_account
from app.onboarding import fund_account, onboard_company
from app.settlement import run_settlement
from app.simclock import set_sim_date


@pytest.fixture()
def fresh_company(db):
    company, admin = onboard_company(
        db, name="Fresh Co", legal_name="Fresh Co", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    return company, admin


@pytest.fixture()
def seasoned_company(db):
    """Three months of revenue growing 10,000 -> 11,000 -> 12,100 (10% MoM) and a recurring
    software bill that's a stable 20% of revenue each month."""
    set_sim_date(db, date(2026, 1, 5))
    company, admin = onboard_company(
        db, name="Seasoned Co", legal_name="Seasoned Co", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=5_000_000, memo="seed")
    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="wire")
    software = get_account(db, company.id, "5010")
    revenue = 10_000_00
    for k in range(3):
        m = date(2026, k + 1, 10)
        set_sim_date(db, m)
        inv = send_invoice(db, company_id=company.id, customer_name="Cust", amount_cents=revenue, memo="", due_date=m, created_by=admin)
        record_invoice_payment(db, inv, method="wire", created_by=admin)
        bill = create_bill(
            db,
            company_id=company.id,
            vendor=vendor,
            category=software,
            amount_cents=round(revenue * 0.20),
            memo="",
            due_date=m,
            created_by=admin,
        )
        schedule_bill_payment(db, bill, admin)
        revenue = round(revenue * 1.10)
    set_sim_date(db, date(2026, 4, 1))
    run_settlement(db, company.id)
    return company, admin


# ---------- default assumptions ----------
def test_no_history_defaults_to_zero_with_warnings(db, fresh_company):
    company, admin = fresh_company
    assumptions, warnings = compute_default_assumptions(db, company.id, date(2026, 6, 1), lookback_months=6)
    assert assumptions["revenue_growth"]["value"] == 0.0
    assert assumptions["expenses"] == {}
    assert any("No historical activity" in w for w in warnings)


def test_growth_and_expense_ratio_derived_from_history(db, seasoned_company):
    company, admin = seasoned_company
    assumptions, warnings = compute_default_assumptions(db, company.id, date(2026, 4, 1), lookback_months=6)
    assert assumptions["revenue_growth"]["value"] == pytest.approx(0.10, abs=0.001)
    sw = assumptions["expenses"]["5010"]
    assert sw["mode"] == "pct_of_revenue"
    assert sw["value"] == pytest.approx(0.20, abs=0.001)
    assert warnings == []  # 3 months of real history: no defaults needed for growth/expense... DSO/DPO still may warn
    _ = warnings  # DSO/DPO not asserted here; see dedicated test


def test_growth_clipped_to_sane_bounds(db, fresh_company):
    company, admin = fresh_company
    set_sim_date(db, date(2026, 1, 5))
    from app.invoicing import record_invoice_payment, send_invoice

    for k, amt in enumerate((100_00, 100_000_00)):  # 1000x month-over-month growth
        m = date(2026, k + 1, 10)
        set_sim_date(db, m)
        inv = send_invoice(db, company_id=company.id, customer_name="Cust", amount_cents=amt, memo="", due_date=m, created_by=admin)
        record_invoice_payment(db, inv, method="wire", created_by=admin)
    set_sim_date(db, date(2026, 3, 1))
    run_settlement(db, company.id)
    assumptions, _ = compute_default_assumptions(db, company.id, date(2026, 3, 1), lookback_months=6)
    assert assumptions["revenue_growth"]["value"] == pytest.approx(1.0)  # clipped to +100%/month


# ---------- build_forecast ----------
def test_forecast_balances_every_month(db, seasoned_company):
    company, admin = seasoned_company
    fc = build_forecast(db, company.id, months=12)
    assert fc["balances"] is True
    assert all(row["balances"] for row in fc["balance_sheet"])
    assert len(fc["months"]) == 12 == len(fc["income_statement"]) == len(fc["balance_sheet"]) == len(fc["cash_flow"])


def test_forecast_revenue_compounds_at_growth_rate(db, seasoned_company):
    company, admin = seasoned_company
    fc = build_forecast(db, company.id, months=3)
    from itertools import pairwise

    revs = [row["revenue_cents"] for row in fc["income_statement"]]
    for a, b in pairwise(revs):
        assert b / a == pytest.approx(1.10, abs=0.01)


def test_forecast_expense_scales_with_revenue_pct_mode(db, seasoned_company):
    company, admin = seasoned_company
    fc = build_forecast(db, company.id, months=3)
    for row in fc["income_statement"]:
        sw = row["expenses_by_category_cents"]["5010"]
        assert sw == pytest.approx(row["revenue_cents"] * 0.20, abs=2)


def test_forecast_on_fresh_company_runs_and_balances_trivially(db, fresh_company):
    company, admin = fresh_company
    fc = build_forecast(db, company.id, months=6)
    assert fc["balances"] is True
    assert all(row["revenue_cents"] == 0 for row in fc["income_statement"])
    assert any("No historical activity" in w for w in fc["warnings"])


def test_forecast_includes_history_with_ar_ap_context(db, seasoned_company):
    company, admin = seasoned_company
    fc = build_forecast(db, company.id, months=3, lookback_months=3)
    assert len(fc["history_months"]) == 3 == len(fc["history"])
    assert fc["history"][-1]["month"] == date(2026, 3, 1)
    assert all("ar_cents" in row and "ap_cents" in row for row in fc["history"])
    # revenue was actually paid via invoice+payment same month, so AR nets back to 0
    assert fc["history"][-1]["ar_cents"] == 0


def test_forecast_months_start_at_the_current_sim_month(db, seasoned_company):
    company, admin = seasoned_company  # sim clock is at 2026-04-01 (last actuals: March)
    fc = build_forecast(db, company.id, months=3)
    assert fc["months"] == [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)]


def test_overrides_change_only_targeted_month(db, seasoned_company):
    company, admin = seasoned_company
    base = build_forecast(db, company.id, months=3)
    bumped = build_forecast(db, company.id, months=3, overrides={"revenue_growth": [0.50, None, None]})
    assert bumped["income_statement"][0]["revenue_cents"] > base["income_statement"][0]["revenue_cents"]
    # month 2 growth off month-1's now-higher revenue, but the growth RATE for months 2-3 is unchanged
    r0b, r1b, r2b = (row["revenue_cents"] for row in bumped["income_statement"])
    assert r1b / r0b == pytest.approx(1.10, abs=0.01)
    assert r2b / r1b == pytest.approx(1.10, abs=0.01)
    assert bumped["balances"] is True


def test_manual_expense_override_with_no_history_is_honored(db, seasoned_company):
    company, admin = seasoned_company
    fc = build_forecast(db, company.id, months=2, overrides={"expense:5050": [5_000_00, 5_000_00]})  # Rent: no history at all
    assert all(row["expenses_by_category_cents"]["5050"] == 5_000_00 for row in fc["income_statement"])
    assert fc["balances"] is True


def test_dso_dpo_override_affects_ar_ap(db, seasoned_company):
    company, admin = seasoned_company
    base = build_forecast(db, company.id, months=1)
    longer_dso = build_forecast(db, company.id, months=1, overrides={"dso_days": [60]})
    assert longer_dso["balance_sheet"][0]["ar_cents"] > base["balance_sheet"][0]["ar_cents"]
    assert longer_dso["balances"] is True


# ---------- scenarios ----------
def test_scenario_create_validates(db, seasoned_company):
    company, admin = seasoned_company
    with pytest.raises(ValueError):
        create_scenario(db, company_id=company.id, name="", months=12, created_by=admin)
    with pytest.raises(ValueError):
        create_scenario(db, company_id=company.id, name="x", months=0, created_by=admin)


def test_scenario_set_and_clear_override_roundtrip(db, seasoned_company):
    company, admin = seasoned_company
    scenario = create_scenario(db, company_id=company.id, name="Base case", months=6, created_by=admin)
    set_override(db, scenario, "revenue_growth", [0.5, None, None, None, None, None])
    assert get_overrides(scenario)["revenue_growth"][0] == 0.5
    fc = build_forecast_for_scenario(db, scenario)
    assert len(fc["months"]) == 6
    clear_override(db, scenario, "revenue_growth")
    assert "revenue_growth" not in get_overrides(scenario)


def test_scenario_override_wrong_length_rejected(db, seasoned_company):
    company, admin = seasoned_company
    scenario = create_scenario(db, company_id=company.id, name="Base case", months=6, created_by=admin)
    with pytest.raises(ValueError):
        set_override(db, scenario, "revenue_growth", [0.1, 0.1])
