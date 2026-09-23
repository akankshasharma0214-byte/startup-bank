"""Shared constructor for every bank-account cash movement. See settlement.py for how these
rows eventually become ledger entries."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .models import Direction, LedgerAccount, Transaction, TxnStatus, TxnType


def create_transaction(
    db: Session,
    *,
    company_id: int,
    account: LedgerAccount,
    direction: Direction,
    amount_cents: int,
    counterparty_account: LedgerAccount,
    txn_type: TxnType,
    description: str,
    settle_date: date,
    created_date: date,
    counterparty: str = "",
    status: TxnStatus = TxnStatus.processing,
    card_id: int | None = None,
    bill_id: int | None = None,
    invoice_id: int | None = None,
    created_by_id: int | None = None,
    category_id: int | None = None,
) -> Transaction:
    if amount_cents <= 0:
        raise ValueError("Transaction amount must be positive.")
    if account.company_id != company_id or counterparty_account.company_id != company_id:
        raise ValueError("Account does not belong to this company.")
    txn = Transaction(
        company_id=company_id,
        account_id=account.id,
        type=txn_type,
        direction=direction,
        amount_cents=amount_cents,
        status=status,
        description=description,
        counterparty=counterparty,
        counterparty_account_id=counterparty_account.id,
        category_id=category_id,
        card_id=card_id,
        bill_id=bill_id,
        invoice_id=invoice_id,
        created_date=created_date,
        settle_date=settle_date,
        created_by_id=created_by_id,
    )
    db.add(txn)
    db.flush()
    return txn
