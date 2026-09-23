"""Regression test for a real bug: enum-typed columns must survive a FRESH query (not just stay
correct because the original Python object with the enum in memory is still identity-mapped).
Forcing db.expire_all() simulates what happens once the original object is garbage collected."""

from datetime import date

from sqlalchemy import select

from app.ledger import credit, debit, get_account, post_journal_entry, seed_chart_of_accounts
from app.models import Company, Direction, JournalLine, LedgerAccount, TxnStatus, TxnType, User
from app.onboarding import onboard_company
from app.transactions import create_transaction


def test_ledger_account_enums_survive_fresh_query(db):
    company = Company(name="Acme", legal_name="Acme", ein="0")
    db.add(company)
    db.flush()
    seed_chart_of_accounts(db, company.id)
    db.commit()
    db.expire_all()

    checking = db.scalar(select(LedgerAccount).where(LedgerAccount.company_id == company.id, LedgerAccount.code == "1000"))
    assert checking.normal_balance == Direction.debit
    assert isinstance(checking.normal_balance, Direction)


def test_journal_line_direction_survives_fresh_query(db):
    company = Company(name="Acme", legal_name="Acme", ein="0")
    db.add(company)
    db.flush()
    seed_chart_of_accounts(db, company.id)
    checking = get_account(db, company.id, "1000")
    capital = get_account(db, company.id, "3000")
    post_journal_entry(
        db, company_id=company.id, entry_date=date.today(), memo="x", source_type="t", legs=[debit(checking, 100), credit(capital, 100)]
    )
    db.commit()
    db.expire_all()

    line = db.scalars(select(JournalLine)).first()
    assert isinstance(line.direction, Direction)
    assert line.direction in (Direction.debit, Direction.credit)


def test_transaction_type_status_direction_survive_fresh_query(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    checking = get_account(db, company.id, "1000")
    other_income = get_account(db, company.id, "4900")
    create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.debit,
        amount_cents=500,
        counterparty_account=other_income,
        txn_type=TxnType.wire_in,
        description="x",
        settle_date=date.today(),
        created_date=date.today(),
    )
    db.commit()
    db.expire_all()

    from app.models import Transaction

    txn = db.scalars(select(Transaction)).first()
    assert isinstance(txn.type, TxnType) and txn.type == TxnType.wire_in
    assert isinstance(txn.status, TxnStatus) and txn.status == TxnStatus.processing
    assert isinstance(txn.direction, Direction) and txn.direction == Direction.debit


def test_user_role_survives_fresh_query(db):
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    db.expire_all()
    u = db.scalars(select(User).where(User.id == admin.id)).first()
    from app.models import Role

    assert isinstance(u.role, Role) and u.role == Role.admin
