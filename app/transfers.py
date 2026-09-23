"""Money movement that is not a card charge, bill, or invoice: moving cash between the company's
own accounts (e.g. sweeping into Treasury/savings), and generic external ACH/wire in or out."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .ledger import LedgerError, get_account
from .models import Direction, LedgerAccount, TxnType
from .simclock import next_business_day, sim_today
from .transactions import create_transaction

EXTERNAL_METHODS = ("ach", "wire")
SETTLE_BUSINESS_DAYS = {"wire": 0, "ach": 1}


def internal_transfer(
    db: Session,
    *,
    company_id: int,
    from_account: LedgerAccount,
    to_account: LedgerAccount,
    amount_cents: int,
    memo: str = "",
    created_by_id: int | None = None,
):
    """Move cash between two of the company's own bank accounts (e.g. Checking -> Savings/Treasury).
    Settles instantly: both accounts sit at the same institution in this prototype."""
    if from_account.id == to_account.id:
        raise LedgerError("Source and destination account must differ.")
    if not (from_account.is_bank_account and to_account.is_bank_account):
        raise LedgerError("Internal transfers must be between two bank accounts.")
    today = sim_today(db)
    txn = create_transaction(
        db,
        company_id=company_id,
        account=from_account,
        direction=Direction.credit,
        amount_cents=amount_cents,
        counterparty_account=to_account,
        txn_type=TxnType.internal_transfer,
        description=memo or f"Transfer to {to_account.name}",
        settle_date=today,
        created_date=today,
        created_by_id=created_by_id,
    )
    db.commit()
    return txn


def send_money(
    db: Session,
    *,
    company_id: int,
    from_account: LedgerAccount,
    amount_cents: int,
    counterparty: str,
    method: str,
    memo: str = "",
    created_by_id: int | None = None,
):
    """Generic outgoing ACH/wire to an external party not tied to a bill (e.g. an owner draw)."""
    if method not in EXTERNAL_METHODS:
        raise ValueError(f"method must be one of {EXTERNAL_METHODS}")
    draws = get_account(db, company_id, "3100")
    today = sim_today(db)
    settle_date = today if SETTLE_BUSINESS_DAYS[method] == 0 else next_business_day(today, SETTLE_BUSINESS_DAYS[method])
    txn = create_transaction(
        db,
        company_id=company_id,
        account=from_account,
        direction=Direction.credit,
        amount_cents=amount_cents,
        counterparty_account=draws,
        txn_type=TxnType.ach_out,
        description=memo or f"Transfer to {counterparty}",
        counterparty=counterparty,
        settle_date=settle_date,
        created_date=today,
        created_by_id=created_by_id,
    )
    db.commit()
    return txn


def receive_money(
    db: Session,
    *,
    company_id: int,
    to_account: LedgerAccount,
    amount_cents: int,
    counterparty: str,
    method: str,
    memo: str = "",
    created_by_id: int | None = None,
):
    """Generic incoming ACH/wire not tied to a customer invoice (e.g. a grant, a refund)."""
    if method not in EXTERNAL_METHODS:
        raise ValueError(f"method must be one of {EXTERNAL_METHODS}")
    other_income = get_account(db, company_id, "4900")
    today = sim_today(db)
    settle_date = today if SETTLE_BUSINESS_DAYS[method] == 0 else next_business_day(today, SETTLE_BUSINESS_DAYS[method])
    txn = create_transaction(
        db,
        company_id=company_id,
        account=to_account,
        direction=Direction.debit,
        amount_cents=amount_cents,
        counterparty_account=other_income,
        txn_type=TxnType.wire_in,
        description=memo or f"Payment from {counterparty}",
        counterparty=counterparty,
        settle_date=settle_date,
        created_date=today,
        created_by_id=created_by_id,
    )
    db.commit()
    return txn
