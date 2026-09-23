"use strict";
/* startup-bank dashboard: vanilla JS, no build step, no framework. Talks to the JSON API only. */

const $ = (id) => document.getElementById(id);
let SESSION = null; // { user, company }
let CATEGORIES = [];
let USERS = [];
let SELECTED_BUDGET_ID = null;
let SELECTED_SCENARIO_ID = null;

// ---------- tiny DOM helpers ----------
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined) continue;
    e.append(typeof k === "string" || typeof k === "number" ? String(k) : k);
  }
  return e;
}
function clear(node) { node.replaceChildren(); }
function msg(target, text, kind) {
  const t = $(target);
  clear(t);
  if (text) t.append(el("div", { class: "msg " + (kind || "") }, text));
}
function badge(text, kind) { return el("span", { class: "badge " + (kind || "") }, text); }
function money(m) { return m ? m.formatted : "$0.00"; }

// ---------- API ----------
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) throw new Error((data && data.detail) || res.statusText || "Request failed");
  return data;
}
const get = (path) => api("GET", path);
const post = (path, body) => api("POST", body ? path : path, body || {});

// ---------- Auth screen ----------
$("tab-login").addEventListener("click", () => showAuthTab("login"));
$("tab-onboard").addEventListener("click", () => showAuthTab("onboard"));
function showAuthTab(which) {
  $("f-login").classList.toggle("hidden", which !== "login");
  $("f-onboard").classList.toggle("hidden", which !== "onboard");
  $("tab-login").classList.toggle("chip", true);
}

$("f-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg("m-login", "");
  try {
    await api("POST", "/api/auth/login", { email: $("login-email").value, password: $("login-password").value });
    await boot();
  } catch (e) { msg("m-login", e.message, "bad"); }
});

$("f-onboard").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg("m-onboard", "");
  try {
    await api("POST", "/api/onboarding", {
      name: $("ob-name").value, legal_name: $("ob-legal").value, industry: $("ob-industry").value, ein: $("ob-ein").value,
      admin_name: $("ob-admin-name").value, admin_email: $("ob-admin-email").value, admin_password: $("ob-admin-password").value,
      initial_funding_dollars: Number($("ob-funding").value || 0),
    });
    await boot();
  } catch (e) { msg("m-onboard", e.message, "bad"); }
});

async function loadDirectory() {
  const box = $("directory");
  try {
    const rows = await get("/api/directory");
    clear(box);
    if (!rows.length) { box.textContent = "None yet -- open the first account above."; return; }
    rows.forEach((r) => {
      const item = el("div", { class: "directory-item" },
        el("span", {}, r.company.name), el("span", { class: "muted" }, r.admin_email));
      item.style.cursor = "pointer";
      item.addEventListener("click", () => { showAuthTab("login"); $("login-email").value = r.admin_email; $("login-password").focus(); });
      box.append(item);
    });
  } catch (e) { box.textContent = "Could not load directory."; }
}

$("b-logout").addEventListener("click", async () => {
  await api("POST", "/api/auth/logout");
  location.reload();
});

$("b-advance").addEventListener("click", async () => {
  try {
    const r = await post("/api/sim/advance", { days: 1 });
    $("hdr-sim-date").textContent = "Sim date: " + r.today;
    msg("m-global", r.settled_count ? `Advanced 1 day -- ${r.settled_count} pending transaction(s) settled.` : "Advanced 1 day.", "ok");
    await refreshCurrentView();
  } catch (e) { msg("m-global", e.message, "bad"); }
});

// ---------- Boot / nav ----------
const NAV = [
  { id: "overview", label: "Overview", roles: ["admin", "bookkeeper", "employee"] },
  { id: "accounts", label: "Accounts", roles: ["admin", "bookkeeper"] },
  { id: "transactions", label: "Transactions", roles: ["admin", "bookkeeper", "employee"] },
  { id: "cards", label: "Cards", roles: ["admin", "bookkeeper", "employee"] },
  { id: "billpay", label: "Bill Pay", roles: ["admin", "bookkeeper"] },
  { id: "invoicing", label: "Invoicing", roles: ["admin", "bookkeeper"] },
  { id: "reports", label: "Reports", roles: ["admin", "bookkeeper"] },
  { id: "budget", label: "Budget", roles: ["admin", "bookkeeper"] },
  { id: "forecast", label: "Forecast", roles: ["admin", "bookkeeper"] },
  { id: "team", label: "Team", roles: ["admin"] },
];
let CURRENT_VIEW = "overview";

async function boot() {
  try {
    SESSION = await get("/api/auth/me");
  } catch {
    $("auth-view").classList.remove("hidden");
    $("app").classList.add("hidden");
    loadDirectory();
    return;
  }
  $("auth-view").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("hdr-company").textContent = SESSION.company.name;
  $("hdr-role").textContent = SESSION.user.role;
  $("hdr-user").textContent = SESSION.user.name + " (" + SESSION.user.email + ")";
  const canAdvance = ["admin", "bookkeeper"].includes(SESSION.user.role);
  $("b-advance").classList.toggle("hidden", !canAdvance);

  const nav = $("nav");
  clear(nav);
  NAV.filter((n) => n.roles.includes(SESSION.user.role)).forEach((n) => {
    const b = el("button", { onclick: () => switchView(n.id) }, n.label);
    b.dataset.view = n.id;
    nav.append(b);
  });

  try {
    CATEGORIES = await get("/api/categories");
    USERS = await get("/api/users");
  } catch { /* non-fatal */ }

  switchView("overview");
}

