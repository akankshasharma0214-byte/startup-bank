"""Accounts receivable: sending customer invoices and recording payment. Sending an invoice is an
accrual (Debit AR, Credit Revenue) posted immediately, since the revenue is recognized when
invoiced. Recording payment is the cash-movement event, with the usual settlement float."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .ledger import LedgerError, credit, debit, get_account, post_journal_entry
from .models import Direction, Invoice, InvoiceStatus, TxnType, User
from .simclock import next_business_day, sim_today
from .transactions import create_transaction

PAYMENT_METHODS = ("ach", "wire", "card")
SETTLE_BUSINESS_DAYS = {"wire": 0, "card": 0, "ach": 1}


def send_invoice(
    db: Session, *, company_id: int, customer_name: str, amount_cents: int, memo: str, due_date: date, created_by: User
) -> Invoice:
    if amount_cents <= 0:
        raise ValueError("Invoice amount must be positive.")
    if not customer_name.strip():
        raise ValueError("Customer name is required.")
    today = sim_today(db)
    ar = get_account(db, company_id, "1200")
    revenue = get_account(db, company_id, "4000")
    entry = post_journal_entry(
        db,
        company_id=company_id,
        entry_date=today,
        memo=f"Invoice to {customer_name}: {memo}",
        source_type="invoice_accrual",
        legs=[debit(ar, amount_cents), credit(revenue, amount_cents)],
    )
    invoice = Invoice(
        company_id=company_id,
        customer_name=customer_name.strip(),
        amount_cents=amount_cents,
        memo=memo,
        due_date=due_date,
        status=InvoiceStatus.sent,
        created_by_id=created_by.id,
        accrual_entry_id=entry.id,
    )
    db.add(invoice)
    db.commit()
    return invoice


def void_invoice(db: Session, invoice: Invoice) -> None:
    if invoice.status == InvoiceStatus.paid:
        raise LedgerError("A paid invoice cannot be voided.")
    if invoice.accrual_entry_id is not None:
        today = sim_today(db)
        post_journal_entry(
            db,
            company_id=invoice.company_id,
            entry_date=today,
            memo=f"Void invoice #{invoice.id}",
            source_type="invoice_void",
            source_id=invoice.id,
            legs=[
                debit(get_account(db, invoice.company_id, "4000"), invoice.amount_cents),
                credit(get_account(db, invoice.company_id, "1200"), invoice.amount_cents),
            ],
        )
    invoice.status = InvoiceStatus.void
    db.commit()


def record_invoice_payment(db: Session, invoice: Invoice, method: str = "ach", created_by: User | None = None):
    if invoice.status != InvoiceStatus.sent:
        raise LedgerError(f"Invoice is {invoice.status.value}, not awaiting payment.")
    if method not in PAYMENT_METHODS:
        raise ValueError(f"method must be one of {PAYMENT_METHODS}")
    checking = get_account(db, invoice.company_id, "1000")
    ar = get_account(db, invoice.company_id, "1200")
    today = sim_today(db)
    settle_date = next_business_day(today, SETTLE_BUSINESS_DAYS[method]) if SETTLE_BUSINESS_DAYS[method] else today
    txn = create_transaction(
        db,
        company_id=invoice.company_id,
        account=checking,
        direction=Direction.debit,
        amount_cents=invoice.amount_cents,
        counterparty_account=ar,
        txn_type=TxnType.invoice_payment,
        description=f"Payment from {invoice.customer_name} ({method.upper()})",
        counterparty=invoice.customer_name,
        settle_date=settle_date,
        created_date=today,
        invoice_id=invoice.id,
        created_by_id=created_by.id if created_by else None,
    )
    invoice.status = InvoiceStatus.paid
    db.commit()
    return txn
