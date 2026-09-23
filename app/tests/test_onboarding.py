import pytest

from app.auth import verify_password
from app.ledger import account_balance, get_account
from app.models import Role
from app.onboarding import fund_account, onboard_company


@pytest.fixture()
def onboarded(db):
    company, admin = onboard_company(
        db,
        name="Acme",
        legal_name="Acme Inc",
        ein="00-0000000",
        industry="Software",
        admin_name="Ada Admin",
        admin_email="ada@acme.com",
        admin_password="s3cret!",
    )
    return company, admin


def test_onboarding_creates_company_chart_and_admin(db, onboarded):
    company, admin = onboarded
    assert company.id is not None
    assert {a.code for a in company.ledger_accounts} >= {"1000", "1010", "1200", "2000", "3000"}
    assert admin.role == Role.admin
    assert verify_password("s3cret!", admin.password_hash)
    assert admin.password_hash != "s3cret!"


def test_onboarding_requires_name_and_email(db):
    with pytest.raises(ValueError, match="name"):
        onboard_company(db, name="", legal_name="", ein="", industry="", admin_name="A", admin_email="a@b.com", admin_password="x")
    with pytest.raises(ValueError, match="email"):
        onboard_company(db, name="Acme", legal_name="", ein="", industry="", admin_name="A", admin_email="not-an-email", admin_password="x")


def test_fund_account_posts_balanced_entry(db, onboarded):
    company, _ = onboarded
    fund_account(db, company_id=company.id, amount_cents=1_000_000, memo="Seed round transfer")
    checking = get_account(db, company.id, "1000")
    capital = get_account(db, company.id, "3000")
    assert account_balance(db, checking) == 1_000_000
    assert account_balance(db, capital) == 1_000_000


def test_fund_account_rejects_non_positive(db, onboarded):
    company, _ = onboarded
    with pytest.raises(ValueError):
        fund_account(db, company_id=company.id, amount_cents=0, memo="")


def test_two_companies_are_isolated(db):
    c1, _ = onboard_company(db, name="A", legal_name="A", ein="1", industry="x", admin_name="a", admin_email="a@a.com", admin_password="x")
    c2, _ = onboard_company(db, name="B", legal_name="B", ein="2", industry="x", admin_name="b", admin_email="b@b.com", admin_password="x")
    fund_account(db, company_id=c1.id, amount_cents=500_00, memo="")
    assert account_balance(db, get_account(db, c1.id, "1000")) == 500_00
    assert account_balance(db, get_account(db, c2.id, "1000")) == 0
