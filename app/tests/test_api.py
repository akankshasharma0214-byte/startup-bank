"""End-to-end HTTP tests against the real FastAPI app: onboard -> log in -> cards -> bills ->
invoices -> reports -> multi-tenant isolation -> role enforcement. Each test gets a fresh
in-memory database (the app's module-level engine is swapped for the duration of the test)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.db import Base
from app.main import app


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[db_module.get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def onboard(client, name="Acme", email="admin@acme.com", funding=10_000):
    r = client.post(
        "/api/onboarding",
        json={
            "name": name,
            "legal_name": name,
            "ein": "00-0000000",
            "industry": "Software",
            "admin_name": "Ada Admin",
            "admin_email": email,
            "admin_password": "s3cret!",
            "initial_funding_dollars": funding,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_onboard_logs_you_in_and_sets_cookie(client):
    out = onboard(client)
    assert out["company"]["name"] == "Acme"
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["role"] == "admin"


def test_login_wrong_password_rejected(client):
    onboard(client)
    r = client.post("/api/auth/login", json={"email": "admin@acme.com", "password": "wrong"})
    assert r.status_code == 401


def test_login_then_me(client):
    onboard(client)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "admin@acme.com", "password": "s3cret!"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == "admin@acme.com"


def test_unauthenticated_request_rejected(client):
    r = client.get("/api/accounts")
    assert r.status_code == 401


def test_initial_funding_reflected_in_accounts(client):
    onboard(client, funding=25_000)
    accounts = client.get("/api/accounts").json()
    checking = next(a for a in accounts if a["bank_subtype"] == "checking")
    assert checking["balance"]["dollars"] == "25000.00"


def test_dashboard_returns_cash_and_recent_activity(client):
    onboard(client, funding=1_000)
    d = client.get("/api/dashboard").json()
    assert d["cash_total"]["dollars"] == "1000.00"
    assert len(d["recent_transactions"]) >= 1


def test_bill_pay_flow(client):
    onboard(client, funding=100_000)
    vendor = client.post("/api/vendors", json={"name": "AWS", "payment_method": "wire"}).json()
    bill = client.post(
        "/api/bills", json={"vendor_id": vendor["id"], "category_code": "5010", "amount_dollars": 500, "due_date": "2026-03-01"}
    ).json()
    assert bill["status"] == "scheduled"  # admin auto-approved
    paid = client.post(f"/api/bills/{bill['id']}/pay").json()
    assert paid["status"] == "paid"
    client.post("/api/sim/advance", json={"days": 1})
    accounts = client.get("/api/accounts").json()
    checking = next(a for a in accounts if a["bank_subtype"] == "checking")
    assert checking["balance"]["dollars"] == "99500.00"


def test_invoice_flow(client):
    onboard(client, funding=0)
    inv = client.post("/api/invoices", json={"customer_name": "Big Co", "amount_dollars": 2500, "due_date": "2026-03-01"}).json()
    assert inv["status"] == "sent"
    client.post(f"/api/invoices/{inv['id']}/pay", json={"method": "wire"})
    accounts = client.get("/api/accounts").json()
    checking = next(a for a in accounts if a["bank_subtype"] == "checking")
    assert checking["balance"]["dollars"] == "2500.00"


def test_card_issue_charge_and_categorization(client):
    onboard(client, funding=100_000)
    me = client.get("/api/auth/me").json()["user"]
    card = client.post(
        "/api/cards", json={"holder_user_id": me["id"], "label": "Ops Card", "limit_dollars": 500, "limit_period": "daily"}
    ).json()
    charge = client.post(f"/api/cards/{card['id']}/charge", json={"amount_dollars": 120, "merchant": "AWS"}).json()
    assert charge["category_code"] == "5010"
    over = client.post(f"/api/cards/{card['id']}/charge", json={"amount_dollars": 500, "merchant": "X"})
    assert over.status_code == 400  # exceeds daily limit (120 already spent + 500 > 500)


def test_reclassify_requires_settlement_first(client):
    onboard(client, funding=100_000)
    me = client.get("/api/auth/me").json()["user"]
    card = client.post("/api/cards", json={"holder_user_id": me["id"], "label": "Card"}).json()
    charge = client.post(f"/api/cards/{card['id']}/charge", json={"amount_dollars": 10, "merchant": "Unlisted Vendor"}).json()
    r = client.post(f"/api/transactions/{charge['id']}/reclassify", json={"category_code": "5010"})
    assert r.status_code == 400  # not settled yet (still authorized)
    client.post("/api/sim/advance", json={"days": 1})
    r2 = client.post(f"/api/transactions/{charge['id']}/reclassify", json={"category_code": "5010"})
    assert r2.status_code == 200 and r2.json()["category_code"] == "5010"


def test_reports_balance_after_activity(client):
    onboard(client, funding=50_000)
    client.post("/api/invoices", json={"customer_name": "Cust", "amount_dollars": 1000, "due_date": "2026-03-01"})
    inv_id = client.get("/api/invoices").json()[0]["id"]
    client.post(f"/api/invoices/{inv_id}/pay", json={"method": "wire"})
    bs = client.get("/api/reports/balance-sheet").json()
    assert bs["balances"] is True
    assert bs["assets_total"]["dollars"] == "51000.00"
    inc = client.get("/api/reports/income-statement").json()
    assert inc["revenue_total"]["dollars"] == "1000.00"


def test_employee_role_cannot_pay_bills_or_issue_cards(client):
    onboard(client)
    client.post("/api/users", json={"name": "Emp", "email": "emp@acme.com", "role": "employee", "password": "x123456"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "emp@acme.com", "password": "x123456"})
    r = client.post("/api/cards", json={"holder_user_id": 1, "label": "x"})
    assert r.status_code == 403
    vendor_resp = client.post("/api/vendors", json={"name": "V"})
    assert vendor_resp.status_code == 403


def test_employee_sees_only_own_cards_and_transactions(client):
    onboard(client, funding=100_000)
    admin = client.get("/api/auth/me").json()["user"]
    client.post("/api/users", json={"name": "Emp", "email": "emp@acme.com", "role": "employee", "password": "x123456"})
    emp = next(u for u in client.get("/api/users").json() if u["email"] == "emp@acme.com")
    admin_card = client.post("/api/cards", json={"holder_user_id": admin["id"], "label": "Admin Card"}).json()
    emp_card = client.post("/api/cards", json={"holder_user_id": emp["id"], "label": "Emp Card"}).json()
    client.post(f"/api/cards/{admin_card['id']}/charge", json={"amount_dollars": 5, "merchant": "X"})
    client.post(f"/api/cards/{emp_card['id']}/charge", json={"amount_dollars": 7, "merchant": "Y"})

    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "emp@acme.com", "password": "x123456"})
    cards_seen = client.get("/api/cards").json()
    assert len(cards_seen) == 1 and cards_seen[0]["id"] == emp_card["id"]
    txns_seen = client.get("/api/transactions").json()
    assert all(t["card_id"] == emp_card["id"] for t in txns_seen if t["card_id"])
    assert any(t["card_id"] == emp_card["id"] for t in txns_seen)


def test_multi_tenant_isolation(client):
    onboard(client, name="Acme", email="a@acme.com", funding=100_000)
    client.cookies.clear()
    onboard(client, name="Beta Inc", email="b@beta.com", funding=5_000)
    beta_accounts = client.get("/api/accounts").json()
    checking = next(a for a in beta_accounts if a["bank_subtype"] == "checking")
    assert checking["balance"]["dollars"] == "5000.00"  # not 100,000 -- no leakage from Acme

    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "a@acme.com", "password": "s3cret!"})
    acme_accounts = client.get("/api/accounts").json()
    assert next(a for a in acme_accounts if a["bank_subtype"] == "checking")["balance"]["dollars"] == "100000.00"


def test_cannot_access_another_companys_transaction(client):
    onboard(client, name="Acme", email="a@acme.com", funding=1_000)
    me = client.get("/api/auth/me").json()["user"]
    card = client.post("/api/cards", json={"holder_user_id": me["id"], "label": "c"}).json()
    txn = client.post(f"/api/cards/{card['id']}/charge", json={"amount_dollars": 5, "merchant": "X"}).json()

    client.cookies.clear()
    onboard(client, name="Beta", email="b@beta.com", funding=1_000)
    r = client.post(f"/api/transactions/{txn['id']}/reclassify", json={"category_code": "5010"})
    assert r.status_code == 404


def test_directory_lists_onboarded_companies(client):
    onboard(client, name="Acme", email="a@acme.com")
    d = client.get("/api/directory").json()
    assert any(row["company"]["name"] == "Acme" and row["admin_email"] == "a@acme.com" for row in d)


def test_sim_advance_settles_pending_transfers(client):
    onboard(client, funding=100_000)
    client.post("/api/accounts/send-money", json={"amount_dollars": 100, "counterparty": "Founder", "method": "ach"})
    before = client.get("/api/accounts").json()
    checking_before = next(a for a in before if a["bank_subtype"] == "checking")
    assert checking_before["balance"]["dollars"] == "100000.00"  # ACH not settled yet
    assert checking_before["available"]["dollars"] == "99900.00"  # but available balance already reflects it

    client.post("/api/sim/advance", json={"days": 1})
    after = client.get("/api/accounts").json()
    assert next(a for a in after if a["bank_subtype"] == "checking")["balance"]["dollars"] == "99900.00"


# ---------- budgeting ----------
def test_budget_create_and_set_line(client):
    onboard(client, funding=100_000)
    budget = client.post("/api/budgets", json={"name": "FY26"}).json()
    assert budget["name"] == "FY26"
    line = client.post(
        f"/api/budgets/{budget['id']}/lines", json={"category_code": "5010", "period": "2026-01-15", "amount_dollars": 500}
    ).json()
    assert line["period"] == "2026-01-01" and line["amount"]["dollars"] == "500.00"
    lines = client.get(f"/api/budgets/{budget['id']}/lines").json()
    assert len(lines) == 1 and lines[0]["category_code"] == "5010"


def test_budget_variance_reflects_actuals(client):
    onboard(client, funding=100_000)
    sim_today = client.get("/api/sim/today").json()["today"]
    month = sim_today[:8] + "01"  # the sim clock's current month, whatever it actually is
    budget = client.post("/api/budgets", json={"name": "FY26"}).json()
    client.post(f"/api/budgets/{budget['id']}/lines", json={"category_code": "4000", "period": month, "amount_dollars": 1000})
    inv = client.post("/api/invoices", json={"customer_name": "Cust", "amount_dollars": 1500, "due_date": sim_today}).json()
    client.post(f"/api/invoices/{inv['id']}/pay", json={"method": "wire"})
    v = client.get(f"/api/budgets/{budget['id']}/variance", params={"start": month, "end": sim_today}).json()
    row = next(r for r in v["rows"] if r["code"] == "4000")
    assert row["favorable"] is True and row["variance"]["dollars"] == "500.00"


def test_budget_populate_from_history(client):
    onboard(client, funding=100_000)
    vendor = client.post("/api/vendors", json={"name": "AWS", "payment_method": "wire"}).json()
    bill = client.post(
        "/api/bills", json={"vendor_id": vendor["id"], "category_code": "5010", "amount_dollars": 200, "due_date": "2026-01-05"}
    ).json()
    client.post(f"/api/bills/{bill['id']}/pay")
    budget = client.post("/api/budgets", json={"name": "Auto"}).json()
    r = client.post(f"/api/budgets/{budget['id']}/populate", json={"months": 3, "lookback_months": 3}).json()
    assert r["created"] == 3


def test_budget_endpoints_require_admin_or_bookkeeper(client):
    onboard(client)
    client.post("/api/users", json={"name": "Emp", "email": "emp@acme.com", "role": "employee", "password": "x123456"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "emp@acme.com", "password": "x123456"})
    assert client.post("/api/budgets", json={"name": "x"}).status_code == 403


def test_cannot_access_another_companys_budget(client):
    onboard(client, name="Acme", email="a@acme.com")
    budget = client.post("/api/budgets", json={"name": "FY26"}).json()
    client.cookies.clear()
    onboard(client, name="Beta", email="b@beta.com")
    assert client.get(f"/api/budgets/{budget['id']}/lines").status_code == 404


# ---------- cash flow & burn/runway ----------
def test_cash_flow_report_ties_to_cash(client):
    onboard(client, funding=50_000)
    cf = client.get("/api/reports/cash-flow").json()
    assert cf["ties_to_cash"] is True
    assert cf["financing_total"]["dollars"] == "50000.00"


def test_burn_and_runway_report(client):
    onboard(client, funding=50_000)
    r = client.get("/api/reports/burn-and-runway", params={"trailing": 1}).json()
    assert "runway_months" in r and "burn" in r


# ---------- forecasting ----------
def test_default_forecast_endpoint(client):
    onboard(client, funding=100_000)
    fc = client.get("/api/forecast", params={"months": 6}).json()
    assert fc["balances"] is True
    assert len(fc["income_statement"]) == 6


def test_scenario_crud_and_override(client):
    onboard(client, funding=100_000)
    scenario = client.post("/api/forecast/scenarios", json={"name": "Base case", "months": 6}).json()
    assert scenario["months"] == 6 and scenario["overrides"] == {}

    r = client.post(
        f"/api/forecast/scenarios/{scenario['id']}/overrides",
        json={"driver_key": "revenue_growth", "values": [0.5, None, None, None, None, None]},
    ).json()
    assert r["overrides"]["revenue_growth"][0] == 0.5

    got = client.get(f"/api/forecast/scenarios/{scenario['id']}").json()
    assert got["forecast"]["balances"] is True
    assert len(got["forecast"]["months"]) == 6

    cleared = client.delete(f"/api/forecast/scenarios/{scenario['id']}/overrides/revenue_growth").json()
    assert "revenue_growth" not in cleared["overrides"]


def test_scenario_endpoints_require_admin_or_bookkeeper(client):
    onboard(client)
    client.post("/api/users", json={"name": "Emp", "email": "emp@acme.com", "role": "employee", "password": "x123456"})
    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "emp@acme.com", "password": "x123456"})
    assert client.post("/api/forecast/scenarios", json={"name": "x", "months": 3}).status_code == 403


def test_cannot_access_another_companys_scenario(client):
    onboard(client, name="Acme", email="a@acme.com")
    scenario = client.post("/api/forecast/scenarios", json={"name": "FY26", "months": 6}).json()
    client.cookies.clear()
    onboard(client, name="Beta", email="b@beta.com")
    assert client.get(f"/api/forecast/scenarios/{scenario['id']}").status_code == 404


def test_scenario_workbook_download_and_validate(client):
    onboard(client, funding=100_000)
    scenario = client.post("/api/forecast/scenarios", json={"name": "Base case", "months": 3}).json()
    r = client.get(f"/api/forecast/scenarios/{scenario['id']}/workbook")
    assert r.status_code == 200 and r.content[:2] == b"PK"

    v = client.get(f"/api/forecast/scenarios/{scenario['id']}/validate").json()
    assert v["passed"] is True, v
