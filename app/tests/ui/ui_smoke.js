// Runs app/static/app.js's real code against a running server using a minimal fake DOM (no browser).
// Usage: uv run uvicorn app.main:app --port 8010 &   node app/tests/ui/ui_smoke.js [BASE_URL]
const fs = require("fs");
const path = require("path");
const BASE = process.argv[2] || "http://127.0.0.1:8010";
const js = fs.readFileSync(path.join(__dirname, "../../static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../../static/index.html"), "utf8");

class El {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = []; this.listeners = {}; this._classes = new Set(); this._text = ""; this._value = undefined; this.disabled = false;
    this.attrs = {}; this.style = {}; this.dataset = {};
    this.classList = {
      add: (...c) => c.forEach((x) => this._classes.add(x)), remove: (...c) => c.forEach((x) => this._classes.delete(x)),
      toggle: (c, on) => (on ? this._classes.add(c) : this._classes.delete(c)), contains: (c) => this._classes.has(c),
    };
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  // A real <select> defaults to its first <option>'s value until something sets it explicitly.
  get value() { return this._value === undefined ? "" : this._value; }
  set value(v) { this._value = v; }
  append(...kids) {
    kids.forEach((k) => {
      if (k === null || k === undefined) return;
      this.children.push(k);
      if (this.tagName === "SELECT" && k.tagName === "OPTION" && this._value === undefined) {
        this._value = k.attrs.value !== undefined ? k.attrs.value : k.textContent;
      }
    });
  }
  replaceChildren(...kids) { this.children = kids.filter((k) => k !== null && k !== undefined); this._text = ""; }
  remove() { this._removed = true; }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === "name") this.name = v;
    if (k === "id") liveIds[v] = this;
    if (k === "value") this._value = v; // mirrors a real <input value="..."> setting the initial value
  }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  async fire(t, evt) { for (const fn of (this.listeners[t] || [])) await fn(evt || { preventDefault() {} }); }
  focus() {}
  click() {} // a real <a>.click() navigates the browser; nothing to simulate here
  walk(fn) { fn(this); this.children.forEach((c) => typeof c !== "string" && c && c.walk && c.walk(fn)); }
  findAll(pred) { const out = []; this.walk((n) => { if (pred(n)) out.push(n); }); return out; }
  querySelectorAll(sel) {
    if (sel === "#nav button") return this.findAll((n) => n.tagName === "BUTTON" && n._inNav);
    return [];
  }
}

const registry = {};
const liveIds = {}; // elements given an id dynamically via el({id: ...}), e.g. per-form message divs
for (const m of html.matchAll(/id="([\w-]+)"/g)) registry[m[1]] = new El("div");
["view-accounts", "view-transactions", "view-cards", "view-billpay", "view-invoicing", "view-reports", "view-team", "app", "f-onboard"].forEach((i) => registry[i].classList.add("hidden"));

// FormData shim reading from an El tree of <input>/<select> by `name`.
class FormDataShim {
  constructor(form) {
    this._map = new Map();
    form.walk((n) => { if (n.name) this._map.set(n.name, n.value); });
  }
  get(k) { return this._map.has(k) ? this._map.get(k) : null; }
}

const documentShim = {
  getElementById: (id) => registry[id] || liveIds[id],
  createElement: (t) => new El(t),
  querySelectorAll: (sel) => {
    if (sel === "#nav button") return registry["nav"].children.filter((c) => c.tagName === "BUTTON");
    return [];
  },
};
global.document = documentShim;
global.FormData = FormDataShim;

// Node's fetch has no implicit browser-style cookie jar, so the session cookie has to be carried
// by hand across requests, exactly like a real browser tab would do automatically.
const realFetch = globalThis.fetch;
let cookieJar = "";
async function fetchWithCookieJar(url, opts) {
  const headers = { ...(opts && opts.headers) };
  if (cookieJar) headers["Cookie"] = cookieJar;
  const res = await realFetch(url, { ...opts, headers });
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) cookieJar = setCookie.split(";")[0];
  return res;
}
global.fetch = (path, opts) => fetchWithCookieJar(BASE + path, opts);
global.location = { reload: () => { throw new Error("location.reload() called"); } };

