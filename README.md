# startup-bank (prototype)

A full-stack prototype of a Rho-style business banking platform for startups and small businesses:
accounts, corporate cards, bill pay, invoicing, real double-entry accounting, and an FP&A layer on
top (budgeting, variance analysis, cash flow analysis, and driver-based 3-statement forecasting with
a live-formula Excel export) -- with a web dashboard. **Multi-tenant** -- onboard as many companies as
you like and each only ever sees its own books.

**This is not a real bank and moves no real money.** ACH/wire/card rails are simulated in-process.
There is no bank partner, no real KYB/KYC, no card network, and no licensing of any kind. See
[Known limitations](#known-limitations) for exactly what that means and what it would take to become
a real product.

## Why this is trustworthy as a prototype

Every dollar in the system moves through one function, `post_journal_entry()` (`app/ledger.py`),
which refuses to post anything that does not balance (debits == credits). Account balances are never
stored -- they are always summed live from the journal, so they cannot drift from the truth. A
150-action randomized stress test (`app/tests/test_invariants.py`) runs a long mix of funding, cards,
bills, invoices, approvals, settlements and recategorizations and asserts **assets == liabilities +
equity after every single step**, then feeds that same messy, randomly-generated history into the
forecasting engine and checks the projection still balances too. All 176 unit/integration tests pass;
see [Testing](#testing).

While building the FP&A layer, this stress test (and a separate Excel-export test) each caught a real
bug before it shipped: a settlement-date bug that misdated ledger entries whenever there was a gap
between a transaction's intended settle date and when the settlement batch actually ran (corrupting
historical reporting for the days in between -- see `app/settlement.py`), and an off-by-one in the
forecast's starting month. Both are fixed and covered by regression tests.

## How money moves (simulated)

- **Chart of accounts**: seeded per company on onboarding (`app/ledger.py`): Checking, Savings/Treasury,
  Accounts Receivable, Accounts Payable, Paid-in Capital, Owner Draws, Revenue, Other Income, and nine
  standard expense categories.
- **Settlement float**: a `Transaction` (`app/models.py`) is created in `processing` (ACH/wire) or
  `authorized` (card) status with a `settle_date`, and only posts to the ledger once that date arrives
  (`app/settlement.py`). Wires and internal transfers settle same-day; ACH next business day; checks in
  three business days; card charges authorize immediately and settle the next business day.
- **Simulated clock** (`app/simclock.py`): the app has its own "today" so you can demo settlement float
  without waiting -- advance it with the "Advance 1 day" button or `POST /api/sim/advance`.
- **Accrual accounting**: a bill posts Debit expense / Credit Accounts Payable the moment it's recorded;
  paying it later is the separate cash-movement event. Same pattern for invoices (Debit AR / Credit
  Revenue on send, cash movement on payment).
- **Card spend**: auto-categorized by a deterministic merchant-keyword matcher (`app/categorization.py`,
  not an LLM) to "Uncategorized" if unrecognized. Recategorizing a settled transaction never edits the
  original journal entry -- it posts a new balanced entry that moves the amount, preserving a full audit
  trail.
- **Spend limits**: per-transaction, daily, or monthly, enforced before a card charge authorizes
  (`app/cards.py`).
- **Roles**: `admin` (full control, approvals, card issuance), `bookkeeper` (bills/invoices/categorize/
  reports, no card issuance), `employee` (sees and manages only their own card and its transactions).
  Bills created by a bookkeeper need admin approval; an admin's own bills auto-approve.

## FP&A layer: budgeting, variance, cash flow, forecasting

Sits on top of the ledger and never posts to it -- these are planning tools, not accounting entries.

- **Budgeting** (`app/budgeting.py`): a named `Budget` holds monthly `BudgetLine` targets per revenue
  or expense category. `populate_from_history` seeds a starting point from the trailing N months'
  average (still fully editable). `budget_vs_actual` computes variance with the standard FP&A sign
  convention -- positive is always *favorable* (revenue ahead of plan, or spending under plan),
  regardless of account type.
- **Cash flow statement** (`reports.cash_flow_statement`): real indirect-method statement --
  operating/investing/financing, classified generically by account code so it stays correct if new
  account types are added later (AR/AP are operating; any other non-bank asset is investing; any other
  liability or equity change is financing). `ties_to_cash` is asserted true: operating + investing +
  financing must equal the actual change in bank balances, since every journal entry is balanced by
  construction.
- **Burn & runway** (`reports.burn_and_runway`): average operating cash flow over the trailing N
  *complete* months (a partial current month never distorts it), and cash-on-hand / burn = runway.
- **3-statement forecast** (`app/forecasting.py`): no LLM, no guessing -- every driver is either a
  historical average computed from the company's own ledger, or a number you explicitly set as an
  override. A driver with no supporting history is left at an honest default (e.g. 0% growth) with an
  explicit warning, never silently invented. Revenue grows at a monthly rate; each expense category is
  modeled as either a % of revenue or a flat monthly amount, whichever the history supports; AR/AP are
  modeled on day counts (DSO/DPO) and drive operating cash flow; cash and equity roll forward from the
  company's actual current balances. The balance-sheet identity holds by construction every projected
  month (see the algebraic note above `_forecast_month` in `app/forecasting.py`), re-checked at runtime
  as defense in depth. Scenarios (`ForecastScenario`) persist your overrides per driver per month so
  tweaks survive a reload.
- **Excel export** (`app/forecast_workbook.py`, adapted from a prior project's proven pattern):
  live-formula `.xlsx` with Historicals / Assumptions / IS / BS / CF / Checks sheets -- blue inputs,
  black formulas, forecast cells never hardcode a number. `app/forecast_validate.py` recalculates it
  (LibreOffice if installed, else the `formulas` library) and checks the *recalculated numbers*, not
  just the formula text: balances every period, no Excel errors, no hardcoded constants in forecast
  cells.

## Running it

```bash
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000. "Open an account" onboards a new company (instant simulated KYB) and logs
you in as its admin; "Log in" is for an existing company. The demo-companies directory on the login
screen is a prototype-only convenience (a real bank would never expose a list of its customers) that
prefills the login email so you can quickly get back into a company you already onboarded.

`STARTUP_BANK_DB` (env var) overrides the SQLite file location (default: `startup_bank.db` in the
project root, gitignored). `STARTUP_BANK_SECRET` overrides the session-signing secret (set a real one
before showing this to anyone besides yourself).

## Testing

```bash
uv run pytest app/tests -q                                    # 176 tests, no server needed
uv run ruff check . && uv run ruff format --check .           # lint + format

uv run uvicorn app.main:app --port 8010 &                     # for the UI smoke test:
node app/tests/ui/ui_smoke.js http://127.0.0.1:8010            # runs the real app.js against the live server with a fake DOM (no browser)
```

`app/tests/test_invariants.py` is the strongest guarantee: 150 randomized actions across the whole
surface, checking the accounting identity after each one, then a forecast built from that same messy
history. `app/tests/test_api.py` covers the HTTP API end to end, including multi-tenant isolation
(company A can never see or touch company B's data) and role enforcement.
`app/tests/test_forecast_workbook.py` recalculates the exported Excel workbook (not just checks its
formula text) and confirms it matches the JSON forecast, including a test that deliberately corrupts a
cell and confirms the validator catches it. `app/tests/ui/ui_smoke.js` executes the actual frontend
JavaScript (not a reimplementation of it) against a running server, so it catches real wiring bugs
between the page and the API -- it has been run and passes (27/27 checks) but has not been verified in
an actual browser.

## Known limitations

- **Not a real bank, and cannot become one by adding code.** A real version needs a licensed
  Banking-as-a-Service partner (e.g. Unit, Increase, Column, Treasury Prime), real KYB/KYC and BSA/AML
  compliance, PCI compliance for card data, and money-transmission licensing -- all business/legal work,
  not engineering. This prototype is useful for product/UX exploration and for understanding the
  accounting model, not as a starting point you can "flip a switch" on.
- **No real card network, ACH, or wire rails.** Settlement timing is a reasonable approximation
  (same-day wires, next-business-day ACH, 3-business-day checks) but transactions never actually leave
  the database.
- **Simplified accounting.** No multi-currency, no payroll processing (payroll is just an expense
  category), no sales tax, no accrual-to-cash toggling, no period close/lock, no depreciation schedules.
  Contra-equity accounts (Owner Draws) must be added with their *parent type's* normal-balance direction,
  not their own natural one -- see the comment in `app/ledger.py`'s chart of accounts; this was a real
  bug caught by the stress test during development.
- **No real KYB/KYC.** Onboarding auto-approves every company instantly; there is no identity
  verification, no OFAC screening, no beneficial-ownership collection.
- **Session security is dev-grade.** Passwords are hashed properly (PBKDF2-HMAC-SHA256, salted), but set
  `STARTUP_BANK_SECRET` to a real secret before this leaves your machine, and it has no rate limiting,
  no 2FA, and no password-reset flow.
- **No email.** Team member invites set a password directly (you tell them what it is); there is no
  invite-email flow. Invoices/bills are not actually emailed to anyone.
- **UI has not been checked in an actual browser** in this environment, only via the Node fake-DOM
  harness above, which exercises real logic but not real rendering/CSS.
- **Forecast is a mechanical trend extrapolation, not a prediction.** It cannot see a funding round,
  a new hire, a pricing change, or a market shift coming; it only knows what already happened. There is
  no assumed future financing or investing activity in the baseline -- add it yourself via a scenario
  override if you're modeling one.
- **No fixed assets or depreciation**, so the cash flow statement's investing section and the forecast
  are both simpler than a company with real capex would need; the code is written to generalize
  automatically if PP&E-style accounts are added to the chart of accounts later, but that hasn't been
  exercised against real capex data.
- **Forecasting and budgeting are monthly only**; there's no weekly/quarterly/annual view, and the
  forecast horizon caps at 36 months.
