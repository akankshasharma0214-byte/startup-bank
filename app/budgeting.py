"""Budgeting and variance analysis. A Budget never touches the ledger -- it is a plan to compare
actuals against, nothing more. Variance follows the standard FP&A convention: positive is always
favorable (revenue ahead of plan, or spending under plan), regardless of account type."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ledger import LedgerError
from .models import AccountType, Budget, BudgetLine, LedgerAccount, User
from .reports import _period_balance, month_bounds, trailing_months
from .simclock import sim_today


def create_budget(db: Session, *, company_id: int, name: str, created_by: User) -> Budget:
    if not name.strip():
        raise ValueError("Budget name is required.")
    budget = Budget(company_id=company_id, name=name.strip(), created_by_id=created_by.id)
    db.add(budget)
    db.commit()
    return budget


def set_budget_line(db: Session, *, budget: Budget, category: LedgerAccount, period: date, amount_cents: int) -> BudgetLine:
    if amount_cents < 0:
        raise ValueError("Budget amount cannot be negative.")
    if category.company_id != budget.company_id:
        raise LedgerError("Category does not belong to this company.")
    if category.type not in (AccountType.revenue, AccountType.expense):
        raise LedgerError("Budgets only apply to revenue or expense categories.")
    period = period.replace(day=1)
    line = db.scalar(
        select(BudgetLine).where(
            BudgetLine.budget_id == budget.id, BudgetLine.ledger_account_id == category.id, BudgetLine.period == period
        )
    )
    if line is None:
        line = BudgetLine(budget_id=budget.id, ledger_account_id=category.id, period=period, amount_cents=amount_cents)
        db.add(line)
    else:
        line.amount_cents = amount_cents
    db.commit()
    return line


def populate_from_history(db: Session, *, budget: Budget, months: int, lookback_months: int = 3) -> list[BudgetLine]:
    """Fill in `months` of forward budget lines per active revenue/expense category, using the
    average of the trailing `lookback_months` of actuals as a starting point (still fully editable
    afterwards -- this is a starting point, not a forecast)."""
    company_id = budget.company_id
    today = sim_today(db)
    history = trailing_months(today, lookback_months)
    categories = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id,
            LedgerAccount.type.in_([AccountType.revenue, AccountType.expense]),
            LedgerAccount.is_active.is_(True),
        )
    ).all()
    forward = [_forward_month(today, k) for k in range(1, months + 1)]
    created = []
    for cat in categories:
        totals = [max(0, _period_balance(db, cat, *month_bounds(m))) for m in history]
        avg = sum(totals) // len(totals) if totals else 0
        if avg == 0:
            continue
        for m in forward:
            created.append(set_budget_line(db, budget=budget, category=cat, period=m, amount_cents=avg))
    return created


def _forward_month(d: date, k: int) -> date:
    from .reports import add_months

    return add_months(date(d.year, d.month, 1), k)


def budget_vs_actual(db: Session, company_id: int, budget_id: int, start: date, end: date) -> dict:
    budget = db.get(Budget, budget_id)
    if budget is None or budget.company_id != company_id:
        raise LedgerError("Budget not found.")
    categories = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id, LedgerAccount.type.in_([AccountType.revenue, AccountType.expense])
        )
    ).all()
    rows = []
    for cat in categories:
        budget_total = db.scalar(
            select(func.coalesce(func.sum(BudgetLine.amount_cents), 0)).where(
                BudgetLine.budget_id == budget_id,
                BudgetLine.ledger_account_id == cat.id,
                BudgetLine.period >= start,
                BudgetLine.period <= end,
            )
        )
        actual = _period_balance(db, cat, start, end)
        if budget_total == 0 and actual == 0:
            continue
        favorable_sign = 1 if cat.type == AccountType.revenue else -1
        variance = favorable_sign * (actual - budget_total)
        variance_pct = (variance / abs(budget_total)) if budget_total else None
        rows.append(
            {
                "code": cat.code,
                "name": cat.name,
                "type": cat.type.value,
                "budget_cents": budget_total,
                "actual_cents": actual,
                "variance_cents": variance,
                "variance_pct": variance_pct,
                "favorable": variance >= 0,
            }
        )
    rows.sort(key=lambda r: (r["type"] != "revenue", r["code"]))
    return {
        "budget_id": budget_id,
        "budget_name": budget.name,
        "start": start,
        "end": end,
        "rows": rows,
        "budget_total_cents": sum(r["budget_cents"] for r in rows),
        "actual_total_cents": sum(r["actual_cents"] for r in rows),
    }