async function switchView(id) {
  CURRENT_VIEW = id;
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === id));
  ["overview", "accounts", "transactions", "cards", "billpay", "invoicing", "reports", "budget", "forecast", "team"].forEach((v) =>
    $("view-" + v).classList.toggle("hidden", v !== id));
  await refreshCurrentView();
}
function refreshCurrentView() {
  msg("m-global", "");
  const fn = { overview: renderOverview, accounts: renderAccounts, transactions: renderTransactions, cards: renderCards,
               billpay: renderBillPay, invoicing: renderInvoicing, reports: renderReports, budget: renderBudget,
               forecast: renderForecast, team: renderTeam }[CURRENT_VIEW];
  return fn ? fn() : null;
}

// ---------- Overview ----------
async function renderOverview() {
  const box = $("view-overview");
  clear(box);
  const d = await get("/api/dashboard");
  $("hdr-sim-date").textContent = "Sim date: " + d.sim_today;
  box.append(
    el("div", { class: "stat-row" },
      el("div", { class: "card stat" }, el("h3", {}, "Total cash"), el("div", { class: "kpi" }, money(d.cash_total))),
      el("div", { class: "card stat" }, el("h3", {}, "Revenue (month to date)"), el("div", { class: "kpi ok" }, money(d.month_to_date_revenue))),
      el("div", { class: "card stat" }, el("h3", {}, "Expenses (month to date)"), el("div", { class: "kpi" }, money(d.month_to_date_expenses))),
    ),
  );
  if (d.has_open_bills) box.append(el("div", { class: "msg warn", style: "margin-top:14px" }, "You have bills awaiting approval or payment. See Bill Pay."));
  const card = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Recent activity"));
  card.append(txnTable(d.recent_transactions, { showReclass: false }));
  box.append(card);
}

// ---------- shared transaction table ----------
function statusBadge(status) {
  const kind = status === "settled" ? "ok" : status === "failed" || status === "voided" ? "bad" : "warn";
  return badge(status, kind);
}
function txnTable(rows, opts) {
  opts = opts || {};
  if (!rows.length) return el("p", { class: "muted" }, "No activity yet.");
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table", {},
    el("tr", {}, el("th", {}, "Date"), el("th", {}, "Description"), el("th", {}, "Category"), el("th", {}, "Status"), el("th", { class: "num" }, "Amount")));
  rows.forEach((t) => {
    const sign = t.direction === "debit" ? "+" : "-";
    const amountCell = el("td", { class: "num " + (t.direction === "debit" ? "ok" : "") }, sign + t.amount.formatted.replace("$", "$"));
    let categoryCell;
    if (opts.showReclass && t.status === "settled" && t.category_code) {
      const select = el("select", {}, ...CATEGORIES.map((c) => el("option", { value: c.code, selected: c.code === t.category_code ? "" : undefined }, c.name)));
      select.value = t.category_code;
      select.addEventListener("change", async () => {
        try { await post(`/api/transactions/${t.id}/reclassify`, { category_code: select.value }); msg("m-global", "Recategorized.", "ok"); refreshCurrentView(); }
        catch (e) { msg("m-global", e.message, "bad"); }
      });
      categoryCell = el("td", {}, select);
    } else {
      categoryCell = el("td", { class: "muted" }, t.category_name || "--");
    }
    table.append(el("tr", {}, el("td", {}, t.created_date), el("td", {}, t.description || t.counterparty || t.type), categoryCell, el("td", {}, statusBadge(t.status)), amountCell));
  });
  wrap.append(table);
  return wrap;
}

