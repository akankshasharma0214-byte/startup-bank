"""Deterministic merchant-name -> expense-category suggestion (a simple, transparent, explainable
stand-in for Rho's AI auto-coding), plus reclassification of a settled transaction to a different
category. Reclassification never edits a posted journal entry; it posts a new balanced entry that
moves the amount, so the audit trail stays intact."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .ledger import LedgerError, credit, debit, get_account, post_journal_entry
from .models import LedgerAccount, Transaction
from .simclock import sim_today

MERCHANT_KEYWORDS: dict[str, list[str]] = {
    "5010": ["aws", "amazon web services", "github", "google cloud", "gcp", "microsoft azure", "slack",
             "notion", "zoom", "figma", "linear", "vercel", "openai", "anthropic", "datadog", "saas"],
    "5020": ["uber", "lyft", "delta air", "united airlines", "airbnb", "marriott", "hilton", "expedia", "airlines"],
    "5030": ["starbucks", "restaurant", "doordash", "grubhub", "cafe", "coffee", "chipotle"],
    "5040": ["staples", "office depot", "best buy", "ikea"],
    "5050": ["wework", "regus", "property management", "landlord"],
    "5060": ["facebook ads", "meta ads", "google ads", "linkedin ads", "mailchimp", "hubspot"],
    "5070": ["law firm", "llp", "consulting", "accountant", "cpa"],
    "5090": ["wire fee", "overdraft fee", "bank fee", "monthly fee"],
}  # fmt: skip


def suggest_category(db: Session, company_id: int, merchant_name: str) -> LedgerAccount:
    low = merchant_name.lower()
    for code, keywords in MERCHANT_KEYWORDS.items():
        if any(k in low for k in keywords):
            return get_account(db, company_id, code)
    return get_account(db, company_id, "5900")  # Uncategorized Expense


def reclassify_transaction(db: Session, txn: Transaction, new_category: LedgerAccount, memo: str = "") -> None:
    from .models import TxnStatus

    if txn.status != TxnStatus.settled:
        raise LedgerError("Only a settled transaction can be recategorized.")
    if txn.category_id is None:
        raise LedgerError("This transaction has no expense category to move.")
    if txn.company_id != new_category.company_id:
        raise LedgerError("New category does not belong to this company.")
    if txn.category_id == new_category.id:
        raise LedgerError("Transaction is already in that category.")
    old_category = db.get(LedgerAccount, txn.category_id)
    today = sim_today(db)
    post_journal_entry(
        db,
        company_id=txn.company_id,
        entry_date=today,
        memo=memo or f"Recategorize: {old_category.name} -> {new_category.name}",
        source_type="reclassification",
        source_id=txn.id,
        legs=[debit(new_category, txn.amount_cents), credit(old_category, txn.amount_cents)],
    )
    txn.category_id = new_category.id
    db.add(txn)
    db.commit()