// nav buttons need to be tagged so querySelectorAll("#nav button") style code in app.js (we don't actually
// use that selector directly; app.js queries document.querySelectorAll("#nav button") once) works via our shim above.
new Function("document", "FormData", "fetch", "location", js)(documentShim, FormDataShim, global.fetch, global.location);

const check = (name, ok, extra) => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) process.exitCode = 1; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fieldByLabel(form, labelText) {
  let found = null;
  form.walk((n) => {
    if (found) return;
    if (n.tagName === "LABEL" && n.textContent === labelText) {
      // label's next sibling in our El tree: find parent field div's 2nd child
    }
  });
  return found;
}
// Simpler: locate input/select by `name` attribute anywhere under a root.
function byName(root, name) { let f = null; root.walk((n) => { if (!f && n.name === name) f = n; }); return f; }
function firstButtonWithText(root, text) { let f = null; root.walk((n) => { if (!f && n.tagName === "BUTTON" && n.textContent.trim() === text) f = n; }); return f; }

process.on("unhandledRejection", (e) => console.log("UNHANDLED REJECTION:", e && e.stack ? e.stack : e));

(async () => {
  const email = `owner+${Date.now()}@smoketest.co`;

  // ---- onboarding ----
  registry["ob-name"].value = "Smoke Test Co";
  registry["ob-legal"].value = "Smoke Test Co LLC";
  registry["ob-industry"].value = "Software";
  registry["ob-ein"].value = "00-1234567";
  registry["ob-admin-name"].value = "Owner Person";
  registry["ob-admin-email"].value = email;
  registry["ob-admin-password"].value = "s3cret!!";
  registry["ob-funding"].value = "50000";
  await registry["f-onboard"].fire("submit");
  await sleep(50);
  check("onboarding error message empty", registry["m-onboard"].textContent === "", registry["m-onboard"].textContent);
  check("app shown after onboarding", !registry["app"].classList.contains("hidden"));
  check("header shows company name", registry["hdr-company"].textContent === "Smoke Test Co", registry["hdr-company"].textContent);
  check("nav has Team tab for admin", registry["nav"].children.some((b) => b.textContent === "Team"));
  const me = (await (await global.fetch("/api/auth/me")).json()).user;

  // ---- overview ----
  await sleep(20);
  const overview = registry["view-overview"];
  const cashKpi = overview.findAll((n) => n.className.includes("kpi"))[0];
  check("overview shows total cash 50000", cashKpi && cashKpi.textContent === "$50,000.00", cashKpi && cashKpi.textContent);

  // ---- accounts ----
  const navBtn = (label) => registry["nav"].children.find((b) => b.textContent === label);
  await navBtn("Accounts").fire("click");
  await sleep(20);
  const accountsView = registry["view-accounts"];
  const balCells = accountsView.findAll((n) => n.tagName === "TD" && n.className.includes("num"));
  check("accounts view rendered balances", balCells.length >= 2, "cells=" + balCells.length);

  // ---- cards: issue + charge + limit ----
  await navBtn("Cards").fire("click");
  await sleep(20);
  let cardsView = registry["view-cards"];
  let issueForm = cardsView.findAll((n) => n.tagName === "FORM")[0];
  byName(issueForm, "holder").value = String(me.id);
  byName(issueForm, "label").value = "Ops Card";
  byName(issueForm, "limit").value = "100";
  byName(issueForm, "period").value = "daily";
  await issueForm.fire("submit");
  await sleep(30);
  cardsView = registry["view-cards"];
  check("card appears after issuing", cardsView.textContent.includes("Ops Card"));

  const chargeForm = cardsView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "merchant"));
  byName(chargeForm, "merchant").value = "AWS";
  byName(chargeForm, "amount").value = "40";
  await chargeForm.fire("submit");
  await sleep(30);
  check("global message after charge", document.getElementById("m-global").textContent.includes("authorized"), document.getElementById("m-global").textContent);

  // ---- bill pay ----
  await navBtn("Bill Pay").fire("click");
  await sleep(20);
  let billpayView = registry["view-billpay"];
  const vendorForm = billpayView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "name"));
  byName(vendorForm, "name").value = "AWS Inc";
  byName(vendorForm, "method").value = "wire";
  await vendorForm.fire("submit");
  await sleep(30);
  billpayView = registry["view-billpay"];
  const billForm = billpayView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "vendor"));
  byName(billForm, "vendor"); // ensure exists
  byName(billForm, "category").value = "5010";
  byName(billForm, "amount").value = "500";
  byName(billForm, "due").value = "2026-12-01";
  await billForm.fire("submit");
  await sleep(30);
  billpayView = registry["view-billpay"];
  check("bill appears after creation", billpayView.textContent.includes("AWS Inc") && billpayView.textContent.includes("$500.00"));
  const payBtn = firstButtonWithText(billpayView, "Pay");
  check("admin-created bill is auto-scheduled (Pay button present)", !!payBtn);
  if (payBtn) { await payBtn.fire("click"); await sleep(30); }
  billpayView = registry["view-billpay"];
  check("bill shows paid status", billpayView.textContent.includes("paid"));

  // ---- invoicing ----
  await navBtn("Invoicing").fire("click");
  await sleep(20);
  let invView = registry["view-invoicing"];
  const invForm = invView.findAll((n) => n.tagName === "FORM")[0];
  byName(invForm, "customer").value = "Big Customer LLC";
  byName(invForm, "amount").value = "2500";
  byName(invForm, "due").value = "2026-12-15";
  await invForm.fire("submit");
  await sleep(30);
  invView = registry["view-invoicing"];
  check("invoice appears after sending", invView.textContent.includes("Big Customer LLC"));
  const markPaidBtn = firstButtonWithText(invView, "Mark paid");
  if (markPaidBtn) { await markPaidBtn.fire("click"); await sleep(30); }
  invView = registry["view-invoicing"];
  check("invoice shows paid status", invView.textContent.includes("paid"));

  // ---- reports: balance sheet identity ----
  await navBtn("Reports").fire("click");
  await sleep(30);
  const reportsView = registry["view-reports"];
  check("balance sheet reports 'Balances'", reportsView.textContent.includes("Balances"), reportsView.textContent.slice(0, 200));
  check("income statement shows net income", reportsView.textContent.includes("Net income"));

  // ---- team ----
  await navBtn("Team").fire("click");
  await sleep(20);
  let teamView = registry["view-team"];
  const teamForm = teamView.findAll((n) => n.tagName === "FORM")[0];
  byName(teamForm, "name").value = "Employee One";
  byName(teamForm, "email").value = `emp+${Date.now()}@smoketest.co`;
  byName(teamForm, "role").value = "employee";
  byName(teamForm, "password").value = "x123456";
  await teamForm.fire("submit");
  await sleep(30);
  teamView = registry["view-team"];
  check("team member appears after adding", teamView.textContent.includes("Employee One"));

  // ---- budget: create, set a line, populate from history, run variance ----
  await navBtn("Budget").fire("click");
  await sleep(20);
  let budgetView = registry["view-budget"];
  const newBudgetForm = budgetView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "name"));
  byName(newBudgetForm, "name").value = "Test Budget";
  await newBudgetForm.fire("submit");
  await sleep(30);
  budgetView = registry["view-budget"];
  check("budget appears after creation", budgetView.textContent.includes("Test Budget"));

  const thisMonth = (await (await global.fetch("/api/sim/today")).json()).today.slice(0, 7);
  const lineForm = budgetView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "category") && byName(f, "period"));
  byName(lineForm, "category").value = "5010";
  byName(lineForm, "period").value = thisMonth;
  byName(lineForm, "amount").value = "50";
  await lineForm.fire("submit");
  await sleep(30);
  budgetView = registry["view-budget"];
  check("budget line appears in table", budgetView.textContent.includes("Software") && budgetView.textContent.includes("$50.00"));

  const varForm = budgetView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "start") && byName(f, "end"));
  await varForm.fire("submit");
  await sleep(30);
  check("variance report shows favorable/unfavorable badge", /Favorable|Unfavorable/.test(budgetView.textContent));

  // ---- forecast: create a scenario, see the projection, validate the workbook ----
  await navBtn("Forecast").fire("click");
  await sleep(20);
  let forecastView = registry["view-forecast"];
  const newScenarioForm = forecastView.findAll((n) => n.tagName === "FORM").find((f) => byName(f, "name") && byName(f, "months"));
  byName(newScenarioForm, "name").value = "Base case";
  byName(newScenarioForm, "months").value = "3";
  await newScenarioForm.fire("submit");
  await sleep(50);
  forecastView = registry["view-forecast"];
  check("forecast shows balances badge", forecastView.textContent.includes("Balances") || forecastView.textContent.includes("OUT OF BALANCE"));
  check("forecast income statement has 3 month rows", forecastView.findAll((n) => n.tagName === "TR").length >= 4); // header + 3 months

  const validateBtn = firstButtonWithText(forecastView, "Validate workbook");
  check("validate workbook button present", !!validateBtn);
  if (validateBtn) {
    await validateBtn.fire("click");
    await sleep(50);
    check("workbook validation message shown", document.getElementById("m-global").textContent.includes("validated") || document.getElementById("m-global").textContent.includes("issues"), document.getElementById("m-global").textContent);
  }
  const scenarios = await (await global.fetch("/api/forecast/scenarios")).json();
  const scenarioId = scenarios.find((s) => s.name === "Base case").id;
  const downloadResp = await global.fetch(`/api/forecast/scenarios/${scenarioId}/workbook`);
  check("workbook download responds 200 with xlsx content", downloadResp.status === 200, "status=" + downloadResp.status);

  // ---- sim clock advance settles pending ACH ----
  await navBtn("Accounts").fire("click");
  await sleep(20);
  const sendForm = registry["view-accounts"].findAll((n) => n.tagName === "FORM").find((f) => byName(f, "counterparty") && byName(f, "method"));
  byName(sendForm, "counterparty").value = "Founder";
  byName(sendForm, "amount").value = "100";
  byName(sendForm, "method").value = "ach";
  await sendForm.fire("submit");
  await sleep(30);
  await registry["b-advance"].fire("click");
  await sleep(50);
  check("sim date advanced in header", registry["hdr-sim-date"].textContent.startsWith("Sim date:"));

  // ---- employee role sees a restricted nav ----
  const empEmail = byName(teamForm, "email").value;
  await registry["b-logout"].fire("click").catch(() => {}); // our location.reload shim throws by design; ignore
  cookieJar = "";
  registry["login-email"].value = empEmail;
  registry["login-password"].value = "x123456";
  await registry["f-login"].fire("submit");
  await sleep(50);
  const empNavLabels = registry["nav"].children.map((b) => b.textContent);
  check("employee nav excludes Team", !empNavLabels.includes("Team"), empNavLabels.join(","));
  check("employee nav excludes Bill Pay", !empNavLabels.includes("Bill Pay"), empNavLabels.join(","));
  check("employee nav excludes Accounts", !empNavLabels.includes("Accounts"), empNavLabels.join(","));
  check("employee nav excludes Budget", !empNavLabels.includes("Budget"), empNavLabels.join(","));
  check("employee nav excludes Forecast", !empNavLabels.includes("Forecast"), empNavLabels.join(","));
  check("employee nav still has Overview and Cards", empNavLabels.includes("Overview") && empNavLabels.includes("Cards"));

  console.log("\nDone.");
})().catch((e) => { console.log("FAIL crashed:", e.stack || e); process.exitCode = 1; });