// ---------- Accounts ----------
async function renderAccounts() {
  const box = $("view-accounts");
  clear(box);
  const accounts = await get("/api/accounts");
  const table = el("table", {}, el("tr", {}, el("th", {}, "Account"), el("th", {}, "Type"), el("th", { class: "num" }, "Balance"), el("th", { class: "num" }, "Available"), el("th", {}, "")));
  accounts.forEach((a) => {
    const stmtBtn = el("button", { class: "ghost small", onclick: () => showStatement(a.code, a.name) }, "Statement");
    table.append(el("tr", {}, el("td", {}, a.name), el("td", { class: "muted" }, a.bank_subtype), el("td", { class: "num" }, money(a.balance)), el("td", { class: "num" }, money(a.available)), el("td", {}, stmtBtn)));
  });
  const acctSelect = () => el("select", { required: "" }, ...accounts.map((a) => el("option", { value: a.code }, a.name)));

  const transferForm = el("form", { class: "card col" }, el("h2", {}, "Move money between your accounts"),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "From"), (function () { const s = acctSelect(); s.name = "from"; return s; })()),
      el("div", { class: "field" }, el("label", {}, "To"), (function () { const s = acctSelect(); s.name = "to"; s.selectedIndex = Math.min(1, accounts.length - 1); return s; })()),
      el("div", { class: "field" }, el("label", {}, "Amount (USD)"), el("input", { type: "number", min: "0.01", step: "0.01", name: "amount", required: "" })),
    ), el("div", { class: "row" }, el("button", { type: "submit" }, "Transfer")), el("div", { id: "m-transfer" }));
  transferForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(transferForm);
    try { await post("/api/accounts/transfer", { from_code: f.get("from"), to_code: f.get("to"), amount_dollars: Number(f.get("amount")) }); msg("m-transfer", "Transferred.", "ok"); renderAccounts(); }
    catch (e) { msg("m-transfer", e.message, "bad"); }
  });

  const sendForm = el("form", { class: "card col" }, el("h2", {}, "Send money (external ACH/wire)"),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "From"), (function () { const s = acctSelect(); s.name = "from"; return s; })()),
      el("div", { class: "field" }, el("label", {}, "Pay to"), el("input", { type: "text", name: "counterparty", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Amount (USD)"), el("input", { type: "number", min: "0.01", step: "0.01", name: "amount", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Method"), el("select", { name: "method" }, el("option", { value: "ach" }, "ACH (next business day)"), el("option", { value: "wire" }, "Wire (same day)"))),
    ), el("div", { class: "row" }, el("button", { type: "submit" }, "Send")), el("div", { id: "m-send" }));
  sendForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(sendForm);
    try { await post("/api/accounts/send-money", { from_code: f.get("from"), counterparty: f.get("counterparty"), amount_dollars: Number(f.get("amount")), method: f.get("method") }); msg("m-send", "Sent (pending settlement).", "ok"); renderAccounts(); }
    catch (e) { msg("m-send", e.message, "bad"); }
  });

  const receiveForm = el("form", { class: "card col" }, el("h2", {}, "Record an incoming payment"),
    el("p", { class: "muted", style: "margin-top:-6px" }, "For money received that is not tied to an invoice (e.g. a grant)."),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "Into"), (function () { const s = acctSelect(); s.name = "to"; return s; })()),
      el("div", { class: "field" }, el("label", {}, "From"), el("input", { type: "text", name: "counterparty", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Amount (USD)"), el("input", { type: "number", min: "0.01", step: "0.01", name: "amount", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Method"), el("select", { name: "method" }, el("option", { value: "wire" }, "Wire (same day)"), el("option", { value: "ach" }, "ACH (next business day)"))),
    ), el("div", { class: "row" }, el("button", { type: "submit" }, "Record")), el("div", { id: "m-receive" }));
  receiveForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(receiveForm);
    try { await post("/api/accounts/receive-money", { to_code: f.get("to"), counterparty: f.get("counterparty"), amount_dollars: Number(f.get("amount")), method: f.get("method") }); msg("m-receive", "Recorded (pending settlement).", "ok"); renderAccounts(); }
    catch (e) { msg("m-receive", e.message, "bad"); }
  });

  box.append(el("div", { class: "card" }, el("h2", {}, "Bank accounts"), el("div", { class: "table-wrap" }, table)),
    el("div", { class: "grid grid-2", style: "margin-top:14px" }, transferForm, sendForm, receiveForm));
}

async function showStatement(code, name) {
  try {
    const lines = await get(`/api/accounts/${code}/statement`);
    const box = $("view-accounts");
    const old = document.getElementById("statement-panel");
    if (old) old.remove();
    const table = el("table", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Memo"), el("th", { class: "num" }, "Amount"), el("th", { class: "num" }, "Balance")));
    lines.slice().reverse().forEach((l) => table.append(el("tr", {}, el("td", {}, l.date), el("td", {}, l.memo), el("td", { class: "num" }, l.amount.formatted), el("td", { class: "num" }, l.balance_after.formatted))));
    box.append(el("div", { id: "statement-panel", class: "card", style: "margin-top:14px" }, el("h2", {}, name + " -- statement"), el("div", { class: "table-wrap" }, table)));
  } catch (e) { msg("m-global", e.message, "bad"); }
}

// ---------- Transactions ----------
async function renderTransactions() {
  const box = $("view-transactions");
  clear(box);
  const rows = await get("/api/transactions?limit=200");
  const canReclass = SESSION.user.role !== "employee";
  box.append(el("div", { class: "card" }, el("h2", {}, "All activity"), txnTable(rows, { showReclass: canReclass })));
}

// ---------- Cards ----------
async function renderCards() {
  const box = $("view-cards");
  clear(box);
  const isAdmin = SESSION.user.role === "admin";
  const cardsList = await get("/api/cards");
  const listCard = el("div", { class: "card" }, el("h2", {}, "Cards"));
  if (!cardsList.length) listCard.append(el("p", { class: "muted" }, "No cards yet."));
  cardsList.forEach((c) => {
    const statusKind = c.status === "active" ? "ok" : c.status === "frozen" ? "warn" : "bad";
    const row = el("div", { class: "card", style: "margin-bottom:10px" },
      el("div", { class: "row" },
        el("strong", {}, c.label), el("span", { class: "muted" }, "•••• " + c.last4), badge(c.status, statusKind),
        el("span", { class: "muted" }, c.holder.name), c.limit ? badge(money(c.limit) + " / " + c.limit_period, "chip") : null,
      ));
    const actions = el("div", { class: "row", style: "margin-top:8px" });
    const canManage = isAdmin || c.holder.email === SESSION.user.email;
    if (canManage && c.status === "active") actions.append(el("button", { class: "ghost small", onclick: () => cardAction(c.id, "freeze") }, "Freeze"));
    if (canManage && c.status === "frozen") actions.append(el("button", { class: "ghost small", onclick: () => cardAction(c.id, "unfreeze") }, "Unfreeze"));
    if (isAdmin && c.status !== "closed") actions.append(el("button", { class: "danger small", onclick: () => cardAction(c.id, "close") }, "Close"));
    row.append(actions);
    if (canManage && c.status === "active") {
      const chargeForm = el("form", { class: "row", style: "margin-top:8px" },
        el("input", { type: "text", placeholder: "Merchant (e.g. AWS, Uber)", name: "merchant", required: "", style: "max-width:220px" }),
        el("input", { type: "number", placeholder: "Amount", min: "0.01", step: "0.01", name: "amount", required: "", style: "max-width:120px" }),
        el("button", { class: "small", type: "submit" }, "Simulate purchase"));
      chargeForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const f = new FormData(chargeForm);
        try { await post(`/api/cards/${c.id}/charge`, { amount_dollars: Number(f.get("amount")), merchant: f.get("merchant") }); msg("m-global", "Charge authorized.", "ok"); renderCards(); }
        catch (e) { msg("m-global", e.message, "bad"); }
      });
      row.append(chargeForm);
    }
    listCard.append(row);
  });
  box.append(listCard);

  if (isAdmin) {
    const accounts = await get("/api/accounts");
    const issueForm = el("form", { class: "card col", style: "margin-top:14px" }, el("h2", {}, "Issue a card"),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "Holder"), el("select", { name: "holder" }, ...USERS.map((u) => el("option", { value: u.id }, u.name + " (" + u.role + ")")))),
        el("div", { class: "field" }, el("label", {}, "Draws from"), el("select", { name: "account" }, ...accounts.map((a) => el("option", { value: a.code }, a.name)))),
        el("div", { class: "field" }, el("label", {}, "Label"), el("input", { type: "text", name: "label", value: "Corporate Card", required: "" })),
      ),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", {}, "Spend limit (USD, optional)"), el("input", { type: "number", name: "limit", min: "0", step: "1" })),
        el("div", { class: "field" }, el("label", {}, "Limit period"), el("select", { name: "period" }, el("option", { value: "none" }, "No limit"), el("option", { value: "per_transaction" }, "Per transaction"), el("option", { value: "daily" }, "Daily"), el("option", { value: "monthly" }, "Monthly"))),
      ),
      el("div", { class: "row" }, el("button", { type: "submit" }, "Issue card")), el("div", { id: "m-issue" }));
    issueForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = new FormData(issueForm);
      try {
        await post("/api/cards", { holder_user_id: Number(f.get("holder")), account_code: f.get("account"), label: f.get("label"), limit_dollars: f.get("limit") ? Number(f.get("limit")) : null, limit_period: f.get("period") });
        msg("m-issue", "Card issued.", "ok"); renderCards();
      } catch (e) { msg("m-issue", e.message, "bad"); }
    });
    box.append(issueForm);
  }
}
async function cardAction(id, action) {
  try { await post(`/api/cards/${id}/${action}`); renderCards(); } catch (e) { msg("m-global", e.message, "bad"); }
}

