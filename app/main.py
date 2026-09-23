"""FastAPI app: JSON API + static dashboard. Every route that touches a specific company's data
derives the company strictly from the authenticated session (`current_user.company_id`) -- a
client can never pass a company_id and see another tenant's books. This is the core multi-tenant
security boundary of the whole app.

Everything here is a PROTOTYPE. No real money moves: ACH/wire/card rails are simulated in-process
(see transactions.py / settlement.py), there is no real bank partner, and KYB is auto-approved."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import billpay, budgeting, cards, categorization, forecasting, invoicing, onboarding, reports, transfers
from .auth import SESSION_COOKIE, hash_password, make_session_token, read_session_token, verify_password
from .db import get_db, init_db
from .forecast_validate import validate_forecast_workbook
from .forecast_workbook import build_forecast_workbook
from .ledger import LedgerError, get_account
from .models import Bill, Budget, Card, Company, ForecastScenario, Invoice, LedgerAccount, Role, Transaction, User, Vendor
from .money import dollars_to_cents
from .serializers import account_out, bill_out, card_out, company_out, invoice_out, money, transaction_out, user_out, vendor_out
from .settlement import available_balance_delta, run_settlement
from .simclock import advance_days, sim_today

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="startup-bank (prototype)", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Error handling: domain functions raise plain Python exceptions; convert them to HTTP responses
# once here instead of a try/except in every route.
# ---------------------------------------------------------------------------


@app.exception_handler(LedgerError)
async def _ledger_error(_request: Request, exc: LedgerError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _value_error(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PermissionError)
async def _permission_error(_request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = read_session_token(token) if token else None
    if user_id is None:
        raise HTTPException(401, "Not authenticated.")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Session refers to a user that no longer exists.")
    return user


def current_user_settled(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """The usual dependency: authenticates AND settles any of this company's transactions that
    have reached their settle date, so the books are always current when you look at them."""
    run_settlement(db, user.company_id)
    return user


def require_admin(user: User = Depends(current_user_settled)) -> User:
    if user.role != Role.admin:
        raise HTTPException(403, "This action requires an admin.")
    return user


def require_admin_or_bookkeeper(user: User = Depends(current_user_settled)) -> User:
    if user.role not in (Role.admin, Role.bookkeeper):
        raise HTTPException(403, "This action requires an admin or bookkeeper.")
    return user


def company_account(db: Session, user: User, code: str) -> LedgerAccount:
    return get_account(db, user.company_id, code)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class OnboardBody(BaseModel):
    name: str
    legal_name: str = ""
    ein: str = ""
    industry: str = ""
    admin_name: str
    admin_email: EmailStr
    admin_password: str = Field(min_length=6)
    initial_funding_dollars: float = 0


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class UserCreateBody(BaseModel):
    name: str
    email: EmailStr
    role: str  # bookkeeper | employee
    password: str = Field(min_length=6)


class TransferBody(BaseModel):
    from_code: str
    to_code: str
    amount_dollars: float
    memo: str = ""


class SendMoneyBody(BaseModel):
    from_code: str = "1000"
    amount_dollars: float
    counterparty: str
    method: str = "ach"
    memo: str = ""


class ReceiveMoneyBody(BaseModel):
    to_code: str = "1000"
    amount_dollars: float
    counterparty: str
    method: str = "ach"
    memo: str = ""


class ReclassifyBody(BaseModel):
    category_code: str


class VendorBody(BaseModel):
    name: str
    default_category_code: str | None = None
    payment_method: str = "ach"


class BillBody(BaseModel):
    vendor_id: int
    category_code: str
    amount_dollars: float
    memo: str = ""
    due_date: date


class InvoiceBody(BaseModel):
    customer_name: str
    amount_dollars: float
    memo: str = ""
    due_date: date


class InvoicePayBody(BaseModel):
    method: str = "ach"


class CardIssueBody(BaseModel):
    account_code: str = "1000"
    holder_user_id: int
    label: str
    limit_dollars: float | None = None
    limit_period: str = "none"


class CardChargeBody(BaseModel):
    amount_dollars: float
    merchant: str


class AdvanceBody(BaseModel):
    days: int = 1


class BudgetCreateBody(BaseModel):
    name: str


class BudgetLineBody(BaseModel):
    category_code: str
    period: date  # any date in the target month; normalized to the 1st
    amount_dollars: float


class BudgetPopulateBody(BaseModel):
    months: int = 12
    lookback_months: int = 3


class ScenarioCreateBody(BaseModel):
    name: str = "Base case"
    months: int = 12


class ScenarioOverrideBody(BaseModel):
    driver_key: str  # "revenue_growth" | "dso_days" | "dpo_days" | "expense:<category code>"
    values: list[float | None]


# ---------------------------------------------------------------------------
# Onboarding + auth
# ---------------------------------------------------------------------------


@app.post("/api/onboarding")
def api_onboard(body: OnboardBody, response: Response, db: Session = Depends(get_db)):
    company, admin = onboarding.onboard_company(
        db,
        name=body.name,
        legal_name=body.legal_name,
        ein=body.ein,
        industry=body.industry,
        admin_name=body.admin_name,
        admin_email=body.admin_email,
        admin_password=body.admin_password,
    )
    if body.initial_funding_dollars > 0:
        onboarding.fund_account(
            db,
            company_id=company.id,
            amount_cents=dollars_to_cents(body.initial_funding_dollars),
            memo="Initial funding",
        )
    response.set_cookie(SESSION_COOKIE, make_session_token(admin.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return {"company": company_out(company), "user": user_out(admin)}


@app.get("/api/directory")
def api_directory(db: Session = Depends(get_db)):
    """Prototype-only convenience: lists onboarded companies and their admin's login email, so a
    demo can find who to log in as. A real bank would never expose this."""
    out = []
    for c in db.scalars(select(Company)).all():
        admin = db.scalars(select(User).where(User.company_id == c.id, User.role == Role.admin)).first()
        out.append({"company": company_out(c), "admin_email": admin.email if admin else None})
    return out


@app.post("/api/auth/login")
def api_login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == body.email.lower())).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password.")
    response.set_cookie(SESSION_COOKIE, make_session_token(user.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return {"user": user_out(user), "company": company_out(db.get(Company, user.company_id))}


@app.post("/api/auth/logout")
def api_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    return {"user": user_out(user), "company": company_out(db.get(Company, user.company_id))}


@app.get("/api/users")
def api_users(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    if user.role == Role.employee:
        return [user_out(user)]
    return [user_out(u) for u in db.scalars(select(User).where(User.company_id == user.company_id)).all()]


@app.post("/api/users")
def api_create_user(body: UserCreateBody, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    if body.role not in ("bookkeeper", "employee"):
        raise HTTPException(400, "role must be 'bookkeeper' or 'employee'.")
    new_user = User(
        company_id=admin.company_id,
        name=body.name,
        email=body.email.lower(),
        role=Role(body.role),
        password_hash=hash_password(body.password),
    )
    db.add(new_user)
    db.commit()
    return user_out(new_user)


# ---------------------------------------------------------------------------
# Dashboard / accounts
# ---------------------------------------------------------------------------


def _account_row(db: Session, user: User, acct: LedgerAccount) -> dict:
    from .ledger import account_balance

    bal = account_balance(db, acct)
    avail = bal + available_balance_delta(db, acct.id, user.company_id) if acct.is_bank_account else None
    return account_out(acct, bal, avail)


@app.get("/api/accounts")
def api_accounts(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    accounts = db.scalars(
        select(LedgerAccount).where(LedgerAccount.company_id == user.company_id, LedgerAccount.is_bank_account.is_(True))
    ).all()
    return [_account_row(db, user, a) for a in accounts]


@app.get("/api/dashboard")
def api_dashboard(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    cash = reports.cash_position(db, user.company_id)
    recent = db.scalars(
        select(Transaction).where(Transaction.company_id == user.company_id).order_by(Transaction.created_at.desc()).limit(10)
    ).all()
    if user.role == Role.employee:
        recent = [t for t in recent if t.card_id and db.get(Card, t.card_id).holder_id == user.id]
    open_bills = (
        db.scalar(select(Bill).where(Bill.company_id == user.company_id, Bill.status.in_(["pending_approval", "scheduled"])).limit(1))
        is not None
    )
    inc = reports.income_statement(db, user.company_id, start=sim_today(db).replace(day=1), end=sim_today(db))
    return {
        "sim_today": sim_today(db).isoformat(),
        "cash_total": money(cash["total_cents"]),
        "accounts": [{"code": a["code"], "name": a["name"], "balance": money(a["balance_cents"])} for a in cash["accounts"]],
        "recent_transactions": [transaction_out(t, db) for t in recent],
        "has_open_bills": open_bills,
        "month_to_date_revenue": money(inc["revenue_total_cents"]),
        "month_to_date_expenses": money(inc["expenses_total_cents"]),
    }


@app.post("/api/accounts/transfer")
def api_transfer(body: TransferBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    frm = company_account(db, user, body.from_code)
    to = company_account(db, user, body.to_code)
    txn = transfers.internal_transfer(
        db,
        company_id=user.company_id,
        from_account=frm,
        to_account=to,
        amount_cents=dollars_to_cents(body.amount_dollars),
        memo=body.memo,
        created_by_id=user.id,
    )
    run_settlement(db, user.company_id)
    return transaction_out(txn, db)


@app.post("/api/accounts/send-money")
def api_send_money(body: SendMoneyBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    frm = company_account(db, user, body.from_code)
    txn = transfers.send_money(
        db,
        company_id=user.company_id,
        from_account=frm,
        amount_cents=dollars_to_cents(body.amount_dollars),
        counterparty=body.counterparty,
        method=body.method,
        memo=body.memo,
        created_by_id=user.id,
    )
    return transaction_out(txn, db)


@app.post("/api/accounts/receive-money")
def api_receive_money(body: ReceiveMoneyBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    to = company_account(db, user, body.to_code)
    txn = transfers.receive_money(
        db,
        company_id=user.company_id,
        to_account=to,
        amount_cents=dollars_to_cents(body.amount_dollars),
        counterparty=body.counterparty,
        method=body.method,
        memo=body.memo,
        created_by_id=user.id,
    )
    return transaction_out(txn, db)


@app.get("/api/accounts/{code}/statement")
def api_statement(
    code: str, start: date | None = None, end: date | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)
):
    acct = company_account(db, user, code)
    lines = reports.account_statement(db, acct, start, end)
    return [
        {
            "date": r["date"].isoformat(),
            "memo": r["memo"],
            "source_type": r["source_type"],
            "amount": money(r["amount_cents"]),
            "balance_after": money(r["balance_after_cents"]),
        }
        for r in lines
    ]


# ---------------------------------------------------------------------------
# Chart of accounts / categories
# ---------------------------------------------------------------------------


@app.get("/api/categories")
def api_categories(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    accounts = db.scalars(
        select(LedgerAccount).where(
            LedgerAccount.company_id == user.company_id, LedgerAccount.type == "expense", LedgerAccount.is_active.is_(True)
        )
    ).all()
    return [{"code": a.code, "name": a.name} for a in accounts]


# ---------------------------------------------------------------------------
# Transactions feed
# ---------------------------------------------------------------------------


@app.get("/api/transactions")
def api_transactions(
    status: str | None = None, limit: int = 100, user: User = Depends(current_user_settled), db: Session = Depends(get_db)
):
    stmt = (
        select(Transaction).where(Transaction.company_id == user.company_id).order_by(Transaction.created_at.desc()).limit(min(limit, 500))
    )
    if status:
        stmt = stmt.where(Transaction.status == status)
    rows = db.scalars(stmt).all()
    if user.role == Role.employee:
        rows = [t for t in rows if t.card_id and db.get(Card, t.card_id).holder_id == user.id]
    return [transaction_out(t, db) for t in rows]


@app.post("/api/transactions/{txn_id}/reclassify")
def api_reclassify(txn_id: int, body: ReclassifyBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None or txn.company_id != user.company_id:
        raise HTTPException(404, "Transaction not found.")
    new_category = company_account(db, user, body.category_code)
    categorization.reclassify_transaction(db, txn, new_category)
    return transaction_out(txn, db)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


@app.get("/api/cards")
def api_cards(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    stmt = select(Card).where(Card.company_id == user.company_id)
    if user.role == Role.employee:
        stmt = stmt.where(Card.holder_id == user.id)
    return [card_out(c, db) for c in db.scalars(stmt).all()]


@app.post("/api/cards")
def api_issue_card(body: CardIssueBody, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .models import LimitPeriod

    holder = db.get(User, body.holder_user_id)
    if holder is None or holder.company_id != admin.company_id:
        raise HTTPException(404, "Holder not found in this company.")
    account = company_account(db, admin, body.account_code)
    if body.limit_period not in [p.value for p in LimitPeriod]:
        raise HTTPException(400, f"limit_period must be one of {[p.value for p in LimitPeriod]}")
    card = cards.issue_card(
        db,
        company_id=admin.company_id,
        account=account,
        holder=holder,
        label=body.label,
        limit_cents=dollars_to_cents(body.limit_dollars) if body.limit_dollars else None,
        limit_period=LimitPeriod(body.limit_period),
    )
    return card_out(card, db)


def _owned_card(db: Session, user: User, card_id: int) -> Card:
    card = db.get(Card, card_id)
    if card is None or card.company_id != user.company_id:
        raise HTTPException(404, "Card not found.")
    return card


@app.post("/api/cards/{card_id}/freeze")
def api_freeze_card(card_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    from .models import CardStatus

    card = _owned_card(db, user, card_id)
    if user.role == Role.employee and card.holder_id != user.id:
        raise HTTPException(403, "You can only freeze your own card.")
    cards.set_card_status(db, card, CardStatus.frozen)
    return card_out(card, db)


@app.post("/api/cards/{card_id}/unfreeze")
def api_unfreeze_card(card_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    from .models import CardStatus

    card = _owned_card(db, user, card_id)
    if user.role == Role.employee and card.holder_id != user.id:
        raise HTTPException(403, "You can only unfreeze your own card.")
    cards.set_card_status(db, card, CardStatus.active)
    return card_out(card, db)


@app.post("/api/cards/{card_id}/close")
def api_close_card(card_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from .models import CardStatus

    card = _owned_card(db, admin, card_id)
    cards.set_card_status(db, card, CardStatus.closed)
    return card_out(card, db)


@app.post("/api/cards/{card_id}/charge")
def api_charge_card(card_id: int, body: CardChargeBody, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    """Simulates swiping the card -- there is no real card network here."""
    card = _owned_card(db, user, card_id)
    if user.role == Role.employee and card.holder_id != user.id:
        raise HTTPException(403, "You can only charge your own card.")
    txn = cards.authorize_charge(
        db, card=card, amount_cents=dollars_to_cents(body.amount_dollars), merchant=body.merchant, created_by_id=user.id
    )
    return transaction_out(txn, db)


# ---------------------------------------------------------------------------
# Bill pay (AP)
# ---------------------------------------------------------------------------


@app.get("/api/vendors")
def api_vendors(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    return [vendor_out(v, db) for v in db.scalars(select(Vendor).where(Vendor.company_id == user.company_id)).all()]


@app.post("/api/vendors")
def api_add_vendor(body: VendorBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    default_category = company_account(db, user, body.default_category_code) if body.default_category_code else None
    vendor = billpay.add_vendor(
        db, company_id=user.company_id, name=body.name, default_category=default_category, payment_method=body.payment_method
    )
    return vendor_out(vendor, db)


def _owned_bill(db: Session, user: User, bill_id: int) -> Bill:
    bill = db.get(Bill, bill_id)
    if bill is None or bill.company_id != user.company_id:
        raise HTTPException(404, "Bill not found.")
    return bill


@app.get("/api/bills")
def api_bills(status: str | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    stmt = select(Bill).where(Bill.company_id == user.company_id).order_by(Bill.created_at.desc())
    if status:
        stmt = stmt.where(Bill.status == status)
    return [bill_out(b, db) for b in db.scalars(stmt).all()]


@app.post("/api/bills")
def api_create_bill(body: BillBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    vendor = db.get(Vendor, body.vendor_id)
    if vendor is None or vendor.company_id != user.company_id:
        raise HTTPException(404, "Vendor not found.")
    category = company_account(db, user, body.category_code)
    bill = billpay.create_bill(
        db,
        company_id=user.company_id,
        vendor=vendor,
        category=category,
        amount_cents=dollars_to_cents(body.amount_dollars),
        memo=body.memo,
        due_date=body.due_date,
        created_by=user,
    )
    return bill_out(bill, db)


@app.post("/api/bills/{bill_id}/approve")
def api_approve_bill(bill_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    bill = _owned_bill(db, admin, bill_id)
    billpay.approve_bill(db, bill, admin)
    return bill_out(bill, db)


@app.post("/api/bills/{bill_id}/pay")
def api_pay_bill(bill_id: int, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    bill = _owned_bill(db, user, bill_id)
    billpay.schedule_bill_payment(db, bill, user)
    return bill_out(bill, db)


@app.post("/api/bills/{bill_id}/void")
def api_void_bill(bill_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    bill = _owned_bill(db, admin, bill_id)
    billpay.void_bill(db, bill)
    return bill_out(bill, db)


# ---------------------------------------------------------------------------
# Invoicing (AR)
# ---------------------------------------------------------------------------


def _owned_invoice(db: Session, user: User, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None or inv.company_id != user.company_id:
        raise HTTPException(404, "Invoice not found.")
    return inv


@app.get("/api/invoices")
def api_invoices(status: str | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    stmt = select(Invoice).where(Invoice.company_id == user.company_id).order_by(Invoice.created_at.desc())
    if status:
        stmt = stmt.where(Invoice.status == status)
    return [invoice_out(i) for i in db.scalars(stmt).all()]


@app.post("/api/invoices")
def api_send_invoice(body: InvoiceBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    inv = invoicing.send_invoice(
        db,
        company_id=user.company_id,
        customer_name=body.customer_name,
        amount_cents=dollars_to_cents(body.amount_dollars),
        memo=body.memo,
        due_date=body.due_date,
        created_by=user,
    )
    return invoice_out(inv)


@app.post("/api/invoices/{invoice_id}/pay")
def api_pay_invoice(
    invoice_id: int, body: InvoicePayBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)
):
    """Records that the customer's payment arrived (there is no real payment page in this prototype)."""
    inv = _owned_invoice(db, user, invoice_id)
    invoicing.record_invoice_payment(db, inv, method=body.method, created_by=user)
    return invoice_out(inv)


