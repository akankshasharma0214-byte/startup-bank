"""Deterministic validation of an exported forecast workbook: recalculate it (LibreOffice if
available, else the `formulas` library) and check the numbers that came back, not just the formula
text. Mirrors the validator pattern from the fin-model-agent project."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import Token

ERRORS = ("#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#N/A", "#NUM!", "#NULL!")
ALLOWED_CONSTANTS = {"0", "1", "20", "30"}  # structural only: (1+g), SUM ranges, and the 30-day/mo convention
STATEMENTS = ("IS", "BS", "CF")


class _CaseInsensitiveWB:
    """The `formulas` engine upper-cases sheet names on write; hide that from callers."""

    def __init__(self, wb):
        self.wb = wb
        self._by_upper = {n.upper(): n for n in wb.sheetnames}

    def __getitem__(self, name):
        return self.wb[self._by_upper[name.upper()]]

    @property
    def worksheets(self):
        return self.wb.worksheets


def _recalc_libreoffice(path: Path, outdir: Path):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    subprocess.run(
        [soffice, "--headless", "--calc", "--convert-to", "xlsx", "--outdir", str(outdir), str(path)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return outdir / path.name


def _recalc_formulas(path: Path, outdir: Path):
    import formulas

    xl = formulas.ExcelModel().loads(str(path)).finish()
    xl.calculate()
    xl.write(dirpath=str(outdir))
    written = [p for p in outdir.iterdir() if p.suffix.lower() == ".xlsx"]
    return written[0] if written else None


def recalculate(path: Path):
    tmp = Path(tempfile.mkdtemp(prefix="forecast_recalc_"))
    last_error = "no engine available"
    for engine, fn in (("libreoffice", _recalc_libreoffice), ("formulas", _recalc_formulas)):
        try:
            out = fn(path, tmp)
        except Exception as e:
            last_error = f"{engine}: {e}"
            continue
        if out and out.exists():
            return _CaseInsensitiveWB(load_workbook(out, data_only=True)), engine
    raise RuntimeError(f"No recalculation engine succeeded ({last_error})")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def check_balances(vals, all_cols, checks_row_assets=5) -> tuple[bool, str]:
    ck = vals["Checks"]
    bad = []
    for col in all_cols:
        v = _num(ck[f"{col}{checks_row_assets}"].value)
        if v is None or abs(v) > 0.5:
            bad.append(f"{ck[f'{col}3'].value}: off by {v}")
    return not bad, ("Balances in every period (tolerance $0.50)." if not bad else "; ".join(bad))


def check_no_errors(vals) -> tuple[bool, str]:
    bad = []
    for ws in vals.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.strip() in ERRORS:
                    bad.append(f"{ws.title}!{c.coordinate}={c.value}")
    return not bad, ("No Excel errors." if not bad else "; ".join(bad[:20]))


def check_no_hardcodes(wb, forecast_cols: list[str]) -> tuple[bool, str]:
    bad = []
    for name in STATEMENTS:
        ws = wb[name]
        for r in range(4, ws.max_row + 1):
            for col in forecast_cols:
                v = ws[f"{col}{r}"].value
                if v is None:
                    continue
                if not (isinstance(v, str) and v.startswith("=")):
                    bad.append(f"{name}!{col}{r} is a constant ({v!r})")
                    continue
                for t in Tokenizer(v).items:
                    if t.type == Token.OPERAND and t.subtype == Token.NUMBER and t.value not in ALLOWED_CONSTANTS:
                        bad.append(f"{name}!{col}{r} contains constant {t.value}")
    return not bad, ("Forecast cells are formulas with no hardcoded constants." if not bad else "; ".join(bad[:20]))


def validate_forecast_workbook(path: str, history_cols: list[str], forecast_cols: list[str]) -> dict:
    p = Path(path)
    if not p.exists():
        return {"passed": False, "error": "file_not_found", "checks": []}
    wb = load_workbook(p)
    try:
        vals, engine = recalculate(p)
    except Exception as e:
        return {"passed": False, "error": "recalc_failed", "detail": str(e), "checks": []}
    all_cols = history_cols + forecast_cols
    checks = [
        {"name": n, "passed": bool(ok), "detail": d}
        for n, (ok, d) in [
            ("balance_sheet_balances", check_balances(vals, all_cols)),
            ("no_excel_errors", check_no_errors(vals)),
            ("no_hardcoded_forecast_constants", check_no_hardcodes(wb, forecast_cols)),
        ]
    ]
    return {"passed": all(c["passed"] for c in checks), "engine": engine, "checks": checks}