// ---------- Bill pay ----------
async function renderBillPay() {
  const box = $("view-billpay");
  clear(box);
  const isAdmin = SESSION.user.role === "admin";
  const [vendors, bills] = await Promise.all([get("/api/vendors"), get("/api/bills")]);

  const billsCard = el("div", { class: "card" }, el("h2", {}, "Bills"));
  if (!bills.length) billsCard.append(el("p", { class: "muted" }, "No bills yet."));
  const btable = el("table", {}, el("tr", {}, el("th", {}, "Vendor"), el("th", {}, "Category"), el("th", {}, "Due"), el("th", { class: "num" }, "Amount"), el("th", {}, "Status"), el("th", {}, "")));
  bills.forEach((b) => {
    const kind = b.status === "paid" ? "ok" : b.status === "void" ? "bad" : b.status === "pending_approval" ? "warn" : "chip";
    const actions = el("div", { class: "row" });
    if (isAdmin && b.status === "pending_approval") actions.append(el("button", { class: "ghost small", onclick: () => billAction(b.id, "approve") }, "Approve"));
    if (b.status === "scheduled") actions.append(el("button", { class: "small", onclick: () => billAction(b.id, "pay") }, "Pay"));
    if (isAdmin && b.status !== "paid" && b.status !== "void") actions.append(el("button", { class: "danger small", onclick: () => billAction(b.id, "void") }, "Void"));
    btable.append(el("tr", {}, el("td", {}, b.vendor), el("td", { class: "muted" }, b.category_name), el("td", {}, b.due_date), el("td", { class: "num" }, money(b.amount)), el("td", {}, badge(b.status.replace("_", " "), kind)), el("td", {}, actions)));
  });
  billsCard.append(el("div", { class: "table-wrap" }, btable));

  const vendorCard = el("div", { class: "card" }, el("h2", {}, "Vendors"));
  const vtable = el("table", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Payment method")));
  vendors.forEach((v) => vtable.append(el("tr", {}, el("td", {}, v.name), el("td", { class: "muted" }, v.payment_method.toUpperCase()))));
  vendorCard.append(el("div", { class: "table-wrap" }, vtable));

  const addVendorForm = el("form", { class: "row" },
    el("input", { type: "text", name: "name", placeholder: "Vendor name", required: "", style: "max-width:220px" }),
    el("select", { name: "method" }, el("option", { value: "ach" }, "ACH"), el("option", { value: "wire" }, "Wire"), el("option", { value: "check" }, "Check")),
    el("button", { class: "small", type: "submit" }, "Add vendor"));
  addVendorForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(addVendorForm);
    try { await post("/api/vendors", { name: f.get("name"), payment_method: f.get("method") }); renderBillPay(); }
    catch (e) { msg("m-global", e.message, "bad"); }
  });
  vendorCard.append(addVendorForm);

  const billForm = el("form", { class: "card col" }, el("h2", {}, "New bill"),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "Vendor"), el("select", { name: "vendor" }, ...vendors.map((v) => el("option", { value: v.id }, v.name)))),
      el("div", { class: "field" }, el("label", {}, "Category"), el("select", { name: "category" }, ...CATEGORIES.map((c) => el("option", { value: c.code }, c.name)))),
      el("div", { class: "field" }, el("label", {}, "Amount (USD)"), el("input", { type: "number", name: "amount", min: "0.01", step: "0.01", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Due date"), el("input", { type: "date", name: "due", required: "" })),
    ), el("div", { class: "row" }, el("input", { type: "text", name: "memo", placeholder: "Memo (optional)" })),
    el("div", { class: "row" }, el("button", { type: "submit" }, "Create bill")), el("div", { id: "m-bill" }));
  billForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!vendors.length) { msg("m-bill", "Add a vendor first.", "bad"); return; }
    const f = new FormData(billForm);
    try { await post("/api/bills", { vendor_id: Number(f.get("vendor")), category_code: f.get("category"), amount_dollars: Number(f.get("amount")), due_date: f.get("due"), memo: f.get("memo") }); msg("m-bill", "Bill created.", "ok"); renderBillPay(); }
    catch (e) { msg("m-bill", e.message, "bad"); }
  });

  box.append(el("div", { class: "grid grid-2" }, billsCard, vendorCard), billForm);
}
async function billAction(id, action) {
  try { await post(`/api/bills/${id}/${action}`); renderBillPay(); } catch (e) { msg("m-global", e.message, "bad"); }
}

