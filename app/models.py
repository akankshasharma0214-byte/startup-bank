from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _enum_col(py_enum):
    """Stores an enum's .value as a plain VARCHAR (portable, no native DB enum type) and always
    reconstructs the Python enum member on load, unlike a bare String column."""
    return SAEnum(py_enum, native_enum=False, validate_strings=True, values_callable=lambda e: [m.value for m in e], length=20)


def _now() -> datetime:
    return datetime.now(UTC)


class AccountType(enum.StrEnum):
    asset = "asset"
    liability = "liability"
    equity = "equity"
    revenue = "revenue"
    expense = "expense"


class Direction(enum.StrEnum):
    debit = "debit"
    credit = "credit"


class Role(enum.StrEnum):
    admin = "admin"  # owner/admin: full control, onboarding, approvals, card issuance
    bookkeeper = "bookkeeper"  # categorize, pay bills, run reports, no card issuance
    employee = "employee"  # cardholder only: sees own card + transactions


class TxnType(enum.StrEnum):
    funding = "funding"  # initial capital deposit into the account
    internal_transfer = "internal_transfer"  # between the company's own accounts (checking <-> savings)
    ach_out = "ach_out"  # generic outgoing transfer to an external party (not a bill)
    wire_in = "wire_in"  # generic incoming wire/ACH (not an invoice payment)
    card = "card"
    bill_payment = "bill_payment"
    invoice_payment = "invoice_payment"


class TxnStatus(enum.StrEnum):
    authorized = "authorized"  # card auth hold: reduces available balance, not yet on the ledger
    processing = "processing"  # ACH/wire in flight
    settled = "settled"  # posted to the ledger, final
    failed = "failed"
    voided = "voided"


class CardStatus(enum.StrEnum):
    active = "active"
    frozen = "frozen"
    closed = "closed"


class LimitPeriod(enum.StrEnum):
    per_transaction = "per_transaction"
    daily = "daily"
    monthly = "monthly"
    none = "none"


class BillStatus(enum.StrEnum):
    pending_approval = "pending_approval"
    scheduled = "scheduled"
    paid = "paid"
    void = "void"


