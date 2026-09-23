from datetime import date

import pytest

from app.ledger import LedgerError, account_balance, credit, debit, get_account, post_journal_entry, seed_chart_of_accounts
from app.models import Company


@pytest.fixture()
def company(db):
    c = Company(name="Acme Inc", legal_name="Acme Incorporated", ein="00-0000000")
    db.add(c)
    db.flush()
    seed_chart_of_accounts(db, c.id)
    db.commit()
    return c


def test_seed_chart_of_accounts_creates_expected_codes(db, company):
    accts = {a.code for a in company.ledger_accounts}
    assert {"1000", "1010", "1200", "2000", "3000", "3100", "4000", "5900"} <= accts
    checking = get_account(db, company.id, "1000")
    assert checking.is_bank_account and checking.bank_subtype == "checking"


def test_balanced_entry_posts_and_balances_update(db, company):
    checking = get_account(db, company.id, "1000")
    capital = get_account(db, company.id, "3000")
    post_journal_entry(
        db,
        company_id=company.id,
        entry_date=date(2026, 1, 1),
        memo="Funding",
        source_type="funding",
        legs=[debit(checking, 500_000), credit(capital, 500_000)],
    )
    db.commit()
    assert account_balance(db, checking) == 500_000
    assert account_balance(db, capital) == 500_000  # credit-normal: positive = has capital


def test_unbalanced_entry_rejected_and_nothing_persists(db, company):
    checking = get_account(db, company.id, "1000")
    capital = get_account(db, company.id, "3000")
    with pytest.raises(LedgerError, match="does not balance"):
        post_journal_entry(
            db,
            company_id=company.id,
            entry_date=date(2026, 1, 1),
            memo="bad",
            source_type="test",
            legs=[debit(checking, 500_000), credit(capital, 400_000)],
        )
    db.rollback()
    assert account_balance(db, checking) == 0


def test_single_leg_entry_rejected(db, company):
    checking = get_account(db, company.id, "1000")
    with pytest.raises(LedgerError, match="at least two legs"):
        post_journal_entry(db, company_id=company.id, entry_date=date.today(), memo="x", source_type="t", legs=[debit(checking, 100)])


def test_zero_or_negative_amount_rejected(db, company):
    checking = get_account(db, company.id, "1000")
    with pytest.raises(LedgerError):
        debit(checking, 0)
    with pytest.raises(LedgerError):
        credit(checking, -5)


def test_cross_company_account_rejected(db, company):
    other = Company(name="Other Co", legal_name="Other Co LLC", ein="11-1111111")
    db.add(other)
    db.flush()
    seed_chart_of_accounts(db, other.id)
    db.commit()

    checking_a = get_account(db, company.id, "1000")
    capital_b = get_account(db, other.id, "3000")
    with pytest.raises(LedgerError, match="does not belong"):
        post_journal_entry(
            db,
            company_id=company.id,
            entry_date=date.today(),
            memo="x",
            source_type="t",
            legs=[debit(checking_a, 100), credit(capital_b, 100)],
        )


def test_three_way_split_entry_balances(db, company):
    checking = get_account(db, company.id, "1000")
    payroll = get_account(db, company.id, "5000")
    software = get_account(db, company.id, "5010")
    capital = get_account(db, company.id, "3000")
    post_journal_entry(
        db,
        company_id=company.id,
        entry_date=date.today(),
        memo="fund",
        source_type="funding",
        legs=[debit(checking, 100_000), credit(capital, 100_000)],
    )
    post_journal_entry(
        db,
        company_id=company.id,
        entry_date=date.today(),
        memo="split spend",
        source_type="test",
        legs=[debit(payroll, 6_000), debit(software, 4_000), credit(checking, 10_000)],
    )
    db.commit()
    assert account_balance(db, checking) == 90_000
    assert account_balance(db, payroll) == 6_000
    assert account_balance(db, software) == 4_000


def test_as_of_date_excludes_future_entries(db, company):
    checking = get_account(db, company.id, "1000")
    capital = get_account(db, company.id, "3000")
    post_journal_entry(
        db,
        company_id=company.id,
        entry_date=date(2026, 1, 1),
        memo="fund",
        source_type="funding",
        legs=[debit(checking, 100_000), credit(capital, 100_000)],
    )
    post_journal_entry(
        db,
        company_id=company.id,
        entry_date=date(2026, 6, 1),
        memo="later fund",
        source_type="funding",
        legs=[debit(checking, 50_000), credit(capital, 50_000)],
    )
    db.commit()
    assert account_balance(db, checking, as_of=date(2026, 3, 1)) == 100_000
    assert account_balance(db, checking, as_of=date(2026, 6, 1)) == 150_000


def test_get_account_unknown_code_raises(db, company):
    with pytest.raises(LedgerError):
        get_account(db, company.id, "9999")