// ---------- Invoicing ----------
async function renderInvoicing() {
  const box = $("view-invoicing");
  clear(box);
  const invoices = await get("/api/invoices");
  const listCard = el("div", { class: "card" }, el("h2", {}, "Invoices"));
  if (!invoices.length) listCard.append(el("p", { class: "muted" }, "No invoices yet."));
  const table = el("table", {}, el("tr", {}, el("th", {}, "Customer"), el("th", {}, "Due"), el("th", { class: "num" }, "Amount"), el("th", {}, "Status"), el("th", {}, "")));
  invoices.forEach((i) => {
    const kind = i.status === "paid" ? "ok" : i.status === "void" ? "bad" : "warn";
    const actions = el("div", { class: "row" });
    if (i.status === "sent") {
      const methodSelect = el("select", {}, el("option", { value: "ach" }, "ACH"), el("option", { value: "wire" }, "Wire"), el("option", { value: "card" }, "Card"));
      actions.append(methodSelect, el("button", { class: "small", onclick: () => invoicePay(i.id, methodSelect.value) }, "Mark paid"));
      actions.append(el("button", { class: "danger small", onclick: () => invoiceVoid(i.id) }, "Void"));
    }
    table.append(el("tr", {}, el("td", {}, i.customer_name), el("td", {}, i.due_date), el("td", { class: "num" }, money(i.amount)), el("td", {}, badge(i.status, kind)), el("td", {}, actions)));
  });
  listCard.append(el("div", { class: "table-wrap" }, table));

  const form = el("form", { class: "card col" }, el("h2", {}, "Send an invoice"),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "Customer"), el("input", { type: "text", name: "customer", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Amount (USD)"), el("input", { type: "number", name: "amount", min: "0.01", step: "0.01", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Due date"), el("input", { type: "date", name: "due", required: "" })),
    ), el("div", { class: "row" }, el("input", { type: "text", name: "memo", placeholder: "Memo (optional)" })),
    el("div", { class: "row" }, el("button", { type: "submit" }, "Send invoice")), el("div", { id: "m-invoice" }));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    try { await post("/api/invoices", { customer_name: f.get("customer"), amount_dollars: Number(f.get("amount")), due_date: f.get("due"), memo: f.get("memo") }); msg("m-invoice", "Invoice sent.", "ok"); renderInvoicing(); }
    catch (e) { msg("m-invoice", e.message, "bad"); }
  });

  box.append(listCard, form);
}
async function invoicePay(id, method) {
  try { await post(`/api/invoices/${id}/pay`, { method }); renderInvoicing(); } catch (e) { msg("m-global", e.message, "bad"); }
}
async function invoiceVoid(id) {
  try { await post(`/api/invoices/${id}/void`); renderInvoicing(); } catch (e) { msg("m-global", e.message, "bad"); }
}

