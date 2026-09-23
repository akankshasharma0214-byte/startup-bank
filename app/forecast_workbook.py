"""Excel export of a 3-statement forecast, with live formulas (not pasted values) so an accountant
can open it, see exactly how every number is derived, and re-run 'what if' by editing a blue input
cell. Forecast cells never contain a hardcoded number -- they always reference the Assumptions
sheet or a prior column, the same rule enforced (and tested) in the fin-model-agent project this
pattern is adapted from."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter as L
from sqlalchemy.orm import Session

from .ledger import get_account

DISCLAIMER = (
    "Illustrative planning tool, not accounting advice. Figures in USD; forecast is a mechanical"
    " projection of historical trends, not a prediction."
)
BLUE, BLACK, BOLD, RED_BOLD = Font(color="0000FF"), Font(color="000000"), Font(bold=True), Font(bold=True, color="C00000")
NUM, PCT, DAYS_FMT = '#,##0.00;(#,##0.00);"-"', "0.0%", "0.0"


def _category_names(db: Session, company_id: int, codes: list[str]) -> dict[str, str]:
    return {code: get_account(db, company_id, code).name for code in codes}


def _all_expense_codes(forecast: dict) -> list[str]:
    codes = set(forecast["resolved_assumptions"]["expenses"])
    for row in forecast["history"]:
        codes |= set(row["expenses_by_category"])
    for row in forecast["income_statement"]:
        codes |= set(row["expenses_by_category_cents"])
    return sorted(codes)


def _put(ws, ref, value, font=BLACK, fmt=NUM):
    c = ws[ref]
    c.value = value
    c.font = font
    if fmt:
        c.number_format = fmt
    return c


def _headers(ws, months, cols, row=3):
    for col, m in zip(cols, months, strict=True):
        c = ws[f"{col}{row}"]
        c.value = m.strftime("%b %Y")
        c.font = BOLD
        c.alignment = Alignment(horizontal="right")
    ws.column_dimensions["A"].width = 34
    for col in cols:
        ws.column_dimensions[col].width = 13


def build_forecast_workbook(db: Session, company_id: int, forecast: dict, output_path: str) -> dict:
    """Write `forecast` (the dict returned by forecasting.build_forecast) to an .xlsx file with
    live formulas. Returns {path}."""
    hc_n, fc_n = len(forecast["history_months"]), len(forecast["months"])
    HC = [L(2 + i) for i in range(hc_n)]  # Historicals sheet: its own narrower column range
    ALLC = [L(2 + i) for i in range(hc_n + fc_n)]  # IS/BS/CF/Checks: history + forecast, continuous
    FC = ALLC[hc_n:]
    all_months = forecast["history_months"] + forecast["months"]

    codes = _all_expense_codes(forecast)
    names = _category_names(db, company_id, codes)
    E = len(codes)

    wb = Workbook()
    wh = wb.active
    wh.title = "Historicals"
    wa, wi, wbs, wc, wk = (wb.create_sheet(n) for n in ["Assumptions", "IS", "BS", "CF", "Checks"])

    # ---- row maps ----
    H = {
        "rev": 4,
        **{f"exp_{c}": 5 + i for i, c in enumerate(codes)},
        "total_exp": 5 + E,
        "net_income": 6 + E,
        "cash": 7 + E,
        "ar": 8 + E,
        "ap": 9 + E,
        "equity": 10 + E,
    }
    IS = {"rev": 4, **{f"exp_{c}": 5 + i for i, c in enumerate(codes)}, "total_exp": 5 + E, "net_income": 6 + E}
    BS = {"cash": 4, "ar": 5, "ap": 6, "equity": 7, "total_assets": 8, "total_le": 9}
    CF = {"net_income": 4, "incr_ar": 5, "incr_ap": 6, "operating_cf": 7, "beg_cash": 8, "end_cash": 9}
    AS = {"growth": 4, "dso": 5, "dpo": 6, **{f"exp_{c}": 7 + i for i, c in enumerate(codes)}}

    # ---- Historicals (blue inputs: the real ledger data) ----
    wh["A1"] = f"Historicals: last {hc_n} month(s) of actuals"
    wh["A1"].font = BOLD
    wh["A2"] = DISCLAIMER
    _headers(wh, forecast["history_months"], HC)
    wh[f"A{H['rev']}"] = "Revenue"
    for c in codes:
        wh[f"A{H[f'exp_{c}']}"] = names[c]
    wh[f"A{H['total_exp']}"], wh[f"A{H['net_income']}"] = "Total expenses", "Net income"
    wh[f"A{H['cash']}"], wh[f"A{H['ar']}"], wh[f"A{H['ap']}"] = "Ending cash", "Accounts receivable", "Accounts payable"
    wh[f"A{H['equity']}"] = "Total equity"
    for r in (H["total_exp"], H["net_income"]):
        wh[f"A{r}"].font = BOLD
    for i, row in enumerate(forecast["history"]):
        col = HC[i]
        _put(wh, f"{col}{H['rev']}", row["revenue_cents"] / 100, BLUE)
        for c in codes:
            _put(wh, f"{col}{H[f'exp_{c}']}", row["expenses_by_category"].get(c, 0) / 100, BLUE)
        _put(
            wh,
            f"{col}{H['total_exp']}",
            f"=SUM({col}{H['exp_' + codes[0]] if codes else H['rev']}:{col}{H['total_exp'] - 1})" if codes else "=0",
        )
        _put(wh, f"{col}{H['net_income']}", f"={col}{H['rev']}-{col}{H['total_exp']}")
        _put(wh, f"{col}{H['cash']}", row["ending_cash_cents"] / 100, BLUE)
        _put(wh, f"{col}{H['ar']}", row["ar_cents"] / 100, BLUE)
        _put(wh, f"{col}{H['ap']}", row["ap_cents"] / 100, BLUE)
        _put(wh, f"{col}{H['equity']}", row["equity_cents"] / 100, BLUE)

    # ---- Assumptions ----
    wa["A1"] = DISCLAIMER
    wa["A1"].font = RED_BOLD
    wa["A2"] = "Blue = input. Black = formula. History columns show the actual derived ratio for context; forecast columns are editable."
    _headers(wa, all_months, ALLC)
    wa[f"A{AS['growth']}"] = "Revenue growth (MoM %)"
    wa[f"A{AS['dso']}"] = "Days sales outstanding (AR)"
    wa[f"A{AS['dpo']}"] = "Days payable outstanding (AP)"
    ra = forecast["resolved_assumptions"]
    for c in codes:
        mode = ra["expenses"].get(c, {}).get("mode", "flat")
        wa[f"A{AS[f'exp_{c}']}"] = f"{names[c]} ({'% of revenue' if mode == 'pct_of_revenue' else '$ flat/mo'})"
    for i, col in enumerate(FC):
        _put(wa, f"{col}{AS['growth']}", ra["revenue_growth"][i], BLUE, PCT)
        _put(wa, f"{col}{AS['dso']}", ra["dso_days"][i], BLUE, DAYS_FMT)
        _put(wa, f"{col}{AS['dpo']}", ra["dpo_days"][i], BLUE, DAYS_FMT)
        for c in codes:
            driver = ra["expenses"].get(c)
            val = driver["values"][i] if driver else 0.0
            fmt = PCT if driver and driver["mode"] == "pct_of_revenue" else NUM
            _put(wa, f"{col}{AS[f'exp_{c}']}", val, BLUE, fmt)
    for i, col in enumerate(HC):  # historical context: the actual ratio that period, as a formula
        prev = HC[i - 1] if i else None
        if prev:
            _put(wa, f"{col}{AS['growth']}", f"=IFERROR(Historicals!{col}{H['rev']}/Historicals!{prev}{H['rev']}-1,0)", BLACK, PCT)
        _put(wa, f"{col}{AS['dso']}", f"=IFERROR(Historicals!{col}{H['ar']}/Historicals!{col}{H['rev']}*30,0)", BLACK, DAYS_FMT)
        _put(wa, f"{col}{AS['dpo']}", f"=IFERROR(Historicals!{col}{H['ap']}/Historicals!{col}{H['total_exp']}*30,0)", BLACK, DAYS_FMT)
        for c in codes:
            mode = ra["expenses"].get(c, {}).get("mode", "flat")
            formula = (
                f"=IFERROR(Historicals!{col}{H[f'exp_{c}']}/Historicals!{col}{H['rev']},0)"
                if mode == "pct_of_revenue"
                else f"=Historicals!{col}{H[f'exp_{c}']}"
            )
            _put(wa, f"{col}{AS[f'exp_{c}']}", formula, BLACK, PCT if mode == "pct_of_revenue" else NUM)

    # ---- IS: continuous history + forecast, history columns pass through from Historicals ----
    wi["A1"], wi["A1"].font = "Income statement", BOLD
    wi["A2"] = DISCLAIMER
    _headers(wi, all_months, ALLC)
    wi[f"A{IS['rev']}"] = "Revenue"
    for c in codes:
        wi[f"A{IS[f'exp_{c}']}"] = names[c]
    wi[f"A{IS['total_exp']}"], wi[f"A{IS['net_income']}"] = "Total expenses", "Net income"
    for r in (IS["total_exp"], IS["net_income"]):
        wi[f"A{r}"].font = BOLD
    for col in HC:
        _put(wi, f"{col}{IS['rev']}", f"=Historicals!{col}{H['rev']}")
        for c in codes:
            _put(wi, f"{col}{IS[f'exp_{c}']}", f"=Historicals!{col}{H[f'exp_{c}']}")
    for i, col in enumerate(FC):
        prev = ALLC[hc_n + i - 1]
        _put(wi, f"{col}{IS['rev']}", f"={prev}{IS['rev']}*(1+Assumptions!{col}{AS['growth']})")
        for c in codes:
            driver = ra["expenses"].get(c)
            formula = (
                f"={col}{IS['rev']}*Assumptions!{col}{AS[f'exp_{c}']}"
                if driver and driver["mode"] == "pct_of_revenue"
                else f"=Assumptions!{col}{AS[f'exp_{c}']}"
            )
            _put(wi, f"{col}{IS[f'exp_{c}']}", formula)
    for col in ALLC:
        _put(wi, f"{col}{IS['total_exp']}", f"=SUM({col}{IS['exp_' + codes[0]]}:{col}{IS[f'exp_{codes[-1]}']})" if codes else "=0")
        _put(wi, f"{col}{IS['net_income']}", f"={col}{IS['rev']}-{col}{IS['total_exp']}")

    # ---- BS ----
    wbs["A1"], wbs["A1"].font = "Balance sheet", BOLD
    wbs["A2"] = DISCLAIMER
    _headers(wbs, all_months, ALLC)
    wbs[f"A{BS['cash']}"], wbs[f"A{BS['ar']}"], wbs[f"A{BS['ap']}"], wbs[f"A{BS['equity']}"] = (
        "Cash",
        "Accounts receivable",
        "Accounts payable",
        "Equity",
    )
    wbs[f"A{BS['total_assets']}"], wbs[f"A{BS['total_le']}"] = "Total assets", "Total liabilities & equity"
    for r in (BS["total_assets"], BS["total_le"]):
        wbs[f"A{r}"].font = BOLD
    for col in HC:
        _put(wbs, f"{col}{BS['cash']}", f"=Historicals!{col}{H['cash']}")
        _put(wbs, f"{col}{BS['ar']}", f"=Historicals!{col}{H['ar']}")
        _put(wbs, f"{col}{BS['ap']}", f"=Historicals!{col}{H['ap']}")
        _put(wbs, f"{col}{BS['equity']}", f"=Historicals!{col}{H['equity']}")
    for i, col in enumerate(FC):
        prev = ALLC[hc_n + i - 1]
        _put(wbs, f"{col}{BS['cash']}", f"={prev}{BS['cash']}+CF!{col}{CF['operating_cf']}")
        _put(wbs, f"{col}{BS['ar']}", f"=IS!{col}{IS['rev']}*Assumptions!{col}{AS['dso']}/30")
        _put(wbs, f"{col}{BS['ap']}", f"=IS!{col}{IS['total_exp']}*Assumptions!{col}{AS['dpo']}/30")
        _put(wbs, f"{col}{BS['equity']}", f"={prev}{BS['equity']}+IS!{col}{IS['net_income']}")
    for col in ALLC:
        _put(wbs, f"{col}{BS['total_assets']}", f"={col}{BS['cash']}+{col}{BS['ar']}")
        _put(wbs, f"{col}{BS['total_le']}", f"={col}{BS['ap']}+{col}{BS['equity']}")

    # ---- CF ----
    wc["A1"], wc["A1"].font = "Cash flow (operating only; no financing/investing assumed in the forecast)", BOLD
    wc["A2"] = DISCLAIMER
    _headers(wc, forecast["months"], FC)  # CF is forecast-only: there is no history to roll forward from before day 1
    wc[f"A{CF['net_income']}"], wc[f"A{CF['incr_ar']}"], wc[f"A{CF['incr_ap']}"] = "Net income", "(Increase) in AR", "Increase in AP"
    wc[f"A{CF['operating_cf']}"], wc[f"A{CF['beg_cash']}"], wc[f"A{CF['end_cash']}"] = (
        "Operating cash flow",
        "Beginning cash",
        "Ending cash",
    )
    for r in (CF["operating_cf"], CF["end_cash"]):
        wc[f"A{r}"].font = BOLD
    for i, col in enumerate(FC):
        prev = ALLC[hc_n + i - 1]
        _put(wc, f"{col}{CF['net_income']}", f"=IS!{col}{IS['net_income']}")
        _put(wc, f"{col}{CF['incr_ar']}", f"=BS!{prev}{BS['ar']}-BS!{col}{BS['ar']}")
        _put(wc, f"{col}{CF['incr_ap']}", f"=BS!{col}{BS['ap']}-BS!{prev}{BS['ap']}")
        _put(wc, f"{col}{CF['operating_cf']}", f"=SUM({col}{CF['net_income']}:{col}{CF['incr_ap']})")
        _put(wc, f"{col}{CF['beg_cash']}", f"=BS!{prev}{BS['cash']}")
        _put(wc, f"{col}{CF['end_cash']}", f"={col}{CF['beg_cash']}+{col}{CF['operating_cf']}")

    # ---- Checks ----
    wk["A1"], wk["A1"].font = "Integrity checks", BOLD
    wk["A2"], wk["B2"] = "Tolerance (USD)", 0.5
    wk["B2"].font = BLUE
    _headers(wk, all_months, ALLC)
    wk["A5"], wk["A6"] = "Total assets less total liabilities & equity", "Balance check"
    for col in ALLC:
        _put(wk, f"{col}5", f"=BS!{col}{BS['total_assets']}-BS!{col}{BS['total_le']}")
        _put(wk, f"{col}6", f'=IF(ABS({col}5)<=$B$2,"OK","ERROR")', fmt=None)
    wk["A8"] = "Overall"
    _put(wk, "B8", f'=IF(COUNTIF(B6:{ALLC[-1]}6,"ERROR")=0,"ALL CHECKS PASS","CHECK FAILED")', fmt=None)
    wk["A10"] = DISCLAIMER

    for ws in wb.worksheets:
        ws.freeze_panes = "B4"

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"path": str(out), "sheets": wb.sheetnames, "history_cols": HC, "forecast_cols": FC}