class InvoiceStatus(enum.StrEnum):
    draft = "draft"
    sent = "sent"
    paid = "paid"
    void = "void"


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    legal_name: Mapped[str] = mapped_column(String(200))
    ein: Mapped[str] = mapped_column(String(20))  # simulated, not a real EIN
    industry: Mapped[str] = mapped_column(String(100), default="")
    kyb_status: Mapped[str] = mapped_column(String(20), default="verified")  # simulated KYB, always instant
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    users: Mapped[list[User]] = relationship(back_populates="company", cascade="all, delete-orphan")
    ledger_accounts: Mapped[list[LedgerAccount]] = relationship(back_populates="company", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[Role] = mapped_column(_enum_col(Role))
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    company: Mapped[Company] = relationship(back_populates="users")


class LedgerAccount(Base):
    """One row of the chart of accounts. `normal_balance` is explicit (not derived from `type`) so
    contra accounts (e.g. Owner Draws, a debit-normal account under equity) are still correct."""

    __tablename__ = "ledger_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[AccountType] = mapped_column(_enum_col(AccountType))
    normal_balance: Mapped[Direction] = mapped_column(_enum_col(Direction))
    is_bank_account: Mapped[bool] = mapped_column(default=False)
    bank_subtype: Mapped[str | None] = mapped_column(String(20), nullable=True)  # checking | savings
    is_system: Mapped[bool] = mapped_column(default=False)  # cannot be deleted (AP/AR/bank/equity roots)
    is_active: Mapped[bool] = mapped_column(default=True)

    company: Mapped[Company] = relationship(back_populates="ledger_accounts")

    __table_args__ = (UniqueConstraint("company_id", "code"),)


class JournalEntry(Base):
    """One balanced accounting event. Immutable once created: corrections are new entries,
    never edits (standard audit-trail practice)."""

    __tablename__ = "journal_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    entry_date: Mapped[date] = mapped_column(Date)
    memo: Mapped[str] = mapped_column(String(300), default="")
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    lines: Mapped[list[JournalLine]] = relationship(back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    ledger_account_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id"))
    direction: Mapped[Direction] = mapped_column(_enum_col(Direction))
    amount_cents: Mapped[int] = mapped_column(Integer)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    ledger_account: Mapped[LedgerAccount] = relationship()

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_journal_line_amount_positive"),)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(200))
    default_category_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_accounts.id"), nullable=True)
    payment_method: Mapped[str] = mapped_column(String(10), default="ach")  # ach | wire | check
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str] = mapped_column(String(300), default="")
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[BillStatus] = mapped_column(_enum_col(BillStatus), default=BillStatus.pending_approval)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    accrual_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_bill_amount_positive"),)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    customer_name: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str] = mapped_column(String(300), default="")
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[InvoiceStatus] = mapped_column(_enum_col(InvoiceStatus), default=InvoiceStatus.draft)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    accrual_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_invoice_amount_positive"),)


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id"))  # bank account it draws from
    holder_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(100))
    last4: Mapped[str] = mapped_column(String(4))
    status: Mapped[CardStatus] = mapped_column(_enum_col(CardStatus), default=CardStatus.active)
    limit_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_period: Mapped[LimitPeriod] = mapped_column(_enum_col(LimitPeriod), default=LimitPeriod.none)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Transaction(Base):
    """The unified activity-feed row and the settlement queue. Every bank-account cash movement
    (deposits, transfers, card spend, bill/invoice payment) is one row here. Accrual events (a bill
    received or an invoice sent) are NOT transactions — they post directly to the ledger with no
    cash movement and therefore no settlement float."""

    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id"))
    type: Mapped[TxnType] = mapped_column(_enum_col(TxnType))
    direction: Mapped[Direction] = mapped_column(_enum_col(Direction))  # debit=inflow to bank acct, credit=outflow
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[TxnStatus] = mapped_column(_enum_col(TxnStatus), default=TxnStatus.processing)
    description: Mapped[str] = mapped_column(String(300), default="")
    counterparty: Mapped[str] = mapped_column(String(200), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_accounts.id"), nullable=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id"), nullable=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    counterparty_account_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_accounts.id"), nullable=True)
    created_date: Mapped[date] = mapped_column(Date)
    settle_date: Mapped[date] = mapped_column(Date)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_txn_amount_positive"),)


class SimClockState(Base):
    """Single-row table holding the app's simulated 'today', so pending ACH/wire float can be
    demoed by advancing days instead of waiting for real time to pass."""

    __tablename__ = "sim_clock"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    sim_date: Mapped[date] = mapped_column(Date)


# ---------------------------------------------------------------------------
# FP&A layer: budgeting, forecasting, planning. Sits on top of the ledger; never posts to it.
# ---------------------------------------------------------------------------


class Budget(Base):
    """A named set of monthly targets by category (e.g. 'FY2026 Budget')."""

    __tablename__ = "budgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(200))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    lines: Mapped[list[BudgetLine]] = relationship(back_populates="budget", cascade="all, delete-orphan")


class BudgetLine(Base):
    """One category's target for one calendar month (period is always the 1st of the month)."""

    __tablename__ = "budget_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"))
    ledger_account_id: Mapped[int] = mapped_column(ForeignKey("ledger_accounts.id"))
    period: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)

    budget: Mapped[Budget] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("budget_id", "ledger_account_id", "period"),
        CheckConstraint("amount_cents >= 0", name="ck_budget_line_amount_nonnegative"),
    )


class ForecastScenario(Base):
    """A saved set of forecast assumption overrides, so a user's tweaks survive a page reload and
    get recomputed against whatever the actuals look like now. `overrides` maps driver key
    (e.g. 'revenue_growth' or 'expense:5010') -> list of per-month decimal values; any driver/month
    not present falls back to the historical-average default computed at forecast time."""

    __tablename__ = "forecast_scenarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(200), default="Base case")
    months: Mapped[int] = mapped_column(Integer, default=12)
    overrides_json: Mapped[str] = mapped_column(String, default="{}")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