// ---------- Reports ----------
async function renderReports() {
  const box = $("view-reports");
  clear(box);
  const [bs, inc, cf, burn] = await Promise.all([
    get("/api/reports/balance-sheet"), get("/api/reports/income-statement"),
    get("/api/reports/cash-flow"), get("/api/reports/burn-and-runway?trailing=3"),
  ]);

  const section = (title, rows, totalLabel, total) => {
    const t = el("table", {}, el("tr", {}, el("th", {}, "Account"), el("th", { class: "num" }, "Balance")));
    rows.forEach((r) => t.append(el("tr", {}, el("td", {}, r.name), el("td", { class: "num" }, money(r.amount)))));
    t.append(el("tr", {}, el("td", {}, el("strong", {}, totalLabel)), el("td", { class: "num" }, el("strong", {}, money(total)))));
    return el("div", {}, el("h3", {}, title), t);
  };

  const bsCard = el("div", { class: "card" },
    el("div", { class: "row" }, el("h2", {}, "Balance sheet"), badge(bs.balances ? "Balances" : "OUT OF BALANCE", bs.balances ? "ok" : "bad")),
    el("p", { class: "muted", style: "margin-top:-6px" }, "As of " + bs.as_of),
    el("div", { class: "grid grid-2" },
      section("Assets", bs.assets, "Total assets", bs.assets_total),
      el("div", { class: "col" }, section("Liabilities", bs.liabilities, "Total liabilities", bs.liabilities_total), section("Equity", bs.equity, "Total equity", bs.equity_total)),
    ));

  const incCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Income statement (month to date)"),
    el("div", { class: "grid grid-2" }, section("Revenue", inc.revenue, "Total revenue", inc.revenue_total), section("Expenses", inc.expenses, "Total expenses", inc.expenses_total)),
    el("div", { class: "stat-row", style: "margin-top:10px" }, el("div", { class: "stat" }, el("h3", {}, "Net income"), el("div", { class: "kpi " + (inc.net_income.cents >= 0 ? "ok" : "bad") }, money(inc.net_income)))));

  const spendCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Spend by category"));
  const maxAmt = Math.max(1, ...inc.expenses.map((r) => r.amount.cents));
  if (!inc.expenses.length) spendCard.append(el("p", { class: "muted" }, "No expenses this period."));
  inc.expenses.forEach((r) => {
    spendCard.append(el("div", { class: "bar-row" }, el("span", {}, r.name),
      el("div", { class: "bar-track" }, el("div", { class: "bar-fill", style: `width:${(100 * r.amount.cents) / maxAmt}%` })),
      el("span", { class: "num" }, money(r.amount))));
  });

  const cfCard = el("div", { class: "card", style: "margin-top:14px" },
    el("div", { class: "row" }, el("h2", {}, "Cash flow statement (indirect method)"), badge(cf.ties_to_cash ? "Ties to cash" : "DOES NOT TIE", cf.ties_to_cash ? "ok" : "bad")),
    el("p", { class: "muted", style: "margin-top:-6px" }, cf.start + " to " + cf.end),
    section("Operating activities", cf.operating, "Net operating cash flow", cf.operating_total),
    cf.investing.length ? section("Investing activities", cf.investing, "Net investing cash flow", cf.investing_total) : null,
    cf.financing.length ? section("Financing activities", cf.financing, "Net financing cash flow", cf.financing_total) : null,
    el("table", {}, el("tr", {}, el("td", {}, el("strong", {}, "Net change in cash")), el("td", { class: "num" }, el("strong", {}, money(cf.net_change)))),
      el("tr", {}, el("td", {}, "Cash, beginning of period"), el("td", { class: "num" }, money(cf.cash_start))),
      el("tr", {}, el("td", {}, "Cash, end of period"), el("td", { class: "num" }, money(cf.cash_end)))));

  const burnCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Burn & runway"),
    el("p", { class: "muted", style: "margin-top:-6px" }, "Trailing 3 complete months: " + burn.months_used.join(", ") || "no complete months yet"),
    el("div", { class: "stat-row" },
      el("div", { class: "stat" }, el("h3", {}, "Monthly burn"), el("div", { class: "kpi " + (burn.burn.cents > 0 ? "bad" : "ok") }, money(burn.burn))),
      el("div", { class: "stat" }, el("h3", {}, "Cash on hand"), el("div", { class: "kpi" }, money(burn.cash_on_hand))),
      el("div", { class: "stat" }, el("h3", {}, "Runway"), el("div", { class: "kpi" }, burn.runway_months == null ? "Cash flow positive" : burn.runway_months.toFixed(1) + " months"))));

  box.append(bsCard, incCard, spendCard, cfCard, burnCard);
}

// ---------- Budget ----------
function monthInputValue(iso) { return (iso || "").slice(0, 7); } // "YYYY-MM-DD" -> "YYYY-MM" for <input type=month>

