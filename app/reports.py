"""Financial reports computed live from the ledger. Nothing here is stored; every figure is a
sum over journal lines, so a report can never drift from the books. The balance sheet identity
(assets == liabilities + equity) holds by construction, since every journal entry is balanced
and net income is folded into equity as unclosed retained earnings on every call."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import account_balance
from .models import AccountType, JournalEntry, JournalLine, LedgerAccount


def month_bounds(d: date) -> tuple[date, date]:
    """First and last calendar day of the month containing d."""
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last_day)


def add_months(d: date, n: int) -> date:
    """d, moved forward n whole months, clamped to the 1st (only ever called with day=1 dates here)."""
    month0 = (d.year * 12 + (d.month - 1)) + n
    return date(month0 // 12, month0 % 12 + 1, 1)


def trailing_months(as_of: date, n: int) -> list[date]:
    """The n month-start dates ending at as_of's month (inclusive), oldest first."""
    start_month = date(as_of.year, as_of.month, 1)
    return [add_months(start_month, -k) for k in range(n - 1, -1, -1)]


def _period_balance(db: Session, account: LedgerAccount, start: date | None, end: date | None) -> int:
    end_balance = account_balance(db, account, as_of=end)
    if start is None:
        return end_balance
    before_start = account_balance(db, account, as_of=start - timedelta(days=1))
    return end_balance - before_start


def income_statement(db: Session, company_id: int, start: date | None = None, end: date | None = None) -> dict:
    accounts = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id, LedgerAccount.type.in_([AccountType.revenue, AccountType.expense])
        )
    ).all()
    revenue, expenses = [], []
    for a in accounts:
        amt = _period_balance(db, a, start, end)
        if amt == 0:
            continue
        row = {"code": a.code, "name": a.name, "amount_cents": amt}
        (revenue if a.type == AccountType.revenue else expenses).append(row)
    revenue.sort(key=lambda r: -r["amount_cents"])
    expenses.sort(key=lambda r: -r["amount_cents"])
    revenue_total = sum(r["amount_cents"] for r in revenue)
    expense_total = sum(r["amount_cents"] for r in expenses)
    return {
        "start": start,
        "end": end,
        "revenue": revenue,
        "revenue_total_cents": revenue_total,
        "expenses": expenses,
        "expenses_total_cents": expense_total,
        "net_income_cents": revenue_total - expense_total,
    }


def balance_sheet(db: Session, company_id: int, as_of: date | None = None) -> dict:
    accounts = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id, LedgerAccount.type.in_([AccountType.asset, AccountType.liability, AccountType.equity])
        )
    ).all()
    assets, liabilities, equity = [], [], []
    for a in accounts:
        bal = account_balance(db, a, as_of)
        if bal == 0 and not a.is_system:
            continue
        row = {"code": a.code, "name": a.name, "balance_cents": bal, "is_bank_account": a.is_bank_account}
        {AccountType.asset: assets, AccountType.liability: liabilities, AccountType.equity: equity}[a.type].append(row)
    assets.sort(key=lambda r: r["code"])
    liabilities.sort(key=lambda r: r["code"])
    equity.sort(key=lambda r: r["code"])

    net_income_to_date = income_statement(db, company_id, start=None, end=as_of)["net_income_cents"]
    equity.append(
        {
            "code": "3900",
            "name": "Retained Earnings (current period, unclosed)",
            "balance_cents": net_income_to_date,
            "is_bank_account": False,
        }
    )

    assets_total = sum(r["balance_cents"] for r in assets)
    liabilities_total = sum(r["balance_cents"] for r in liabilities)
    equity_total = sum(r["balance_cents"] for r in equity)
    return {
        "as_of": as_of,
        "assets": assets,
        "assets_total_cents": assets_total,
        "liabilities": liabilities,
        "liabilities_total_cents": liabilities_total,
        "equity": equity,
        "equity_total_cents": equity_total,
        "balances": assets_total == liabilities_total + equity_total,
    }


def account_statement(db: Session, ledger_account: LedgerAccount, start: date | None = None, end: date | None = None) -> list[dict]:
    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.ledger_account_id == ledger_account.id)
        .order_by(JournalEntry.entry_date, JournalEntry.id)
    )
    if start is not None:
        stmt = stmt.where(JournalEntry.entry_date >= start)
    if end is not None:
        stmt = stmt.where(JournalEntry.entry_date <= end)
    running = account_balance(db, ledger_account, as_of=start - timedelta(days=1)) if start is not None else 0
    out = []
    for line, entry in db.execute(stmt).all():
        signed = line.amount_cents if line.direction == ledger_account.normal_balance else -line.amount_cents
        running += signed
        out.append(
            {
                "date": entry.entry_date,
                "memo": entry.memo,
                "source_type": entry.source_type,
                "amount_cents": signed,
                "balance_after_cents": running,
            }
        )
    return out


