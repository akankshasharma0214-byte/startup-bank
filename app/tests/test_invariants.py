"""Property-style stress test: run a long, varied sequence of realistic activity and check the
fundamental accounting identity (assets == liabilities + equity) after every single step, plus that
the ledger never contains an unbalanced entry. This is the strongest guarantee that the system is
honest about money: if any code path posted a lopsided entry, this test would catch it."""

import random
from datetime import date

from app.billpay import add_vendor, approve_bill, create_bill, schedule_bill_payment
from app.cards import authorize_charge, issue_card
from app.categorization import reclassify_transaction
from app.invoicing import record_invoice_payment, send_invoice
from app.ledger import get_account
from app.models import JournalEntry, JournalLine, LimitPeriod, Role, User
from app.onboarding import fund_account, onboard_company
from app.reports import balance_sheet
from app.settlement import run_settlement
from app.simclock import advance_days


def _assert_ledger_and_balance_sheet_sane(db, company_id):
    from sqlalchemy import func, select

    per_entry = (
        select(JournalLine.entry_id, JournalLine.direction, func.sum(JournalLine.amount_cents))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.company_id == company_id)
        .group_by(JournalLine.entry_id, JournalLine.direction)
    )
    totals: dict[int, dict[str, int]] = {}
    for entry_id, direction, total in db.execute(per_entry).all():
        totals.setdefault(entry_id, {})[direction] = total
    for entry_id, sides in totals.items():
        assert sides.get("debit", 0) == sides.get("credit", 0), f"entry {entry_id} is unbalanced: {sides}"

    bs = balance_sheet(db, company_id)
    assert bs["balances"], bs
    assert bs["assets_total_cents"] == bs["liabilities_total_cents"] + bs["equity_total_cents"]


def test_random_activity_never_breaks_the_accounting_identity(db):
    rng = random.Random(20260101)
    company, admin = onboard_company(
        db,
        name="Stress Co",
        legal_name="Stress Co Inc",
        ein="0",
        industry="x",
        admin_name="Admin",
        admin_email="admin@stress.co",
        admin_password="x",
    )
    bookkeeper = User(company_id=company.id, name="BK", email="bk@stress.co", role=Role.bookkeeper, password_hash="x")
    employee = User(company_id=company.id, name="Emp", email="emp@stress.co", role=Role.employee, password_hash="x")
    db.add_all([bookkeeper, employee])
    db.commit()

    fund_account(db, company_id=company.id, amount_cents=rng.randint(1_000_000, 10_000_000), memo="seed")
    _assert_ledger_and_balance_sheet_sane(db, company.id)

    checking = get_account(db, company.id, "1000")
    card = issue_card(
        db,
        company_id=company.id,
        account=checking,
        holder=employee,
        label="Stress card",
        limit_cents=2_000_000,
        limit_period=LimitPeriod.monthly,
    )
    vendors = [
        add_vendor(db, company_id=company.id, name=f"Vendor {i}", payment_method=rng.choice(["ach", "wire", "check"])) for i in range(3)
    ]
    category_codes = ["5000", "5010", "5020", "5030", "5040", "5050", "5060", "5070", "5080"]
    merchants = ["AWS", "Uber", "Starbucks", "Random Corp", "Staples", "Delta Air", "Unlisted Biz"]

    open_bills, open_invoices, settled_card_txns = [], [], []
    for _ in range(150):
        action = rng.choice(["card", "bill", "approve_bill", "pay_bill", "invoice", "pay_invoice", "advance", "reclass"])
        try:
            if action == "card":
                authorize_charge(db, card=card, amount_cents=rng.randint(100, 50_000), merchant=rng.choice(merchants))
            elif action == "bill":
                creator = rng.choice([admin, bookkeeper])
                bill = create_bill(
                    db,
                    company_id=company.id,
                    vendor=rng.choice(vendors),
                    category=get_account(db, company.id, rng.choice(category_codes)),
                    amount_cents=rng.randint(100, 200_000),
                    memo="stress",
                    due_date=date(2026, 3, 1),
                    created_by=creator,
                )
                open_bills.append(bill)
            elif action == "approve_bill" and open_bills:
                bill = rng.choice(open_bills)
                if bill.status.value == "pending_approval":
                    approve_bill(db, bill, admin)
            elif action == "pay_bill" and open_bills:
                bill = rng.choice(open_bills)
                if bill.status.value == "scheduled":
                    schedule_bill_payment(db, bill, admin)
                    open_bills.remove(bill)
            elif action == "invoice":
                inv = send_invoice(
                    db,
                    company_id=company.id,
                    customer_name=f"Customer {rng.randint(1, 20)}",
                    amount_cents=rng.randint(1_000, 500_000),
                    memo="stress",
                    due_date=date(2026, 3, 1),
                    created_by=admin,
                )
                open_invoices.append(inv)
            elif action == "pay_invoice" and open_invoices:
                inv = rng.choice(open_invoices)
                record_invoice_payment(db, inv, method=rng.choice(["ach", "wire"]), created_by=admin)
                open_invoices.remove(inv)
            elif action == "advance":
                advance_days(db, rng.randint(1, 3))
                for t in run_settlement(db, company.id):
                    if t.type.value == "card" and t.category_id is not None:
                        settled_card_txns.append(t)
            elif action == "reclass" and settled_card_txns:
                t = rng.choice(settled_card_txns)
                new_cat = get_account(db, company.id, rng.choice(category_codes))
                if new_cat.id != t.category_id:
                    reclassify_transaction(db, t, new_cat)
                    settled_card_txns.remove(t)
        except Exception as e:  # a domain rule can legitimately refuse an action; the ledger must stay sane regardless
            assert not isinstance(e, AssertionError)
        _assert_ledger_and_balance_sheet_sane(db, company.id)

    advance_days(db, 10)
    run_settlement(db, company.id)
    _assert_ledger_and_balance_sheet_sane(db, company.id)

    # The forecasting engine reads this randomly-generated, messy history: it must never crash and
    # must always produce a balanced projection, no matter how irregular the actuals were.
    from app.forecasting import build_forecast

    fc = build_forecast(db, company.id, months=12)
    assert fc["balances"] is True
    assert all(row["balances"] for row in fc["balance_sheet"])