async function renderBudget() {
  const box = $("view-budget");
  clear(box);
  const budgets = await get("/api/budgets");
  if (SELECTED_BUDGET_ID && !budgets.some((b) => b.id === SELECTED_BUDGET_ID)) SELECTED_BUDGET_ID = null;
  if (!SELECTED_BUDGET_ID && budgets.length) SELECTED_BUDGET_ID = budgets[0].id;

  const listCard = el("div", { class: "card" }, el("h2", {}, "Budgets"));
  const picker = el("div", { class: "row" });
  budgets.forEach((b) => {
    const btn = el("button", { class: b.id === SELECTED_BUDGET_ID ? "small" : "ghost small", onclick: async () => { SELECTED_BUDGET_ID = b.id; await renderBudget(); } }, b.name);
    picker.append(btn);
  });
  listCard.append(picker);
  const newForm = el("form", { class: "row", style: "margin-top:10px" },
    el("input", { type: "text", name: "name", placeholder: "New budget name (e.g. FY2026)", required: "", style: "max-width:240px" }),
    el("button", { class: "small", type: "submit" }, "Create budget"));
  newForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const b = await post("/api/budgets", { name: new FormData(newForm).get("name") });
    SELECTED_BUDGET_ID = b.id;
    await renderBudget();
  });
  listCard.append(newForm);
  box.append(listCard);
  if (!SELECTED_BUDGET_ID) { box.append(el("p", { class: "muted" }, "Create a budget to get started.")); return; }

  const lines = await get(`/api/budgets/${SELECTED_BUDGET_ID}/lines`);
  const linesCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Budget lines"));
  const ltable = el("table", {}, el("tr", {}, el("th", {}, "Category"), el("th", {}, "Month"), el("th", { class: "num" }, "Amount")));
  lines.sort((a, b) => a.period.localeCompare(b.period) || a.category_code.localeCompare(b.category_code));
  lines.forEach((l) => ltable.append(el("tr", {}, el("td", {}, l.category_name), el("td", {}, monthInputValue(l.period)), el("td", { class: "num" }, money(l.amount)))));
  linesCard.append(el("div", { class: "table-wrap" }, ltable));

  const lineForm = el("form", { class: "row", style: "margin-top:10px" },
    el("select", { name: "category" }, ...CATEGORIES.map((c) => el("option", { value: c.code }, c.name))),
    el("input", { type: "month", name: "period", required: "" }),
    el("input", { type: "number", name: "amount", placeholder: "Amount (USD)", min: "0", step: "0.01", required: "", style: "max-width:140px" }),
    el("button", { class: "small", type: "submit" }, "Set line"));
  lineForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(lineForm);
    try {
      await post(`/api/budgets/${SELECTED_BUDGET_ID}/lines`, { category_code: f.get("category"), period: f.get("period") + "-01", amount_dollars: Number(f.get("amount")) });
      await renderBudget();
    } catch (e) { msg("m-global", e.message, "bad"); }
  });
  linesCard.append(lineForm);

  const populateForm = el("form", { class: "row", style: "margin-top:10px" },
    el("span", { class: "muted" }, "Auto-fill"),
    el("input", { type: "number", name: "months", value: "12", min: "1", max: "36", style: "max-width:80px" }),
    el("span", { class: "muted" }, "months forward from"),
    el("input", { type: "number", name: "lookback", value: "3", min: "1", max: "24", style: "max-width:80px" }),
    el("span", { class: "muted" }, "months of history average"),
    el("button", { class: "ghost small", type: "submit" }, "Populate"));
  populateForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(populateForm);
    const r = await post(`/api/budgets/${SELECTED_BUDGET_ID}/populate`, { months: Number(f.get("months")), lookback_months: Number(f.get("lookback")) });
    msg("m-global", `Created ${r.created} budget line(s) from history.`, "ok");
    await renderBudget();
  });
  linesCard.append(populateForm);
  box.append(linesCard);

  const varCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Budget vs. actual"));
  const today = (await get("/api/sim/today")).today;
  const varForm = el("form", { class: "row" },
    el("input", { type: "date", name: "start", value: today.slice(0, 8) + "01" }),
    el("input", { type: "date", name: "end", value: today }),
    el("button", { class: "small", type: "submit" }, "Run"));
  const varResults = el("div", { style: "margin-top:10px" });
  varForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(varForm);
    const v = await get(`/api/budgets/${SELECTED_BUDGET_ID}/variance?start=${f.get("start")}&end=${f.get("end")}`);
    clear(varResults);
    const t = el("table", {}, el("tr", {}, el("th", {}, "Category"), el("th", { class: "num" }, "Budget"), el("th", { class: "num" }, "Actual"), el("th", { class: "num" }, "Variance"), el("th", {}, "")));
    v.rows.forEach((r) => t.append(el("tr", {},
      el("td", {}, r.name), el("td", { class: "num" }, money(r.budget)), el("td", { class: "num" }, money(r.actual)),
      el("td", { class: "num " + (r.favorable ? "ok" : "bad") }, (r.favorable ? "+" : "") + money(r.variance).replace("$", "$") + (r.variance_pct != null ? ` (${(r.variance_pct * 100).toFixed(0)}%)` : "")),
      el("td", {}, badge(r.favorable ? "Favorable" : "Unfavorable", r.favorable ? "ok" : "bad")))));
    if (!v.rows.length) t.append(el("tr", {}, el("td", { colspan: "5", class: "muted" }, "No budget or actual activity in this range.")));
    varResults.append(t);
  });
  varCard.append(varForm, varResults);
  box.append(varCard);
}