@app.post("/api/invoices/{invoice_id}/void")
def api_void_invoice(invoice_id: int, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    inv = _owned_invoice(db, user, invoice_id)
    invoicing.void_invoice(db, inv)
    return invoice_out(inv)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _money_rows(rows: list[dict], amount_key: str) -> list[dict]:
    return [{**{k: v for k, v in r.items() if k != amount_key}, "amount": money(r[amount_key])} for r in rows]


@app.get("/api/reports/balance-sheet")
def api_balance_sheet(as_of: date | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    bs = reports.balance_sheet(db, user.company_id, as_of)
    return {
        "as_of": (as_of or sim_today(db)).isoformat(),
        "assets": _money_rows(bs["assets"], "balance_cents"),
        "assets_total": money(bs["assets_total_cents"]),
        "liabilities": _money_rows(bs["liabilities"], "balance_cents"),
        "liabilities_total": money(bs["liabilities_total_cents"]),
        "equity": _money_rows(bs["equity"], "balance_cents"),
        "equity_total": money(bs["equity_total_cents"]),
        "balances": bs["balances"],
    }


@app.get("/api/reports/income-statement")
def api_income_statement(
    start: date | None = None, end: date | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)
):
    inc = reports.income_statement(db, user.company_id, start, end)
    return {
        "start": start.isoformat() if start else None,
        "end": (end or sim_today(db)).isoformat(),
        "revenue": _money_rows(inc["revenue"], "amount_cents"),
        "revenue_total": money(inc["revenue_total_cents"]),
        "expenses": _money_rows(inc["expenses"], "amount_cents"),
        "expenses_total": money(inc["expenses_total_cents"]),
        "net_income": money(inc["net_income_cents"]),
    }


@app.get("/api/reports/cash-flow")
def api_cash_flow(
    start: date | None = None, end: date | None = None, user: User = Depends(current_user_settled), db: Session = Depends(get_db)
):
    today = sim_today(db)
    start = start or date(today.year, today.month, 1)
    end = end or today
    cf = reports.cash_flow_statement(db, user.company_id, start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "operating": _money_rows(cf["operating"], "amount_cents"),
        "operating_total": money(cf["operating_total_cents"]),
        "investing": _money_rows(cf["investing"], "amount_cents"),
        "investing_total": money(cf["investing_total_cents"]),
        "financing": _money_rows(cf["financing"], "amount_cents"),
        "financing_total": money(cf["financing_total_cents"]),
        "net_change": money(cf["net_change_cents"]),
        "cash_start": money(cf["cash_start_cents"]),
        "cash_end": money(cf["cash_end_cents"]),
        "ties_to_cash": cf["ties_to_cash"],
    }


@app.get("/api/reports/burn-and-runway")
def api_burn_and_runway(trailing: int = 3, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    r = reports.burn_and_runway(db, user.company_id, as_of=sim_today(db), trailing=trailing)
    return {
        "as_of": r["as_of"].isoformat(),
        "months_used": r["months_used"],
        "monthly_operating_cash_flow": [money(c) for c in r["monthly_operating_cash_flow_cents"]],
        "avg_monthly_operating_cash_flow": money(r["avg_monthly_operating_cash_flow_cents"]),
        "burn": money(r["burn_cents"]),
        "cash_on_hand": money(r["cash_on_hand_cents"]),
        "runway_months": r["runway_months"],
    }


# ---------------------------------------------------------------------------
# Budgeting & variance analysis
# ---------------------------------------------------------------------------


def _budget_out(b: Budget) -> dict:
    return {"id": b.id, "name": b.name, "created_at": b.created_at.isoformat()}


def _owned_budget(db: Session, user: User, budget_id: int) -> Budget:
    b = db.get(Budget, budget_id)
    if b is None or b.company_id != user.company_id:
        raise HTTPException(404, "Budget not found.")
    return b


@app.get("/api/budgets")
def api_budgets(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    return [_budget_out(b) for b in db.scalars(select(Budget).where(Budget.company_id == user.company_id)).all()]


@app.post("/api/budgets")
def api_create_budget(body: BudgetCreateBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    b = budgeting.create_budget(db, company_id=user.company_id, name=body.name, created_by=user)
    return _budget_out(b)


@app.get("/api/budgets/{budget_id}/lines")
def api_budget_lines(budget_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    budget = _owned_budget(db, user, budget_id)
    out = []
    for line in budget.lines:
        cat = db.get(LedgerAccount, line.ledger_account_id)
        out.append(
            {
                "id": line.id,
                "category_code": cat.code,
                "category_name": cat.name,
                "period": line.period.isoformat(),
                "amount": money(line.amount_cents),
            }
        )
    return out


@app.post("/api/budgets/{budget_id}/lines")
def api_set_budget_line(
    budget_id: int, body: BudgetLineBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)
):
    budget = _owned_budget(db, user, budget_id)
    category = company_account(db, user, body.category_code)
    line = budgeting.set_budget_line(
        db, budget=budget, category=category, period=body.period, amount_cents=dollars_to_cents(body.amount_dollars)
    )
    return {"id": line.id, "category_code": category.code, "period": line.period.isoformat(), "amount": money(line.amount_cents)}


@app.post("/api/budgets/{budget_id}/populate")
def api_populate_budget(
    budget_id: int, body: BudgetPopulateBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)
):
    budget = _owned_budget(db, user, budget_id)
    lines = budgeting.populate_from_history(db, budget=budget, months=body.months, lookback_months=body.lookback_months)
    return {"created": len(lines)}


@app.get("/api/budgets/{budget_id}/variance")
def api_budget_variance(budget_id: int, start: date, end: date, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    _owned_budget(db, user, budget_id)
    report = budgeting.budget_vs_actual(db, user.company_id, budget_id, start, end)
    rows = [
        {
            "code": r["code"],
            "name": r["name"],
            "type": r["type"],
            "budget": money(r["budget_cents"]),
            "actual": money(r["actual_cents"]),
            "variance": money(r["variance_cents"]),
            "variance_pct": r["variance_pct"],
            "favorable": r["favorable"],
        }
        for r in report["rows"]
    ]
    return {
        "budget_id": budget_id,
        "budget_name": report["budget_name"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": rows,
        "budget_total": money(report["budget_total_cents"]),
        "actual_total": money(report["actual_total_cents"]),
    }


# ---------------------------------------------------------------------------
# Forecasting & 3-statement modeling
# ---------------------------------------------------------------------------


def _scenario_out(s: ForecastScenario) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "months": s.months,
        "overrides": forecasting.get_overrides(s),
        "updated_at": s.updated_at.isoformat(),
    }


def _owned_scenario(db: Session, user: User, scenario_id: int) -> ForecastScenario:
    s = db.get(ForecastScenario, scenario_id)
    if s is None or s.company_id != user.company_id:
        raise HTTPException(404, "Scenario not found.")
    return s


def _forecast_out(fc: dict) -> dict:
    return {
        "as_of": fc["as_of"].isoformat(),
        "months": [m.isoformat() for m in fc["months"]],
        "history_months": [m.isoformat() for m in fc["history_months"]],
        "warnings": fc["warnings"],
        "balances": fc["balances"],
        "assumptions": {
            "revenue_growth": fc["assumptions"]["revenue_growth"],
            "dso_days": fc["assumptions"]["dso_days"],
            "dpo_days": fc["assumptions"]["dpo_days"],
            "expenses": fc["assumptions"]["expenses"],
        },
        "income_statement": [
            {
                "month": row["month"].isoformat(),
                "revenue": money(row["revenue_cents"]),
                "expenses_by_category": {c: money(v) for c, v in row["expenses_by_category_cents"].items()},
                "expenses_total": money(row["expenses_total_cents"]),
                "net_income": money(row["net_income_cents"]),
            }
            for row in fc["income_statement"]
        ],
        "balance_sheet": [
            {
                "month": row["month"].isoformat(),
                "cash": money(row["cash_cents"]),
                "ar": money(row["ar_cents"]),
                "ap": money(row["ap_cents"]),
                "equity": money(row["equity_cents"]),
                "assets_total": money(row["assets_total_cents"]),
                "liabilities_and_equity_total": money(row["liabilities_and_equity_total_cents"]),
                "balances": row["balances"],
            }
            for row in fc["balance_sheet"]
        ],
        "cash_flow": [
            {
                "month": row["month"].isoformat(),
                "net_income": money(row["net_income_cents"]),
                "delta_ar": money(row["delta_ar_cents"]),
                "delta_ap": money(row["delta_ap_cents"]),
                "operating_cash_flow": money(row["operating_cash_flow_cents"]),
                "ending_cash": money(row["ending_cash_cents"]),
            }
            for row in fc["cash_flow"]
        ],
    }


@app.get("/api/forecast")
def api_forecast(months: int = 12, lookback_months: int = 6, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    """A one-off forecast with default (historical-average) assumptions, no saved scenario."""
    fc = forecasting.build_forecast(db, user.company_id, months=months, lookback_months=lookback_months)
    return _forecast_out(fc)


@app.get("/api/forecast/scenarios")
def api_scenarios(user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    return [_scenario_out(s) for s in db.scalars(select(ForecastScenario).where(ForecastScenario.company_id == user.company_id)).all()]


@app.post("/api/forecast/scenarios")
def api_create_scenario(body: ScenarioCreateBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    s = forecasting.create_scenario(db, company_id=user.company_id, name=body.name, months=body.months, created_by=user)
    return _scenario_out(s)


@app.get("/api/forecast/scenarios/{scenario_id}")
def api_get_scenario_forecast(scenario_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    scenario = _owned_scenario(db, user, scenario_id)
    fc = forecasting.build_forecast_for_scenario(db, scenario)
    return {"scenario": _scenario_out(scenario), "forecast": _forecast_out(fc)}


@app.post("/api/forecast/scenarios/{scenario_id}/overrides")
def api_set_scenario_override(
    scenario_id: int, body: ScenarioOverrideBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)
):
    scenario = _owned_scenario(db, user, scenario_id)
    forecasting.set_override(db, scenario, body.driver_key, body.values)
    return _scenario_out(scenario)


@app.delete("/api/forecast/scenarios/{scenario_id}/overrides/{driver_key}")
def api_clear_scenario_override(
    scenario_id: int, driver_key: str, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)
):
    scenario = _owned_scenario(db, user, scenario_id)
    forecasting.clear_override(db, scenario, driver_key)
    return _scenario_out(scenario)


@app.get("/api/forecast/scenarios/{scenario_id}/workbook")
def api_download_scenario_workbook(scenario_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    scenario = _owned_scenario(db, user, scenario_id)
    fc = forecasting.build_forecast_for_scenario(db, scenario)
    company = db.get(Company, user.company_id)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in company.name)
    path = OUTPUT_DIR / f"{safe_name}_forecast_{scenario.id}.xlsx"
    out = build_forecast_workbook(db, user.company_id, fc, str(path))
    return FileResponse(out["path"], filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/forecast/scenarios/{scenario_id}/validate")
def api_validate_scenario_workbook(scenario_id: int, user: User = Depends(current_user_settled), db: Session = Depends(get_db)):
    """Builds the workbook, recalculates it, and reports whether it actually balances -- proof the
    export is correct, not just that it was written."""
    scenario = _owned_scenario(db, user, scenario_id)
    fc = forecasting.build_forecast_for_scenario(db, scenario)
    company = db.get(Company, user.company_id)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in company.name)
    path = OUTPUT_DIR / f"{safe_name}_forecast_{scenario.id}.xlsx"
    out = build_forecast_workbook(db, user.company_id, fc, str(path))
    report = validate_forecast_workbook(out["path"], out["history_cols"], out["forecast_cols"])
    return report


# ---------------------------------------------------------------------------
# Simulated clock
# ---------------------------------------------------------------------------


@app.get("/api/sim/today")
def api_sim_today(db: Session = Depends(get_db)):
    return {"today": sim_today(db).isoformat()}


@app.post("/api/sim/advance")
def api_sim_advance(body: AdvanceBody, user: User = Depends(require_admin_or_bookkeeper), db: Session = Depends(get_db)):
    new_date = advance_days(db, body.days)
    settled = run_settlement(db, user.company_id)
    return {"today": new_date.isoformat(), "settled_count": len(settled)}


# ---------------------------------------------------------------------------
# Static dashboard
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
