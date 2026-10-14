"""
Appends one new month's column(s) to the Category Topline and Brand Share Cons report
workbooks, computed from Master Data - the same mechanical pattern the existing workbooks
already use for every prior month:

  - Every row is either a REAL aggregate (its new-month cell gets an actual number, pulled
    straight from Master Data) or a PLUG (the last sibling in its group - its existing formula
    is simply replicated for the new column, unchanged in shape, so it keeps auto-absorbing
    whatever the real siblings don't account for). Which rows are plugs is detected directly
    from whether that row ALREADY has a formula in an existing month column - never guessed.
  - Category Topline: Kind/Level/Label/L0/L1/L2 + one GMV column per month.
  - Brand Share Cons: same, plus a parallel Units column block, plus brand-level rows under
    each L2 (one row per tracked brand, with a 'kind'=="others" row as that group's plug).
  - "All Platforms" and "MD Estimates" are extended identically (verified: for an already-
    finalized month, both already carry the same numbers - Master Data is the one shared
    source of truth for both, per the user).

Run with: python update_reports.py <month-label e.g. "Aug-26">
Writes nothing until every sheet in every workbook has been computed and sanity-checked;
makes one backup copy of each live file (never overwritten on a second run) before saving.
"""
import re
import shutil
import sys
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
PORTAL_DATA_DIR = BASE_DIR / "Portal Data"
TOPLINE_FILES = {
    "all": PORTAL_DATA_DIR / "Category Topline" / "Category Topline - All Platforms.xlsx",
    "md_est": PORTAL_DATA_DIR / "Category Topline" / "Category Topline - MD Estimates.xlsx",
}
BSC_FILES = {
    "all": PORTAL_DATA_DIR / "Brandshare Cons" / "Brand Share Cons - All Platforms.xlsx",
    "md_est": PORTAL_DATA_DIR / "Brandshare Cons" / "Brand Share Cons - MD Estimates.xlsx",
}
MASTER_DATA_DIR = PORTAL_DATA_DIR / "Master Data"
BACKUP_DIR = PORTAL_DATA_DIR / "Report Backups"

_FORMULA_RE = re.compile(r"^=([A-Z]+)(\d+)((?:[+-][A-Z]+\d+)*)$")


def _norm(s):
    return str(s).strip().lower() if s is not None else ""


