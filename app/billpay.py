"""Accounts payable: vendors, bills, and paying them. A bill's accrual (Debit expense category,
Credit Accounts Payable) posts the moment the bill is recorded, matching when the expense was
actually incurred. Paying it is a separate cash-movement event with ACH/wire/check float, handled
through the Transaction/settlement pipeline like everything else."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from .ledger import LedgerError, credit, debit, get_account, post_journal_entry
from .models import Bill, BillStatus, Direction, LedgerAccount, Role, TxnType, User, Vendor
from .simclock import next_business_day, sim_today
from .transactions import create_transaction

PAYMENT_METHODS = ("ach", "wire", "check")
SETTLE_BUSINESS_DAYS = {"wire": 0, "ach": 1, "check": 3}


def add_vendor(
    db: Session, *, company_id: int, name: str, default_category: LedgerAccount | None = None, payment_method: str = "ach"
) -> Vendor:
    if not name.strip():
        raise ValueError("Vendor name is required.")
    if payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {PAYMENT_METHODS}")
    vendor = Vendor(
        company_id=company_id,
        name=name.strip(),
        default_category_id=default_category.id if default_category else None,
        payment_method=payment_method,
    )
    db.add(vendor)
    db.commit()
    return vendor


def create_bill(
    db: Session,
    *,
    company_id: int,
    vendor: Vendor,
    category: LedgerAccount,
    amount_cents: int,
    memo: str,
    due_date: date,
    created_by: User,
) -> Bill:
    if amount_cents <= 0:
        raise ValueError("Bill amount must be positive.")
    if vendor.company_id != company_id or category.company_id != company_id:
        raise LedgerError("Vendor or category does not belong to this company.")
    today = sim_today(db)
    ap = get_account(db, company_id, "2000")
    entry = post_journal_entry(
        db,
        company_id=company_id,
        entry_date=today,
        memo=f"Bill from {vendor.name}: {memo}",
        source_type="bill_accrual",
        legs=[debit(category, amount_cents), credit(ap, amount_cents)],
    )
    auto_approved = created_by.role == Role.admin
    bill = Bill(
        company_id=company_id,
        vendor_id=vendor.id,
        category_id=category.id,
        amount_cents=amount_cents,
        memo=memo,
        due_date=due_date,
        status=BillStatus.scheduled if auto_approved else BillStatus.pending_approval,
        created_by_id=created_by.id,
        approved_by_id=created_by.id if auto_approved else None,
        accrual_entry_id=entry.id,
    )
    db.add(bill)
    db.commit()
    return bill


def approve_bill(db: Session, bill: Bill, approver: User) -> None:
    if approver.role != Role.admin:
        raise PermissionError("Only an admin can approve a bill.")
    if bill.status != BillStatus.pending_approval:
        raise LedgerError(f"Bill is {bill.status.value}, not pending approval.")
    bill.status = BillStatus.scheduled
    bill.approved_by_id = approver.id
    db.commit()


def void_bill(db: Session, bill: Bill) -> None:
    if bill.status == BillStatus.paid:
        raise LedgerError("A paid bill cannot be voided.")
    if bill.accrual_entry_id is not None:  # reverse the accrual: Debit AP, Credit the original category
        original_category = db.get(LedgerAccount, bill.category_id)
        today = sim_today(db)
        post_journal_entry(
            db,
            company_id=bill.company_id,
            entry_date=today,
            memo=f"Void bill #{bill.id}",
            source_type="bill_void",
            source_id=bill.id,
            legs=[debit(get_account(db, bill.company_id, "2000"), bill.amount_cents), credit(original_category, bill.amount_cents)],
        )
    bill.status = BillStatus.void
    db.commit()


def schedule_bill_payment(db: Session, bill: Bill, created_by: User):
    if bill.status != BillStatus.scheduled:
        raise LedgerError(f"Bill must be approved (scheduled) before payment; it is {bill.status.value}.")
    vendor = db.get(Vendor, bill.vendor_id)
    checking = get_account(db, bill.company_id, "1000")
    ap = get_account(db, bill.company_id, "2000")
    today = sim_today(db)
    settle_date = (
        next_business_day(today, SETTLE_BUSINESS_DAYS[vendor.payment_method]) if SETTLE_BUSINESS_DAYS[vendor.payment_method] else today
    )
    txn = create_transaction(
        db,
        company_id=bill.company_id,
        account=checking,
        direction=Direction.credit,
        amount_cents=bill.amount_cents,
        counterparty_account=ap,
        txn_type=TxnType.bill_payment,
        description=f"Bill payment: {vendor.name} ({vendor.payment_method.upper()})",
        counterparty=vendor.name,
        settle_date=settle_date,
        created_date=today,
        bill_id=bill.id,
        created_by_id=created_by.id,
    )
    bill.status = BillStatus.paid
    db.commit()
    return txn
