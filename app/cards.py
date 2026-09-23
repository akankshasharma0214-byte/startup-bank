"""Virtual corporate cards: issuing, freezing, and spend-limit-enforced authorization. A card
charge is a card-network authorization hold today (status=authorized, reduces available balance
immediately) that settles to the ledger the next business day, same as real card rails."""

from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .categorization import suggest_category
from .ledger import LedgerError
from .models import Card, CardStatus, Direction, LedgerAccount, LimitPeriod, Transaction, TxnStatus, TxnType, User
from .simclock import next_business_day, sim_today
from .transactions import create_transaction


class CardError(LedgerError):
    pass


def issue_card(
    db: Session,
    *,
    company_id: int,
    account: LedgerAccount,
    holder: User,
    label: str,
    limit_cents: int | None = None,
    limit_period: LimitPeriod = LimitPeriod.none,
) -> Card:
    if not account.is_bank_account:
        raise CardError("A card must draw from a bank account.")
    if limit_period != LimitPeriod.none and (limit_cents is None or limit_cents <= 0):
        raise CardError("A spend limit amount is required when a limit period is set.")
    card = Card(
        company_id=company_id,
        account_id=account.id,
        holder_id=holder.id,
        label=label.strip() or "Corporate Card",
        last4=f"{secrets.randbelow(10_000):04d}",
        limit_cents=limit_cents,
        limit_period=limit_period,
    )
    db.add(card)
    db.commit()
    return card


def set_card_status(db: Session, card: Card, status: CardStatus) -> None:
    if card.status == CardStatus.closed:
        raise CardError("A closed card cannot be reactivated.")
    card.status = status
    db.commit()


def _period_start(card: Card, today: date) -> date | None:
    if card.limit_period == LimitPeriod.daily:
        return today
    if card.limit_period == LimitPeriod.monthly:
        return today.replace(day=1)
    return None


def _spent_in_period(db: Session, card: Card, start: date) -> int:
    stmt = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        Transaction.card_id == card.id,
        Transaction.status.in_([TxnStatus.authorized, TxnStatus.processing, TxnStatus.settled]),
        Transaction.created_date >= start,
    )
    return db.scalar(stmt) or 0


def check_limit(db: Session, card: Card, amount_cents: int) -> None:
    if card.limit_period == LimitPeriod.none or card.limit_cents is None:
        return
    if card.limit_period == LimitPeriod.per_transaction:
        if amount_cents > card.limit_cents:
            raise CardError(f"Purchase of {amount_cents} cents exceeds the per-transaction limit of {card.limit_cents} cents.")
        return
    today = sim_today(db)
    start = _period_start(card, today)
    already_spent = _spent_in_period(db, card, start)
    if already_spent + amount_cents > card.limit_cents:
        raise CardError(
            f"Purchase would exceed the {card.limit_period.value} limit: "
            f"{already_spent} already spent + {amount_cents} > {card.limit_cents} cents."
        )


def authorize_charge(db: Session, *, card: Card, amount_cents: int, merchant: str, created_by_id: int | None = None) -> Transaction:
    if card.status != CardStatus.active:
        raise CardError(f"Card is {card.status.value}; it cannot be used.")
    check_limit(db, card, amount_cents)
    today = sim_today(db)
    bank_account = db.get(LedgerAccount, card.account_id)
    category = suggest_category(db, card.company_id, merchant)
    txn = create_transaction(
        db,
        company_id=card.company_id,
        account=bank_account,
        direction=Direction.credit,
        amount_cents=amount_cents,
        counterparty_account=category,
        txn_type=TxnType.card,
        description=f"Card purchase: {merchant}",
        counterparty=merchant,
        settle_date=next_business_day(today),
        created_date=today,
        status=TxnStatus.authorized,
        card_id=card.id,
        created_by_id=created_by_id,
        category_id=category.id,
    )
    db.commit()
    return txn