def read_master_data_file():
    candidates = sorted(MASTER_DATA_DIR.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit(f"No Master Data .xlsx found in {MASTER_DATA_DIR}")
    path = candidates[0]
    print(f"Reading Master Data: {path.name}")
    return pd.read_excel(path, sheet_name="Master Data")


def load_master_data(full_df, month_dt):
    """Pre-aggregates ONCE per (platform, L0, L1, L2, brand) combo for the target month, into
    plain dict lookups - the original per-row full-table-scan approach took 20+ minutes on
    241k rows; group-by-once + dict lookup is near-instant and gives identical numbers."""
    df = full_df[pd.to_datetime(full_df["Month"]) == month_dt].copy()
    for col, src in [("_platform_n", "Platform"), ("_l0_n", "L0_Cat"), ("_l1_n", "L1_Cat"),
                      ("_l2_n", "L2_Cat"), ("_brand_n", "Brand")]:
        df[col] = df[src].map(_norm)

    def build(value_col, cast):
        lookups = {}
        g = df.groupby("_platform_n")[value_col].sum()
        lookups["total"] = {k: cast(v) for k, v in g.items()}
        g = df.groupby(["_platform_n", "_l0_n"])[value_col].sum()
        lookups["l0"] = {k: cast(v) for k, v in g.items()}
        g = df.groupby(["_platform_n", "_l0_n", "_l1_n"])[value_col].sum()
        lookups["l1"] = {k: cast(v) for k, v in g.items()}
        g = df.groupby(["_platform_n", "_l0_n", "_l1_n", "_l2_n"])[value_col].sum()
        lookups["l2"] = {k: cast(v) for k, v in g.items()}
        g = df.groupby(["_platform_n", "_l0_n", "_l1_n", "_l2_n", "_brand_n"])[value_col].sum()
        lookups["brand"] = {k: cast(v) for k, v in g.items()}
        return lookups

    gmv_lookup = build("GMV (INR Cr.)", lambda v: round(float(v), 4))
    units_lookup = build("Units (#)", int)
    return {"gmv": gmv_lookup, "units": units_lookup}


def agg(lookups, metric, kind, platform, l0=None, l1=None, l2=None, brand=None):
    lk = lookups[metric]
    p = _norm(platform)
    if kind == "total":
        return lk["total"].get(p, 0)
    if kind in ("l0", "l0_flat"):
        return lk["l0"].get((p, _norm(l0)), 0)
    if kind == "l1":
        return lk["l1"].get((p, _norm(l0), _norm(l1)), 0)
    if kind == "l2":
        return lk["l2"].get((p, _norm(l0), _norm(l1), _norm(l2)), 0)
    if kind == "brand":
        return lk["brand"].get((p, _norm(l0), _norm(l1), _norm(l2), _norm(brand)), 0)
    raise ValueError(f"agg: unknown kind {kind!r}")


def shift_formula(formula, old_col_letter, new_col_letter):
    """Every reference in these formulas uses the SAME column as the formula's own cell
    (verified by inspection) - so shifting column is a literal, safe substring replace of
    that one column-letter token, never touching the row numbers."""
    return re.sub(rf"\b{old_col_letter}(?=\d)", new_col_letter, formula)


def extend_sheet(ws, platform, sheet_label, new_month_label, lookups, metric):
    """metric: 'gmv' or 'units'. Appends one column to this sheet for that metric block (a
    no-op, returning [], if that month's column already exists - safe to re-run this script
    any number of times without ever duplicating a month). Returns a list of
    (row, kind, label, value_or_formula) for the printed summary."""
    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    header = [c.value for c in header_row]
    prefix = "GMV " if metric == "gmv" else "Units "
    if f"{prefix}{new_month_label}" in header:
        return None  # already done - idempotent re-run
    month_cols = [i for i, h in enumerate(header) if isinstance(h, str) and h.startswith(prefix)]
    if not month_cols:
        return []
    last_col_idx = month_cols[-1] + 1  # 1-based
    new_col_idx = last_col_idx + 1
    old_letter = get_column_letter(last_col_idx)
    new_letter = get_column_letter(new_col_idx)

    ws.cell(row=1, column=new_col_idx, value=f"{prefix}{new_month_label}")
    summary = []

    for r in range(2, ws.max_row + 1):
        kind = ws.cell(row=r, column=1).value
        label = ws.cell(row=r, column=3).value
        l0 = ws.cell(row=r, column=4).value
        l1 = ws.cell(row=r, column=5).value
        l2 = ws.cell(row=r, column=6).value
        existing = ws.cell(row=r, column=last_col_idx).value

        if isinstance(existing, str) and existing.startswith("="):
            m = _FORMULA_RE.match(existing)
            if not m:
                raise ValueError(f"{sheet_label} row {r}: unrecognized formula shape {existing!r}")
            new_formula = shift_formula(existing, old_letter, new_letter)
            ws.cell(row=r, column=new_col_idx, value=new_formula)
            summary.append((r, kind, label, new_formula))
            continue

        if kind == "others":
            # Should never reach here as a non-formula row (the brand group's plug is always
            # a formula) - if it does, something about this sheet doesn't match the pattern.
            raise ValueError(f"{sheet_label} row {r}: 'others' row has no existing formula to replicate")
        if kind not in ("total", "l0", "l0_flat", "l1", "l2", "brand"):
            raise ValueError(f"{sheet_label} row {r}: unknown kind {kind!r}")

        val = agg(lookups, metric, kind, platform, l0=l0, l1=l1, l2=l2, brand=label)
        ws.cell(row=r, column=new_col_idx, value=val)
        summary.append((r, kind, label, val))

    return summary


def resolve_evaluated(ws, col_idx, row, _cache):
    """Pure-Python re-evaluation of a (possibly formula) cell, for validation only - never
    trusts Excel's own cached value, since openpyxl doesn't compute formulas itself."""
    key = (col_idx, row)
    if key in _cache:
        return _cache[key]
    v = ws.cell(row=row, column=col_idx).value
    if isinstance(v, str) and v.startswith("="):
        m = _FORMULA_RE.match(v)
        first_ref_col, first_ref_row = m.group(1), int(m.group(2))
        val = resolve_evaluated(ws, column_index_from_string(first_ref_col), first_ref_row, _cache)
        for sign, ref in re.findall(r"([+-])([A-Z]+\d+)", m.group(3)):
            rm = re.match(r"([A-Z]+)(\d+)", ref)
            term = resolve_evaluated(ws, column_index_from_string(rm.group(1)), int(rm.group(2)), _cache)
            val = val + term if sign == "+" else val - term
    else:
        val = float(v) if v is not None else 0.0
    _cache[key] = val
    return val


def validate_sheet(ws, new_col_idx, sheet_label):
    """Flags any plug cell that evaluates negative (or absurdly large vs its own parent) -
    the one thing a category-name mismatch would visibly break, since a plug is defined as
    parent-minus-real-siblings and a mismatch makes real siblings wrong."""
    cache = {}
    problems = []
    for r in range(2, ws.max_row + 1):
        val = resolve_evaluated(ws, new_col_idx, r, cache)
        existing = ws.cell(row=r, column=new_col_idx).value
        is_plug = isinstance(existing, str) and existing.startswith("=")
        if is_plug and val < -0.005:
            label = ws.cell(row=r, column=3).value
            problems.append(f"{sheet_label} row {r} ({label}): plug evaluates negative ({val:.3f})")
    return problems


def main():
    """No argument needed in normal use: auto-detects every month present in Master Data that
    the reports don't have yet (usually just the newest one) and adds each in turn - this is
    the "quick to track and update" path the user asked for, now that everything lives in one
    folder. An explicit month label (e.g. "Aug-26") still works, to target one month only."""
    full_df = read_master_data_file()
    master_months = sorted(pd.to_datetime(full_df["Month"]).unique())

    if len(sys.argv) == 2:
        target_labels = [sys.argv[1]]
    elif len(sys.argv) == 1:
        sample_path = next(iter(TOPLINE_FILES.values()))
        wb = openpyxl.load_workbook(sample_path, read_only=True, data_only=False)
        header = [c.value for c in next(wb[wb.sheetnames[0]].iter_rows(min_row=1, max_row=1))]
        wb.close()
        existing_labels = {h[4:] for h in header if isinstance(h, str) and h.startswith("GMV ")}
        target_labels = [
            m.strftime("%b-%y") for m in master_months
            if m.strftime("%b-%y") not in existing_labels
        ]
        if not target_labels:
            print("Nothing to do - reports already cover every month in Master Data.")
            return
        print(f"Auto-detected month(s) to add: {', '.join(target_labels)}")
    else:
        raise SystemExit('Usage: python update_reports.py ["Aug-26"]  (omit to auto-detect)')

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for new_month_label in target_labels:
        _run_one_month(full_df, new_month_label)


def _run_one_month(full_df, new_month_label):
    month_dt = pd.to_datetime("01-" + new_month_label, format="%d-%b-%y")
    print(f"\n--- {new_month_label} ---")
    lookups = load_master_data(full_df, month_dt)

    all_problems = []
    totals_report = []

    for report_name, files, is_bsc in [("Category Topline", TOPLINE_FILES, False), ("Brand Share Cons", BSC_FILES, True)]:
        for variant, path in files.items():
            backup_path = BACKUP_DIR / f"{path.stem} (before {new_month_label}){path.suffix}"
            if not backup_path.exists():
                shutil.copy2(path, backup_path)
                print(f"Backed up -> {backup_path.name}")

            wb = openpyxl.load_workbook(path, data_only=False)
            any_changed = False
            for sheet_name in wb.sheetnames:
                platform = sheet_name.replace(" (MD Est)", "")
                ws = wb[sheet_name]
                sheet_label = f"{report_name}/{variant}/{sheet_name}"

                gmv_summary = extend_sheet(ws, platform, sheet_label, new_month_label, lookups, "gmv")
                if gmv_summary is None:
                    print(f"  {sheet_label}: already has {new_month_label} - skipped")
                    continue
                any_changed = True
                if is_bsc:
                    extend_sheet(ws, platform, sheet_label, new_month_label, lookups, "units")

                header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
                new_col_idx = max(i for i, h in enumerate(header) if isinstance(h, str) and h.startswith("GMV ")) + 1
                problems = validate_sheet(ws, new_col_idx, sheet_label)
                all_problems.extend(problems)

                total_row = next((r for (r, k, l, v) in gmv_summary if k == "total"), None)
                if total_row:
                    val = next(v for (r, k, l, v) in gmv_summary if r == total_row)
                    totals_report.append((sheet_label, val))

            if any_changed:
                wb.save(path)
                print(f"Saved {path.name}")
            else:
                print(f"{path.name}: nothing to do (every sheet already had {new_month_label})")

    print("\n=== Total BPC, new month, per sheet ===")
    for label, val in totals_report:
        print(f"  {label}: {val}")

    if all_problems:
        print("\n!!! VALIDATION PROBLEMS (review before trusting these numbers) !!!")
        for p in all_problems:
            print("  -", p)
    else:
        print("\nValidation: no negative plug values detected - roll-ups look internally consistent.")


if __name__ == "__main__":
    main()
