"""Turn ORM objects into JSON-safe dicts. Money is always exposed both as raw integer cents and
as a formatted string; the frontend never re-derives a dollar amount from a float."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import (
    Bill,
    Card,
    Company,
    Invoice,
    LedgerAccount,
    Transaction,
    User,
    Vendor,
)
from .money import cents_to_dollars, fmt_usd


def money(cents: int) -> dict:
    return {"cents": cents, "dollars": str(cents_to_dollars(cents)), "formatted": fmt_usd(cents)}


def user_out(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role.value}


def company_out(c: Company) -> dict:
    return {"id": c.id, "name": c.name, "legal_name": c.legal_name, "ein": c.ein, "industry": c.industry, "kyb_status": c.kyb_status}


def account_out(a: LedgerAccount, balance_cents: int, available_cents: int | None = None) -> dict:
    out = {
        "code": a.code,
        "name": a.name,
        "type": a.type.value,
        "is_bank_account": a.is_bank_account,
        "bank_subtype": a.bank_subtype,
        "balance": money(balance_cents),
    }
    if available_cents is not None:
        out["available"] = money(available_cents)
    return out


def card_out(c: Card, db: Session) -> dict:
    holder = db.get(User, c.holder_id)
    account = db.get(LedgerAccount, c.account_id)
    return {
        "id": c.id,
        "label": c.label,
        "last4": c.last4,
        "status": c.status.value,
        "holder": user_out(holder),
        "account_code": account.code,
        "limit": money(c.limit_cents) if c.limit_cents is not None else None,
        "limit_period": c.limit_period.value,
    }


def vendor_out(v: Vendor, db: Session) -> dict:
    cat = db.get(LedgerAccount, v.default_category_id) if v.default_category_id else None
    return {"id": v.id, "name": v.name, "payment_method": v.payment_method, "default_category_code": cat.code if cat else None}


def bill_out(b: Bill, db: Session) -> dict:
    vendor = db.get(Vendor, b.vendor_id)
    category = db.get(LedgerAccount, b.category_id)
    return {
        "id": b.id,
        "vendor": vendor.name,
        "vendor_id": b.vendor_id,
        "category_code": category.code,
        "category_name": category.name,
        "amount": money(b.amount_cents),
        "memo": b.memo,
        "due_date": b.due_date.isoformat(),
        "status": b.status.value,
        "created_at": b.created_at.isoformat(),
    }


def invoice_out(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "customer_name": inv.customer_name,
        "amount": money(inv.amount_cents),
        "memo": inv.memo,
        "due_date": inv.due_date.isoformat(),
        "status": inv.status.value,
        "created_at": inv.created_at.isoformat(),
    }


def transaction_out(t: Transaction, db: Session) -> dict:
    account = db.get(LedgerAccount, t.account_id)
    category = db.get(LedgerAccount, t.category_id) if t.category_id else None
    return {
        "id": t.id,
        "type": t.type.value,
        "direction": t.direction.value,
        "amount": money(t.amount_cents),
        "status": t.status.value,
        "description": t.description,
        "counterparty": t.counterparty,
        "account_code": account.code,
        "category_code": category.code if category else None,
        "category_name": category.name if category else None,
        "card_id": t.card_id,
        "bill_id": t.bill_id,
        "invoice_id": t.invoice_id,
        "created_date": t.created_date.isoformat(),
        "settle_date": t.settle_date.isoformat(),
        "settled_at": t.settled_at.isoformat() if t.settled_at else None,
    }
