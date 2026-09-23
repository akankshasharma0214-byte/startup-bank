import pytest

from app.categorization import reclassify_transaction, suggest_category
from app.ledger import LedgerError, account_balance, get_account
from app.models import Direction, TxnStatus, TxnType
from app.onboarding import fund_account, onboard_company
from app.settlement import run_settlement
from app.simclock import sim_today
from app.transactions import create_transaction


@pytest.fixture()
def company(db):
    c, _ = onboard_company(
        db, name="Acme", legal_name="Acme", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=c.id, amount_cents=1_000_000, memo="seed")
    return c


@pytest.mark.parametrize(
    "merchant,expected_code",
    [
        ("AWS", "5010"),
        ("GitHub Inc", "5010"),
        ("Uber Technologies", "5020"),
        ("Delta Air Lines", "5020"),
        ("Starbucks #4521", "5030"),
        ("Staples Office Supply", "5040"),
        ("Random Merchant XYZ", "5900"),
    ],
)
def test_suggest_category(db, company, merchant, expected_code):
    assert suggest_category(db, company.id, merchant).code == expected_code


def _settled_card_txn(db, company, merchant="Random Merchant XYZ", amount=5_000):
    checking = get_account(db, company.id, "1000")
    category = suggest_category(db, company.id, merchant)
    today = sim_today(db)
    txn = create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.credit,
        amount_cents=amount,
        counterparty_account=category,
        txn_type=TxnType.card,
        description=f"Card: {merchant}",
        counterparty=merchant,
        settle_date=today,
        created_date=today,
        status=TxnStatus.authorized,
        category_id=category.id,
    )
    db.commit()
    run_settlement(db, company.id)
    db.refresh(txn)
    return txn


def test_reclassify_moves_balance_between_categories(db, company):
    txn = _settled_card_txn(db, company)
    uncategorized = get_account(db, company.id, "5900")
    software = get_account(db, company.id, "5010")
    assert account_balance(db, uncategorized) == 5_000
    assert account_balance(db, software) == 0

    reclassify_transaction(db, txn, software)
    assert account_balance(db, uncategorized) == 0
    assert account_balance(db, software) == 5_000
    assert txn.category_id == software.id


def test_reclassify_checking_balance_unaffected(db, company):
    txn = _settled_card_txn(db, company)
    checking = get_account(db, company.id, "1000")
    before = account_balance(db, checking)
    reclassify_transaction(db, txn, get_account(db, company.id, "5010"))
    assert account_balance(db, checking) == before


def test_reclassify_unsettled_transaction_rejected(db, company):
    checking = get_account(db, company.id, "1000")
    category = get_account(db, company.id, "5900")
    today = sim_today(db)
    txn = create_transaction(
        db,
        company_id=company.id,
        account=checking,
        direction=Direction.credit,
        amount_cents=100,
        counterparty_account=category,
        txn_type=TxnType.card,
        description="x",
        settle_date=today,
        created_date=today,
        status=TxnStatus.authorized,
        category_id=category.id,
    )
    db.commit()
    with pytest.raises(LedgerError, match="settled"):
        reclassify_transaction(db, txn, get_account(db, company.id, "5010"))


def test_reclassify_to_same_category_rejected(db, company):
    txn = _settled_card_txn(db, company)
    same = get_account(db, company.id, "5900")  # _settled_card_txn's merchant defaults to Uncategorized
    with pytest.raises(LedgerError, match="already"):
        reclassify_transaction(db, txn, same)