def monthly_series(db: Session, company_id: int, months: list[date]) -> list[dict]:
    """Actual revenue/expenses (by category) and ending cash for each given month-start date.
    The base data FP&A (budgeting, forecasting) is built on -- always derived live from the ledger."""
    out = []
    for m in months:
        start, end = month_bounds(m)
        inc = income_statement(db, company_id, start, end)
        cash = cash_position(db, company_id, as_of=end)["total_cents"]
        out.append(
            {
                "month": start,
                "revenue_cents": inc["revenue_total_cents"],
                "expenses_cents": inc["expenses_total_cents"],
                "net_income_cents": inc["net_income_cents"],
                "revenue_by_category": {r["code"]: r["amount_cents"] for r in inc["revenue"]},
                "expenses_by_category": {r["code"]: r["amount_cents"] for r in inc["expenses"]},
                "ending_cash_cents": cash,
            }
        )
    return out


def cash_flow_statement(db: Session, company_id: int, start: date, end: date) -> dict:
    """Indirect-method cash flow statement: start from net income, adjust for changes in
    non-cash working-capital accounts. Classification is by account code, generalized so it stays
    correct if new account types are added later: Accounts Receivable (1200) and Accounts Payable
    (2000) are operating; any OTHER non-bank asset is investing; any OTHER liability is financing;
    equity accounts (capital raised, owner draws) are financing.

    `ties_to_cash` should always be True: operating + investing + financing must equal the actual
    change in bank balances over the period, since every journal entry is balanced by construction.
    """
    accounts = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == company_id,
            LedgerAccount.type.in_([AccountType.asset, AccountType.liability, AccountType.equity]),
            LedgerAccount.is_bank_account.is_(False),
        )
    ).all()

    net_income = income_statement(db, company_id, start, end)["net_income_cents"]
    operating, investing, financing = [{"label": "Net income", "amount_cents": net_income}], [], []
    for a in accounts:
        delta = _period_balance(db, a, start, end)  # change in this account's own-normal-balance-signed value
        if delta == 0:
            continue
        if a.type == AccountType.asset:
            # An increase in a non-cash asset (e.g. AR growing) is a USE of cash.
            row = {"label": f"Change in {a.name}", "amount_cents": -delta}
            (operating if a.code == "1200" else investing).append(row)
        else:
            # An increase in a liability or equity account is a SOURCE of cash.
            row = {"label": f"Change in {a.name}", "amount_cents": delta}
            (operating if a.code == "2000" else financing).append(row)

    operating_total = sum(r["amount_cents"] for r in operating)
    investing_total = sum(r["amount_cents"] for r in investing)
    financing_total = sum(r["amount_cents"] for r in financing)
    net_change = operating_total + investing_total + financing_total

    cash_start = cash_position(db, company_id, as_of=start - timedelta(days=1))["total_cents"]
    cash_end = cash_position(db, company_id, as_of=end)["total_cents"]
    return {
        "start": start,
        "end": end,
        "operating": operating,
        "operating_total_cents": operating_total,
        "investing": investing,
        "investing_total_cents": investing_total,
        "financing": financing,
        "financing_total_cents": financing_total,
        "net_change_cents": net_change,
        "cash_start_cents": cash_start,
        "cash_end_cents": cash_end,
        "ties_to_cash": net_change == (cash_end - cash_start),
    }


def burn_and_runway(db: Session, company_id: int, as_of: date | None = None, trailing: int = 3) -> dict:
    """Monthly cash burn (average operating cash outflow over the trailing N complete months) and
    runway (months of cash left at that burn rate). Only whole, already-elapsed months are used, so
    a partial current month never distorts the average."""
    as_of = as_of or date.today()
    last_complete_month = add_months(date(as_of.year, as_of.month, 1), -1)
    months = trailing_months(last_complete_month, trailing)
    monthly_operating = []
    for m in months:
        start, end = month_bounds(m)
        cf = cash_flow_statement(db, company_id, start, end)
        monthly_operating.append(cf["operating_total_cents"])
    avg_operating = sum(monthly_operating) // len(monthly_operating) if monthly_operating else 0
    cash_on_hand = cash_position(db, company_id, as_of=as_of)["total_cents"]
    burn_cents = max(0, -avg_operating)  # 0 when cash-flow positive or breakeven: no burn, not negative runway
    runway_months = (cash_on_hand / burn_cents) if burn_cents > 0 else None
    return {
        "as_of": as_of,
        "months_used": [m.isoformat() for m in months],
        "monthly_operating_cash_flow_cents": monthly_operating,
        "avg_monthly_operating_cash_flow_cents": avg_operating,
        "burn_cents": burn_cents,
        "cash_on_hand_cents": cash_on_hand,
        "runway_months": runway_months,
    }


def cash_position(db: Session, company_id: int, as_of: date | None = None) -> dict:
    """Sum of every bank-account (checking + savings/treasury) balance for the company."""
    accounts = db.scalars(
        select(LedgerAccount).where(LedgerAccount.company_id == company_id, LedgerAccount.is_bank_account.is_(True))
    ).all()
    by_account = [
        {"code": a.code, "name": a.name, "subtype": a.bank_subtype, "balance_cents": account_balance(db, a, as_of)} for a in accounts
    ]
    return {"accounts": by_account, "total_cents": sum(a["balance_cents"] for a in by_account)}