// ---------- Forecast ----------
async function renderForecast() {
  const box = $("view-forecast");
  clear(box);
  const scenarios = await get("/api/forecast/scenarios");
  if (SELECTED_SCENARIO_ID && !scenarios.some((s) => s.id === SELECTED_SCENARIO_ID)) SELECTED_SCENARIO_ID = null;
  if (!SELECTED_SCENARIO_ID && scenarios.length) SELECTED_SCENARIO_ID = scenarios[0].id;

  const topCard = el("div", { class: "card" }, el("h2", {}, "Scenarios"),
    el("p", { class: "muted", style: "margin-top:-6px" }, "Every driver defaults to a historical average. Override any month to explore a what-if."));
  const picker = el("div", { class: "row" });
  scenarios.forEach((s) => picker.append(el("button", { class: s.id === SELECTED_SCENARIO_ID ? "small" : "ghost small", onclick: async () => { SELECTED_SCENARIO_ID = s.id; await renderForecast(); } }, `${s.name} (${s.months}mo)`)));
  topCard.append(picker);
  const newForm = el("form", { class: "row", style: "margin-top:10px" },
    el("input", { type: "text", name: "name", value: "Base case", style: "max-width:180px" }),
    el("input", { type: "number", name: "months", value: "12", min: "1", max: "36", style: "max-width:80px" }),
    el("button", { class: "small", type: "submit" }, "New scenario"));
  newForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(newForm);
    const s = await post("/api/forecast/scenarios", { name: f.get("name"), months: Number(f.get("months")) });
    SELECTED_SCENARIO_ID = s.id;
    await renderForecast();
  });
  topCard.append(newForm);
  box.append(topCard);
  if (!SELECTED_SCENARIO_ID) { box.append(el("p", { class: "muted" }, "Create a scenario to build a 3-statement forecast.")); return; }

  const { scenario, forecast: fc } = await get(`/api/forecast/scenarios/${SELECTED_SCENARIO_ID}`);

  if (fc.warnings.length) box.append(el("div", { class: "msg warn" }, fc.warnings.join(" ")));

  const dl = el("a", { href: `/api/forecast/scenarios/${scenario.id}/workbook` });
  const dlBtn = el("button", { class: "ghost small", type: "button" }, "Download Excel (live formulas)");
  dlBtn.addEventListener("click", () => { dl.click(); });
  const validateBtn = el("button", { class: "ghost small", type: "button" }, "Validate workbook");
  validateBtn.addEventListener("click", async () => {
    const v = await get(`/api/forecast/scenarios/${scenario.id}/validate`);
    msg("m-global", v.passed ? `Workbook validated (${v.engine}): all checks pass.` : "Workbook validation found issues: " + v.checks.filter((c) => !c.passed).map((c) => c.detail).join(" | "), v.passed ? "ok" : "bad");
  });
  const summaryCard = el("div", { class: "card", style: "margin-top:14px" },
    el("div", { class: "row" }, el("h2", {}, "Projection"), badge(fc.balances ? "Balances" : "OUT OF BALANCE", fc.balances ? "ok" : "bad")),
    el("div", { class: "row" }, dl, dlBtn, validateBtn));

  const t = el("table", {}, el("tr", {}, el("th", {}, "Month"), el("th", { class: "num" }, "Revenue"), el("th", { class: "num" }, "Expenses"), el("th", { class: "num" }, "Net income"), el("th", { class: "num" }, "Ending cash")));
  fc.income_statement.forEach((row, i) => t.append(el("tr", {},
    el("td", {}, row.month.slice(0, 7)), el("td", { class: "num" }, money(row.revenue)), el("td", { class: "num" }, money(row.expenses_total)),
    el("td", { class: "num " + (row.net_income.cents >= 0 ? "ok" : "bad") }, money(row.net_income)),
    el("td", { class: "num" }, money(fc.cash_flow[i].ending_cash)))));
  summaryCard.append(el("div", { class: "table-wrap", style: "margin-top:10px" }, t));
  box.append(summaryCard);

  const assumCard = el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Assumptions"),
    el("p", { class: "muted", style: "margin-top:-6px" }, "Edit any month; leave blank to keep the historical-average default. Applies to this scenario only."));
  const rows = [
    { key: "revenue_growth", label: "Revenue growth (MoM %)", basis: fc.assumptions.revenue_growth.basis, scale: 100 },
    { key: "dso_days", label: "Days sales outstanding", basis: "", scale: 1 },
    { key: "dpo_days", label: "Days payable outstanding", basis: "", scale: 1 },
  ];
  Object.entries(fc.assumptions.expenses).forEach(([code, d]) => rows.push({ key: `expense:${code}`, label: (CATEGORIES.find((c) => c.code === code) || {}).name || code, basis: d.basis, scale: d.mode === "pct_of_revenue" ? 100 : 1, isPct: d.mode === "pct_of_revenue" }));
  rows.forEach((r) => {
    const rowEl = el("div", { class: "card", style: "margin-bottom:8px" }, el("div", { class: "row" }, el("strong", {}, r.label), el("span", { class: "muted" }, r.basis || "")));
    const inputsRow = el("div", { class: "row" });
    const overrideVals = (scenario.overrides[r.key] || fc.months.map(() => null));
    const inputs = fc.months.map((m, i) => {
      const raw = overrideVals[i];
      const i2 = el("input", { type: "number", step: "0.01", placeholder: m.slice(0, 7), style: "max-width:100px" });
      if (raw !== null && raw !== undefined) i2.value = (raw * r.scale).toFixed(2);
      return i2;
    });
    inputs.forEach((i2) => inputsRow.append(i2));
    const saveBtn = el("button", { class: "ghost small", type: "button" }, "Save row");
    saveBtn.addEventListener("click", async () => {
      const values = inputs.map((i2) => (i2.value === "" ? null : Number(i2.value) / r.scale));
      await post(`/api/forecast/scenarios/${scenario.id}/overrides`, { driver_key: r.key, values });
      await renderForecast();
    });
    rowEl.append(inputsRow, saveBtn);
    assumCard.append(rowEl);
  });
  box.append(assumCard);
}

// ---------- Team ----------
async function renderTeam() {
  const box = $("view-team");
  clear(box);
  const users = await get("/api/users");
  const table = el("table", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Email"), el("th", {}, "Role")));
  users.forEach((u) => table.append(el("tr", {}, el("td", {}, u.name), el("td", { class: "muted" }, u.email), el("td", {}, badge(u.role, "chip")))));

  const form = el("form", { class: "card col", style: "margin-top:14px" }, el("h2", {}, "Add a team member"),
    el("p", { class: "muted", style: "margin-top:-6px" }, "Prototype: you set their initial password directly (no invite email)."),
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "Name"), el("input", { type: "text", name: "name", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Email"), el("input", { type: "email", name: "email", required: "" })),
      el("div", { class: "field" }, el("label", {}, "Role"), el("select", { name: "role" }, el("option", { value: "bookkeeper" }, "Bookkeeper"), el("option", { value: "employee" }, "Employee"))),
      el("div", { class: "field" }, el("label", {}, "Initial password"), el("input", { type: "text", name: "password", minlength: "6", required: "" })),
    ), el("div", { class: "row" }, el("button", { type: "submit" }, "Add")), el("div", { id: "m-team" }));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    try {
      await post("/api/users", { name: f.get("name"), email: f.get("email"), role: f.get("role"), password: f.get("password") });
      msg("m-team", "Team member added.", "ok");
      USERS = await get("/api/users");
      renderTeam();
    } catch (e) { msg("m-team", e.message, "bad"); }
  });

  box.append(el("div", { class: "card" }, el("h2", {}, "Team"), el("div", { class: "table-wrap" }, table)), form);
}

// ---------- init ----------
boot();
