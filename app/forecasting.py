"""Deterministic 3-statement forecasting: no LLM, no guessing. Every driver is either a historical
average computed from the company's own ledger, or a number the user explicitly set as an override.
A driver with no history and no override is left at 0 with a warning -- never silently invented.

The model: revenue grows at a monthly rate; each expense category is either a % of revenue or a
flat monthly amount (whichever the history supports); AR/AP are modeled on day counts (DSO/DPO) and
their period-over-period change drives operating cash flow; cash and equity roll forward from the
company's actual current balances. This makes the forecast balance (assets == liabilities + equity)
by construction every month -- see the algebraic note above `_forecast_month` -- which
`build_forecast` still re-checks at runtime as defense in depth.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import account_balance, get_account
from .models import AccountType, ForecastScenario, LedgerAccount, User
from .reports import add_months, cash_position, month_bounds, monthly_series, trailing_months
from .simclock import sim_today

GROWTH_CLIP = (-0.5, 1.0)  # sane bounds on monthly revenue growth: -50% to +100%
DEFAULT_DAYS = 30  # fallback DSO/DPO when there is no history to derive it from
DAYS_IN_MONTH_FALLBACK = 30


def _days_in(m: date) -> int:
    start, end = month_bounds(m)
    return (end - start).days + 1


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_default_assumptions(db: Session, company_id: int, as_of: date, lookback_months: int) -> tuple[dict, list[str]]:
    """Historical-average assumptions and any warnings about missing/thin data. Never guesses:
    a driver with no supporting history gets an explicit 0/default and a warning, not an estimate."""
    warnings: list[str] = []
    last_complete_month = add_months(date(as_of.year, as_of.month, 1), -1)
    history_months = trailing_months(last_complete_month, lookback_months)
    series = monthly_series(db, company_id, history_months)
    if not series or all(m["revenue_cents"] == 0 and m["expenses_cents"] == 0 for m in series):
        warnings.append(
            "No historical activity found. Forecast defaults to 0% growth and no recurring expenses"
            " until you add actuals or set assumptions manually."
        )

    revenue = [m["revenue_cents"] for m in series]
    growth_rates = [(revenue[i] - revenue[i - 1]) / revenue[i - 1] for i in range(1, len(revenue)) if revenue[i - 1] > 0]
    avg_growth = _mean(growth_rates)
    if avg_growth is None:
        warnings.append("Not enough revenue history to estimate growth; defaulted revenue_growth to 0%.")
        avg_growth = 0.0
    avg_growth = _clip(avg_growth, *GROWTH_CLIP)

    categories = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id,
            LedgerAccount.type.in_([AccountType.revenue, AccountType.expense]),
            LedgerAccount.is_active.is_(True),
        )
    ).all()
    expense_drivers = {}
    for cat in categories:
        if cat.type != AccountType.expense:
            continue
        by_month = [m["expenses_by_category"].get(cat.code, 0) for m in series]
        if not any(by_month):
            continue
        pct_samples = [by_month[i] / revenue[i] for i in range(len(series)) if revenue[i] > 0]
        if pct_samples:
            expense_drivers[cat.code] = {
                "mode": "pct_of_revenue",
                "value": _mean(pct_samples),
                "basis": f"average of {len(pct_samples)} month(s) as % of revenue",
            }
        else:
            flat = _mean(by_month) or 0.0
            expense_drivers[cat.code] = {
                "mode": "flat",
                "value": flat,
                "basis": f"average of {len(by_month)} month(s), flat $ (no revenue to ratio against)",
            }

    ar_account = get_account(db, company_id, "1200")
    ap_account = get_account(db, company_id, "2000")
    dso_samples, dpo_samples = [], []
    for i, m in enumerate(history_months):
        _, end = month_bounds(m)
        ar_bal = account_balance(db, ar_account, as_of=end)
        ap_bal = account_balance(db, ap_account, as_of=end)
        days = _days_in(m)
        if revenue[i] > 0:
            dso_samples.append(ar_bal / revenue[i] * days)
        expenses_i = series[i]["expenses_cents"]
        if expenses_i > 0:
            dpo_samples.append(ap_bal / expenses_i * days)
    dso = _mean(dso_samples)
    dpo = _mean(dpo_samples)
    if dso is None:
        warnings.append(f"Not enough history to estimate days sales outstanding; defaulted to {DEFAULT_DAYS} days.")
        dso = float(DEFAULT_DAYS)
    if dpo is None:
        warnings.append(f"Not enough history to estimate days payable outstanding; defaulted to {DEFAULT_DAYS} days.")
        dpo = float(DEFAULT_DAYS)

    return (
        {
            "revenue_growth": {
                "value": avg_growth,
                "basis": f"average of {len(growth_rates)} month(s) of history" if growth_rates else "no history",
            },
            "expenses": expense_drivers,
            "dso_days": {"value": dso},
            "dpo_days": {"value": dpo},
        },
        warnings,
    )


def _resolve_series(default_value: float, months: int, overrides: dict, key: str) -> list[float]:
    row = overrides.get(key)
    return [row[i] if row and i < len(row) and row[i] is not None else default_value for i in range(months)]


def build_forecast(db: Session, company_id: int, *, months: int = 12, lookback_months: int = 6, overrides: dict | None = None) -> dict:
    """Project `months` of income statement, balance sheet and cash flow forward from today.

    overrides: {driver_key: [per-month values]} where driver_key is "revenue_growth", "dso_days",
    "dpo_days", or "expense:<category code>". A month with no override uses the historical default.
    """
    overrides = overrides or {}
    as_of = sim_today(db)
    assumptions, warnings = compute_default_assumptions(db, company_id, as_of, lookback_months)
    last_complete_month = add_months(date(as_of.year, as_of.month, 1), -1)
    history_months = trailing_months(last_complete_month, lookback_months)
    history_series = monthly_series(db, company_id, history_months)
    from .reports import balance_sheet as _balance_sheet_report

    ar_account, ap_account = get_account(db, company_id, "1200"), get_account(db, company_id, "2000")
    for row in history_series:  # ending AR/AP/equity per month: display context, and the Excel export's roll-forward anchor
        _, month_end = month_bounds(row["month"])
        row["ar_cents"] = account_balance(db, ar_account, as_of=month_end)
        row["ap_cents"] = account_balance(db, ap_account, as_of=month_end)
        row["equity_cents"] = _balance_sheet_report(db, company_id, as_of=month_end)["equity_total_cents"]
    # Baseline is actuals through the end of last month (see last_month_end below), so the first
    # projected month is the current one, not next month -- otherwise a forecast made on the 1st of
    # the month would skip that whole month.
    forecast_months = [add_months(date(as_of.year, as_of.month, 1), k) for k in range(months)]

    growth = _resolve_series(assumptions["revenue_growth"]["value"], months, overrides, "revenue_growth")
    dso = _resolve_series(assumptions["dso_days"]["value"], months, overrides, "dso_days")
    dpo = _resolve_series(assumptions["dpo_days"]["value"], months, overrides, "dpo_days")
    expense_series: dict[str, list[float]] = {}
    for code, driver in assumptions["expenses"].items():
        expense_series[code] = _resolve_series(driver["value"], months, overrides, f"expense:{code}")
    for key in overrides:  # a manually-added category with no historical driver at all
        if key.startswith("expense:") and key[len("expense:") :] not in expense_series:
            code = key[len("expense:") :]
            expense_series[code] = _resolve_series(0.0, months, overrides, key)

    last_month_end = date(as_of.year, as_of.month, 1) - timedelta(days=1)
    revenue_prev = monthly_series(db, company_id, [add_months(date(as_of.year, as_of.month, 1), -1)])[0]["revenue_cents"]
    cash_prev = cash_position(db, company_id, as_of=last_month_end)["total_cents"]
    ar_prev = account_balance(db, get_account(db, company_id, "1200"), as_of=last_month_end)
    ap_prev = account_balance(db, get_account(db, company_id, "2000"), as_of=last_month_end)
    from .reports import balance_sheet as _balance_sheet

    equity_prev = _balance_sheet(db, company_id, as_of=last_month_end)["equity_total_cents"]

    income_statement, cash_flow, bs = [], [], []
    revenue_i = float(revenue_prev)
    for i, m in enumerate(forecast_months):
        revenue_i = revenue_i * (1 + growth[i])
        by_category = {
            code: (revenue_i * series[i] if assumptions["expenses"][code]["mode"] == "pct_of_revenue" else series[i])
            for code, series in expense_series.items()
            if code in assumptions["expenses"]
        }
        for code, series in expense_series.items():  # manually-added categories with no historical driver: treated as flat $ overrides
            if code not in assumptions["expenses"]:
                by_category[code] = series[i]
        expenses_total = sum(by_category.values())
        net_income = revenue_i - expenses_total
        income_statement.append(
            {
                "month": m,
                "revenue_cents": round(revenue_i),
                "expenses_by_category_cents": {c: round(v) for c, v in by_category.items()},
                "expenses_total_cents": round(expenses_total),
                "net_income_cents": round(net_income),
            }
        )

        days = _days_in(m)
        ar_i = revenue_i * dso[i] / days
        ap_i = expenses_total * dpo[i] / days
        delta_ar, delta_ap = ar_i - ar_prev, ap_i - ap_prev
        operating_cf = net_income - delta_ar + delta_ap
        cash_i = cash_prev + operating_cf
        equity_i = equity_prev + net_income
        cash_flow.append(
            {
                "month": m,
                "net_income_cents": round(net_income),
                "delta_ar_cents": round(delta_ar),
                "delta_ap_cents": round(delta_ap),
                "operating_cash_flow_cents": round(operating_cf),
                "ending_cash_cents": round(cash_i),
            }
        )
        assets = round(cash_i) + round(ar_i)
        liab_plus_equity = round(ap_i) + round(equity_i)
        bs.append(
            {
                "month": m,
                "cash_cents": round(cash_i),
                "ar_cents": round(ar_i),
                "ap_cents": round(ap_i),
                "equity_cents": round(equity_i),
                "assets_total_cents": assets,
                "liabilities_and_equity_total_cents": liab_plus_equity,
                "balances": abs(assets - liab_plus_equity) <= 1,
            }
        )
        cash_prev, ar_prev, ap_prev, equity_prev = cash_i, ar_i, ap_i, equity_i

    resolved_expenses = {
        code: {"mode": assumptions["expenses"][code]["mode"] if code in assumptions["expenses"] else "flat", "values": series}
        for code, series in expense_series.items()
    }
    return {
        "as_of": as_of,
        "months": forecast_months,
        "history_months": history_months,
        "history": history_series,
        "assumptions": assumptions,
        "resolved_assumptions": {"revenue_growth": growth, "dso_days": dso, "dpo_days": dpo, "expenses": resolved_expenses},
        "warnings": warnings,
        "income_statement": income_statement,
        "balance_sheet": bs,
        "cash_flow": cash_flow,
        "balances": all(row["balances"] for row in bs),
    }


# ---------------------------------------------------------------------------
# Scenarios: persisted assumption overrides, so tweaks survive a reload.
# ---------------------------------------------------------------------------


def create_scenario(db: Session, *, company_id: int, name: str, months: int, created_by: User) -> ForecastScenario:
    if not name.strip():
        raise ValueError("Scenario name is required.")
    if not 1 <= months <= 36:
        raise ValueError("months must be between 1 and 36.")
    scenario = ForecastScenario(company_id=company_id, name=name.strip(), months=months, overrides_json="{}", created_by_id=created_by.id)
    db.add(scenario)
    db.commit()
    return scenario


def get_overrides(scenario: ForecastScenario) -> dict:
    return json.loads(scenario.overrides_json or "{}")


def set_override(db: Session, scenario: ForecastScenario, driver_key: str, values: list[float | None]) -> ForecastScenario:
    if len(values) != scenario.months:
        raise ValueError(f"Expected {scenario.months} values, got {len(values)}.")
    overrides = get_overrides(scenario)
    overrides[driver_key] = values
    scenario.overrides_json = json.dumps(overrides)
    db.commit()
    return scenario


def clear_override(db: Session, scenario: ForecastScenario, driver_key: str) -> ForecastScenario:
    overrides = get_overrides(scenario)
    overrides.pop(driver_key, None)
    scenario.overrides_json = json.dumps(overrides)
    db.commit()
    return scenario


def build_forecast_for_scenario(db: Session, scenario: ForecastScenario, lookback_months: int = 6) -> dict:
    return build_forecast(
        db, scenario.company_id, months=scenario.months, lookback_months=lookback_months, overrides=get_overrides(scenario)
    )
