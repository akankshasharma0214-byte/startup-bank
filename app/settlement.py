"""Turns pending Transactions into posted ledger entries once their settle_date arrives. This is
the only place that posts the ledger for a Transaction row; callers create the Transaction in
'processing'/'authorized' status and this function is what makes it real money on the books."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import credit, debit, post_journal_entry
from .models import Direction, LedgerAccount, Transaction, TxnStatus


def _bank_account(db: Session, txn: Transaction) -> LedgerAccount:
    return db.get(LedgerAccount, txn.account_id)


def _counterparty_account(db: Session, txn: Transaction) -> LedgerAccount:
    if txn.counterparty_account_id is None:
        raise ValueError(f"Transaction {txn.id} of type {txn.type} has no counterparty ledger account set.")
    return db.get(LedgerAccount, txn.counterparty_account_id)


def _post_for_transaction(db: Session, txn: Transaction) -> None:
    bank = _bank_account(db, txn)
    if txn.direction == Direction.debit:  # inflow to the bank account
        legs = [debit(bank, txn.amount_cents), credit(_counterparty_account(db, txn), txn.amount_cents)]
    else:  # outflow from the bank account
        legs = [debit(_counterparty_account(db, txn), txn.amount_cents), credit(bank, txn.amount_cents)]
    entry = post_journal_entry(
        db,
        company_id=txn.company_id,
        # Dated when the transaction was supposed to clear, not whenever this batch happened to run --
        # otherwise a settlement backlog (e.g. the sim clock jumping forward several days between
        # calls) would misdate every entry in it to "today", corrupting historical reporting for the
        # days in between.
        entry_date=txn.settle_date,
        memo=txn.description or txn.type.value,
        source_type=f"txn:{txn.type.value}",
        source_id=txn.id,
        legs=legs,
    )
    txn.journal_entry_id = entry.id
    txn.status = TxnStatus.settled
    txn.settled_at = datetime.now(UTC)
    db.add(txn)


def run_settlement(db: Session, company_id: int | None = None) -> list[Transaction]:
    """Post every Transaction whose settle_date has arrived. Idempotent: safe to call as often as
    you like (e.g. on every page load). Returns the transactions that were just settled."""
    from .simclock import sim_today

    today = sim_today(db)
    stmt = select(Transaction).where(
        Transaction.status.in_([TxnStatus.processing, TxnStatus.authorized]),
        Transaction.settle_date <= today,
    )
    if company_id is not None:
        stmt = stmt.where(Transaction.company_id == company_id)
    due = db.scalars(stmt).all()
    settled = []
    for txn in due:
        _post_for_transaction(db, txn)
        settled.append(txn)
    if settled:
        db.commit()
    return settled


def available_balance_delta(db: Session, ledger_account_id: int, company_id: int) -> int:
    """Net effect (in cents, signed toward inflow) of not-yet-settled transactions on this bank
    account, so the UI can show 'available' balance separately from the posted ledger balance."""
    stmt = select(Transaction).where(
        Transaction.account_id == ledger_account_id,
        Transaction.company_id == company_id,
        Transaction.status.in_([TxnStatus.processing, TxnStatus.authorized]),
    )
    delta = 0
    for txn in db.scalars(stmt).all():
        delta += txn.amount_cents if txn.direction == Direction.debit else -txn.amount_cents
    return delta
