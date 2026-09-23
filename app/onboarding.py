"""Company onboarding: create the company, seed its chart of accounts, create the admin user,
and (optionally) fund the checking account with initial capital. KYB is simulated as instantly
verified — there is no real identity-verification step here."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .auth import hash_password
from .ledger import get_account, seed_chart_of_accounts
from .models import Company, Direction, Role, TxnType, User
from .settlement import run_settlement
from .simclock import sim_today
from .transactions import create_transaction


def onboard_company(
    db: Session,
    *,
    name: str,
    legal_name: str,
    ein: str,
    industry: str,
    admin_name: str,
    admin_email: str,
    admin_password: str,
) -> tuple[Company, User]:
    if not name.strip():
        raise ValueError("Company name is required.")
    if not admin_email.strip() or "@" not in admin_email:
        raise ValueError("A valid admin email is required.")

    company = Company(name=name.strip(), legal_name=legal_name.strip() or name.strip(), ein=ein.strip(), industry=industry.strip())
    db.add(company)
    db.flush()
    seed_chart_of_accounts(db, company.id)

    admin = User(
        company_id=company.id,
        name=admin_name.strip(),
        email=admin_email.strip().lower(),
        role=Role.admin,
        password_hash=hash_password(admin_password),
    )
    db.add(admin)
    db.flush()
    db.commit()
    return company, admin


def fund_account(db: Session, *, company_id: int, amount_cents: int, memo: str = "Initial funding"):
    """Simulated initial capital deposit (e.g. wired in from the founders' existing bank). Goes
    through the normal Transaction/settlement pipeline like everything else -- so it shows up in
    the activity feed and account statements -- but settles the same (simulated) day, since this
    models opening the account rather than day-to-day ACH float."""
    if amount_cents <= 0:
        raise ValueError("Funding amount must be positive.")
    checking = get_account(db, company_id, "1000")
    capital = get_account(db, company_id, "3000")
    today = sim_today(db)
    txn = create_transaction(
        db,
        company_id=company_id,
        account=checking,
        direction=Direction.debit,
        amount_cents=amount_cents,
        counterparty_account=capital,
        txn_type=TxnType.funding,
        description=memo or "Initial funding",
        settle_date=today,
        created_date=today,
    )
    db.commit()
    run_settlement(db, company_id)
    return txn
