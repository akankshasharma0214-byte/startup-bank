from datetime import date

import pytest

from app.billpay import add_vendor, create_bill, schedule_bill_payment
from app.cards import authorize_charge, issue_card
from app.categorization import reclassify_transaction
from app.invoicing import record_invoice_payment, send_invoice
from app.ledger import get_account
from app.onboarding import fund_account, onboard_company
from app.reports import account_statement, balance_sheet, cash_position, income_statement
from app.settlement import run_settlement
from app.simclock import advance_days, set_sim_date


@pytest.fixture()
def company_scenario(db):
    """A realistic mixed month: funding on day 1, a paid invoice, a paid bill, and card spend two
    weeks later. The sim clock is set explicitly so the timeline is deterministic (independent of
    the real wall-clock date), which is what lets test_as_of_date_excludes_later_activity below
    reliably distinguish 'before' from 'after'."""
    set_sim_date(db, date(2026, 1, 1))
    company, admin = onboard_company(
        db, name="Acme", legal_name="Acme Inc", ein="0", industry="x", admin_name="A", admin_email="a@a.com", admin_password="x"
    )
    fund_account(db, company_id=company.id, amount_cents=5_000_000, memo="seed round")

    advance_days(db, 14)  # 2026-01-15: later activity is dated after the funding

    inv = send_invoice(
        db,
        company_id=company.id,
        customer_name="Big Co",
        amount_cents=200_000,
        memo="services",
        due_date=date(2026, 2, 1),
        created_by=admin,
    )
    record_invoice_payment(db, inv, method="wire", created_by=admin)

    vendor = add_vendor(db, company_id=company.id, name="AWS", payment_method="wire")
    bill = create_bill(
        db,
        company_id=company.id,
        vendor=vendor,
        category=get_account(db, company.id, "5010"),
        amount_cents=30_000,
        memo="hosting",
        due_date=date(2026, 2, 5),
        created_by=admin,
    )
    schedule_bill_payment(db, bill, admin)

    checking = get_account(db, company.id, "1000")
    card = issue_card(db, company_id=company.id, account=checking, holder=admin, label="Card")
    txn = authorize_charge(db, card=card, amount_cents=5_000, merchant="Local Unlisted Vendor")

    run_settlement(db, company.id)
    return company, admin, txn


def test_balance_sheet_identity_holds(db, company_scenario):
    company, admin, txn = company_scenario
    bs = balance_sheet(db, company.id)
    assert bs["balances"] is True
    assert bs["assets_total_cents"] == bs["liabilities_total_cents"] + bs["equity_total_cents"]


def test_balance_sheet_cash_reflects_all_settled_flows(db, company_scenario):
    company, admin, txn = company_scenario
    bs = balance_sheet(db, company.id)
    checking_row = next(r for r in bs["assets"] if r["code"] == "1000")
    # +5,000,000 funding +200,000 invoice -30,000 bill -5,000 card(authorized, not yet settled)
    assert checking_row["balance_cents"] == 5_000_000 + 200_000 - 30_000


def test_income_statement_reflects_revenue_and_expense(db, company_scenario):
    company, admin, txn = company_scenario
    inc = income_statement(db, company.id)
    assert inc["revenue_total_cents"] == 200_000
    assert inc["expenses_total_cents"] == 30_000  # AWS hosting bill; card txn still authorized, not settled
    assert inc["net_income_cents"] == 170_000


def test_income_statement_period_filter_excludes_out_of_range(db, company_scenario):
    company, admin, txn = company_scenario
    inc = income_statement(db, company.id, start=date(2027, 1, 1), end=date(2027, 12, 31))
    assert inc["revenue_total_cents"] == 0 and inc["expenses_total_cents"] == 0


def test_card_settlement_then_reclassification_shows_up_in_income_statement(db, company_scenario):
    company, admin, txn = company_scenario
    advance_days(db, 1)
    run_settlement(db, company.id)
    db.refresh(txn)
    inc = income_statement(db, company.id)
    assert inc["expenses_total_cents"] == 30_000 + 5_000

    reclassify_transaction(db, txn, get_account(db, company.id, "5020"))  # move Uncategorized to Travel
    inc2 = income_statement(db, company.id)
    travel = next((r for r in inc2["expenses"] if r["code"] == "5020"), None)
    assert travel is not None and travel["amount_cents"] == 5_000


def test_balance_sheet_identity_holds_after_owner_draw(db, company_scenario):
    """Regression: Owner Draws is a contra-equity account. If it were given its own natural
    debit-normal balance instead of the parent equity type's credit-normal convention, a draw would
    wrongly ADD to total equity instead of reducing it, and assets would stop equalling liabilities
    + equity."""
    company, admin, txn = company_scenario
    from app.transfers import send_money

    checking = get_account(db, company.id, "1000")
    send_money(db, company_id=company.id, from_account=checking, amount_cents=50_000, counterparty="Founder", method="wire")
    run_settlement(db, company.id)
    bs = balance_sheet(db, company.id)
    assert bs["balances"] is True
    draws_row = next(r for r in bs["equity"] if r["code"] == "3100")
    assert draws_row["balance_cents"] == -50_000


def test_account_statement_running_balance_matches_final_balance(db, company_scenario):
    company, admin, txn = company_scenario
    checking = get_account(db, company.id, "1000")
    lines = account_statement(db, checking)
    from app.ledger import account_balance

    assert lines[-1]["balance_after_cents"] == account_balance(db, checking)
    assert len(lines) >= 3  # funding, invoice payment, bill payment


def test_cash_position_sums_checking_and_savings(db, company_scenario):
    company, admin, txn = company_scenario
    from app.transfers import internal_transfer

    checking = get_account(db, company.id, "1000")
    savings = get_account(db, company.id, "1010")
    internal_transfer(db, company_id=company.id, from_account=checking, to_account=savings, amount_cents=1_000_000)
    run_settlement(db, company.id)
    pos = cash_position(db, company.id)
    checking_bal = next(a["balance_cents"] for a in pos["accounts"] if a["code"] == "1000")
    savings_bal = next(a["balance_cents"] for a in pos["accounts"] if a["code"] == "1010")
    assert savings_bal == 1_000_000
    assert pos["total_cents"] == checking_bal + savings_bal


def test_as_of_date_excludes_later_activity(db, company_scenario):
    company, admin, txn = company_scenario
    bs_early = balance_sheet(db, company.id, as_of=date(2026, 1, 1))  # the day funding posted, before the Jan-15 activity
    checking_row = next(r for r in bs_early["assets"] if r["code"] == "1000")
    assert checking_row["balance_cents"] == 5_000_000  # only the funding, before invoice/bill payments
    assert bs_early["balances"] is True
