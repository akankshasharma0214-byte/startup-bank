"""The double-entry accounting core. Every dollar that moves in this app moves through
post_journal_entry(), which refuses to post anything that does not balance. Balances are
always derived from journal lines, never stored/cached, so they cannot drift from the truth."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountType, Direction, JournalEntry, JournalLine, LedgerAccount

# code -> (name, type, normal_balance, is_bank_account, bank_subtype, is_system)
CHART_OF_ACCOUNTS = [
    ("1000", "Checking Account", AccountType.asset, Direction.debit, True, "checking", True),
    ("1010", "Savings / Treasury", AccountType.asset, Direction.debit, True, "savings", True),
    ("1200", "Accounts Receivable", AccountType.asset, Direction.debit, False, None, True),
    ("2000", "Accounts Payable", AccountType.liability, Direction.credit, False, None, True),
    ("3000", "Paid-in Capital", AccountType.equity, Direction.credit, False, None, True),
    # Contra-equity, but deliberately kept credit-normal (matching its parent type=equity, not its own
    # natural debit tendency): a debit here must show as a NEGATIVE balance so it nets correctly when
    # simply summed with other equity accounts in balance_sheet(). Giving it debit-normal instead would
    # make draws ADD to equity totals instead of reducing them.
    ("3100", "Owner Draws / Distributions", AccountType.equity, Direction.credit, False, None, True),
    ("4000", "Sales Revenue", AccountType.revenue, Direction.credit, False, None, True),
    ("4900", "Other Income", AccountType.revenue, Direction.credit, False, None, False),
    ("5000", "Payroll", AccountType.expense, Direction.debit, False, None, False),
    ("5010", "Software & Subscriptions", AccountType.expense, Direction.debit, False, None, False),
    ("5020", "Travel", AccountType.expense, Direction.debit, False, None, False),
    ("5030", "Meals & Entertainment", AccountType.expense, Direction.debit, False, None, False),
    ("5040", "Office Supplies & Equipment", AccountType.expense, Direction.debit, False, None, False),
    ("5050", "Rent", AccountType.expense, Direction.debit, False, None, False),
    ("5060", "Marketing & Advertising", AccountType.expense, Direction.debit, False, None, False),
    ("5070", "Professional Services", AccountType.expense, Direction.debit, False, None, False),
    ("5080", "Contractors", AccountType.expense, Direction.debit, False, None, False),
    ("5090", "Bank Fees", AccountType.expense, Direction.debit, False, None, False),
    ("5900", "Uncategorized Expense", AccountType.expense, Direction.debit, False, None, True),
]


class LedgerError(ValueError):
    pass


def seed_chart_of_accounts(db: Session, company_id: int) -> dict[str, LedgerAccount]:
    """Create the standard chart of accounts for a newly onboarded company. Returns {code: account}."""
    out: dict[str, LedgerAccount] = {}
    for code, name, type_, normal, is_bank, subtype, is_system in CHART_OF_ACCOUNTS:
        acct = LedgerAccount(
            company_id=company_id,
            code=code,
            name=name,
            type=type_,
            normal_balance=normal,
            is_bank_account=is_bank,
            bank_subtype=subtype,
            is_system=is_system,
        )
        db.add(acct)
        out[code] = acct
    db.flush()
    return out


def get_account(db: Session, company_id: int, code: str) -> LedgerAccount:
    acct = db.scalar(select(LedgerAccount).where(LedgerAccount.company_id == company_id, LedgerAccount.code == code))
    if acct is None:
        raise LedgerError(f"No ledger account with code {code!r} for company {company_id}")
    return acct


class Leg:
    """One side of a journal entry, before it is posted."""

    __slots__ = ("account", "direction", "amount_cents")

    def __init__(self, account: LedgerAccount, direction: Direction, amount_cents: int):
        if amount_cents <= 0:
            raise LedgerError(f"Journal line amount must be positive, got {amount_cents}")
        self.account, self.direction, self.amount_cents = account, direction, amount_cents


def debit(account: LedgerAccount, amount_cents: int) -> Leg:
    return Leg(account, Direction.debit, amount_cents)


def credit(account: LedgerAccount, amount_cents: int) -> Leg:
    return Leg(account, Direction.credit, amount_cents)


def post_journal_entry(
    db: Session,
    *,
    company_id: int,
    entry_date: date,
    memo: str,
    source_type: str,
    legs: list[Leg],
    source_id: int | None = None,
) -> JournalEntry:
    """Post a balanced journal entry. Refuses anything that does not balance, has fewer than two
    legs, or references an account belonging to a different company."""
    if len(legs) < 2:
        raise LedgerError("A journal entry needs at least two legs.")
    total_debit = sum(leg.amount_cents for leg in legs if leg.direction == Direction.debit)
    total_credit = sum(leg.amount_cents for leg in legs if leg.direction == Direction.credit)
    if total_debit != total_credit:
        raise LedgerError(f"Journal entry does not balance: debits={total_debit} credits={total_credit}")
    for leg in legs:
        if leg.account.company_id != company_id:
            raise LedgerError(f"Ledger account {leg.account.id} does not belong to company {company_id}")

    entry = JournalEntry(company_id=company_id, entry_date=entry_date, memo=memo, source_type=source_type, source_id=source_id)
    db.add(entry)
    db.flush()
    for leg in legs:
        db.add(JournalLine(entry_id=entry.id, ledger_account_id=leg.account.id, direction=leg.direction, amount_cents=leg.amount_cents))
    db.flush()
    return entry


def _line_total(db: Session, ledger_account: LedgerAccount, direction: Direction, as_of: date | None) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.coalesce(func.sum(JournalLine.amount_cents), 0))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.ledger_account_id == ledger_account.id, JournalLine.direction == direction)
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    return db.scalar(stmt) or 0


def account_balance(db: Session, ledger_account: LedgerAccount, as_of: date | None = None) -> int:
    """Signed balance in cents, positive in the account's normal-balance direction."""
    debit_total = _line_total(db, ledger_account, Direction.debit, as_of)
    credit_total = _line_total(db, ledger_account, Direction.credit, as_of)
    signed = debit_total - credit_total
    return signed if ledger_account.normal_balance == Direction.debit else -signed


def account_balances(db: Session, company_id: int, as_of: date | None = None) -> dict[str, int]:
    """{code: signed balance in cents} for every ledger account of a company."""
    accounts = db.scalars(select(LedgerAccount).where(LedgerAccount.company_id == company_id)).all()
    return {a.code: account_balance(db, a, as_of) for a in accounts}
