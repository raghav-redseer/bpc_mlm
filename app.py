"""
L'Oreal BPC Tagging Engine - local web app.

Wraps the existing, hand-tuned Python engine (engine/pipeline_v6.py,
engine/bundle_detect.py, engine/keyword_repo.py) with a FastAPI backend and
serves the vanilla HTML/JS frontend from static/. The tagging logic itself is
never modified here - this app only calls into it, introspects its rule
tables for the glossary view, and captures rule *suggestions* to
pending_rules.json for manual review (see engine/pipeline_v6.py priority
order before wiring any suggestion in by hand).

Run with:
    python app.py
or:
    uvicorn app:app --reload
"""
import csv
import hashlib
import hmac
import io
import json
import math
import re
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import openpyxl
from openpyxl.utils import get_column_letter
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

BASE_DIR = Path(__file__).resolve().parent
ENGINE_DIR = BASE_DIR / "engine"
sys.path.insert(0, str(ENGINE_DIR))

# All of the portal's OWN generated state/output - AND now Master Data, the single source of
# truth for everything the portal reads - lives in this one dedicated, protected folder, separate
# from everything else in this project directory. See admin_auth.json for why it's protected.
PORTAL_DATA_DIR = BASE_DIR / "Portal Data"
PORTAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

import bundle_detect as bd  # noqa: E402
import keyword_repo as kw  # noqa: E402
import pipeline_v6 as p6  # noqa: E402
from bundle_detect import tag_sku_bundle_aware  # noqa: E402
import category_mapping as cm  # noqa: E402

PENDING_FILE = BASE_DIR / "pending_rules.json"
APPROVED_FILE = BASE_DIR / "approved_rules.json"
GOLDEN_FILE = BASE_DIR / "golden_tests.json"
SKU_HISTORY_FILE = BASE_DIR / "sku_tag_history.json"

# --------------------------------------------------------------------------
# Report workbooks (Category Topline / Brand Share Cons / Master Data) - the
# tabs that VIEW these live Excel files, never edit them. Source of truth
# stays the .xlsx on disk; this app only reads it.
# --------------------------------------------------------------------------
TOPLINE_FILES = {
    "all": PORTAL_DATA_DIR / "Category Topline" / "Category Topline - All Platforms.xlsx",
    "md_est": PORTAL_DATA_DIR / "Category Topline" / "Category Topline - MD Estimates.xlsx",
}
BSC_FILES = {
    "all": PORTAL_DATA_DIR / "Brandshare Cons" / "Brand Share Cons - All Platforms.xlsx",
    "md_est": PORTAL_DATA_DIR / "Brandshare Cons" / "Brand Share Cons - MD Estimates.xlsx",
}
# Master Data lives inside Portal Data now, not the loose "Master Data/" folder - the portal
# reads ONLY from here, auto-discovering whichever .xlsx is there (most recently modified wins)
# so replacing the file each month (any filename) is all that's needed - no path to hand-edit.
MASTER_DATA_DIR = PORTAL_DATA_DIR / "Master Data"
MASTER_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_master_data_file():
    candidates = sorted(MASTER_DATA_DIR.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None

# Every Manual Change commit appends here - a durable, human-readable audit trail independent of
# the workbooks themselves (which only ever hold the latest value, never their own history).
CHANGE_LOG_FILE = BASE_DIR / "Change Log" / "manual_edit_log.csv"

# Legacy default (pre-Portal Data consolidation) for bulk-tag's optional "save a copy to disk"
# feature - no longer the default (see TAGGED_EXPERT_DATA_DIR below), kept only so files already
# saved there over time aren't orphaned from the constant that locates them.
RETAGGED_DIR = BASE_DIR / "Expert Data" / "Retagged Expert Files"
RETAGGED_DIR.mkdir(parents=True, exist_ok=True)

# All raw expert-data source files now live under Portal Data (see "Raw expert data") - this is
# the equivalent home for tagged OUTPUT going forward: one subfolder per month, created lazily on
# first save. "Save a copy to disk" defaults to UNCHECKED and, when checked, defaults its folder
# path here (see /tag-bulk/default-folder) rather than to RETAGGED_DIR above.
TAGGED_EXPERT_DATA_DIR = PORTAL_DATA_DIR / "Tagged Expert Data"
TAGGED_EXPERT_DATA_DIR.mkdir(parents=True, exist_ok=True)

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _safe_filename_part(text):
    return _UNSAFE_FILENAME_CHARS.sub("_", text).strip()

# Methods that mean "the engine wasn't confident / needs a human" - used for the
# bulk-tag summary counts (clean vs flagged) shown before download. Bundle-ManualReview-
# MultiCategory (a genuine 2+-category bundle) always stays flagged - see Manual_Review_
# Required below. The others get a chance to resolve via FALLBACK_METHODS first.
FLAGGED_METHODS = {
    "Unmapped", "Ambiguous-NoModifier", "Ambiguous-Gap",
    "Bundle-ManualReview-NoSignal", "Bundle-ManualReview-MultiCategory",
}

# Methods where the engine is stuck (not just uncertain) - when a prior/existing tag is
# available for this exact SKU name, prefer it over leaving the row unresolved. Deliberately
# excludes Bundle-ManualReview-MultiCategory: a genuine 2+-category bundle is a judgment call
# about which category is "primary," not something a borrowed tag safely resolves - those
# stay flagged for manual review instead (see Manual_Review_Required column).
FALLBACK_METHODS = {"Unmapped", "Ambiguous-NoModifier", "Ambiguous-Gap", "Bundle-ManualReview-NoSignal"}

app = FastAPI(title="L'Oreal")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # This tool's CSS/JS changes frequently while it's being actively worked on - without this,
    # a browser can silently keep serving an old cached copy of style.css/app.js after an edit
    # (no explicit Cache-Control was being sent, so browsers fall back to their own heuristic
    # caching), which looks exactly like "the fix didn't work" even though the server is correct.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

# In-memory job store for bulk-tagging progress. Fine for a single-user local
# tool; not meant to survive a server restart.
JOBS = {}


def _static_version():
    """Same 'newest mtime' signal /version reports - reused here as a cache-busting query string
    on style.css/app.js so a plain refresh (not just a hard-refresh) always picks up the latest
    static files. See /version's own docstring: a stale tab silently running old JS/CSS has been
    this project's single biggest source of 'I fixed it but it's still broken' reports."""
    files = [BASE_DIR / "app.py", *((BASE_DIR / "static").glob("*"))]
    newest = max((f.stat().st_mtime for f in files if f.is_file()), default=0)
    return int(newest)


@app.get("/", response_class=HTMLResponse)
def root():
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    v = _static_version()
    html = html.replace('/static/style.css"', f'/static/style.css?v={v}"')
    html = html.replace('/static/app.js"', f'/static/app.js?v={v}"')
    return html


@app.get("/version")
def version():
    """Lets the page show 'is my tab running the latest code' - the single biggest source of
    confusing-looking bugs so far has been a browser tab left open across a code change, silently
    still running the old JS. Reports the newest mtime among app.py and everything in static/ so
    the footer can flag a stale tab at a glance instead of leaving it to guesswork."""
    files = [BASE_DIR / "app.py", *((BASE_DIR / "static").glob("*"))]
    newest = max((f.stat().st_mtime for f in files if f.is_file()), default=0)
    return {"server_code_updated_at": datetime.fromtimestamp(newest).isoformat(timespec="seconds")}


# --------------------------------------------------------------------------
# Tab 1: Test a Product
# --------------------------------------------------------------------------
class TagIn(BaseModel):
    product_name: str


@app.post("/tag")
def tag_one(payload: TagIn):
    if not payload.product_name or not payload.product_name.strip():
        raise HTTPException(400, "product_name is required")
    return tag_sku_bundle_aware(payload.product_name)


# --------------------------------------------------------------------------
# Tab 2: Bulk Tag a File
#
# Monthly workflow: the uploaded file is transaction/GMV-level, so the same
# product name repeats many times (e.g. 61,717 rows but ~21,000 unique names
# in the April Blinkit file). We tag each UNIQUE name once - not once per row -
# then join back to every row. This is both faster and guarantees the same
# SKU name gets exactly the same tag everywhere in the file.
#
# sku_tag_history.json tracks every unique name ever tagged (its most recent tag)
# purely for the New-vs-Recurring split in the summary. It does NOT skip re-tagging -
# every unique name is re-run through the live engine every time, so engine fixes
# automatically apply to repeat SKUs too. Confirmed manual corrections belong in
# sku_exceptions.json instead, which the engine itself already checks on every
# low-confidence result.
# --------------------------------------------------------------------------
def _load_sku_history():
    if not SKU_HISTORY_FILE.exists():
        return {}
    return json.loads(SKU_HISTORY_FILE.read_text(encoding="utf-8"))


def _save_sku_history(history):
    SKU_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


# Bulk-tag jobs run in background threads (see _process_bulk_job), and each one's verification
# pass does read -> modify -> write on pending_rules.json. Two jobs running close together could
# otherwise race: both read the same snapshot, both append their own new candidates, and whichever
# writes second silently overwrites (loses) the first one's additions. Every read-modify-write
# sequence against pending_rules.json/approved_rules.json takes this lock for its full span, not
# just the individual load/save calls, so the two files stay consistent under concurrent requests.
# Reentrant (not a plain Lock) because _existing_rule_match does its own _load_pending() call,
# which can happen from inside a section that's already holding the lock (e.g. _run_verification).
_PENDING_LOCK = threading.RLock()


def _load_pending():
    if not PENDING_FILE.exists():
        return []
    return json.loads(PENDING_FILE.read_text(encoding="utf-8"))


def _save_pending(rules):
    PENDING_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_approved():
    if not APPROVED_FILE.exists():
        return []
    return json.loads(APPROVED_FILE.read_text(encoding="utf-8"))


def _save_approved(rules):
    APPROVED_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")


# Stopwords excluded when the verification pass looks for a recurring, un-modeled word next to
# a Context-Matrix base - generic connective/marketing filler that would never make a useful
# modifier on its own, so surfacing it as a "candidate rule" would just be noise.
_VERIFICATION_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "of", "in", "on", "new", "pack",
    "combo", "set", "ml", "gm", "g", "kg", "pc", "pcs", "no", "x", "by", "to",
    "ltr", "litre", "liter", "oz", "piece", "pieces", "count", "value", "twin",
    "offer", "free", "gift", "improved", "strip", "strips", "edition",
}

_BRAND_WORDS_CACHE = {"words": None}


def _brand_words():
    """Individual lowercased words drawn from Master Data's Brand column - a candidate modifier
    that's actually just a brand name (e.g. 'dove' next to 'wash') isn't a genuine category
    signal, it just means 'this SKU happens to be Dove's version of the base product'. Cached
    once since Master Data doesn't change within a server run."""
    if _BRAND_WORDS_CACHE["words"] is None:
        words = set()
        try:
            df = _get_master_data_df()
            for val in df["Brand"].dropna().unique():
                words.update(w for w in re.findall(r"[a-zA-Z]+", str(val).lower()) if len(w) >= 3)
        except Exception:
            pass  # Master Data unavailable - fall back to no brand filtering rather than failing tagging
        _BRAND_WORDS_CACHE["words"] = words
    return _BRAND_WORDS_CACHE["words"]


def _existing_rule_match(keyword, modifier):
    """Does this (keyword, modifier) already exist in the live engine or the pending queue?
    Checked before adding a suggestion, whether it comes from the verification pass or a
    manual submission - never re-suggest something already covered."""
    kw = keyword.strip().lower()
    mod = (modifier or "").strip().lower()
    if not mod and kw in p6.DIRECT_KEYWORDS:
        return {"source": "Direct Keyword (live engine)", "l1_l2_l3": p6.DIRECT_KEYWORDS[kw]}
    if not mod and kw in p6.LOW_PRIORITY_KEYWORDS:
        return {"source": "Low Priority Keyword (live engine)", "l1_l2_l3": p6.LOW_PRIORITY_KEYWORDS[kw]}
    if kw in p6.CONTEXT_MATRIX:
        for m, l1, l2, l3, conf in p6.CONTEXT_MATRIX[kw]:
            if (m or "").strip().lower() == mod:
                return {"source": "Context Matrix (live engine)", "l1_l2_l3": (l1, l2, l3)}
    for entry in _load_pending():
        if entry.get("keyword", "").strip().lower() == kw and (entry.get("modifier") or "").strip().lower() == mod:
            return {"source": "Already in the pending review queue", "l1_l2_l3": (entry.get("l1"), entry.get("l2"), entry.get("l3"))}
    return None


_VERIFICATION_MAX_EXAMPLES = 5


def _run_verification(per_name):
    """Post-tagging check: among this run's Medium-confidence results (a Context-Matrix base
    matched but no modifier did, so the generic default was used), look for a word that recurs
    often enough next to the same base to plausibly deserve its own rule. Never applied to the
    live engine automatically - only ever queued to pending_rules.json for manual review, same
    as the existing 'Suggest a New Rule' flow.

    Runs a few background checks first so a large file's candidate list is small enough to
    actually review: the recurrence bar scales with how many unique SKUs this run covered (a flat
    3 is noise on a file with thousands of unique names), and any word that's really just a brand
    name (per Master Data's Brand column) is dropped outright - a brand always co-occurring with a
    base isn't a category signal, it's just "this SKU happens to be that brand's version."
    """
    from collections import Counter
    candidates = Counter()  # (base, word) -> count
    examples = {}  # (base, word) -> up to _VERIFICATION_MAX_EXAMPLES distinct product names
    tag_of = {}  # (base, word) -> (L1, L2, L3) of the first example seen

    brand_words = _brand_words()
    for name, tag in per_name.items():
        if tag["confidence"] != "Medium" or not tag.get("matched_on"):
            continue
        base = tag["matched_on"]
        if base not in p6.CONTEXT_MATRIX:
            continue
        known_modifiers = {(m or "").strip().lower() for m, *_ in p6.CONTEXT_MATRIX[base]}
        words = re.findall(r"[a-z]+", name.lower())
        for w in set(words):
            if w == base or w in known_modifiers or w in _VERIFICATION_STOPWORDS or len(w) < 3 or w in brand_words:
                continue
            key = (base, w)
            candidates[key] += 1
            tag_of.setdefault(key, (tag["L1"], tag["L2"], tag["L3"]))
            ex = examples.setdefault(key, [])
            if name not in ex and len(ex) < _VERIFICATION_MAX_EXAMPLES:
                ex.append(name)

    added = 0
    if not candidates:
        return added
    # A word recurring 3 times in a 20-SKU run is meaningful; the same 3 times in a 2000-SKU run
    # is noise. Scaling the bar keeps the review queue's size roughly proportional to how much
    # genuine signal is in the file, not to how many rows happened to be in it.
    min_count = max(3, round(len(per_name) * 0.005))
    with _PENDING_LOCK:
        pending = _load_pending()
        for (base, word), count in candidates.items():
            if count < min_count:
                continue
            if _existing_rule_match(base, word):
                continue
            l1, l2, l3 = tag_of[(base, word)]
            ex_list = examples[(base, word)]
            example = ex_list[0]
            entry = {
                "id": uuid.uuid4().hex[:8],
                "keyword": base,
                "modifier": word,
                "l1": l1, "l2": l2, "l3": l3,
                "examples": ex_list,
                "reason": (
                    f"Auto-Verification: '{word}' appeared next to '{base}' in {count} product names "
                    f"this run, all currently falling to the generic Medium-confidence default "
                    f"(e.g. \"{example}\"). L1/L2/L3 shown is that default, NOT a verified answer - "
                    f"review whether '{word}' deserves its own rule and correct the category if not."
                ),
                "status": "PENDING REVIEW",
                "source": "Auto-Verification",
            }
            pending.append(entry)
            added += 1
        if added:
            _save_pending(pending)
    return added


def _process_bulk_job(job_id, file_bytes, filename, column_name, platform,
                       save_to_disk, save_folder, overwrite_existing):
    job = JOBS[job_id]
    try:
        job["status"] = "reading"
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes))

        if column_name not in df.columns:
            job["status"] = "error"
            job["error"] = (
                f"Column '{column_name}' not found. Available columns: "
                f"{', '.join(str(c) for c in df.columns)}"
            )
            return

        # A blank cell in this column reads as NaN (a float) - and on an object-dtype column
        # (the normal case once ANY row is a real string), pandas' own .astype(str) leaves an
        # existing NaN as-is instead of converting it, so it silently survives every downstream
        # .astype(str) call in this function too. Cleaning it once here, right after the column is
        # validated, keeps every blank/stray-numeric cell a plain string everywhere below instead
        # of crashing deep inside the tagging engine on the first .lower() call.
        df[column_name] = df[column_name].fillna("").astype(str)

        total = len(df)
        # Different platforms name their historical-tag columns differently (Blinkit/Myntra/
        # Nykaa use "L1"/"L2"/"L3"; Swiggy Instamart/Zepto use "L1_Cat"/"L2_Cat"/"L3_Cat") - try
        # each known convention in turn so conflict-checking and fallback-to-existing-tag work
        # regardless of which platform's file this is.
        L1_COLUMN_ALIASES = [("L1", "L2", "L3"), ("L1_Cat", "L2_Cat", "L3_Cat")]
        l1_col = l2_col = l3_col = None
        for a1, a2, a3 in L1_COLUMN_ALIASES:
            if all(c in df.columns for c in (a1, a2, a3)):
                l1_col, l2_col, l3_col = a1, a2, a3
                break
        has_existing = l1_col is not None
        history = _load_sku_history()

        unique_names = df[column_name].astype(str).unique().tolist()
        # Progress tracks unique names (that's what actually gets computed), not total
        # rows - tagging is deduped, so iterating to `total` would never complete.
        job.update(status="processing", total=len(unique_names), total_rows=total, done=0)

        # Per-name existing/prior tag, for the FALLBACK_METHODS safety net below. Uses the
        # most common (L1,L2,L3) per name in case the same SKU name was historically tagged
        # inconsistently across rows/months.
        existing_lookup = {}
        if has_existing:
            valid = df[df[l2_col].notna() & (df[l2_col].astype(str).str.strip() != "")]
            if len(valid):
                for name, grp in valid.groupby(df[column_name].astype(str)):
                    # FIX: take the mode of the (L1,L2,L3) TUPLE together, not each column's
                    # mode independently - a SKU historically tagged inconsistently across rows
                    # (e.g. sometimes Fragrance/Perfume, sometimes Hygiene/Deos & Antiperspirants)
                    # was otherwise getting the most-common L1 stitched to a most-common L2 that
                    # never actually co-occurred with it, producing a nonsensical combination.
                    combo_counts = grp[[l1_col, l2_col, l3_col]].value_counts(dropna=False)
                    # Cast out of pandas/numpy scalar types (e.g. a numeric-looking category column
                    # reads in as int64) into plain str/None - an int64 surviving into the JSON
                    # preview response later crashes FastAPI's encoder ("Object of type int64 is
                    # not JSON serializable"), and every downstream comparison expects plain text.
                    top_l1, top_l2, top_l3 = (
                        None if pd.isna(v) else str(v) for v in combo_counts.index[0]
                    )
                    existing_lookup[name] = {"L1": top_l1, "L2": top_l2, "L3": top_l3}

        per_name = {}
        for i, raw_name in enumerate(unique_names):
            result = tag_sku_bundle_aware(raw_name)
            method = result.get("method")
            l1, l2, l3, confidence = result.get("L1"), result.get("L2"), result.get("L3"), result.get("confidence", "")

            # "Low" confidence is reserved exclusively for the brand-only last-resort fallback in
            # pipeline_v6.py (a real category, but the weakest signal the engine ever acts on -
            # brand alone, no product-type word at all) - flagged for review same as a genuine
            # multi-category bundle, since neither is a confident automatic call.
            manual_review = method == "Bundle-ManualReview-MultiCategory" or confidence == "Low"
            if method in FALLBACK_METHODS:
                old = existing_lookup.get(raw_name)
                if old and old.get("L1") and old.get("L2"):
                    l1, l2, l3 = old["L1"], old["L2"], old.get("L3")
                    method = "Fallback-ExistingTag"
                    confidence = "Medium"

            per_name[raw_name] = {
                "L1": l1, "L2": l2, "L3": l3,
                "method": method, "confidence": confidence,
                "matched_on": result.get("matched_on"),
                "is_new": raw_name not in history,
                "manual_review": manual_review,
            }
            if i % 100 == 0 or i == len(unique_names) - 1:
                job["done"] = i + 1

        # Verification pass: look for a recurring, un-modeled word next to a Medium-confidence
        # base - queued to pending_rules.json for review, never applied to the live engine here.
        verification_added = _run_verification(per_name)

        # Raw data itself (every original column, including any month column already in the
        # file) is never touched - these are pure additions, appended after everything else.
        df["New_L1"] = df[column_name].astype(str).map(lambda n: per_name[n]["L1"])
        df["New_L2"] = df[column_name].astype(str).map(lambda n: per_name[n]["L2"])
        df["New_L3"] = df[column_name].astype(str).map(lambda n: per_name[n]["L3"])
        df["Tag_Method"] = df[column_name].astype(str).map(lambda n: per_name[n]["method"])
        df["Confidence"] = df[column_name].astype(str).map(lambda n: per_name[n]["confidence"])
        df["New_SKU"] = df[column_name].astype(str).map(lambda n: "Yes" if per_name[n]["is_new"] else "No")
        df["Manual_Review_Required"] = df[column_name].astype(str).map(lambda n: "Yes" if per_name[n]["manual_review"] else "No")

        if has_existing:
            old_l2 = df[l2_col].astype(str).str.strip().str.lower()
            new_l2 = df["New_L2"].astype(str).str.strip().str.lower()
            df["L2_Conflict_Flag"] = [
                "N/A" if o in ("", "nan", "none") else ("NO" if o == n else "YES")
                for o, n in zip(old_l2, new_l2)
            ]

        # Second sheet: one row per unique SKU name with its tag - a quick reference list,
        # deduplicated out of the row-per-transaction raw data on the first sheet.
        unique_ref = pd.DataFrame([
            {
                column_name: name,
                "L1": tag["L1"], "L2": tag["L2"], "L3": tag["L3"],
                "Tag_Method": tag["method"], "Confidence": tag["confidence"],
                "New_SKU": "Yes" if tag["is_new"] else "No",
                "Manual_Review_Required": "Yes" if tag["manual_review"] else "No",
            }
            for name, tag in per_name.items()
        ])
        if has_existing:
            conflict_by_name = dict(zip(df[column_name].astype(str), df["L2_Conflict_Flag"]))
            unique_ref["L2_Conflict_Flag"] = unique_ref[column_name].map(conflict_by_name)

        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Tagged Data")
            unique_ref.to_excel(writer, index=False, sheet_name="Unique SKU Tag Reference")
        out.seek(0)

        # Preview shown in the UI before the user decides where to save - every UNIQUE SKU and
        # what it was tagged (not a small sample of raw transaction rows), so the search/filter
        # controls on the frontend actually have something to filter across the whole run.
        preview_df = unique_ref.astype(object).where(unique_ref.notna(), None)
        preview_rows = preview_df.to_dict(orient="records")

        methods = [per_name[n]["method"] for n in df[column_name].astype(str)]
        new_flags = [per_name[n]["is_new"] for n in unique_names]
        summary = {
            "total_rows": total,
            "unique_skus": len(unique_names),
            "new_skus": sum(new_flags),
            "recurring_skus": len(unique_names) - sum(new_flags),
            "clean": sum(1 for m in methods if m not in FLAGGED_METHODS),
            "flagged": sum(1 for m in methods if m in FLAGGED_METHODS),
            "fallback_to_existing": sum(1 for m in methods if m == "Fallback-ExistingTag"),
            "manual_review": sum(1 for n in df[column_name].astype(str) if per_name[n]["manual_review"]),
            "conflicts": int((df["L2_Conflict_Flag"] == "YES").sum()) if has_existing else 0,
            "has_conflict_check": has_existing,
            "verification_added": verification_added,
        }

        for name, tag in per_name.items():
            history[name] = {
                "L1": tag["L1"], "L2": tag["L2"], "L3": tag["L3"],
                "method": tag["method"], "confidence": tag["confidence"],
            }
        _save_sku_history(history)

        # Name the output after the platform (e.g. "Blinkit_Tagged.xlsx") so files in
        # Retagged Expert Files/ are identifiable at a glance. Falls back to the uploaded
        # file's own name if no platform was given. This is always the DOWNLOAD filename,
        # regardless of what the disk auto-save ends up being called (see below).
        stem = _safe_filename_part(platform) if platform.strip() else Path(filename).stem
        result_filename = f"{stem}_Tagged.xlsx"

        saved_path = None
        save_error = None
        if save_to_disk:
            # A blank save_folder means "use the app's default Tagged Expert Data folder"; a
            # non-blank one is a path the user typed in the UI, taken as-is.
            target_dir = Path(save_folder) if save_folder.strip() else TAGGED_EXPERT_DATA_DIR
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                candidate = target_dir / result_filename
                if candidate.exists() and not overwrite_existing:
                    # Never silently clobber a previous run's file (e.g. an earlier upload for
                    # this platform that had better coverage/different rows) - stamp on a
                    # timestamp instead so both copies survive side by side. The user can still
                    # tick "overwrite" explicitly if they genuinely want the old one replaced.
                    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                    candidate = target_dir / f"{stem}_Tagged_{stamp}.xlsx"
                candidate.write_bytes(out.getvalue())
                saved_path = str(candidate)
            except OSError as exc:
                # The disk auto-save is a convenience, not the primary deliverable - the download
                # button below serves the same bytes regardless. A locked destination file (e.g.
                # still open in Excel from reviewing the last run) must not throw away an entire
                # bulk-tag run's worth of work just because this one write failed.
                save_error = str(exc)

        job.update(
            status="done",
            result=out.getvalue(),
            summary=summary,
            result_filename=result_filename,
            preview=preview_rows,
            saved_path=saved_path,
            save_error=save_error,
        )
    except Exception as exc:  # surface any parse/processing error to the UI
        job["status"] = "error"
        job["error"] = str(exc)


@app.get("/tag-bulk/default-folder")
def tag_bulk_default_folder(month: str = ""):
    """Base folder: Portal Data/Tagged Expert Data. With a month given (e.g. from the Monthly
    Setup board, which always knows which month it's operating on), returns that month's own
    subfolder instead, created lazily - so Tagged Expert Data stays organized one subfolder per
    month rather than one flat pile of files."""
    path = TAGGED_EXPERT_DATA_DIR / month.strip() if month.strip() else TAGGED_EXPERT_DATA_DIR
    return {"path": str(path)}


@app.post("/tag-bulk")
async def tag_bulk(
    file: UploadFile = File(...),
    column_name: str = Form("product_name"),
    platform: str = Form(""),
    save_to_disk: bool = Form(True),
    save_folder: str = Form(""),
    overwrite_existing: bool = Form(False),
):
    content = await file.read()
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "done": 0, "total": 0}
    thread = threading.Thread(
        target=_process_bulk_job,
        args=(job_id, content, file.filename, column_name.strip(), platform.strip(),
              save_to_disk, save_folder.strip(), overwrite_existing),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/tag-bulk/{job_id}/status")
def tag_bulk_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job_id")
    return {k: v for k, v in job.items() if k != "result"}


@app.get("/tag-bulk/{job_id}/download")
def tag_bulk_download(job_id: str):
    job = JOBS.get(job_id)
    if job is None or job.get("status") != "done":
        raise HTTPException(404, "Result not ready")
    return StreamingResponse(
        io.BytesIO(job["result"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{job["result_filename"]}"'},
    )


# --------------------------------------------------------------------------
# Category-level tagging (Flipkart and similar): the raw expert-data export already arrives
# pre-aggregated to Category x Sub-cat1 x Sub-cat2 x Brand, with a single summed GMV per row - no
# product/SKU name to run the keyword engine against at all. So instead of pipeline_v6's NLP
# matching, this looks each row's (Category, Sub cat 1, Sub cat 2) triple up verbatim against a
# persistent, per-platform mapping table (engine/category_mapping.py). Reuses the SAME JOBS dict
# and /tag-bulk/{job_id}/status + /download endpoints above - only the job-producing function and
# its entrypoint endpoint differ, so the frontend's existing polling/preview/download code works
# unchanged for either kind of platform.
# --------------------------------------------------------------------------
@app.get("/category-level-platforms")
def category_level_platforms():
    return {"platforms": sorted(cm.CATEGORY_LEVEL_PLATFORMS)}


def _process_category_bulk_job(job_id, file_bytes, filename, platform, sheet_name,
                                category_col, subcat1_col, subcat2_col, brand_col,
                                save_to_disk, save_folder, overwrite_existing):
    job = JOBS[job_id]
    try:
        job["status"] = "reading"
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet_name or 0)

        # A platform whose raw export has only ONE native category field (e.g. Amazon's single
        # concatenated "AZ Expert Data" tag, vs Flipkart's three separate Category/Sub-cat1/
        # Sub-cat2 columns) just leaves Sub cat 1/Sub cat 2 blank in the UI rather than retyping
        # the same column name three times - the mapping table's key convention (that tag
        # repeated three times) still matches exactly.
        subcat1_col = subcat1_col or category_col
        subcat2_col = subcat2_col or category_col

        required = [category_col, subcat1_col, subcat2_col]
        missing = [c for c in required if c not in df.columns]
        if missing:
            job["status"] = "error"
            job["error"] = (
                f"Column(s) {', '.join(missing)} not found. Available columns: "
                f"{', '.join(str(c) for c in df.columns)}"
            )
            return
        has_brand = brand_col in df.columns

        for c in required:
            df[c] = df[c].fillna("").astype(str)
        if has_brand:
            df[brand_col] = df[brand_col].fillna("").astype(str)

        total = len(df)
        combo_cols = df[required].apply(lambda r: tuple(r), axis=1)
        unique_combos = combo_cols.unique().tolist()
        job.update(status="processing", total=len(unique_combos), total_rows=total, done=0)

        per_combo = {}
        for i, combo in enumerate(unique_combos):
            category, subcat1, subcat2 = combo
            result = cm.tag_category_row(platform, category, subcat1, subcat2)
            per_combo[combo] = result
            if i % 50 == 0 or i == len(unique_combos) - 1:
                job["done"] = i + 1

        row_counts = combo_cols.value_counts()
        unmapped_combos = [
            (c[0], c[1], c[2], int(row_counts.get(c, 0)))
            for c, tag in per_combo.items() if tag["manual_review"]
        ]
        newly_queued = cm.queue_unmapped_combos(platform, unmapped_combos) if unmapped_combos else 0

        df["New_L0"] = combo_cols.map(lambda c: per_combo[c]["L0"])
        df["New_L1"] = combo_cols.map(lambda c: per_combo[c]["L1"])
        df["New_L2"] = combo_cols.map(lambda c: per_combo[c]["L2"])
        df["Tag_Method"] = combo_cols.map(lambda c: per_combo[c]["method"])
        df["Confidence"] = combo_cols.map(lambda c: per_combo[c]["confidence"])
        df["Manual_Review_Required"] = combo_cols.map(lambda c: "Yes" if per_combo[c]["manual_review"] else "No")

        unique_ref = pd.DataFrame([
            {
                category_col: combo[0], subcat1_col: combo[1], subcat2_col: combo[2],
                "L0": tag["L0"], "L1": tag["L1"], "L2": tag["L2"],
                "Tag_Method": tag["method"], "Confidence": tag["confidence"],
                "Manual_Review_Required": "Yes" if tag["manual_review"] else "No",
                "Row_Count": int(row_counts.get(combo, 0)),
            }
            for combo, tag in per_combo.items()
        ])

        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Tagged Data")
            unique_ref.to_excel(writer, index=False, sheet_name="Unique Category Tag Reference")
        out.seek(0)

        preview_df = unique_ref.astype(object).where(unique_ref.notna(), None)
        preview_rows = preview_df.to_dict(orient="records")

        methods = [per_combo[c]["method"] for c in combo_cols]
        summary = {
            "total_rows": total,
            "unique_skus": len(unique_combos),  # reuses the generic frontend summary card labels
            "new_skus": newly_queued,
            "recurring_skus": len(unique_combos) - newly_queued,
            "clean": sum(1 for m in methods if m == "Category-Mapped"),
            "flagged": sum(1 for m in methods if m == "Unmapped-Category"),
            "fallback_to_existing": 0,
            "manual_review": sum(1 for c in combo_cols if per_combo[c]["manual_review"]),
            "conflicts": 0,
            "has_conflict_check": False,
            "verification_added": 0,
        }

        stem = _safe_filename_part(platform) if platform.strip() else Path(filename).stem
        result_filename = f"{stem}_Tagged.xlsx"

        saved_path = None
        save_error = None
        if save_to_disk:
            target_dir = Path(save_folder) if save_folder.strip() else TAGGED_EXPERT_DATA_DIR
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                candidate = target_dir / result_filename
                if candidate.exists() and not overwrite_existing:
                    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                    candidate = target_dir / f"{stem}_Tagged_{stamp}.xlsx"
                candidate.write_bytes(out.getvalue())
                saved_path = str(candidate)
            except OSError as exc:
                save_error = str(exc)

        job.update(
            status="done",
            result=out.getvalue(),
            summary=summary,
            result_filename=result_filename,
            preview=preview_rows,
            saved_path=saved_path,
            save_error=save_error,
        )
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/tag-bulk-category")
async def tag_bulk_category(
    file: UploadFile = File(...),
    platform: str = Form(...),
    sheet_name: str = Form(""),
    category_col: str = Form("Category"),
    subcat1_col: str = Form("Sub cat 1"),
    subcat2_col: str = Form("Sub cat 2"),
    brand_col: str = Form("brand"),
    save_to_disk: bool = Form(True),
    save_folder: str = Form(""),
    overwrite_existing: bool = Form(False),
):
    content = await file.read()
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "done": 0, "total": 0}
    thread = threading.Thread(
        target=_process_category_bulk_job,
        # sheet_name AND the column-name fields are NOT stripped - like the sheet name, a real
        # workbook's column header can (and for Amazon's "AZ Expert Data " / "Corrected Brand ",
        # does) legitimately carry a trailing space, so trimming it would silently break the
        # lookup against the user's actual file.
        args=(job_id, content, file.filename, platform.strip(), sheet_name,
              category_col, subcat1_col, subcat2_col, brand_col,
              save_to_disk, save_folder.strip(), overwrite_existing),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/pending-category-rules")
def pending_category_rules():
    return cm._load_pending_category_rules()


class CategoryRuleApprovalIn(BaseModel):
    id: str
    l0: str
    l1: str
    l2: str


class BulkApproveCategoryIn(BaseModel):
    approvals: list[CategoryRuleApprovalIn]


@app.post("/pending-category-rules/bulk-approve")
def bulk_approve_pending_category(payload: BulkApproveCategoryIn):
    approved = cm.approve_category_rules([a.dict() for a in payload.approvals])
    if not approved:
        raise HTTPException(404, "No matching pending rule ids found")
    return {"approved": approved, "count": len(approved)}


class CategoryBulkIdsIn(BaseModel):
    ids: list[str]


@app.post("/pending-category-rules/bulk-reject")
def bulk_reject_pending_category(payload: CategoryBulkIdsIn):
    rejected_count = cm.reject_category_rules(payload.ids)
    if rejected_count == 0:
        raise HTTPException(404, "No matching pending rule ids found")
    return {"rejected_count": rejected_count}


# --------------------------------------------------------------------------
# Tab: Monthly Setup (per-month platform expert-data gating)
#
# For a given month, every platform is either "no_expert_data" (nothing to do - skips straight
# past this phase) or walks a fixed pipeline:
#   pending_upload -> draft_ready -> final_uploaded -> finalized
# One JSON file (monthly_platform_state.json) is the single source of truth for every month/
# platform's status - never a versioned copy. Likewise the draft/final workbook for a given
# platform+month always lands at the SAME filename (overwritten on every re-run), so there's
# exactly one file to find per platform/month instead of a pile of timestamped ones.
#
# Tagging itself is never duplicated here - "Tag & Create Draft" in the UI calls the existing
# /tag-bulk job pipeline verbatim; this section only takes an already-completed job's bytes and
# files them under the platform+month's canonical name (see attach-draft below).
# --------------------------------------------------------------------------
# All of the portal's own generated state/output - plus, now, Master Data itself - lives under
# PORTAL_DATA_DIR (defined up near BASE_DIR, before anything else needs it).
PORTAL_TAGGED_DIR = PORTAL_DATA_DIR / "Tagged Files"
PORTAL_TAGGED_DIR.mkdir(parents=True, exist_ok=True)

MONTHLY_STATE_FILE = PORTAL_DATA_DIR / "monthly_platform_state.json"
_MONTHLY_LOCK = threading.RLock()

# Manual YoY/MoM targets an analyst enters for a specific category, platform and month - ONLY
# meant for categories Expert data doesn't cover at all (confirmed with the user: "if we have the
# YoY for some categories where we don't receive expert data, how do I enter that information for
# you to process"). Kept as its own small file, separate from monthly_platform_state.json's own
# validated schema, since this is an orthogonal concern (a category-level input, not a platform/
# month lifecycle state) - {platform: {month_label: {row_key: {"type": "yoy"|"mom", "value": pct}}}}.
CATEGORY_YOY_OVERRIDE_FILE = PORTAL_DATA_DIR / "category_yoy_overrides.json"
_CATEGORY_YOY_OVERRIDE_LOCK = threading.RLock()


def _load_category_yoy_overrides():
    if not CATEGORY_YOY_OVERRIDE_FILE.exists():
        return {}
    return json.loads(CATEGORY_YOY_OVERRIDE_FILE.read_text(encoding="utf-8"))


def _save_category_yoy_overrides(state):
    CATEGORY_YOY_OVERRIDE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# Three separate passwords, three separate roles:
#   - the SL password gates the day-to-day review/approval actions: Monthly Setup's "Start
#     tagging" (lock) and "Edit platform preferences" (unlock), the expert-data per-platform
#     Finalize/Reopen, and Category Topline's first-stage sign-off (analyst -> SL review).
#   - the MANAGER password gates Category Topline's second, final sign-off (SL review ->
#     Manager review -> finalized) - a genuine two-step verification, not just SL alone.
#   - the general ADMIN password is reserved for the bigger, rarer decisions: marking a month
#     Complete, reopening an already-Complete month, and (in future) Master Data/portal-level
#     updates - not routine monthly workflow.
# Viewing the Expert Data Tagging board, tagging, and uploading/re-uploading a Draft or Final are
# all password-free - day-to-day analyst work is never slowed down; only the approval-type actions
# are gated. Both are salted PBKDF2 hashes on disk - never the plaintext.
ADMIN_AUTH_FILE = PORTAL_DATA_DIR / "admin_auth.json"
SL_AUTH_FILE = PORTAL_DATA_DIR / "sl_auth.json"
MANAGER_AUTH_FILE = PORTAL_DATA_DIR / "manager_auth.json"


def _verify_password(auth_file, password):
    if not auth_file.exists():
        return False
    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    salt = bytes.fromhex(auth["salt"])
    candidate = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, 200_000).hex()
    return hmac.compare_digest(candidate, auth["hash"])


def _verify_admin_password(password):
    return _verify_password(ADMIN_AUTH_FILE, password)


def _verify_sl_password(password):
    return _verify_password(SL_AUTH_FILE, password)


def _verify_manager_password(password):
    return _verify_password(MANAGER_AUTH_FILE, password)


# Schema guard for monthly_platform_state.json - used only to VALIDATE shape on every load and
# save, not to replace the plain-dict code below (that would be a much larger rewrite for the
# same safety win). A hand-edit, a half-written file from a crash mid-save, or a future bug that
# writes the wrong shape now fails loudly and immediately here, in one place, instead of
# propagating into some endpoint three calls later as a confusing KeyError or silent wrong value.
class PlatformState(BaseModel):
    has_expert_data: bool
    status: Literal[
        "no_expert_data", "pending_upload", "draft_ready",
        "final_uploaded", "pending_sl_review", "finalized",
    ]
    draft_file: Optional[str] = None
    final_file: Optional[str] = None
    finalized_at: Optional[str] = None
    history: list[dict] = Field(default_factory=list)


class MonthState(BaseModel):
    locked: bool = False
    platforms: dict[str, PlatformState] = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    month_complete: bool = False
    completed_at: Optional[str] = None


class PortalState(BaseModel):
    months: dict[str, MonthState] = Field(default_factory=dict)
    month_order: list[str] = Field(default_factory=list)
    active_month: Optional[str] = None


def _load_monthly_state():
    if not MONTHLY_STATE_FILE.exists():
        return {"months": {}, "month_order": [], "active_month": None}
    state = json.loads(MONTHLY_STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("active_month", None)
    try:
        PortalState.model_validate(state)
    except ValidationError as exc:
        raise HTTPException(500, f"monthly_platform_state.json is corrupted - refusing to use it: {exc}")
    return state


def _ts():
    return datetime.now().isoformat(timespec="seconds")


def _log_month_event(m, event):
    """Month-level audit trail (preferences locked/unlocked, completed/reopened) - append-only,
    never overwritten, so 'what happened a few days back' is always answerable from the one state
    file on disk, not from memory that a refresh or restart could lose."""
    m.setdefault("history", []).append({"event": event, "at": _ts()})


def _log_platform_event(p, event):
    """Per-platform audit trail (draft tagged, final uploaded, finalized, reopened, etc.)."""
    p.setdefault("history", []).append({"event": event, "at": _ts()})


def _save_monthly_state(state):
    try:
        PortalState.model_validate(state)
    except ValidationError as exc:
        # An internal bug trying to write a bad shape - refuse rather than corrupt the one file
        # every endpoint here trusts.
        raise HTTPException(500, f"Refusing to save - state shape is invalid: {exc}")
    MONTHLY_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _monthly_draft_path(platform, month):
    return PORTAL_TAGGED_DIR / f"{_safe_filename_part(platform)}_{_safe_filename_part(month)}_Draft.xlsx"


def _monthly_final_path(platform, month):
    return PORTAL_TAGGED_DIR / f"{_safe_filename_part(platform)}_{_safe_filename_part(month)}_Final.xlsx"


def _ensure_month(state, month):
    if month not in state["months"]:
        state["months"][month] = {
            "locked": False, "platforms": {}, "history": [],
            "month_complete": False, "completed_at": None,
        }
        state["month_order"].append(month)
    return state["months"][month]


def _require_month_not_complete(m, month):
    """A Complete month is fully archived - no edits of any kind, lock/unlock, tagging, final
    upload, finalize or reopen, until an admin explicitly reopens the whole month."""
    if m.get("month_complete"):
        raise HTTPException(400, f"'{month}' is marked Complete and archived - an admin must reopen the month first")


def _monthly_platform_entry(state, month, platform, require_open=False):
    """Looks up an expert-data platform's state entry - 404/400s (never KeyErrors) on a bad
    month/platform/stage, since every endpoint below shares this same lookup. require_open=True
    for every MUTATING endpoint (a completed month is archived - no more changes); left False for
    the read-only draft/final download endpoints, which stay accessible even once complete."""
    m = state["months"].get(month)
    if m is None:
        raise HTTPException(404, f"Unknown month '{month}'")
    if require_open:
        _require_month_not_complete(m, month)
    p = m["platforms"].get(platform)
    if p is None or not p.get("has_expert_data"):
        raise HTTPException(400, f"'{platform}' is not set up with expert data for {month}")
    return m, p


def _monthly_any_platform_entry(state, month, platform, require_open=False):
    """Same as _monthly_platform_entry but for the Category Topline review track, which applies
    to EVERY locked platform - whether it has expert data (real tagged numbers) or not (a
    forecast) - not just expert-data ones."""
    m = state["months"].get(month)
    if m is None:
        raise HTTPException(404, f"Unknown month '{month}'")
    if require_open:
        _require_month_not_complete(m, month)
    p = m["platforms"].get(platform)
    if p is None:
        raise HTTPException(400, f"'{platform}' has no preferences set for {month}")
    return m, p


def _ensure_category_topline(p):
    """Back-fills the Category Topline review sub-state the first time it's touched - lets
    already-existing platform entries (from before this feature existed) pick it up lazily
    instead of needing a one-off migration of the whole state file."""
    return p.setdefault("category_topline", {
        "status": "analyst", "total_gmv_input": None, "gmv_input_mode": None,
        "gmv_input_value": None, "forecast": None, "finalized_at": None, "history": [],
    })


# Single source of truth for the per-platform pipeline's legal moves:
#   pending_upload -> draft_ready -> final_uploaded -> pending_sl_review -> finalized
# Every status-changing endpoint below calls _transition() instead of hand-rolling its own
# "if status != X: raise" check, so the whole state machine is readable in one place and never
# drifts out of sync between endpoints - the exact kind of bug class that's bitten this project
# before (one endpoint's check quietly not matching another's as the pipeline grew). A typo'd
# action name here is a KeyError, not a silently-wrong check.
PLATFORM_TRANSITIONS = {
    "attach_draft": {"from": None, "to": "draft_ready", "event": "Draft tagged and saved"},
    "upload_final": {"from": {"draft_ready", "final_uploaded"}, "to": "final_uploaded", "event": "Reviewed final file uploaded"},
    "send_to_review": {"from": {"final_uploaded"}, "to": "pending_sl_review", "event": "Sent to SL review"},
    "send_back": {"from": {"pending_sl_review"}, "to": "final_uploaded", "event": "Sent back to analyst by SL"},
    "finalize": {"from": {"pending_sl_review"}, "to": "finalized", "event": "Finalized by SL"},
    "reopen": {"from": {"finalized"}, "to": "final_uploaded", "event": "Reopened by SL (finalize undone)"},
}


def _transition(p, action, error_message=None):
    """Applies one named move from PLATFORM_TRANSITIONS - checks the platform is in a legal
    starting status (if the move restricts one), then sets the new status and logs it. Any
    OTHER field changes (file paths, finalized_at) are the caller's own responsibility, right
    before or after this call - this function owns only the status machine itself."""
    _apply_transition(p, PLATFORM_TRANSITIONS, action, p, error_message)


# The Category Topline review track - separate from the expert-data tagging pipeline above, and
# applying to EVERY platform (not just expert-data ones): a forecast (no expert data) or the real
# tagged numbers (expert data finalized) both still need an analyst to adjust them, an SL to
# review, and a manager to sign off - a genuine two-step verification before anything is final.
# Same shape, same reasoning as PLATFORM_TRANSITIONS - one table, not scattered checks.
#   analyst -> pending_sl_review -> pending_manager_review -> finalized
CATEGORY_TOPLINE_TRANSITIONS = {
    "send_to_review": {"from": {"analyst"}, "to": "pending_sl_review", "event": "Sent to SL review"},
    "send_back_to_analyst": {"from": {"pending_sl_review"}, "to": "analyst", "event": "Sent back to analyst by SL"},
    "sl_approve": {"from": {"pending_sl_review"}, "to": "pending_manager_review", "event": "Approved by SL - sent to Manager review"},
    "send_back_to_sl": {"from": {"pending_manager_review"}, "to": "pending_sl_review", "event": "Sent back to SL by Manager"},
    "manager_finalize": {"from": {"pending_manager_review"}, "to": "finalized", "event": "Finalized by Manager"},
    "reopen": {"from": {"finalized"}, "to": "pending_manager_review", "event": "Reopened by Manager (finalize undone)"},
}


def _transition_category_topline(ct, action, error_message=None):
    _apply_transition(ct, CATEGORY_TOPLINE_TRANSITIONS, action, ct, error_message)


def _apply_transition(obj, table, action, log_target, error_message=None):
    spec = table[action]
    allowed_from = spec["from"]
    if allowed_from is not None and obj["status"] not in allowed_from:
        raise HTTPException(400, error_message or f"Not allowed from status '{obj['status']}'")
    obj["status"] = spec["to"]
    log_target.setdefault("history", []).append({"event": spec["event"], "at": _ts()})


@app.get("/monthly-setup/platforms")
def monthly_setup_platforms():
    # Reuses the Category Topline workbook's own sheet list - every platform this team already
    # tracks, with nothing new to keep in sync by hand.
    return {"platforms": _workbook_platforms(TOPLINE_FILES["all"])}


def _master_data_latest_month_label():
    """The newest month actually present in Master Data right now, as a 'Sep'26'-style label -
    advances automatically the moment Master Data itself gets updated, instead of being pinned to
    a date someone has to remember to bump by hand."""
    df = _get_master_data_df()
    months_sorted = sorted(
        df["_MonthLabel"].dropna().unique().tolist(),
        key=lambda s: datetime.strptime(s, "%b-%y"),
    )
    latest = months_sorted[-1] if months_sorted else None
    return latest.replace("-", "'") if latest else None


def _add_one_month(label):
    """'Sep'26' -> 'Oct'26' (also accepts the 'Sep-26' report-file spelling)."""
    dt = datetime.strptime(label.replace("'", "-"), "%b-%y")
    y, mo = dt.year, dt.month + 1
    if mo > 12:
        mo = 1
        y += 1
    return datetime(y, mo, 1).strftime("%b-%y").replace("-", "'")


@app.get("/monthly-setup/months")
def monthly_setup_months():
    """Every month already created, plus - ONLY once every one of those is Complete - the single
    next sequential month, never a window of several future months to pick from: Oct'26 can't
    actually be built before Sep'26 is, since each month's no-expert-data forecast is derived
    from the prior actual month's own numbers, so offering it early was never actually usable."""
    state = _load_monthly_state()
    order = state.get("month_order", [])
    all_complete = all(state["months"].get(m, {}).get("month_complete") for m in order)

    next_month = None
    if all_complete:
        md_latest = _master_data_latest_month_label()
        candidates = ([md_latest] if md_latest else []) + order
        if candidates:
            base = max(candidates, key=lambda s: datetime.strptime(s.replace("'", "-"), "%b-%y"))
            candidate = _add_one_month(base)
            if candidate not in order:
                next_month = candidate

    return {"months": order, "next_month": next_month}


@app.get("/monthly-setup/master-data-latest-month")
def monthly_setup_master_data_latest_month():
    return {"month": _master_data_latest_month_label()}


@app.get("/monthly-setup/active")
def monthly_setup_active():
    """The month most recently locked - the Expert Data Tagging tab defaults to this one so the
    platforms actually in progress are what greets you, without losing access to any other
    month (still reachable there via its own month picker). Login-gated: only used by the Expert
    Data Tagging tab, never by Monthly Setup's own preference editing."""
    state = _load_monthly_state()
    return {"month": state.get("active_month")}


@app.get("/monthly-setup/locked-months")
def monthly_setup_locked_months():
    state = _load_monthly_state()
    return {"months": [m for m in state.get("month_order", []) if state["months"].get(m, {}).get("locked")]}


@app.get("/monthly-setup/{month}")
def monthly_setup_get(month: str):
    """Ungated - used by Monthly Setup's own month picker/preference matrix, which stays
    password-free by design (see module docstring above). The Expert Data Tagging tab uses the
    separate, login-gated /board endpoint below instead."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        m = _ensure_month(state, month)
        _save_monthly_state(state)
        return {"month": month, **m}


@app.get("/monthly-setup/{month}/board")
def monthly_setup_board(month: str):
    """Same shape as the plain GET above - what the Expert Data Tagging tab actually calls to
    view a month's per-platform tagging AND Category Topline review progress. Backfills
    category_topline in-memory for any platform that's never had a CT action called on it yet
    (e.g. just locked, never touched) - a read-only default, not persisted here; it's written for
    real the first time any CT endpoint actually runs for that platform."""
    state = _load_monthly_state()
    m = state["months"].get(month)
    if m is None:
        raise HTTPException(404, f"Unknown month '{month}'")
    for p in m["platforms"].values():
        _ensure_category_topline(p)
    return {"month": month, **m}


class AdminPasswordIn(BaseModel):
    password: str


class MonthlyLockIn(BaseModel):
    platforms: dict[str, bool]
    password: str


@app.post("/monthly-setup/{month}/lock")
def monthly_setup_lock(month: str, payload: MonthlyLockIn):
    """Starts tagging for this month (NOT the final word - see /complete below for that). A
    platform whose has_expert_data flag is unchanged from last time keeps its existing progress
    untouched (so re-locking after an Edit doesn't wipe out a draft/final that's already in
    flight) - only a flag that actually flipped resets that platform back to the start of its
    pipeline. SL-password gated, same as /unlock - the admin password is reserved for the bigger,
    rarer decisions (Master Data/portal updates, Month Complete, reopening archived data), not
    routine monthly setup. Day-to-day tagging work past this point (uploads, re-uploads, sending
    to SL review) stays password-free."""
    if not _verify_sl_password(payload.password):
        raise HTTPException(403, "Incorrect SL password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        m = _ensure_month(state, month)
        _require_month_not_complete(m, month)
        for platform, has_expert in payload.platforms.items():
            existing = m["platforms"].get(platform)
            if existing is None or existing.get("has_expert_data") != has_expert:
                m["platforms"][platform] = {
                    "has_expert_data": has_expert,
                    "status": "pending_upload" if has_expert else "no_expert_data",
                    "draft_file": None,
                    "final_file": None,
                    "finalized_at": None,
                    "history": [],
                }
                _log_platform_event(
                    m["platforms"][platform],
                    f"Expert data set to {'Yes' if has_expert else 'No'}",
                )
        m["locked"] = True
        _log_month_event(m, "Platform preferences locked")
        _save_monthly_state(state)
        return {"month": month, **m}


@app.post("/monthly-setup/{month}/set-active")
def monthly_setup_set_active(month: str, payload: AdminPasswordIn):
    """Which month the Expert Data Tagging tab defaults to is a deliberate, admin-password-gated
    choice - NOT a side effect of routine locking (it used to silently follow whichever month was
    most recently locked, which was confusing when working multiple months)."""
    if not _verify_admin_password(payload.password):
        raise HTTPException(403, "Incorrect admin password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        if month not in state["months"]:
            raise HTTPException(404, f"Unknown month '{month}'")
        state["active_month"] = month
        _save_monthly_state(state)
        return {"active_month": month}


@app.post("/monthly-setup/{month}/unlock")
def monthly_setup_unlock(month: str, payload: AdminPasswordIn):
    """Reopens the platform-preference console for this month without touching anything else -
    the explicit 'edit preferences' escape hatch so a lock is never a one-way door. SL-password
    gated, same as /lock."""
    if not _verify_sl_password(payload.password):
        raise HTTPException(403, "Incorrect SL password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        if month not in state["months"]:
            raise HTTPException(404, f"Unknown month '{month}'")
        m = state["months"][month]
        _require_month_not_complete(m, month)
        m["locked"] = False
        _log_month_event(m, "Platform preferences unlocked for editing")
        _save_monthly_state(state)
        return {"month": month, **m}


@app.post("/monthly-setup/{month}/complete")
def monthly_setup_complete(month: str, payload: AdminPasswordIn):
    """The real lock: permanently archives this month once every expert-data platform has been
    individually Finalized. Admin-password gated (unlike every other action here, which is freely
    reversible) because this is the one point-of-no-return step - nothing else in this flow needs
    a password, so day-to-day tagging work is never slowed down."""
    if not _verify_admin_password(payload.password):
        raise HTTPException(403, "Incorrect admin password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        if month not in state["months"]:
            raise HTTPException(404, f"Unknown month '{month}'")
        m = state["months"][month]
        _require_month_not_complete(m, month)
        if not m.get("locked"):
            raise HTTPException(400, "Lock this month's platform preferences before completing it")
        not_ready = [
            name for name, p in m["platforms"].items()
            if p.get("has_expert_data") and p.get("status") != "finalized"
        ]
        if not_ready:
            raise HTTPException(400, f"Not every platform is Finalized yet: {', '.join(not_ready)}")
        m["month_complete"] = True
        m["completed_at"] = _ts()
        _log_month_event(m, "Month marked Complete (admin)")
        _save_monthly_state(state)
        return {"month": month, **m}


@app.post("/monthly-setup/{month}/reopen-month")
def monthly_setup_reopen_month(month: str, payload: AdminPasswordIn):
    """Admin-only escape hatch to undo /complete - everything stays exactly as it was, just no
    longer archived, so edits can resume."""
    if not _verify_admin_password(payload.password):
        raise HTTPException(403, "Incorrect admin password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        if month not in state["months"]:
            raise HTTPException(404, f"Unknown month '{month}'")
        m = state["months"][month]
        if not m.get("month_complete"):
            raise HTTPException(400, "This month isn't marked Complete")
        m["month_complete"] = False
        m["completed_at"] = None
        _log_month_event(m, "Month reopened by admin")
        _save_monthly_state(state)
        return {"month": month, **m}


class AttachDraftIn(BaseModel):
    job_id: str


@app.post("/monthly-setup/{month}/{platform}/attach-draft")
def monthly_setup_attach_draft(month: str, platform: str, payload: AttachDraftIn):
    job = JOBS.get(payload.job_id)
    if job is None or job.get("status") != "done":
        raise HTTPException(400, "job_id is not a completed tagging job")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        path = _monthly_draft_path(platform, month)
        path.write_bytes(job["result"])
        p["draft_file"] = str(path)
        p["final_file"] = None
        p["finalized_at"] = None
        _transition(p, "attach_draft")
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


@app.get("/monthly-setup/{month}/{platform}/draft")
def monthly_setup_download_draft(month: str, platform: str):
    state = _load_monthly_state()
    _m, p = _monthly_platform_entry(state, month, platform)
    if not p.get("draft_file") or not Path(p["draft_file"]).exists():
        raise HTTPException(404, "No draft file saved yet")
    path = Path(p["draft_file"])
    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.post("/monthly-setup/{month}/{platform}/upload-final")
async def monthly_setup_upload_final(month: str, platform: str, file: UploadFile = File(...)):
    """The file the user manually reviewed/corrected outside the tool, re-uploaded as-is (never
    re-tagged) - this becomes THE final file for this platform+month, overwriting any earlier
    final upload rather than keeping both around."""
    content = await file.read()
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        path = _monthly_final_path(platform, month)
        path.write_bytes(content)
        p["final_file"] = str(path)
        p["finalized_at"] = None
        _transition(p, "upload_final", error_message=f"Tag a draft first for '{platform}' before uploading a final file")
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


@app.get("/monthly-setup/{month}/{platform}/final")
def monthly_setup_download_final(month: str, platform: str):
    state = _load_monthly_state()
    _m, p = _monthly_platform_entry(state, month, platform)
    if not p.get("final_file") or not Path(p["final_file"]).exists():
        raise HTTPException(404, "No final file uploaded yet")
    path = Path(p["final_file"])
    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.post("/monthly-setup/{month}/{platform}/send-to-review")
def monthly_setup_send_to_review(month: str, platform: str):
    """Analyst hands this platform off to SL for review - no password (that's only for SL's own
    actions below). Locks the analyst's own upload controls until SL either finalizes it or sends
    it back - no re-uploading over SL's shoulder while they're looking at it."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        _transition(p, "send_to_review", error_message="Upload the reviewed final file before sending it to SL review")
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


@app.post("/monthly-setup/{month}/{platform}/send-back")
def monthly_setup_send_back(month: str, platform: str):
    """SL rejects back to the analyst stage - no password needed (only the approval itself,
    Finalize, is gated). Unlocks the analyst's upload controls again."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        _transition(p, "send_back", error_message="Only a platform pending SL review can be sent back")
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


@app.post("/monthly-setup/{month}/{platform}/finalize")
def monthly_setup_finalize(month: str, platform: str, payload: AdminPasswordIn):
    """SL's approval - the one step in the whole analyst->SL pipeline that needs a password
    (the SL password, distinct from the general admin one), since this is what actually commits
    the platform's numbers."""
    if not _verify_sl_password(payload.password):
        raise HTTPException(403, "Incorrect SL password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        _transition(p, "finalize", error_message="Send this platform to SL review before it can be finalized")
        p["finalized_at"] = _ts()
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


@app.post("/monthly-setup/{month}/{platform}/reopen")
def monthly_setup_reopen(month: str, platform: str, payload: AdminPasswordIn):
    """Undoes an SL finalize (e.g. a mistake was spotted right after approving it) - keeps both
    files in place, just drops the platform back to 'final_uploaded' so it goes through SL review
    again rather than skipping straight back to finalized. SL-password gated, same as Finalize -
    undoing an approval is just as sensitive as granting one."""
    if not _verify_sl_password(payload.password):
        raise HTTPException(403, "Incorrect SL password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_platform_entry(state, month, platform, require_open=True)
        _transition(p, "reopen", error_message="Only a finalized platform can be reopened")
        p["finalized_at"] = None
        _save_monthly_state(state)
        return {"month": month, "platform": platform, **p}


# --------------------------------------------------------------------------
# Category Topline's own analyst -> SL-review -> finalized track, per platform per month.
# Applies to EVERY locked platform regardless of has_expert_data - a forecast (no expert data)
# or the real tagged numbers (expert data finalized) both need an analyst to review/adjust and an
# SL to sign off. Separate state from the expert-data tagging pipeline above (a platform can be
# mid-tagging and mid-Category-Topline-review at the same time - they don't block each other).
# --------------------------------------------------------------------------
@app.get("/monthly-setup/{month}/{platform}/category-topline")
def monthly_setup_category_topline_get(month: str, platform: str):
    state = _load_monthly_state()
    _m, p = _monthly_any_platform_entry(state, month, platform)
    return _ensure_category_topline(p)


class TotalGmvIn(BaseModel):
    total_gmv: float | None = None
    mom_pct: float | None = None
    yoy_pct: float | None = None


class CategoryYoyOverrideIn(BaseModel):
    platform: str
    month: str
    row_key: str
    type: Literal["yoy", "mom"]
    value: float  # a percentage, e.g. -22 for -22%


class CategoryYoyOverrideDeleteIn(BaseModel):
    platform: str
    month: str
    row_key: str


@app.get("/category-topline/yoy-overrides")
def category_topline_get_yoy_overrides(platform: str, month: str):
    """Every manual YoY/MoM override currently set for this platform/month - {row_key: {type,
    value}}. Used both to populate the table of existing overrides in the UI, and (via
    _manual_yoy_override_lookup) to feed set-total-gmv's forecast for categories Expert data
    doesn't cover at all."""
    overrides = _load_category_yoy_overrides()
    month_label = month.replace("'", "-")
    return overrides.get(platform, {}).get(month_label, {})


@app.post("/category-topline/yoy-overrides")
def category_topline_set_yoy_override(payload: CategoryYoyOverrideIn):
    month_label = payload.month.replace("'", "-")
    with _CATEGORY_YOY_OVERRIDE_LOCK:
        overrides = _load_category_yoy_overrides()
        overrides.setdefault(payload.platform, {}).setdefault(month_label, {})[payload.row_key] = {
            "type": payload.type, "value": payload.value,
        }
        _save_category_yoy_overrides(overrides)
    return {"status": "saved"}


@app.delete("/category-topline/yoy-overrides")
def category_topline_delete_yoy_override(payload: CategoryYoyOverrideDeleteIn):
    month_label = payload.month.replace("'", "-")
    with _CATEGORY_YOY_OVERRIDE_LOCK:
        overrides = _load_category_yoy_overrides()
        overrides.get(payload.platform, {}).get(month_label, {}).pop(payload.row_key, None)
        _save_category_yoy_overrides(overrides)
    return {"status": "deleted"}


def _manual_yoy_override_lookup(platform, month_label, rows):
    """Turns this platform/month's manual YoY/MoM overrides (see CategoryYoyOverrideIn) into a
    synthetic expert_gmv_lookup entry per overridden row - implied GMV = the row's OWN real
    year-ago (for a "yoy" override) or prior-month (for "mom") GMV, times (1 + value/100) - so it
    flows through EXACTLY the same _expert_alignment_for_row pipeline a real expert figure would:
    same tiered tolerance clip, same Market Share guardrail, same disagreement gate, same
    reconciliation and sanity-cap treatment downstream. Confirmed with the user: a manual override
    gets the SAME trust level as real expert data, not special-cased or applied unconditionally."""
    overrides = _load_category_yoy_overrides().get(platform, {}).get(month_label, {})
    if not overrides:
        return {}
    by_key = {_row_key(r): r for r in rows}
    lookup = {}
    for row_key, override in overrides.items():
        r = by_key.get(row_key)
        if r is None:
            continue
        cutoff_local = len(r["share"])
        gmv_history = r.get("gmv") or []
        if override["type"] == "yoy":
            anchor = gmv_history[cutoff_local - 12] if 12 <= cutoff_local <= len(gmv_history) else None
        else:
            anchor = gmv_history[cutoff_local - 1] if 1 <= cutoff_local <= len(gmv_history) else None
        if not anchor:
            continue  # no real anchor to apply this override against - silently skipped, not an error
        implied_gmv = anchor * (1 + override["value"] / 100)
        key = _expert_lookup_key(r)
        if key is not None:
            lookup[key] = implied_gmv
    return lookup


@app.post("/monthly-setup/{month}/{platform}/category-topline/set-total-gmv")
def monthly_setup_category_topline_set_total_gmv(month: str, platform: str, payload: TotalGmvIn):
    """The one hard input for a no-expert-data platform's forecast: the overall platform GMV for
    this month - entered either as an absolute GMV, or as a MoM%/YoY% growth rate resolved
    against this platform's own last-actual-month/year-ago-month Total GMV (read from the live
    Category Topline workbook). Free to set/edit while still in the 'analyst' stage; locked once
    sent for SL review, same as every other analyst-stage edit in this app.

    Setting it immediately (re)computes and writes the full category-level forecast for this
    month into the Category Topline workbook - no separate trigger needed, per the agreed design
    ("once a platform is passed from monthly setup as no expert data, the category topline should
    be automatically calculated"). Writing a real column (not just storing a number in state)
    means every other part of the system - the normal read view, the month multi-select, future
    Manual Change review - sees it automatically, with no special-casing anywhere else."""
    provided = [v for v in (payload.total_gmv, payload.mom_pct, payload.yoy_pct) if v is not None]
    if len(provided) != 1:
        raise HTTPException(400, "Provide exactly one of total_gmv, mom_pct, or yoy_pct")

    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        if ct["status"] != "analyst":
            raise HTTPException(400, "Total GMV can only be set while Category Topline is at the analyst stage")

        month_label = month.replace("'", "-")
        all_months, rows = _get_topline_rows("all", platform)
        # Excludes this platform's OWN previously-written forecast for this same month (if this
        # is a re-run before being sent to review) from the historical input - otherwise a second
        # recompute would forecast off its own first guess instead of off real history.
        cutoff = all_months.index(month_label) if month_label in all_months else len(all_months)

        if payload.total_gmv is not None:
            resolved_gmv, mode, raw_value = payload.total_gmv, "gmv", payload.total_gmv
        else:
            total_row = next(r for r in rows if r["kind"] == "total")
            total_gmv_hist = total_row["gmv"][:cutoff]
            if payload.mom_pct is not None:
                prev_total = total_gmv_hist[-1] if total_gmv_hist else None
                if not prev_total:
                    raise HTTPException(400, f"No prior actual month's Total GMV on file for '{platform}' to apply MoM% to")
                resolved_gmv, mode, raw_value = prev_total * (1 + payload.mom_pct / 100), "mom", payload.mom_pct
            else:
                yoy_total = total_gmv_hist[-12] if len(total_gmv_hist) >= 12 else None
                if not yoy_total:
                    raise HTTPException(400, f"No year-ago month's Total GMV on file for '{platform}' to apply YoY% to")
                resolved_gmv, mode, raw_value = yoy_total * (1 + payload.yoy_pct / 100), "yoy", payload.yoy_pct

        rows_for_forecast = [{**r, "share": r["share"][:cutoff]} for r in rows]
        # Expert-data platforms still use this exact same manual Total-GMV trigger (not an
        # automatic fire on finalize) - only the per-category calculation changes, aligning each
        # row's Market Share to what the finalized expert data implies rather than pure ETS+Trend.
        # Uses the SAME current-month-aware lookup the Expert MoM/YoY Preview and Manual Change
        # already use (finalized Monthly Setup tagged file first, Raw expert data as fallback) -
        # confirmed this was a real bug: the plain Raw-expert-data lookup alone almost never
        # covers the CURRENT in-progress month at all (it's a slower, independently-updating
        # benchmark file, not the just-finalized tagging), so the forecast/alignment engine was
        # silently running with ZERO expert signal for the one month this feature exists for,
        # even though a platform's expert data had genuinely just been finalized.
        expert_gmv_lookup = _build_current_month_expert_lookup(platform, month_label) if p["has_expert_data"] else {}
        # Manual YoY/MoM overrides (see CategoryYoyOverrideIn) fill in ONLY the categories real
        # expert data doesn't cover - applies regardless of whether this platform has expert data
        # at all, since the gap can be "this whole platform has none" or "this one category wasn't
        # covered this month" either way. Real expert data always wins where both exist.
        manual_lookup = _manual_yoy_override_lookup(platform, month_label, rows_for_forecast)
        expert_gmv_lookup = {**manual_lookup, **expert_gmv_lookup} or None
        forecast = _compute_category_topline_forecast(rows_for_forecast, resolved_gmv, expert_gmv_lookup)
        sheet = _topline_sheet_name("all", platform)
        _write_forecast_column(TOPLINE_FILES["all"], sheet, month_label, rows, forecast)
        _write_forecast_column(TOPLINE_FILES["md_est"], f"{platform} (MD Est)"[:31], month_label, rows, forecast)
        _REPORT_CACHE["topline_platforms"].clear()
        _REPORT_CACHE["topline_rows"].clear()

        # Brand Share Cons: step 1 - its own Total/L0/L1/L2 rows are "owned by Category Topline"
        # (see _build_bsc_edit_rows's docstring) - propagate the same just-computed category
        # totals straight into BSC's matching rows (same _row_key, different row_num/workbook).
        # Step 2 - brand rows within each L2 (or flat L0) group get their OWN ETS+Trend+expert-
        # alignment+reconciliation forecast, one level deeper, using that same just-propagated
        # category GMV as their group's parent total. Both are written in one pass by merging
        # the two forecast dicts (they key disjoint row kinds, so no collision).
        expert_brand_gmv_lookup = _build_expert_brand_gmv_lookup(platform, month_label) if p["has_expert_data"] else None
        for variant, bsc_path in BSC_FILES.items():
            bsc_sheet = sheet if variant == "all" else f"{platform} (MD Est)"[:31]
            if not bsc_path.exists():
                continue
            try:
                bsc_months, bsc_rows = _get_bsc_rows(variant, platform)
            except HTTPException:
                continue  # no matching platform sheet in this BSC workbook - nothing to propagate
            bsc_cutoff = bsc_months.index(month_label) if month_label in bsc_months else len(bsc_months)
            bsc_rows_for_forecast = [{**r, "share": r["share"][:bsc_cutoff]} for r in bsc_rows]
            brand_forecast = _compute_brand_share_forecast(bsc_rows_for_forecast, forecast, expert_brand_gmv_lookup)
            _write_forecast_column(bsc_path, bsc_sheet, month_label, bsc_rows, {**forecast, **brand_forecast})
        _REPORT_CACHE["bsc_platforms"].clear()
        _REPORT_CACHE["bsc_rows"].clear()

        ct["total_gmv_input"] = resolved_gmv
        ct["gmv_input_mode"] = mode
        ct["gmv_input_value"] = raw_value
        ct["forecast"] = {
            "month_label": month_label,
            "rows": {k: {"forecast_share": v["forecast_share"], "trend_share": v["trend_share"],
                         "expert_mom": v["expert_mom"], "expert_yoy": v["expert_yoy"],
                         "expert_flag": v["expert_flag"]}
                     for k, v in forecast.items()},
        }
        mode_label = {"gmv": "Total GMV", "mom": "MoM%", "yoy": "YoY%"}[mode]
        ct.setdefault("history", []).append({
            "event": f"{mode_label} input set to {raw_value} -> Total GMV {round(resolved_gmv, 2)}; category forecast computed",
            "at": _ts(),
        })
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/send-to-review")
def monthly_setup_category_topline_send_to_review(month: str, platform: str):
    """Analyst hands off to SL - no password (only the two sign-off steps below are gated)."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        if not p["has_expert_data"] and ct["total_gmv_input"] is None:
            raise HTTPException(400, "Enter this platform's Total GMV (or MoM%/YoY%) before sending it to review")
        _transition_category_topline(ct, "send_to_review", error_message="Already sent to SL review or beyond")
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/send-back-to-analyst")
def monthly_setup_category_topline_send_back_to_analyst(month: str, platform: str):
    """SL rejects back to the analyst - no password needed, same as the expert-data send-back."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        _transition_category_topline(ct, "send_back_to_analyst", error_message="Only a platform pending SL review can be sent back")
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/sl-approve")
def monthly_setup_category_topline_sl_approve(month: str, platform: str, payload: AdminPasswordIn):
    """SL's half of the two-step verification - approves and hands off to the Manager for the
    final sign-off. SL-password gated, same reasoning as every other SL approval in this app."""
    if not _verify_sl_password(payload.password):
        raise HTTPException(403, "Incorrect SL password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        _transition_category_topline(ct, "sl_approve", error_message="Send to SL review first")
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/send-back-to-sl")
def monthly_setup_category_topline_send_back_to_sl(month: str, platform: str):
    """Manager rejects back to SL - no password needed (only the two approval steps are gated)."""
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        _transition_category_topline(ct, "send_back_to_sl", error_message="Only a platform pending Manager review can be sent back")
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/manager-finalize")
def monthly_setup_category_topline_manager_finalize(month: str, platform: str, payload: AdminPasswordIn):
    """Manager's half of the two-step verification - the one action that actually commits this
    platform's published Category Topline numbers. Manager-password gated - distinct from SL's
    own password, for genuine two-person accountability."""
    if not _verify_manager_password(payload.password):
        raise HTTPException(403, "Incorrect Manager password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        _transition_category_topline(ct, "manager_finalize", error_message="SL must approve before Manager can finalize")
        ct["finalized_at"] = _ts()
        _save_monthly_state(state)
        return ct


@app.post("/monthly-setup/{month}/{platform}/category-topline/reopen")
def monthly_setup_category_topline_reopen(month: str, platform: str, payload: AdminPasswordIn):
    """Undoes the Manager's finalize - Manager-password gated, same as finalizing it in the
    first place. Drops back to pending Manager review (not all the way to analyst), since SL's
    own approval still stands."""
    if not _verify_manager_password(payload.password):
        raise HTTPException(403, "Incorrect Manager password")
    with _MONTHLY_LOCK:
        state = _load_monthly_state()
        _m, p = _monthly_any_platform_entry(state, month, platform, require_open=True)
        ct = _ensure_category_topline(p)
        _transition_category_topline(ct, "reopen", error_message="Only a finalized Category Topline can be reopened")
        ct["finalized_at"] = None
        _save_monthly_state(state)
        return ct


# --------------------------------------------------------------------------
# Tab 3: Rule Glossary
# --------------------------------------------------------------------------
def _build_glossary():
    rows = []
    approved_lookup = {
        (e.get("keyword", "").strip().lower(), (e.get("modifier") or "").strip().lower()): e
        for e in _load_approved()
    }

    for keyword, (l1, l2, l3) in p6.DIRECT_KEYWORDS.items():
        rows.append(
            {"rule_type": "Direct Keyword", "keyword": keyword, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "High", "notes": ""}
        )

    for base, rules in p6.CONTEXT_MATRIX.items():
        for mod, l1, l2, l3, conf in rules:
            approved = approved_lookup.get((base.strip().lower(), (mod or "").strip().lower()))
            if approved:
                notes = f"Approved via Glossary Pending Review on {approved.get('approved_at', '?')}"
            else:
                notes = "" if l1 else "Ambiguous - flags for manual review, no auto-tag"
            rows.append(
                {"rule_type": "Context Matrix", "keyword": base, "modifier": mod or "(bare word)",
                 "l1": l1 or "", "l2": l2 or "", "l3": l3 or "", "confidence": conf or "", "notes": notes}
            )

    for phrase, l1, l2, l3 in p6.SPECIFICITY_PHRASES:
        rows.append(
            {"rule_type": "Specificity Phrase", "keyword": phrase, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "High", "notes": ""}
        )

    for keyword, (l1, l2, l3) in p6.LOW_PRIORITY_KEYWORDS.items():
        rows.append(
            {"rule_type": "Low Priority Keyword", "keyword": keyword, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "Medium", "notes": "Only applies if nothing more specific matched"}
        )

    for text, (l1, l2, l3) in p6.SKU_MEMORY.items():
        rows.append(
            {"rule_type": "SKU Memory", "keyword": text, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "High", "notes": "Exact-match override"}
        )

    for keyword, (l1, l2, l3) in kw.TIER0_HARDWARE.items():
        rows.append(
            {"rule_type": "Tier0 Hardware", "keyword": keyword, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "High", "notes": "Checked before Direct Keywords"}
        )

    for keyword, l3 in kw.FEMININE_HYGIENE_KEYWORDS.items():
        rows.append(
            {"rule_type": "Feminine Hygiene", "keyword": keyword, "modifier": "",
             "l1": "Others", "l2": "Feminine Hygiene", "l3": l3, "confidence": "High", "notes": ""}
        )

    for keyword, l3 in kw.BABY_CARE_KEYWORDS.items():
        rows.append(
            {"rule_type": "Baby Care", "keyword": keyword, "modifier": "",
             "l1": "Others", "l2": "Baby Care", "l3": l3, "confidence": "High", "notes": ""}
        )

    # These are bespoke multi-word conditions checked in code (pipeline_v6.py's _tag_sku_core),
    # not simple keyword+modifier dict entries - shown here as plain informational rows so they're
    # still visible in the glossary even though they can't be represented as a Context Matrix row.
    SPECIAL_CASE_RULES = [
        ("strobe / strobing (any format)", "Make up", "Face make-up", "Highlighters & Illuminators", ""),
        ("contour (any format, excl. brush)", "Make up", "Face make-up", "Contour", ""),
        ("camouflage + cream/concealer", "Make up", "Face make-up", "Concealer", "Same family as a regular Concealer"),
        ("cc serum", "Make up", "Face make-up", "CC Creams", "Wins over the serum base's own spf/tinted modifiers"),
        ("bb serum", "Make up", "Face make-up", "Bb Creams", "Wins over the serum base's own spf/tinted modifiers"),
        ("pre-makeup serum / makeup serum", "Make up", "Face make-up", "Primers", ""),
        ("serum + hair-signal word (bond repair, strong roots, hair fall, anti frizz, peptide, anti grey/gray)", "Hair Care", "Hair Serums", "Hair Serums", "Only when the SKU doesn't also say face/facial"),
        ("lotion + dandruff", "Hair Care", "Hair Serums", "Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums", ""),
        ("brush + vega/ikonic/majestique (no other qualifying word)", "Others", "Others Others", "Hair Accessories", "Brand-only fallback, Medium confidence"),
    ]
    for keyword, l1, l2, l3, notes in SPECIAL_CASE_RULES:
        rows.append(
            {"rule_type": "Special Case (code)", "keyword": keyword, "modifier": "",
             "l1": l1, "l2": l2, "l3": l3, "confidence": "High", "notes": notes}
        )

    rows.append(
        {"rule_type": "Bundle Marker (Strong)", "keyword": ", ".join(bd.STRONG_BUNDLE_MARKERS),
         "modifier": "", "l1": "", "l2": "", "l3": "Sets/Combos", "confidence": "",
         "notes": "Any of these + 2+ distinct product hits triggers Bundle-* logic in bundle_detect.py"}
    )
    rows.append(
        {"rule_type": "Bundle Marker (Weak)", "keyword": ", ".join(bd.WEAK_BUNDLE_MARKERS),
         "modifier": "", "l1": "", "l2": "", "l3": "", "confidence": "",
         "notes": "Weaker signal - still requires 2+ distinct product hits to trigger a bundle result"}
    )
    return rows


@app.get("/glossary")
def glossary():
    return {"rules": _build_glossary()}


# --------------------------------------------------------------------------
# Tab 4: Regression Check
# --------------------------------------------------------------------------
@app.post("/regression-test")
def regression_test():
    if not GOLDEN_FILE.exists():
        raise HTTPException(404, "golden_tests.json not found next to app.py")
    cases = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))

    results = []
    passed = 0
    for case in cases:
        result = tag_sku_bundle_aware(case["product_name"])
        ok = True
        for level in ("l1", "l2", "l3"):
            expected = case.get(f"expected_{level}")
            if expected and result.get(level.upper()) != expected:
                ok = False
        passed += ok
        results.append(
            {
                "product_name": case["product_name"],
                "expected": f"{case.get('expected_l1','')} > {case.get('expected_l2','')} > {case.get('expected_l3','')}",
                "got": f"{result.get('L1')} > {result.get('L2')} > {result.get('L3')}",
                "method": result.get("method"),
                "pass": ok,
            }
        )
    return {"passed": passed, "total": len(cases), "results": results}


# --------------------------------------------------------------------------
# Tab 5: Suggest a New Rule (staged - never writes into the live engine)
# --------------------------------------------------------------------------
class RuleSuggestion(BaseModel):
    keyword: str
    modifier: Optional[str] = ""
    l1: str
    l2: str
    l3: str
    reason: str


@app.get("/pending-rules")
def pending_rules():
    return _load_pending()


class RulePreviewIn(BaseModel):
    keyword: str
    modifier: Optional[str] = ""
    l1: Optional[str] = ""
    l2: Optional[str] = ""
    l3: Optional[str] = ""


@app.post("/pending-rules/preview")
def preview_pending_rule(payload: RulePreviewIn):
    """Sanity-check step before a manual rule gets submitted: does this (keyword, modifier)
    already exist anywhere (live engine or pending queue), and what would the engine currently
    tag it as? Both are shown to the user before they can submit - never applied automatically."""
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(400, "keyword is required")
    modifier = (payload.modifier or "").strip()
    test_text = f"{modifier} {keyword}".strip()
    current = tag_sku_bundle_aware(test_text)
    duplicate = _existing_rule_match(keyword, modifier)
    return {"test_text": test_text, "current_tag": current, "duplicate": duplicate}


@app.post("/pending-rules")
def add_pending_rule(rule: RuleSuggestion):
    with _PENDING_LOCK:
        rules = _load_pending()
        entry = rule.dict()
        entry["id"] = uuid.uuid4().hex[:8]
        entry["status"] = "PENDING REVIEW"
        entry["source"] = "Manual"
        entry["examples"] = []
        rules.append(entry)
        _save_pending(rules)
    return entry


@app.delete("/pending-rules/{rule_id}")
def delete_pending_rule(rule_id: str):
    with _PENDING_LOCK:
        rules = _load_pending()
        remaining = [r for r in rules if r.get("id") != rule_id]
        if len(remaining) == len(rules):
            raise HTTPException(404, "Unknown rule id")
        PENDING_FILE.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
    return {"deleted": rule_id}


class BulkIdsIn(BaseModel):
    ids: list[str]


class ApprovalIn(BaseModel):
    id: str
    l1: str
    l2: str
    l3: str


class BulkApproveIn(BaseModel):
    approvals: list[ApprovalIn]


@app.post("/pending-rules/bulk-approve")
def bulk_approve_pending(payload: BulkApproveIn):
    """Moves the selected pending entries into approved_rules.json and immediately reloads the
    live engine's in-memory Context Matrix from it - the glossary and any tagging from this point
    on reflect the approval right away, with no server restart and no .py file ever touched.

    L1/L2/L3 come from the request, NOT the original pending entry - the auto-suggested category
    on an Auto-Verification candidate is just whatever generic default the base word already
    falls to, not a verified answer, so the reviewer can correct it right here before approving
    rather than being stuck with whatever the engine happened to guess."""
    overrides = {a.id: a for a in payload.approvals}
    with _PENDING_LOCK:
        pending = _load_pending()
        to_approve = [r for r in pending if r.get("id") in overrides]
        if not to_approve:
            raise HTTPException(404, "No matching pending rule ids found")
        remaining = [r for r in pending if r.get("id") not in overrides]

        approved = _load_approved()
        ts = datetime.now().isoformat(timespec="seconds")
        for r in to_approve:
            entry = dict(r)
            override = overrides[r["id"]]
            entry["l1"] = override.l1.strip()
            entry["l2"] = override.l2.strip()
            entry["l3"] = override.l3.strip()
            entry["approved_at"] = ts
            approved.append(entry)
        _save_approved(approved)
        _save_pending(remaining)
        p6.reload_approved_rules()
    return {"approved": [r["id"] for r in to_approve], "count": len(to_approve)}


@app.post("/pending-rules/bulk-reject")
def bulk_reject_pending(payload: BulkIdsIn):
    ids = set(payload.ids)
    with _PENDING_LOCK:
        pending = _load_pending()
        remaining = [r for r in pending if r.get("id") not in ids]
        rejected_count = len(pending) - len(remaining)
        if rejected_count == 0:
            raise HTTPException(404, "No matching pending rule ids found")
        _save_pending(remaining)
    return {"rejected_count": rejected_count}


# --------------------------------------------------------------------------
# Tab: Category Topline (read-only view of the live workbooks)
# --------------------------------------------------------------------------
def _topline_sheet_name(variant, platform):
    return f"{platform} (MD Est)"[:31] if variant == "md_est" else platform


def _workbook_platforms(path):
    """Sheet names, stripped of the '(MD Est)' suffix if present. These workbooks are built
    entirely by this app's own report generator (see build scripts), so every sheet present is
    a real platform - no whitelist needed to filter out stray leftover sheets."""
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = wb.sheetnames
    finally:
        wb.close()
    suffix = " (MD Est)"
    return [n[: -len(suffix)] if n.endswith(suffix) else n for n in names]


def _workbook_months(path, sheet_name=None):
    """Full month-label list for a report workbook, read from its own header row (self-
    describing format: 'GMV <label>' columns) - used to populate the date-range picker with
    every month actually in the file, independent of the display window a request asks for."""
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = sheet_name or wb.sheetnames[0]
        header = next(wb[sheet].iter_rows(min_row=1, max_row=1, values_only=True))
    finally:
        wb.close()
    return [h[4:] for h in header if isinstance(h, str) and h.startswith("GMV ")]


# These report workbooks live on OneDrive and can be slow to open on the first touch after a
# change (sync/indexing contention) - server-side caching means that cost is paid once, not on
# every tab click. A "Refresh" in the UI calls the matching /reload endpoint to bust it.
_REPORT_CACHE = {"topline_platforms": {}, "topline_rows": {}, "bsc_platforms": {}, "bsc_rows": {}}


@app.post("/category-topline/reload")
def category_topline_reload():
    _REPORT_CACHE["topline_platforms"].clear()
    _REPORT_CACHE["topline_rows"].clear()
    return {"status": "cleared"}


def _workbook_all_months(path):
    """Union of every month-label ANY sheet in this workbook has, sorted chronologically - NOT
    just one reference sheet's own months. A no-expert-data platform's forecasted current month
    is written to only THAT platform's own sheet (never all of them at once the way a real
    Master-Data-driven month is), so a single reference sheet's month list would silently miss it
    - exactly what caused the month picker not to show a freshly-computed month after Save &
    Compute. Months absent from a given platform's own sheet are simply not returned for it by
    /category-topline's own _pick_months - this union only decides what's offered to pick from."""
    if not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        months = set()
        for sheet in wb.sheetnames:
            header = next(wb[sheet].iter_rows(min_row=1, max_row=1, values_only=True))
            months.update(h[4:] for h in header if isinstance(h, str) and h.startswith("GMV "))
    finally:
        wb.close()
    return sorted(months, key=lambda s: datetime.strptime(s, "%b-%y"))


@app.get("/category-topline/meta")
def category_topline_meta(platform: str = ""):
    out = {}
    months = []
    for variant, path in TOPLINE_FILES.items():
        if variant not in _REPORT_CACHE["topline_platforms"]:
            _REPORT_CACHE["topline_platforms"][variant] = _workbook_platforms(path)
        out[variant] = _REPORT_CACHE["topline_platforms"][variant]
        if not months and path.exists():
            months = _workbook_all_months(path)
    # The month-select checkbox panel is built from this list - without this, a platform whose
    # expert data just got finalized (but has no Total GMV entered yet) would never be offerable
    # as a month to view at all, even though /category-topline itself already returns it (see
    # _merge_category_topline_forecast's append-if-missing logic for the Expert MoM/YoY preview).
    if platform:
        state = _load_monthly_state()
        for month_key, m in state.get("months", {}).items():
            p = m.get("platforms", {}).get(platform)
            if p and p.get("status") == "finalized" and p.get("final_file"):
                month_label = month_key.replace("'", "-")
                if month_label not in months:
                    months.append(month_label)
    return {"variants": out, "months": months}


def _slice_range(months, from_month, to_month):
    """Index bounds [start, end) into the full month list for an optional display window.
    Falls back to the whole range if a bound is missing/unrecognized, rather than erroring -
    a stale 'from'/'to' after the underlying file changes should degrade gracefully."""
    start = months.index(from_month) if from_month in months else 0
    end = (months.index(to_month) + 1) if to_month in months else len(months)
    return start, max(end, start + 1)


_FORMULA_RE = re.compile(r"^=([A-Z]+\d+)((?:[+-][A-Z]+\d+)*)$")
_FORMULA_TERM_RE = re.compile(r"([+-])([A-Z]+\d+)")


def _resolve_cell(ref, raw_cache, resolved_cache):
    """Resolves one cell's true numeric value, evaluating a formula if it has one. Formulas
    written by the report builder are always a simple '=Cell-Cell-Cell...' chain (a parent cell
    minus its sibling cells) and always reference earlier rows only (the one dynamic slot in a
    group is always the LAST row in that group), so a single top-to-bottom, left-to-right pass
    always resolves a cell's dependencies before the cell itself is needed - no recursion, no
    dependency ordering to solve, unlike a general-purpose spreadsheet engine."""
    if ref in resolved_cache:
        return resolved_cache[ref]
    raw = raw_cache.get(ref)
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str) and raw.startswith("="):
        m = _FORMULA_RE.match(raw)
        if not m:
            value = 0.0  # unrecognized formula shape - shouldn't happen for this report format
        else:
            first_ref, rest = m.groups()
            value = _resolve_cell(first_ref, raw_cache, resolved_cache)
            for sign, term_ref in _FORMULA_TERM_RE.findall(rest):
                term_val = _resolve_cell(term_ref, raw_cache, resolved_cache)
                value += term_val if sign == "+" else -term_val
    else:
        value = 0.0
    # GMV/Units can never be legitimately negative. A plug cell (parent-minus-siblings) can land
    # a hair below zero from float noise on a near-zero "Others" bucket, or from a forecasted
    # month's siblings independently summing to just over 100% - floored here, once, so every
    # caller (both reports, historical and forecasted months alike) sees a real number instead of
    # a nonsense negative. Share/MoM/YoY are all derived from this value afterward, so this alone
    # is enough to keep them from showing nonsensical negative GMV or negative Market Share too.
    resolved_cache[ref] = max(0.0, value)
    return resolved_cache[ref]


def _read_report_sheet(ws, with_units):
    """Reads the self-describing report format shared by Category Topline and Brand Share
    Cons: a header row of 'GMV <month>' columns (and 'Units <month>' columns for BSC), then one
    row per category/brand carrying its own Kind/Level/Label/L0/L1/L2. Some cells are live Excel
    formulas (see build script) rather than plain values - data_only mode would return None for
    those until a human opens+saves the file in real Excel, so this reads raw cell content and
    evaluates any formula itself via _resolve_cell. Share is computed against the parent looked
    up by L0/L1/L2 fields (not row position), and growth/ASP are computed once here over the
    FULL month range so a later display-window slice never loses the true prior-month value a
    boundary month's growth needs."""
    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    gmv_idx = [i for i, h in enumerate(header) if isinstance(h, str) and h.startswith("GMV ")]
    n = len(gmv_idx)
    units_idx = [i for i, h in enumerate(header) if isinstance(h, str) and h.startswith("Units ")] if with_units else []

    raw_cache = {}  # 'G6' -> raw cell content (number or formula string)
    resolved_cache = {}  # 'G6' -> resolved float, filled in lazily by _resolve_cell
    all_data_idx = sorted(set(gmv_idx) | set(units_idx))
    data_rows = []
    for r_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row[0] is None:
            continue
        data_rows.append((r_idx, row))
        for i in all_data_idx:
            col_letter = get_column_letter(i + 1)
            raw_cache[f"{col_letter}{r_idx}"] = row[i] if i < len(row) else None

    def resolved_row_values(r_idx, idx_list):
        return [_resolve_cell(f"{get_column_letter(i + 1)}{r_idx}", raw_cache, resolved_cache) for i in idx_list]

    rows = []
    total_gmv = None
    l0_gmv, l1_gmv, l2_gmv = {}, {}, {}
    for r_idx, row in data_rows:
        kind, level, label, l0, l1, l2 = row[0], row[1], row[2], row[3], row[4], row[5]
        gmv = resolved_row_values(r_idx, gmv_idx)
        units = resolved_row_values(r_idx, units_idx) if with_units else None

        if kind == "total":
            parent = None
            total_gmv = gmv
        elif kind in ("l0", "l0_flat"):
            parent = total_gmv
            l0_gmv[l0] = gmv
        elif kind == "l1":
            parent = l0_gmv.get(l0)
            l1_gmv[(l0, l1)] = gmv
        elif kind == "l2":
            parent = l1_gmv.get((l0, l1))
            l2_gmv[(l0, l1, l2)] = gmv
        else:  # brand / others
            # Normally nested under an L2; a brand sitting directly under a flat L0 bucket (no
            # L1/L2 levels at all) has l1==l2==l0 by convention - fall back to the L0 total.
            parent = l2_gmv.get((l0, l1, l2), l0_gmv.get(l0))

        if kind == "total":
            share = [1.0 for _ in gmv]
        elif parent is None:
            share = [None] * n
        else:
            share = [(v / p) if p else None for v, p in zip(gmv, parent)]
        growth = [None] + [
            ((gmv[i] - gmv[i - 1]) / gmv[i - 1]) if gmv[i - 1] else None
            for i in range(1, n)
        ]
        # Year-on-year: same calendar month, 12 columns back. Computed over the FULL month
        # range (like growth above) so a display-side month pick can still show YoY for a month
        # whose year-ago comparator wasn't itself selected.
        yoy = [
            ((gmv[i] - gmv[i - 12]) / gmv[i - 12]) if i >= 12 and gmv[i - 12] else None
            for i in range(n)
        ]
        entry = {"label": label, "kind": kind, "level": level, "l0": l0, "l1": l1, "l2": l2,
                 "gmv": gmv, "share": share, "growth": growth, "yoy": yoy, "row_num": r_idx}
        if with_units:
            entry["units"] = units
            entry["asp"] = [(g * 1e7 / u) if u else None for g, u in zip(gmv, units)]
        rows.append(entry)
    return rows, raw_cache, gmv_idx, units_idx


def _pick_months(months, requested):
    """Indices of a caller-chosen, non-contiguous subset of months - used by Category Topline's
    month multi-select (pick exactly the months you want, not a from/to range). Preserves the
    workbook's own chronological order regardless of the order the caller listed them in. An
    empty/blank 'requested' means "all months", so a first-time visit shows everything."""
    wanted = requested.strip()
    if not wanted:
        return list(range(len(months)))
    picked = {m.strip() for m in wanted.split(",") if m.strip()}
    return [i for i, m in enumerate(months) if m in picked]


def _get_topline_rows(variant, platform):
    """Loads (and caches) one platform's Category Topline sheet - shared by the read-only GET
    endpoint below AND the forecast-computation code in the Monthly Setup section (set-total-gmv
    needs this exact same historical share/GMV data as its forecasting input, and the GMV
    MoM%/YoY% hard-input modes need the Total row's own GMV history)."""
    cache_key = (variant, platform)
    if cache_key not in _REPORT_CACHE["topline_rows"]:
        path = TOPLINE_FILES.get(variant)
        if path is None or not path.exists():
            raise HTTPException(404, f"Workbook not found for variant '{variant}'")
        sheet = _topline_sheet_name(variant, platform)
        # data_only=False: some cells are live formulas (see build script) - data_only=True
        # would return openpyxl's cached-at-last-save value, which is None until a human opens
        # and saves this file in real Excel. _read_report_sheet evaluates those formulas itself.
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            if sheet not in wb.sheetnames:
                raise HTTPException(404, f"Sheet '{sheet}' not found in {path.name}")
            # NOTE: must not be named `months` - that would shadow the /category-topline GET
            # endpoint's own `months` query-string param (the caller's requested subset) on a
            # cache-miss request, which is exactly what happened here before: the very first
            # request for any given platform (or the first after a /reload) crashed in
            # _pick_months with "'list' object has no attribute 'strip'" because this line had
            # silently replaced the query string with the full workbook month list.
            wb_months = _workbook_months(path, sheet)
            rows, _raw, _gmv_idx, _units_idx = _read_report_sheet(wb[sheet], with_units=False)
        finally:
            wb.close()
        _REPORT_CACHE["topline_rows"][cache_key] = (wb_months, rows)
    return _REPORT_CACHE["topline_rows"][cache_key]


def _preview_lookup_key(r):
    """Same shape as _expert_lookup_key, extended to the 'total' row (which Category Topline has
    but the Raw-expert-data-based lookup never needed to) - used only for the finalized-tagged-
    data preview below, not the forecast-alignment engine."""
    if r["kind"] == "total":
        return ("total", None, None, None)
    return _expert_lookup_key(r)


_FINALIZED_TAGGED_LOOKUP_CACHE = {}  # str(path) -> (mtime, lookup dict)


def _build_finalized_tagged_gmv_lookup(path):
    """Reads one platform/month's finalized Monthly Setup tagged output (the 'Tagged Data' sheet
    _process_category_bulk_job produces - New_L0/New_L1/New_L2/'GMV in cr ' columns) and rolls it
    up to Total/L0/L1/L2 GMV totals for that single month, keyed like _preview_lookup_key reads a
    Category Topline row. This is the REAL, reviewed, finalized tagged data for the current
    in-progress month - not the separate Raw expert data benchmark file - used only for the
    Expert MoM/YoY PREVIEW columns below. Empty (not an error) if the file can't be read.

    Cached in memory per (path, mtime) - confirmed this file (78,545 rows for Flipkart) took a
    consistent ~6.5-7s to read on EVERY call with no caching at all, and this function is called
    on every single /category-topline request for a finalized platform, directly explaining why
    Category Topline stayed slow for Flipkart even after Master Data/Raw expert data were fully
    warmed. A finalized tagged file never changes in place (a re-finalize would write a new
    Final.xlsx and change its mtime), so this cache needs no explicit invalidation endpoint."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _FINALIZED_TAGGED_LOOKUP_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        df = pd.read_excel(path, sheet_name="Tagged Data",
                            usecols=["New_L0", "New_L1", "New_L2", "GMV in cr "])
    except Exception:
        return {}

    # See the shared _L0_ALIASES constant for why this correction is needed - this tagged file's
    # own New_L0 column has the same "Makeup and Fragrances" (plural) convention.
    def norm(v):
        n = str(v or "").strip().lower()
        return _L0_ALIASES.get(n, n)

    l2_totals = df.groupby([df["New_L0"].map(norm), df["New_L1"].map(norm),
                             df["New_L2"].map(norm)])["GMV in cr "].sum()
    lookup, l1_totals, l0_totals = {}, {}, {}
    for (l0, l1, l2), val in l2_totals.items():
        val = float(val)
        lookup[("l2", l0, l1, l2)] = val
        l1_totals[(l0, l1)] = l1_totals.get((l0, l1), 0.0) + val
        l0_totals[l0] = l0_totals.get(l0, 0.0) + val
    for (l0, l1), val in l1_totals.items():
        lookup[("l1", l0, l1, None)] = val
    for l0, val in l0_totals.items():
        lookup[("l0", l0, None, None)] = val
    lookup[("total", None, None, None)] = sum(l0_totals.values())
    _FINALIZED_TAGGED_LOOKUP_CACHE[key] = (mtime, lookup)
    return lookup


def _merge_category_topline_forecast(variant, platform, out_months, sliced_rows):
    """Attaches 'forecast'/'trend'/'expert_mom'/'expert_yoy' Market Share diagnostic columns to
    the response (see _compute_category_topline_forecast) - never written into the workbook
    itself, so they're looked up from Monthly Setup's own state instead of the report file. None
    for every historical (real) month and for every platform that doesn't currently have a
    forecasted month at all. expert_mom/expert_yoy are only non-None for an expert-data platform
    whose Total GMV has been (re)computed since its expert data was consolidated into Master
    Data - they're what the analyst/SL use to judge whether the computed MS actually lines up
    with what the expert data says, and to decide whether a manual override is warranted.

    Also attaches 'expert_preview_abs'/'expert_preview_mom'/'expert_preview_yoy' - confirmed
    non-negotiable: as soon as a platform's expert-data tagging is FINALIZED for a month, these
    must appear, regardless of whether Category Topline's own Total GMV has been entered yet (the
    whole point is to inform that decision, not follow it) - so if the finalized month isn't
    already a real column in the workbook, it's appended here with every OTHER field left None,
    just these three populated."""
    for r in sliced_rows:
        r["forecast"] = [None] * len(out_months)
        r["trend"] = [None] * len(out_months)
        r["expert_mom"] = [None] * len(out_months)
        r["expert_yoy"] = [None] * len(out_months)
        r["expert_flag"] = [None] * len(out_months)
        r["expert_preview_abs"] = [None] * len(out_months)
        r["expert_preview_mom"] = [None] * len(out_months)
        r["expert_preview_yoy"] = [None] * len(out_months)
    if variant != "all":
        return
    state = _load_monthly_state()
    for month_key, m in state.get("months", {}).items():
        p = m.get("platforms", {}).get(platform)
        if not p:
            continue
        fc = (p.get("category_topline") or {}).get("forecast")
        if fc and fc.get("month_label") in out_months:
            pos = out_months.index(fc["month_label"])
            rows_meta = fc.get("rows", {})
            for r in sliced_rows:
                meta = rows_meta.get(_row_key(r))
                if meta:
                    r["forecast"][pos] = meta.get("forecast_share")
                    r["trend"][pos] = meta.get("trend_share")
                    r["expert_mom"][pos] = meta.get("expert_mom")
                    r["expert_yoy"][pos] = meta.get("expert_yoy")
                    r["expert_flag"][pos] = meta.get("expert_flag")

        if p.get("status") != "finalized" or not p.get("final_file"):
            continue
        month_label = month_key.replace("'", "-")
        if month_label not in out_months:
            out_months.append(month_label)
            for r in sliced_rows:
                for field in ("gmv", "share", "growth", "yoy", "forecast", "trend", "expert_mom",
                              "expert_yoy", "expert_flag", "expert_preview_abs",
                              "expert_preview_mom", "expert_preview_yoy"):
                    r[field].append(None)
        pos = out_months.index(month_label)
        tagged_lookup = _build_finalized_tagged_gmv_lookup(Path(p["final_file"]))
        if not tagged_lookup:
            continue
        # Confirmed explicitly: this must be a PURE expert-data-to-expert-data comparison - both
        # sides from the Raw expert data file, never Master Data. Mixing them is not just a style
        # choice, it produces genuinely wrong numbers: Flipkart's Sep-25 Master Data total (980.8
        # Cr) and its own Raw expert data total for that same month (1107.4 Cr) differ by ~11%
        # (Master Data's consolidation for that month is simply stale relative to the Raw expert
        # data file - confirmed, not a tagging bug), so comparing this month's tagged total
        # against Master Data's year-ago figure silently blends two different, disagreeing
        # sources into one "YoY" that matches neither.
        prior_month_label = _shift_month_label(month_label, -1)
        yearago_month_label = _shift_month_label(month_label, -12)
        prior_lookup = _build_expert_gmv_lookup(platform, prior_month_label) if prior_month_label else {}
        yearago_lookup = _build_expert_gmv_lookup(platform, yearago_month_label) if yearago_month_label else {}
        for r in sliced_rows:
            key = _preview_lookup_key(r)
            current = tagged_lookup.get(key)
            if current is None:
                continue
            prior_gmv = prior_lookup.get(key)
            yearago_gmv = yearago_lookup.get(key)
            r["expert_preview_abs"][pos] = current
            r["expert_preview_mom"][pos] = (current / prior_gmv - 1) if prior_gmv else None
            r["expert_preview_yoy"][pos] = (current / yearago_gmv - 1) if yearago_gmv else None


@app.get("/category-topline")
def category_topline(variant: str = "all", platform: str = "Blinkit", months: str = ""):
    all_months, rows = _get_topline_rows(variant, platform)

    idx = _pick_months(all_months, months)
    sliced = [
        {**r, "row_key": _row_key(r), "gmv": [r["gmv"][i] for i in idx], "share": [r["share"][i] for i in idx],
         "growth": [r["growth"][i] for i in idx], "yoy": [r["yoy"][i] for i in idx]}
        for r in rows
    ]
    out_months = [all_months[i] for i in idx]
    _merge_category_topline_forecast(variant, platform, out_months, sliced)
    return {"months": out_months, "rows": sliced}


# --------------------------------------------------------------------------
# Tab: Brand Share Cons (read-only view of the live workbooks)
# --------------------------------------------------------------------------
@app.post("/brand-share-cons/reload")
def brand_share_cons_reload():
    _REPORT_CACHE["bsc_platforms"].clear()
    _REPORT_CACHE["bsc_rows"].clear()
    return {"status": "cleared"}


@app.get("/brand-share-cons/meta")
def brand_share_cons_meta():
    out = {}
    months = []
    for variant, path in BSC_FILES.items():
        if variant not in _REPORT_CACHE["bsc_platforms"]:
            _REPORT_CACHE["bsc_platforms"][variant] = _workbook_platforms(path)
        out[variant] = _REPORT_CACHE["bsc_platforms"][variant]
        if not months and path.exists():
            months = _workbook_months(path)
    return {"variants": out, "months": months}


def _get_bsc_rows(variant, platform):
    """Loads (and caches) one platform's Brand Share Cons sheet - mirrors _get_topline_rows, so
    both the read-only GET endpoint below and any code that needs to WRITE into this same sheet
    (e.g. Category Topline's own forecast, propagating Total/L0/L1/L2 into BSC - see Step 1 of
    the brand-share-cons plan) share one cached loader."""
    cache_key = (variant, platform)
    if cache_key not in _REPORT_CACHE["bsc_rows"]:
        path = BSC_FILES.get(variant)
        if path is None or not path.exists():
            raise HTTPException(404, f"Workbook not found for variant '{variant}'")
        sheet = _topline_sheet_name(variant, platform)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            if sheet not in wb.sheetnames:
                raise HTTPException(404, f"Sheet '{sheet}' not found in {path.name}")
            months = _workbook_months(path, sheet)
            rows, _raw, _gmv_idx, _units_idx = _read_report_sheet(wb[sheet], with_units=True)
        finally:
            wb.close()
        _REPORT_CACHE["bsc_rows"][cache_key] = (months, rows)
    return _REPORT_CACHE["bsc_rows"][cache_key]


@app.get("/brand-share-cons")
def brand_share_cons(variant: str = "all", platform: str = "Blinkit", from_month: str = "", to_month: str = ""):
    months, rows = _get_bsc_rows(variant, platform)

    start, end = _slice_range(months, from_month, to_month)
    sliced = [
        {**r, "gmv": r["gmv"][start:end], "share": r["share"][start:end], "growth": r["growth"][start:end],
         "units": r["units"][start:end], "asp": r["asp"][start:end]}
        for r in rows
    ]
    return {"months": months[start:end], "rows": sliced}


# --------------------------------------------------------------------------
# Tab: Brand Validation (step 3 of Brand Share Cons) - a holistic, whole-brand view: each brand's
# GMV summed across EVERY category it appears in (not broken out by category), Absolute/MoM/YoY,
# compared side by side against what the real expert data (Master Data) says for that same brand.
# Purely a read-only reflection of whatever's currently committed in Brand Share Cons (including
# any Manual Change edits made at the category level) - recomputed fresh on every read, never
# itself the source of a change. "Others" plug rows are excluded - each one is a generic per-
# category leftover bucket, not one coherent brand that makes sense to track across categories.
# --------------------------------------------------------------------------
def _brand_growth_series(series):
    """MoM/YoY for one brand's own (already-summed-across-categories) GMV series - computed over
    the FULL month range (like _read_report_sheet does for a single row) so a growth rate for the
    first displayed month can still reach back before the display window starts."""
    n = len(series)
    mom = [None] * n
    yoy = [None] * n
    for i in range(n):
        if series[i] is None:
            continue  # this month's own value is missing (e.g. an unconsolidated expert month)
        if i >= 1 and series[i - 1]:
            mom[i] = (series[i] - series[i - 1]) / series[i - 1]
        if i >= 12 and series[i - 12]:
            yoy[i] = (series[i] - series[i - 12]) / series[i - 12]
    return mom, yoy


@app.get("/brand-validation")
def brand_validation(platform: str, from_month: str = "", to_month: str = ""):
    all_months, bsc_rows = _get_bsc_rows("all", platform)
    n = len(all_months)

    def norm(v):
        return str(v or "").strip().lower()

    # Grouped by NORMALIZED label, not the raw text - the same real brand can appear across
    # several L2 rows with inconsistent casing (e.g. "LANEIGE" in most categories but "Laneige"
    # in one more) and must be summed as one brand, not silently fragmented into two separate
    # rows that each then look like they're missing most of their own GMV against Master Data's
    # (correctly case-insensitive) total. The display label shown is whichever raw casing was
    # seen most often, purely cosmetic.
    bsc_gmv_full = {}  # normalized brand -> [gmv per month]
    display_label_counts = {}  # normalized brand -> {raw label: occurrence count}
    for r in bsc_rows:
        if r["kind"] != "brand":
            continue
        brand_norm = norm(r["label"])
        series = bsc_gmv_full.setdefault(brand_norm, [0.0] * n)
        for i in range(n):
            v = r["gmv"][i]
            if v is not None:
                series[i] += v
        counts = display_label_counts.setdefault(brand_norm, {})
        counts[r["label"]] = counts.get(r["label"], 0) + 1

    display_label = {bn: max(counts.items(), key=lambda kv: kv[1])[0] for bn, counts in display_label_counts.items()}

    # "Expert" here means the Raw expert data folder specifically (not Master Data) - confirmed
    # with the user: BSC is BSC's own numbers, Expert is whatever the Raw expert data file says,
    # for any metric, NA wherever that file doesn't cover a given month.
    expert_gmv_full = {}  # normalized brand -> [gmv or None per month]
    raw_agg = _load_raw_expert_agg(platform)
    if not raw_agg.empty:
        month_pos = {m: i for i, m in enumerate(all_months)}
        grouped = raw_agg.groupby(["month_label", "brand"])["gmv"].sum()
        for (month_label, brand_norm), val in grouped.items():
            if month_label not in month_pos or not brand_norm:
                continue
            series = expert_gmv_full.setdefault(brand_norm, [None] * n)
            i = month_pos[month_label]
            series[i] = (series[i] or 0.0) + float(val)

    # Tier/TM Brand are a brand-identity classification, not a GMV/MoM/YoY metric - these still
    # come from Master Data (the Raw expert data files don't carry LO Brand/TM Brand columns at
    # all), same as before.
    lo_flag_by_norm = {}
    tm_brand_by_norm = {}
    try:
        md = _get_master_data_df()
        md_platform = md[md["Platform"] == platform] if "Platform" in md.columns else None
    except HTTPException:
        md_platform = None
    if md_platform is not None and not md_platform.empty and "Brand" in md_platform.columns:
        if "LO Brand" in md_platform.columns:
            lo_counts = md_platform.groupby(md_platform["Brand"].map(norm))["LO Brand"].agg(
                lambda s: s.mode().iloc[0] if not s.mode().empty else None)
            lo_flag_by_norm = lo_counts.to_dict()
        # TM Brand is Master Data's own real parent-company field (L'Oreal, HUL, Honasa, P&G,
        # Himalaya, Henkel, ...) - shown directly in the Tier column instead of any guessed list.
        if "TM Brand" in md_platform.columns:
            tm_counts = md_platform.groupby(md_platform["Brand"].map(norm))["TM Brand"].agg(
                lambda s: s.mode().iloc[0] if not s.mode().empty else None)
            tm_brand_by_norm = tm_counts.to_dict()

    start, end = _slice_range(all_months, from_month, to_month)
    idx = list(range(start, end))
    out_months = [all_months[i] for i in idx]

    rows_out = []
    for brand_norm, bsc_series in bsc_gmv_full.items():
        bsc_mom, bsc_yoy = _brand_growth_series(bsc_series)
        expert_series = expert_gmv_full.get(brand_norm)
        expert_mom, expert_yoy = _brand_growth_series(expert_series) if expert_series else (None, None)

        is_lo = lo_flag_by_norm.get(brand_norm) == "LO"
        tier = "lo" if is_lo else "other"
        tm_brand = tm_brand_by_norm.get(brand_norm)
        sort_gmv = sum(bsc_series[i] or 0.0 for i in idx)

        rows_out.append({
            "brand": display_label[brand_norm], "tier": tier, "tm_brand": tm_brand,
            "bsc_absolute": [bsc_series[i] for i in idx],
            "bsc_mom": [bsc_mom[i] for i in idx],
            "bsc_yoy": [bsc_yoy[i] for i in idx],
            "expert_absolute": [expert_series[i] if expert_series else None for i in idx],
            "expert_mom": [expert_mom[i] if expert_mom else None for i in idx],
            "expert_yoy": [expert_yoy[i] if expert_yoy else None for i in idx],
            "_sort_gmv": sort_gmv,
        })

    # L'Oreal brands first (live from Master Data's own LO flag), everyone else by GMV descending
    # - TM Brand (shown per-row) conveys the "which conglomerate" grouping now, not a separate
    # guessed sort tier.
    tier_rank = {"lo": 0, "other": 1}
    rows_out.sort(key=lambda r: (tier_rank[r["tier"]], -r["_sort_gmv"], r["brand"]))
    for r in rows_out:
        del r["_sort_gmv"]

    return {"months": out_months, "rows": rows_out, "has_expert_data": bool(expert_gmv_full)}


# --------------------------------------------------------------------------
# Manual Change mode (Category Topline): Market Share becomes the editable input for a single
# month at a time - GMV is derived as Share x parent GMV. Each L1/L0/Total group already has one
# "locked" cell (a live formula - see build script) that auto-absorbs the difference to keep the
# parent unchanged; editing here only ever touches the OTHER (plain-value) cells in a group,
# never the locked one, so the existing formula keeps working exactly as it does when edited
# directly in Excel. Nothing is written to disk until /edit-commit - /edit-preview computes the
# same cascade for review first, per the explicit "confirm before rewriting" requirement.
# --------------------------------------------------------------------------
def _row_key(r):
    return "|".join([r["kind"], r.get("l0") or "", r.get("l1") or "", r.get("l2") or "", r["label"]])


def _month_index(months, month):
    if month not in months:
        raise HTTPException(400, f"Unknown month '{month}'")
    return months.index(month)


def _parent_gmv_lookup(rows):
    """row_key -> its immediate parent's full-range gmv list, mirroring _read_report_sheet's own
    parent resolution (by L0/L1/L2 identity, not row position)."""
    total_gmv = None
    l0_gmv, l1_gmv, l2_gmv = {}, {}, {}
    parent_of = {}
    for r in rows:
        key = _row_key(r)
        if r["kind"] == "total":
            parent_of[key] = None
            total_gmv = r["gmv"]
        elif r["kind"] in ("l0", "l0_flat"):
            parent_of[key] = total_gmv
            l0_gmv[r["l0"]] = r["gmv"]
        elif r["kind"] == "l1":
            parent_of[key] = l0_gmv.get(r["l0"])
            l1_gmv[(r["l0"], r["l1"])] = r["gmv"]
        elif r["kind"] == "l2":
            parent_of[key] = l1_gmv.get((r["l0"], r["l1"]))
            l2_gmv[(r["l0"], r["l1"], r["l2"])] = r["gmv"]
        else:
            parent_of[key] = l2_gmv.get((r["l0"], r["l1"], r["l2"]), l0_gmv.get(r["l0"]))
    return parent_of


def _parent_key_lookup(rows):
    """row_key -> its immediate parent's row_key (or None) - mirrors _parent_gmv_lookup but
    returns identity instead of the gmv series, so callers can resolve a parent's CURRENT value
    (e.g. from a `changes` dict after an edit/cascade) instead of always its original pre-edit one."""
    total_key = None
    l0_key, l1_key = {}, {}
    parent_key_of = {}
    for r in rows:
        key = _row_key(r)
        if r["kind"] == "total":
            parent_key_of[key] = None
            total_key = key
        elif r["kind"] in ("l0", "l0_flat"):
            parent_key_of[key] = total_key
            l0_key[r["l0"]] = key
        elif r["kind"] == "l1":
            parent_key_of[key] = l0_key.get(r["l0"])
            l1_key[(r["l0"], r["l1"])] = key
        elif r["kind"] == "l2":
            parent_key_of[key] = l1_key.get((r["l0"], r["l1"]))
        else:
            parent_key_of[key] = None
    return parent_key_of


def _is_locked(raw_cache, gmv_idx, month_idx, row_num):
    col_letter = get_column_letter(gmv_idx[month_idx] + 1)
    raw = raw_cache.get(f"{col_letter}{row_num}")
    return isinstance(raw, str) and raw.startswith("=")


def _topline_group_key(r):
    if r["kind"] in ("l0", "l0_flat"):
        return "TOTAL"
    if r["kind"] == "l1":
        return f"L0:{r['l0']}"
    if r["kind"] == "l2":
        return f"L1:{r['l0']}:{r['l1']}"
    return None


# --------------------------------------------------------------------------
# Category Topline: no-expert-data forecasting engine (the "automation" Monthly Setup's Total
# GMV input triggers - see _compute_category_topline_forecast below for the agreed rule). Only
# Category Topline uses this - Brand Share Cons' equivalent (ASP trend + reverse-calculated
# Units, bottom-up from brand level) is explicitly a separate, not-yet-built piece of work.
# --------------------------------------------------------------------------
def _forecast_ets(history):
    """Approximates Excel's FORECAST.ETS: a one-step-ahead additive-trend exponential smoothing
    forecast over this row's own historical Market Share series. Falls back to the latest actual
    value when there isn't enough history to fit a model (e.g. a newly-introduced category)."""
    clean = [v for v in history if v is not None]
    if len(clean) < 4:
        return clean[-1] if clean else None
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        fit = ExponentialSmoothing(
            clean, trend="add", damped_trend=True, seasonal=None, initialization_method="estimated"
        ).fit(optimized=True)
        return float(fit.forecast(1)[0])
    except Exception:
        return clean[-1]


def _trend_blend(history):
    """Confirmed rule: average of the most recent month's share and the same month last year's
    share - whichever of the two actually exist (a row with under 12 months of history just uses
    the recent value alone)."""
    recent = history[-1] if history else None
    year_ago = history[-12] if len(history) >= 12 else None
    vals = [v for v in (recent, year_ago) if v is not None]
    return sum(vals) / len(vals) if vals else None


# NOTE: a platform-specific variant for Amazon/Flipkart (mirroring the external BPC Forecast
# Model's documented A/C/D/E signal weights, and separately a pure anchor-mean mix) was built and
# backtested against both platforms' own real continuous tagged history before being wired in
# anywhere - per the explicit "test before adding" instruction. Both attempts came out WORSE than
# this plain blend (MoM/YoY MAE higher, not lower), so neither was adopted. Every platform uses
# the one blend below; see project_category_topline_automation memory for the full backtest
# numbers if a different candidate is worth trying later.
# --------------------------------------------------------------------------
# "Expert data" benchmark source: Portal Data/Raw expert data/{Platform}.xlsx - the independently
# tagged, not-yet-consolidated-into-Master-Data per-platform exports. This is deliberately NOT
# Master Data: Category Topline's and Brand Share Cons' own committed numbers for the current
# in-progress month are themselves produced by this forecasting engine (and historical months are
# a straight paste of Master Data), so benchmarking against Master Data would be circular for any
# month Master Data hasn't independently re-sourced yet. The Raw expert data files exist
# precisely to give an earlier, independent read before that manual consolidation happens.
# Confirmed with the user: this benchmark role applies to BOTH the Category Topline/Brand Share
# Cons forecast-alignment engine below AND the read-only Brand Validation tab.
# --------------------------------------------------------------------------
RAW_EXPERT_DIR = PORTAL_DATA_DIR / "Raw expert data"

# One entry per platform that has a raw file (Big Basket and Purplle have none - simply absent
# here, so every lookup for them returns empty, same graceful no-expert-data fallback as always).
# l0_col is None for platforms whose raw file only tags L1_Cat/L2_Cat (not L0_Cat) - L0_Cat is
# then derived via _L1_TO_L0 below, the same deterministic mapping Master Data itself uses.
RAW_EXPERT_CONFIG = {
    "Amazon": {"file": "Amazon.xlsx", "sheets": ["Amazon All Months"], "month_col": "Month",
               "l0_col": "RSC L0", "l1_col": "RSC L1", "l2_col": "RSC L2",
               "brand_col": "Corrected Brand ", "brand_col_fallback": "Brand", "gmv_col": "GMV in Cr.",
               "source_tag_col": "AZ Expert Data "},
    "Flipkart": {"file": "Flipkart.xlsx", "sheets": ["Flipkart 2025", "Flipkart 2026"], "month_col": "Month",
                 "l0_col": "L0_Cat", "l1_col": "L1_Cat", "l2_col": "L2_Cat",
                 "brand_col": "Brand Final", "gmv_col": "GMV in Cr."},
    "Blinkit": {"file": "Blinkit.xlsx", "sheets": ["Blinkit All Months"], "month_col": "Month",
                "l0_col": None, "l1_col": "L1", "l2_col": "L2",
                "brand_col": "Brand", "gmv_col": "GMV in Cr"},
    "Myntra": {"file": "Myntra.xlsx", "sheets": ["Myntra All Months"], "month_col": "Month",
               "l0_col": None, "l1_col": "L1", "l2_col": "L2",
               "brand_col": "Brand", "gmv_col": "GMV in Cr."},
    "Nykaa": {"file": "Nykaa.xlsx", "sheets": ["Nykaa All Months"], "month_col": "Month",
              "l0_col": None, "l1_col": "L1", "l2_col": "L2",
              "brand_col": "Brand", "gmv_col": "GMV in Cr."},
    "Swiggy Instamart": {"file": "Swiggy Instamart.xlsx", "sheets": ["Swiggy IM All Months"], "month_col": "Month",
                          "l0_col": None, "l1_col": "L1_Cat", "l2_col": "L2_Cat",
                          "brand_col": "Brand", "gmv_col": "Gmv_in_cr"},
    "Zepto": {"file": "Zepto.xlsx", "sheets": ["Zepto All Months"], "month_col": "Month",
              "l0_col": None, "l1_col": "L1_Cat", "l2_col": "L2_Cat",
              "brand_col": "Brand", "gmv_col": "GMV in CR."},
}

# Confirmed 1:1 from Master Data's own L0_Cat/L1_Cat pairing.
_L1_TO_L0 = {
    "body skin": "grooming", "face skin": "grooming", "hair care": "grooming",
    "hair care others": "grooming", "hair colour": "grooming", "hygiene": "grooming",
    "fragrance": "makeup and fragrance", "make up": "makeup and fragrance", "others": "others",
}

# Flipkart's own category_mapping_data consistently tags L0 as "Makeup and Fragrances" (plural) -
# confirmed a 100%-consistent, systemic labeling convention there (82/82 entries in its mapping
# file, and the SAME plural form in its Raw expert data file's own L0_Cat column), not a one-off
# typo - while Master Data/Category Topline use the singular "Makeup and Fragrance" throughout.
# Shared here (not duplicated per call site) since this must be applied identically everywhere an
# L0 value from Flipkart's own tagging flows into something keyed by Master Data's convention -
# confirmed this was missing from _load_raw_expert_agg specifically broke expert-alignment AND the
# Expert MoM/YoY preview for every Makeup/Fragrance row on Flipkart, silently falling back to "no
# expert data for this row" instead of erroring, which is why it went unnoticed until asked about.
_L0_ALIASES = {"makeup and fragrances": "makeup and fragrance"}


def _parse_raw_expert_month(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.strftime("%b-%y")
    s = str(v).strip()
    for fmt in ("%b'%y", "%b-%y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%b-%y")
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).strftime("%b-%y")
    except Exception:
        return None


# --------------------------------------------------------------------------
# Amazon's raw expert data lumps "Shampoo and Conditioner" (and several differently-worded
# variants of the same thing) into one source tag covering 5 real L2s at once (Shampoo, Rinse-Off
# Conditioners, Leave-In Conditioners, Hair Serums, Hair Care Others) - these 8 normalized source
# tags (keys from engine/category_mapping_data/Amazon.json, first ||-segment only) are the ones
# that get split per-brand below rather than trusted as a single L2. Confirmed with the user -
# NOT a general-platform mechanism, Amazon only.
_AMAZON_SHAMPOO_CONDITIONER_TAGS = {
    "groominghaircareandstylingshampooandconditioner",
    "groominghaircareshampooandconditioner",
    "groominghaircareshampooconditioner",
    "groominghaircarestylingshampooconditioner",
    "groomingskincareshampooandconditioner",
    "groomingskincareshampooconditioner",
    "makeup and fragranceshaircareandstylingshampooandconditioner",
    "makeup and fragranceshaircareshampooandconditioner",
}
# Every other previously-L2:null Amazon source tag resolves to a single forced L2, confirmed
# tag-by-tag with the user. Scalp-treatment tags move to Hair Serums (Amazon.json had them on the
# generic "Hair Care Others" catch-all); everything else is a genuinely generic/unspecified tag
# ("na", "miscellaneous", "sample", "professional"/"prestige" with no product signal either way)
# that lands on its L1's own Others bucket, same as any other real catch-all in this taxonomy.
# groomingskincarehomed√©coraccents is a garbled source tag with ~0 GMV (1 row, Rs 0) -
# confirmed safe to just fold into Face Skin Others rather than exclude separately.
# Applied directly here (not just in Amazon.json) so it corrects rows already baked into the raw
# file's own RSC L2 column, without ever needing that file itself rewritten.
_AMAZON_FORCED_L2_BY_TAG = {
    "groominghaircareandstylingscalptreatments": "hair serums",
    "groominghaircarestylingscalptreatments": "hair serums",
    "makeup and fragranceshaircareandstylingscalptreatments": "hair serums",
    "groominghaircarena": "hair care others",
    "groomingnaprofessionalhaircare": "hair care others",
    "groomingprestigehaircarena": "hair care others",
    "groomingsamplinghaircaresample": "hair care others",
    "makeup and fragrancessamplinghaircaresample": "hair care others",
    "groomingnaface": "face skin others",
    "groomingprestigeskincarena": "face skin others",
    "groomingskincaremiscellaneousskincare": "face skin others",
    "groomingskincarena": "face skin others",
    "groomingskincarehomed√©coraccents": "face skin others",
    "groomingcosmeticsna": "make up others",
    "makeup and fragrancescosmeticswomen": "make up others",
}


def _build_amazon_haircare_split_lookup():
    """Per-brand split of the lumped Shampoo+Conditioner tags across the 5 real Hair Care L2s -
    confirmed mechanism: L'Oreal brands use their OWN real observed Amazon split from Master Data
    directly (Master Data already carries a genuinely granular L2 breakdown for LO brands
    specifically, verified e.g. L'Oreal Paris: 58.5% Shampoo / 15.4% Rinse-Off Conditioners /
    14.6% Hair Serums / 9.5% Hair Combo / 2.0% Leave-In). Every other brand uses a GMV-weighted
    blend of Flipkart's and Nykaa's OWN per-brand Hair Care L2 mix (those platforms already tag
    this distinction natively). Brands absent from all of the above fall back to the overall
    Flipkart+Nykaa category-wide blend. Returns (per_brand: {brand_norm: {l2_norm: fraction}},
    category_fallback: {l2_norm: fraction})."""
    try:
        md = _get_master_data_df()
    except HTTPException:
        return {}, {}
    gmv_col = "GMV (INR Cr.)" if "GMV (INR Cr.)" in md.columns else "GMV (INR #)"

    def norm(v):
        return str(v or "").strip().lower()

    hc = md[md["L1_Cat"] == "Hair Care"]

    category_totals = hc[hc["Platform"].isin(["Flipkart", "Nykaa"])].groupby(
        hc["L2_Cat"].map(norm))[gmv_col].sum()
    cat_total = category_totals.sum()
    category_fallback = (category_totals / cat_total).to_dict() if cat_total else {}

    per_brand = {}
    fk_nk = hc[hc["Platform"].isin(["Flipkart", "Nykaa"])]
    if "Brand" in fk_nk.columns:
        for brand, idx in fk_nk.groupby(fk_nk["Brand"].map(norm)).groups.items():
            totals = fk_nk.loc[idx].groupby(fk_nk.loc[idx, "L2_Cat"].map(norm))[gmv_col].sum()
            total = totals.sum()
            if total and brand:
                per_brand[brand] = (totals / total).to_dict()

    lo_amz = hc[(hc["Platform"] == "Amazon") & (hc.get("LO Brand") == "LO")] if "LO Brand" in hc.columns else hc.iloc[0:0]
    if "Brand" in lo_amz.columns:
        for brand, idx in lo_amz.groupby(lo_amz["Brand"].map(norm)).groups.items():
            totals = lo_amz.loc[idx].groupby(lo_amz.loc[idx, "L2_Cat"].map(norm))[gmv_col].sum()
            total = totals.sum()
            if total and brand:
                per_brand[brand] = (totals / total).to_dict()  # overrides the FK/Nykaa entry above

    return per_brand, category_fallback


def _apply_amazon_haircare_correction(combined):
    """Applied once, right after Amazon's raw sheets are concatenated (combined has a 'source_tag'
    column - the row's original, pre-RSC-tagging category text, normalized): every tag in
    _AMAZON_FORCED_L2_BY_TAG gets force-corrected to its confirmed L2 regardless of whatever the
    raw file's own RSC L2 column already carries, and Shampoo+Conditioner rows get exploded into
    the 5 real Hair Care L2s per that row's own brand's split (see
    _build_amazon_haircare_split_lookup). Everything else passes through unchanged. This never
    touches the source Amazon.xlsx file itself - it's a live, reproducible transformation of
    whatever's actually in it."""
    forced = combined["source_tag"].map(_AMAZON_FORCED_L2_BY_TAG)
    combined.loc[forced.notna(), "l2"] = forced[forced.notna()]

    is_split = combined["source_tag"].isin(_AMAZON_SHAMPOO_CONDITIONER_TAGS)
    if not is_split.any():
        return combined.drop(columns=["source_tag"])

    split_rows = combined[is_split]
    rest = combined[~is_split].drop(columns=["source_tag"])

    per_brand, category_fallback = _build_amazon_haircare_split_lookup()
    grouped = split_rows.groupby(["month_label", "brand"], as_index=False)["gmv"].sum()
    exploded = []
    for _, row in grouped.iterrows():
        split = per_brand.get(row["brand"]) or category_fallback
        if not split:
            exploded.append({"month_label": row["month_label"], "l0": "grooming", "l1": "hair care",
                              "l2": "hair care others", "brand": row["brand"], "gmv": row["gmv"]})
            continue
        for l2, frac in split.items():
            exploded.append({"month_label": row["month_label"], "l0": "grooming", "l1": "hair care",
                              "l2": l2, "brand": row["brand"], "gmv": row["gmv"] * frac})
    exploded_df = pd.DataFrame(exploded, columns=["month_label", "l0", "l1", "l2", "brand", "gmv"])
    return pd.concat([rest, exploded_df], ignore_index=True)


_RAW_EXPERT_CACHE = {}
_RAW_EXPERT_LOCKS = {platform: threading.Lock() for platform in RAW_EXPERT_CONFIG}


def _load_raw_expert_agg(platform):
    """Loads+aggregates one platform's Raw expert data file (if any) down to one row per
    (month_label, l0, l1, l2, brand) with a summed GMV (Cr.), all four tag fields normalized
    (.strip().lower()) to match how Category Topline/Brand Share Cons/Master Data rows are keyed
    elsewhere. Cached per platform for the life of the process - cleared via
    /raw-expert-data/reload, same convention as Master Data. Returns an empty DataFrame (not an
    error) when no raw file exists for this platform, or nothing in it can be read - callers treat
    that exactly like "no expert data yet", same graceful fallback as always.

    These files are large (Flipkart's two sheets alone are 1.2M+ rows) and parsing them with
    openpyxl is slow - a cold load can take minutes. A small pickled copy of the aggregated result
    is kept alongside the source file (keyed by the source's own mtime) so every app restart after
    the first doesn't have to re-parse the full workbook, only read back a tiny pickle - cleared
    automatically the moment the source file is actually replaced.

    Locked per platform so a real request arriving while the startup warm-up thread is still
    loading this same platform WAITS for it and reuses the result, instead of redundantly
    re-parsing the same huge file a second time concurrently (the same class of bug confirmed and
    fixed for Master Data - two simultaneous cold loads fighting over CPU made the request slower
    than if there had been no warm-up thread at all)."""
    if platform in _RAW_EXPERT_CACHE:
        return _RAW_EXPERT_CACHE[platform]
    lock = _RAW_EXPERT_LOCKS.get(platform)
    if lock is None:  # not a known expert-data platform at all (e.g. Big Basket, Purplle)
        empty = pd.DataFrame(columns=["month_label", "l0", "l1", "l2", "brand", "gmv"])
        _RAW_EXPERT_CACHE[platform] = empty
        return empty
    with lock:
        if platform in _RAW_EXPERT_CACHE:  # another thread just finished while we waited
            return _RAW_EXPERT_CACHE[platform]
        return _load_raw_expert_agg_uncached(platform)


def _load_raw_expert_agg_uncached(platform):
    empty = pd.DataFrame(columns=["month_label", "l0", "l1", "l2", "brand", "gmv"])
    cfg = RAW_EXPERT_CONFIG.get(platform)
    if cfg is None:
        _RAW_EXPERT_CACHE[platform] = empty
        return empty
    path = RAW_EXPERT_DIR / cfg["file"]
    if not path.exists():
        _RAW_EXPERT_CACHE[platform] = empty
        return empty

    src_mtime = path.stat().st_mtime
    cache_path = RAW_EXPERT_DIR / ".agg_cache" / f"{platform}.pkl"
    if cache_path.exists():
        try:
            cached_mtime, cached_df = pd.read_pickle(cache_path)
            if cached_mtime == src_mtime:
                _RAW_EXPERT_CACHE[platform] = cached_df
                return cached_df
        except Exception:
            pass

    def norm(v):
        return str(v or "").strip().lower()

    source_tag_col = cfg.get("source_tag_col")
    frames = []
    for sheet in cfg["sheets"]:
        wanted = {cfg["month_col"], cfg["l1_col"], cfg["l2_col"], cfg["gmv_col"], cfg["brand_col"]}
        if cfg["l0_col"]:
            wanted.add(cfg["l0_col"])
        if cfg.get("brand_col_fallback"):
            wanted.add(cfg["brand_col_fallback"])
        if source_tag_col:
            wanted.add(source_tag_col)
        try:
            df = pd.read_excel(path, sheet_name=sheet, usecols=lambda c: c in wanted)
        except Exception:
            continue
        if df.empty:
            continue
        brand_series = df[cfg["brand_col"]]
        fallback_col = cfg.get("brand_col_fallback")
        if fallback_col and fallback_col in df.columns:
            blank = brand_series.isna() | (brand_series.astype(str).str.strip() == "")
            brand_series = brand_series.where(~blank, df[fallback_col])
        out = pd.DataFrame({
            "month_label": df[cfg["month_col"]].map(_parse_raw_expert_month),
            "l1": df[cfg["l1_col"]].map(norm),
            "l2": df[cfg["l2_col"]].map(norm),
            "brand": brand_series.map(norm),
            "gmv": pd.to_numeric(df[cfg["gmv_col"]], errors="coerce"),
        })
        out["l0"] = df[cfg["l0_col"]].map(lambda v: _L0_ALIASES.get(norm(v), norm(v))) if cfg["l0_col"] else out["l1"].map(
            lambda v: _L1_TO_L0.get(v, ""))
        if source_tag_col and source_tag_col in df.columns:
            out["source_tag"] = df[source_tag_col].map(norm)
        frames.append(out)

    if not frames:
        _RAW_EXPERT_CACHE[platform] = empty
        return empty
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.dropna(subset=["month_label", "gmv"])
    if platform == "Amazon" and "source_tag" in combined.columns:
        combined = _apply_amazon_haircare_correction(combined)
    agg = combined.groupby(["month_label", "l0", "l1", "l2", "brand"], as_index=False)["gmv"].sum()
    _RAW_EXPERT_CACHE[platform] = agg
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle((src_mtime, agg), cache_path)
    except Exception:
        pass
    return agg


@app.post("/raw-expert-data/reload")
def raw_expert_data_reload():
    _RAW_EXPERT_CACHE.clear()
    return {"status": "cleared"}


def _expert_lookup_key(r):
    def norm(v):
        return str(v or "").strip().lower()
    if r["kind"] in ("l0", "l0_flat"):
        return ("l0", norm(r.get("l0")), None, None)
    if r["kind"] == "l1":
        return ("l1", norm(r.get("l0")), norm(r.get("l1")), None)
    if r["kind"] == "l2":
        return ("l2", norm(r.get("l0")), norm(r.get("l1")), norm(r.get("l2")))
    return None


def _build_expert_gmv_lookup(platform, month_label):
    """For an expert-data platform/month: rolls the Raw expert data file (the independently
    tagged, not-yet-Master-Data-consolidated export - see the RAW_EXPERT_CONFIG block above) up
    to L0/L1/L2 GMV totals for that one month, keyed the same way _expert_lookup_key reads a
    Category Topline row - so _compute_category_topline_forecast can look up "what did the
    expert data actually say this category's GMV was" per row. Empty (not an error) if this
    platform has no raw file, or it doesn't cover this month yet - the forecast then just falls
    back to plain ETS+Trend for every row, same as a no-expert-data platform."""
    agg = _load_raw_expert_agg(platform)
    if agg.empty:
        return {}
    sub = agg[agg["month_label"] == month_label]
    if sub.empty:
        return {}
    l2_totals = sub.groupby(["l0", "l1", "l2"])["gmv"].sum()
    lookup, l1_totals, l0_totals = {}, {}, {}
    for (l0, l1, l2), val in l2_totals.items():
        val = float(val)
        lookup[("l2", l0, l1, l2)] = val
        l1_totals[(l0, l1)] = l1_totals.get((l0, l1), 0.0) + val
        l0_totals[l0] = l0_totals.get(l0, 0.0) + val
    for (l0, l1), val in l1_totals.items():
        lookup[("l1", l0, l1, None)] = val
    for l0, val in l0_totals.items():
        lookup[("l0", l0, None, None)] = val
    lookup[("total", None, None, None)] = sum(l0_totals.values())
    return lookup


def _shift_month_label(label, delta_months):
    """'Sep-26' shifted by -1 -> 'Aug-26', by -12 -> 'Sep-25' - handles year rollover. None if
    label isn't a parseable month."""
    try:
        dt = datetime.strptime(label, "%b-%y")
    except ValueError:
        return None
    total = dt.year * 12 + (dt.month - 1) + delta_months
    y, m = divmod(total, 12)
    return datetime(y, m + 1, 1).strftime("%b-%y")


# Guardrails on the expert-data alignment below - all confirmed with the user rather than picked
# arbitrarily. EXPERT_MIN_ANCHOR_GMV mirrors the MIN_BASE near-zero-base guard used elsewhere in
# this project's backtesting scripts - a growth rate computed off a near-zero prior/year-ago GMV
# is noise, not signal, same artifact as a 0.01 Cr year-ago base turning a trivial absolute change
# into a 5000% "YoY".
EXPERT_MIN_ANCHOR_GMV = 0.5  # Cr

# Tiered tolerance band: how close the FINAL adjusted growth rate must land to the expert's own
# figure. Confirmed via the Face Sunscreen example (forecast 20% YoY, expert 30% - double-digit,
# so +/-2pp band - the engine does NOT jump all the way to 30%, it clips to the nearest edge of
# the band, 28%, "moving towards" the expert without blowing up MoM trend sanity). Identical
# tiering for MoM (confirmed - not a separate flat band).
GROWTH_TOLERANCE_SINGLE_DIGIT = 0.01  # expert growth magnitude < 10%
GROWTH_TOLERANCE_DOUBLE_DIGIT = 0.02  # expert growth magnitude >= 10%

# Market Share guardrail, two-tier (replaces the old single flat +/-3pp accept/reject gate):
# within 3pp of the plain Forecast+Trend baseline, applied silently; 3-4pp, applied but flagged
# for manual review; beyond 4pp, hard-capped there - confirmed as a ceiling with no exceptions.
MARKET_SHARE_FLAG_THRESHOLD = 0.03
MARKET_SHARE_HARD_CAP = 0.04

# A row with NO expert data of its own still needs a real, finite limit on how far group
# RECONCILIATION can push it - confirmed: an unbounded "it has no expert signal, let it absorb
# whatever's left" rule let one row drift wildly out of line with its own peers and the overall
# market, which is exactly the failure mode the guardrail exists to prevent, just via a different
# path (unresolved group residual instead of a bad expert figure). Wider than the expert-anchored
# hard cap (a no-expert row is "less certain" either way, so gets more natural room), but never
# infinite.
NO_EXPERT_RECONCILE_BAND = 0.10

# A row's final growth off its OWN anchor (year-ago preferred, else prior-month) can't exceed this
# magnitude either way - e.g. 1.50 means GMV must land within [anchor*-0.5, anchor*2.5], i.e. YoY/
# MoM within [-100%, +150%]. This is deliberately generous (confirmed: a genuinely volatile real
# category in this data, Body Skin's own +110% YoY, sits comfortably inside it) but still finite -
# it exists specifically to catch the case the share-point bands above structurally can't: a SMALL
# category (tiny own anchor) being handed "room" sized relative to a much bigger PARENT, which can
# dwarf its own historical scale many times over and produce an implausible percentage swing even
# though the absolute share-point move looks modest. Applied uniformly - to an expert-driven
# candidate and to ordinary reconciliation spillover alike - since both can produce the same
# failure mode; whichever bound (this or the share-point one) is tighter always wins.
MAX_PLAUSIBLE_GROWTH_MAGNITUDE = 1.50

# Minimum-agreement gate: confirmed necessary after backtesting this engine against real data
# (Nykaa/Nov-25) showed WAPE getting WORSE with the tiered-clip rule alone (25.1% vs 7.9% with no
# expert alignment at all) - we'd already separately proven, extensively, that at the category
# level the plain Forecast+Trend engine is MORE accurate than the expert signal more often than
# not, so "always move at least to the tolerance band" actively hurts whenever the expert simply
# disagrees with a baseline that's usually the better predictor. If the expert's FULL, unclipped
# implied move is wildly larger than this gate, that's much more likely a tagging/category-mapping
# mismatch for this specific row than a genuine trend worth partially trusting - no move is
# applied at all past this point, though it's still flagged for review either way.
EXPERT_DISAGREEMENT_GATE = 2 * MARKET_SHARE_HARD_CAP  # 8 percentage points


def _growth_tolerance(growth_rate):
    return GROWTH_TOLERANCE_SINGLE_DIGIT if abs(growth_rate) < 0.10 else GROWTH_TOLERANCE_DOUBLE_DIGIT


def _expert_aligned_share(base_share, anchor_gmv, denom_gmv, expert_growth):
    """Clips the plain Forecast+Trend candidate's OWN implied growth rate (expressed from the same
    anchor the expert growth rate itself was computed from) to within the expert's tolerance band,
    rather than the old binary accept-in-full/reject-entirely gate. Does NOT apply the Market
    Share guardrail - that's a separate, shared concern applied once by the caller regardless of
    whether YoY or MoM drove this candidate."""
    if not denom_gmv or anchor_gmv is None:
        return base_share
    base_growth = (base_share * denom_gmv) / anchor_gmv - 1
    tol = _growth_tolerance(expert_growth)
    adjusted_growth = min(max(base_growth, expert_growth - tol), expert_growth + tol)
    return max(0.0, (anchor_gmv * (1 + adjusted_growth)) / denom_gmv)


def _apply_market_share_guardrail(candidate_share, base_share):
    """Two-tier Market Share guardrail - see MARKET_SHARE_FLAG_THRESHOLD/MARKET_SHARE_HARD_CAP
    above. Returns (final_share, flagged)."""
    delta = candidate_share - base_share
    if abs(delta) <= MARKET_SHARE_FLAG_THRESHOLD:
        return candidate_share, False
    if abs(delta) <= MARKET_SHARE_HARD_CAP:
        return candidate_share, True
    capped = base_share + (MARKET_SHARE_HARD_CAP if delta > 0 else -MARKET_SHARE_HARD_CAP)
    return max(0.0, capped), True


def _expert_alignment_for_row(base_share, denom_gmv, prior_gmv, yearago_gmv, expert_gmv):
    """The full per-row expert-alignment decision - shared by both the Category Topline and Brand
    Share Cons forecast engines (previously near-duplicated in each, with the old flat +/-3pp
    cap). Priority order confirmed with the user: YoY is preferred and drives the adjustment
    whenever its own anchor is trustworthy (>= EXPERT_MIN_ANCHOR_GMV); MoM only drives it when
    YoY's anchor isn't usable for this row. Whichever one drives it, the OTHER (when also usable)
    is still checked as a flag-only "do these two signals actually agree" signal - it never
    overrides the chosen result; MoM is the lowest-priority of the three signals, used only to
    flag discordance, never to override a YoY-driven outcome.
    Returns (final_share, expert_mom, expert_yoy, needs_review) - needs_review is True when the
    Market Share guardrail flagged/capped the result, OR the non-driving signal disagrees with the
    final result by more than its own tolerance band."""
    expert_mom = (expert_gmv / prior_gmv - 1) if expert_gmv is not None and prior_gmv else None
    expert_yoy = (expert_gmv / yearago_gmv - 1) if expert_gmv is not None and yearago_gmv else None

    yoy_usable = expert_yoy is not None and yearago_gmv is not None and yearago_gmv >= EXPERT_MIN_ANCHOR_GMV
    mom_usable = expert_mom is not None and prior_gmv is not None and prior_gmv >= EXPERT_MIN_ANCHOR_GMV

    if not yoy_usable and not mom_usable:
        # Neither anchor is trustworthy (near-zero-base noise) - no adjustment is applied, same as
        # true no-expert-data, but still flagged if a raw expert figure existed at all, so an
        # analyst can see "expert data existed here, the anchor just wasn't reliable enough to
        # trust" rather than it silently looking identical to no coverage at all.
        had_any_expert_signal = expert_mom is not None or expert_yoy is not None
        return base_share, expert_mom, expert_yoy, had_any_expert_signal

    if yoy_usable:
        driving_anchor, driving_growth = yearago_gmv, expert_yoy
    else:
        driving_anchor, driving_growth = prior_gmv, expert_mom

    if denom_gmv:
        raw_expert_implied_share = max(0.0, (driving_anchor * (1 + driving_growth)) / denom_gmv)
        if abs(raw_expert_implied_share - base_share) > EXPERT_DISAGREEMENT_GATE:
            return base_share, expert_mom, expert_yoy, True

    candidate_share = _expert_aligned_share(base_share, driving_anchor, denom_gmv, driving_growth)
    final_share, ms_flag = _apply_market_share_guardrail(candidate_share, base_share)

    mom_discord = False
    if yoy_usable and mom_usable and denom_gmv and prior_gmv:
        implied_mom = (final_share * denom_gmv) / prior_gmv - 1
        # +1e-9 epsilon: when the driving (YoY) growth clips to exactly its own tolerance
        # boundary and prior_gmv happens to equal yearago_gmv, the back-computed implied MoM
        # lands exactly on ITS boundary too - floating-point rounding can then push a
        # mathematically-equal comparison to either side of a strict ">" by a trillionth, flagging
        # spuriously. The epsilon only matters at that exact boundary; it never masks a real
        # disagreement of any practical size.
        mom_discord = abs(implied_mom - expert_mom) > _growth_tolerance(expert_mom) + 1e-9

    return final_share, expert_mom, expert_yoy, (ms_flag or mom_discord)


def _joint_reconcile_group(members, parent_gmv):
    """members: [{"row_num", "target_gmv", "anchor", "lo", "hi"}, ...] - one sibling group whose
    GMVs must sum to exactly `parent_gmv`. Returns (result: {row_num: final_gmv}, exhausted: bool).

    Jointly (not priority-ordered, not one flat scale factor) redistributes the gap between
    sum(target_gmv) and parent_gmv across every member at once, weighted 1/anchor^2 - a closed-
    form weighted-least-squares projection onto the "sum = parent_gmv" constraint, clamped to each
    member's own [lo, hi] bound (its guardrail-capped share range - see resolve_level). `anchor`
    is whatever GMV scale that member's own percentage distortion should be measured against (its
    year-ago GMV when trustworthy, else prior-month, else its own target) - weighting by 1/anchor^2
    means the reconciliation spreads so every member's OWN percentage-term distortion from its
    target is as equal as possible, rather than one flat ratio applied regardless of how much room
    each one actually has.

    Solved by water-filling (confirmed with the user: this is exactly the manual process - "push
    this category as far as it can plausibly go, see which other one can absorb the rest without
    going implausible, push that one to its own max, repeat until a consensus combination is
    reached"): solve the unconstrained projection for whoever's still free, clamp anyone whose
    result would breach their own [lo, hi], lock them at that boundary, then re-solve the
    (smaller) residual across whoever's left - repeating until nobody left free violates their
    bound. Converges in at most len(members) rounds (one more member gets locked each round).

    Previously this had no bounds at all - the per-row guardrail (stated as a hard rule, "no
    exceptions") was computed into `target_gmv` but then silently overridden here whenever the
    group's reconciliation needed more room than one member had - confirmed as the actual cause of
    a large sibling (e.g. Shampoo) swinging to the WRONG SIDE of its own expert-implied growth to
    cover a gap that belonged to its L1 parent's own alignment, not to Shampoo's own data at all.

    Returns exhausted=True when every member has been clamped to its own bound and the group
    STILL doesn't sum to parent_gmv - a real, un-absorbable conflict (not silently broken; the
    caller flags every member of a group like this for manual review instead)."""
    if not members:
        return {}, False
    free = list(members)
    fixed = {}
    while free:
        residual = parent_gmv - sum(fixed.values()) - sum(m["target_gmv"] for m in free)
        inv_w = [m["anchor"] ** 2 for m in free]
        sum_inv_w = sum(inv_w)
        proposed, violated = {}, []
        for m, iw in zip(free, inv_w):
            frac = (iw / sum_inv_w) if sum_inv_w else (1.0 / len(free))
            val = m["target_gmv"] + residual * frac
            clipped = min(max(val, m["lo"]), m["hi"])
            proposed[m["row_num"]] = clipped
            if abs(clipped - val) > 1e-9:
                violated.append(m)
        if not violated:
            fixed.update(proposed)
            return fixed, False
        for m in violated:
            fixed[m["row_num"]] = proposed[m["row_num"]]
        free = [m for m in free if m not in violated]
    return fixed, True


def _compute_category_topline_forecast(rows, total_gmv, expert_gmv_lookup=None):
    """The agreed no-expert-data forecasting rule for one platform's current in-progress month:
    - Every category node (the group's last sibling included) gets its own raw Market Share
      candidate: CM = average(ETS forecast, recent/year-ago trend), from that node's OWN
      historical share series.
    - EXPERT-DATA ALIGNMENT (only when expert_gmv_lookup is given, i.e. this platform's expert
      data for this month is available): a row's raw candidate is instead anchored to the
      expert's own implied growth via _expert_alignment_for_row - YoY preferred, MoM only drives
      it when YoY's anchor isn't usable, the OTHER checked as a flag-only "do these agree" signal.
      The result is CLIPPED to within the expert's tiered tolerance band (not a binary accept/
      reject), then passed through the Market Share guardrail (apply+flag between 3-4pp, hard
      cap at 4pp). Falls back to the plain ETS+Trend average above whenever no expert figure, or
      no historical anchor to compute a growth rate from, exists for that particular row.
      expert_mom/expert_yoy are kept as their own diagnostic fields regardless (see below) - this
      is "align to expert", not "equal expert data" - joint reconciliation below still rescales
      every row to sum to 100%, so the final number is not necessarily identical to the expert's
      own raw figure.
    - Processed LEVEL BY LEVEL, top-down (L0 under Total, then each L0's L1 children using that
      L0's just-resolved GMV, then each L1's L2 children using that L1's just-resolved GMV) -
      not one flat pass over every row using the grand Total as every row's denominator. That
      flat-pass design was a confirmed bug: _expert_alignment_for_row's disagreement gate and
      tolerance-band clipping compare the row's OWN baseline share (always relative to its
      IMMEDIATE parent, e.g. an L1's share of its L0) against an expert-implied share computed
      from a DIFFERENT denominator (the grand Total) - two numbers on completely different
      scales for any L1/L2 row, which made the disagreement gate fire almost universally below
      L0 and silently fall back to the plain forecast, mislabeled as "expert disagrees". Passing
      each level's own just-resolved parent GMV as the denominator fixes this at the source.
    - JOINT RECONCILIATION (_joint_reconcile_group): each group (all L0s under Total; each L0's
      L1 children; each L1's L2 children) is reconciled to sum to exactly its parent's GMV by a
      weighted-least-squares projection, not one flat scale factor - every member's own
      percentage-term distortion from its (guardrail-clipped) candidate is spread as evenly as
      possible, so the group's reconciliation gap never gets dumped onto whichever member
      happens to be last, and a small category is never swung wildly just to absorb a large
      one's error (or vice versa) - see _joint_reconcile_group's own docstring for the exact math.
    - The group's last sibling still gets its value from a real '=Parent-Sibling-...' formula,
      not a plain number - mathematically it lands on exactly that same jointly-reconciled value
      (every other member already accounts for it via the "sum = parent_gmv" constraint), so
      Manual Change's "edit any sibling, the locked one adjusts to absorb it" behaviour is
      completely unchanged; only the numbers everyone starts from are more accurate.
    - Absolute GMV cascades top-down from the hard-input Total BPC GMV, L0 -> L1 -> L2.
    Returns {row_key: {forecast_share, trend_share, expert_mom, expert_yoy, expert_flag, share,
    gmv, is_plug, parent_row, sibling_rows}} - forecast_share/trend_share/expert_mom/expert_yoy
    are each row's own RAW (pre-reconciliation) diagnostics, shown as-is for transparency so an
    analyst can see why a number landed where it did and manually override it (Manual Change) if
    the alignment looks wrong. expert_flag is True when this row is worth a manual look - either
    the Market Share guardrail flagged/capped the move (3pp+ from the plain baseline), or the
    non-driving signal (MoM when YoY drove the adjustment, or vice versa) disagrees with the
    final result by more than its own tolerance band - a deliberate "go look at it" signal, not an
    error (see _expert_alignment_for_row). share/gmv are the final, reconciled values actually
    used. parent_row/sibling_rows are only populated for the plug, giving _write_forecast_column
    exactly what it needs to write its formula."""
    total_row_num = next(r["row_num"] for r in rows if r["kind"] == "total")
    group_members = {}
    for r in rows:
        gk = _topline_group_key(r)
        if gk:
            group_members.setdefault(gk, []).append(r)
    last_row_of_group = {gk: members[-1]["row_num"] for gk, members in group_members.items()}

    # Level-independent: every non-total row's own plain ETS+Trend baseline, from its OWN
    # historical share series (share-of-immediate-parent) - doesn't need any parent GMV resolved
    # yet, so can be computed for every row up front regardless of level.
    forecast_of, trend_of, base_share_of = {}, {}, {}
    prior_gmv_of, yearago_gmv_of = {}, {}
    for r in rows:
        if r["kind"] == "total":
            # Not forecasted (its value is the hard Total GMV input) - but its OWN prior/year-ago
            # GMV is still captured here, needed below so every level's sanity cap can be measured
            # against its PARENT's own growth, not an absolute magnitude in isolation.
            cutoff_local = len(r["share"])
            gmv_history = r.get("gmv") or []
            prior_gmv_of[r["row_num"]] = gmv_history[cutoff_local - 1] if 1 <= cutoff_local <= len(gmv_history) else None
            yearago_gmv_of[r["row_num"]] = gmv_history[cutoff_local - 12] if 12 <= cutoff_local <= len(gmv_history) else None
            continue
        forecast_share = _forecast_ets(r["share"])
        if forecast_share is not None:
            forecast_share = max(0.0, forecast_share)
        trend_share = _trend_blend(r["share"])
        if trend_share is not None:
            trend_share = max(0.0, trend_share)
        vals = [v for v in (forecast_share, trend_share) if v is not None]
        forecast_of[r["row_num"]] = forecast_share
        trend_of[r["row_num"]] = trend_share
        base_share_of[r["row_num"]] = max(0.0, sum(vals) / len(vals)) if vals else 0.0

        cutoff_local = len(r["share"])
        gmv_history = r.get("gmv") or []
        prior_gmv_of[r["row_num"]] = gmv_history[cutoff_local - 1] if 1 <= cutoff_local <= len(gmv_history) else None
        yearago_gmv_of[r["row_num"]] = gmv_history[cutoff_local - 12] if 12 <= cutoff_local <= len(gmv_history) else None

    expert_mom_of, expert_yoy_of, expert_flag_of = {}, {}, {}
    own_target_of = {}  # row_num -> each row's OWN pre-reconciliation candidate GMV (every pass)

    def resolve_level(members, parent_gmv, parent_row_num, target_override=None):
        """One sibling group, now that its parent's GMV is actually known: runs each member
        through the per-row expert-alignment decision using the CORRECT (immediate-parent)
        denominator, then jointly reconciles the whole group to parent_gmv - respecting every
        member's own guardrail bound as a hard limit on how far RECONCILIATION (not just its own
        per-row candidate) can push it. `target_override` (row_num -> gmv), when given, REPLACES
        a member's own candidate as the reconciliation target (used for the second, "informed"
        pass below) while the bound stays centered on that member's own plain baseline - see
        _compute_category_topline_forecast's docstring for why. Returns {row_num: final_gmv}."""
        parent_prior_gmv = prior_gmv_of.get(parent_row_num)
        parent_yearago_gmv = yearago_gmv_of.get(parent_row_num)
        if parent_yearago_gmv and parent_yearago_gmv >= EXPERT_MIN_ANCHOR_GMV:
            parent_growth = parent_gmv / parent_yearago_gmv - 1
        elif parent_prior_gmv and parent_prior_gmv >= EXPERT_MIN_ANCHOR_GMV:
            parent_growth = parent_gmv / parent_prior_gmv - 1
        else:
            parent_growth = None
        joint_members = []
        for r in members:
            prior_gmv, yearago_gmv = prior_gmv_of[r["row_num"]], yearago_gmv_of[r["row_num"]]
            expert_gmv = expert_gmv_lookup.get(_expert_lookup_key(r)) if expert_gmv_lookup else None
            base_share = base_share_of[r["row_num"]]
            candidate_share, expert_mom, expert_yoy, needs_review = _expert_alignment_for_row(
                base_share, parent_gmv, prior_gmv, yearago_gmv, expert_gmv)
            expert_mom_of[r["row_num"]] = expert_mom
            expert_yoy_of[r["row_num"]] = expert_yoy
            expert_flag_of[r["row_num"]] = needs_review
            own_target_gmv = candidate_share * parent_gmv if parent_gmv else 0.0
            own_target_of[r["row_num"]] = own_target_gmv
            target_gmv = (target_override.get(r["row_num"], own_target_gmv) if target_override else own_target_gmv)
            if yearago_gmv and yearago_gmv >= EXPERT_MIN_ANCHOR_GMV:
                anchor = yearago_gmv
            elif prior_gmv and prior_gmv >= EXPERT_MIN_ANCHOR_GMV:
                anchor = prior_gmv
            else:
                # No real history to protect (brand-new row, or both anchors near-zero noise) -
                # anchor^2 must come out PROPORTIONAL TO target_gmv (not equal to it), so this
                # degenerate case reduces to exactly the old flat-proportional-scaling behaviour
                # (every member scaled by the same ratio) instead of over-weighting bigger rows'
                # flexibility quadratically just because they happen to have a bigger target.
                anchor = math.sqrt(max(own_target_gmv, EXPERT_MIN_ANCHOR_GMV))
            # Same ±MARKET_SHARE_HARD_CAP bound _apply_market_share_guardrail already enforces on
            # the per-row candidate, carried through as a hard limit on the group RECONCILIATION
            # too - a category's share can wander within its own guardrail range to help its
            # siblings reconcile, but reconciliation alone must never push it further than its own
            # per-row move ever could have. A row with NO expert data of its own still gets a real
            # (wider, but finite) band, not unlimited room - confirmed: dumping an entire group's
            # unresolved residual onto whichever row happens to have no expert coverage produced
            # results wildly out of line with its own peers and the overall market, which is
            # exactly as wrong as overshooting an expert-anchored row. Ordinary reconciliation
            # (every group needs some - raw ETS+Trend forecasts never sum to exactly 100% on
            # their own) still gets more room than an expert-anchored move, just not infinite.
            band = MARKET_SHARE_HARD_CAP if (expert_mom is not None or expert_yoy is not None) else NO_EXPERT_RECONCILE_BAND
            lo = max(0.0, (base_share - band) * parent_gmv) if parent_gmv else 0.0
            hi = (base_share + band) * parent_gmv if parent_gmv else 0.0
            # The share-point band above is sized relative to the PARENT - entirely reasonable
            # for a category that's a meaningful slice of it, but meaningless for a small one: 10pp
            # of a 270 Cr parent is 27 Cr of "room", nearly 5x a row whose own year-ago base is
            # only ~6 Cr - confirmed this is exactly how a small, no-expert plug row got pushed to
            # +167% YoY purely by reconciliation math, nothing to do with its own data. A SECOND,
            # independent cap closes that gap: this row's own growth can't diverge from its
            # PARENT's own growth by more than a generous but finite margin - RELATIVE to the
            # parent's movement, not an absolute magnitude in isolation, since the whole platform
            # (or category) can legitimately resize a lot between periods and every row under it
            # should be allowed to move with it. Confirmed: an absolute cap alone wrongly flagged
            # a row whose own growth simply tracked a large, genuine swing in its parent's total.
            # The tighter of this and the share-point bound above always wins.
            sanity_anchor = yearago_gmv if (yearago_gmv and yearago_gmv >= EXPERT_MIN_ANCHOR_GMV) else (
                prior_gmv if (prior_gmv and prior_gmv >= EXPERT_MIN_ANCHOR_GMV) else None)
            if sanity_anchor:
                center = parent_growth if parent_growth is not None else 0.0
                lo = max(lo, sanity_anchor * (1 + center - MAX_PLAUSIBLE_GROWTH_MAGNITUDE))
                hi = min(hi, sanity_anchor * (1 + center + MAX_PLAUSIBLE_GROWTH_MAGNITUDE))
            joint_members.append({"row_num": r["row_num"], "target_gmv": target_gmv, "anchor": anchor,
                                   "lo": lo, "hi": max(hi, lo)})
        result, exhausted = _joint_reconcile_group(joint_members, parent_gmv or 0.0)
        if exhausted:
            # Every member hit its own guardrail limit and the group still can't reconcile to its
            # parent's GMV - a real conflict between this group's guardrails and its parent total,
            # not something to silently override. Flag every member for manual review rather than
            # breach anyone's bound anyway.
            for m in joint_members:
                expert_flag_of[m["row_num"]] = True
        return result

    def run_pass(target_override_l0=None, target_override_l1=None):
        """One full top-down resolution of every level. Called twice: once "rough" (no overrides,
        exactly the original sequential design) purely to learn what each L1/L2 group's children
        collectively want, and once "informed" (with each L0/L1's target blended toward its own
        children's aggregate preference - see below) to produce the real, final result."""
        l0_members_ = [r for r in rows if r["kind"] in ("l0", "l0_flat")]
        l0_gmv_by_row_ = resolve_level(l0_members_, total_gmv, total_row_num, target_override_l0) if l0_members_ else {}
        l0_gmv_ = {r["l0"]: l0_gmv_by_row_.get(r["row_num"], 0.0) for r in l0_members_}
        l0_row_num_ = {r["l0"]: r["row_num"] for r in l0_members_}

        l0_to_l1_ = {}
        for r in rows:
            if r["kind"] == "l1":
                l0_to_l1_.setdefault(r["l0"], []).append(r)
        l1_gmv_, l1_row_num_ = {}, {}
        for l0, members in l0_to_l1_.items():
            resolved = resolve_level(members, l0_gmv_.get(l0, 0.0), l0_row_num_.get(l0), target_override_l1)
            for r in members:
                l1_gmv_[(r["l0"], r["l1"])] = resolved.get(r["row_num"], 0.0)
                l1_row_num_[(r["l0"], r["l1"])] = r["row_num"]

        l1_to_l2_ = {}
        for r in rows:
            if r["kind"] == "l2":
                l1_to_l2_.setdefault((r["l0"], r["l1"]), []).append(r)
        l2_gmv_ = {}
        for (l0, l1), members in l1_to_l2_.items():
            resolved = resolve_level(members, l1_gmv_.get((l0, l1), 0.0), l1_row_num_.get((l0, l1)))
            for r in members:
                l2_gmv_[(r["l0"], r["l1"], r["l2"])] = resolved.get(r["row_num"], 0.0)
        return l0_members_, l0_gmv_, l0_to_l1_, l1_gmv_, l1_to_l2_, l2_gmv_

    # PASS 1 (rough): the original sequential design, each level's target coming only from its
    # OWN per-row candidate, oblivious to what its children would separately want. Run purely to
    # learn own_target_of for every row - its l0_gmv/l1_gmv/l2_gmv are discarded, not the final
    # answer (confirmed via real data: a level resolved in isolation from its children routinely
    # disagreed with what they wanted, and no amount of smarter RESIDUAL-SPREADING at the child
    # level can fix a disagreement that originates one level above it).
    run_pass()

    # Blend each parent's own candidate with its children's aggregate ask (50/50 - letting a
    # parent's resolved value flex toward its children's collective preference, instead of
    # treating it as fixed and forcing 100% of any disagreement onto the children). Skipped for
    # a parent with no children at this granularity (a flat L0, or an L1 with no L2 breakdown) -
    # nothing to blend toward.
    l0_to_l1_members, l1_to_l2_members = {}, {}
    for r in rows:
        if r["kind"] == "l1":
            l0_to_l1_members.setdefault(r["l0"], []).append(r)
        elif r["kind"] == "l2":
            l1_to_l2_members.setdefault((r["l0"], r["l1"]), []).append(r)

    blended_l1_target = {}
    for (l0, l1), children in l1_to_l2_members.items():
        l1_row = next((r for r in rows if r["kind"] == "l1" and r["l0"] == l0 and r["l1"] == l1), None)
        if l1_row is None:
            continue
        own = own_target_of[l1_row["row_num"]]
        children_sum = sum(own_target_of[c["row_num"]] for c in children)
        blended_l1_target[l1_row["row_num"]] = 0.5 * own + 0.5 * children_sum

    blended_l0_target = {}
    for l0, children in l0_to_l1_members.items():
        l0_row = next((r for r in rows if r["kind"] in ("l0", "l0_flat") and r["l0"] == l0), None)
        if l0_row is None:
            continue
        own = own_target_of[l0_row["row_num"]]
        children_sum = sum(blended_l1_target.get(c["row_num"], own_target_of[c["row_num"]]) for c in children)
        blended_l0_target[l0_row["row_num"]] = 0.5 * own + 0.5 * children_sum

    # PASS 2 (informed, final): re-resolve top-down using the blended targets - the real answer.
    l0_members, l0_gmv, l0_to_l1, l1_gmv, l1_to_l2, l2_gmv = run_pass(blended_l0_target, blended_l1_target)
    l0_row = {r["l0"]: r["row_num"] for r in l0_members}
    l1_row = {(r["l0"], r["l1"]): r["row_num"] for l0 in l0_to_l1 for r in l0_to_l1[l0]}

    out = {}
    for r in rows:
        key = _row_key(r)
        if r["kind"] == "total":
            out[key] = {"forecast_share": None, "trend_share": None, "expert_mom": None, "expert_yoy": None,
                        "expert_flag": None, "share": 1.0, "gmv": total_gmv, "is_plug": False,
                        "parent_row": None, "sibling_rows": None}
            continue

        gk = _topline_group_key(r)
        is_plug = gk is not None and last_row_of_group.get(gk) == r["row_num"]
        if is_plug:
            forecast_share = trend_share = expert_mom = expert_yoy = expert_flag = None
            sibling_rows = [m["row_num"] for m in group_members[gk] if m["row_num"] != r["row_num"]]
        else:
            forecast_share = forecast_of[r["row_num"]]
            trend_share = trend_of[r["row_num"]]
            expert_mom = expert_mom_of.get(r["row_num"])
            expert_yoy = expert_yoy_of.get(r["row_num"])
            expert_flag = expert_flag_of.get(r["row_num"])
            sibling_rows = None

        if r["kind"] in ("l0", "l0_flat"):
            parent_row, parent_gmv = total_row_num, total_gmv
            gmv = l0_gmv.get(r["l0"], 0.0)
        elif r["kind"] == "l1":
            parent_row, parent_gmv = l0_row.get(r["l0"]), l0_gmv.get(r["l0"], 0.0)
            gmv = l1_gmv.get((r["l0"], r["l1"]), 0.0)
        elif r["kind"] == "l2":
            parent_row, parent_gmv = l1_row.get((r["l0"], r["l1"])), l1_gmv.get((r["l0"], r["l1"]), 0.0)
            gmv = l2_gmv.get((r["l0"], r["l1"], r["l2"]), 0.0)
        else:
            parent_row, parent_gmv = None, 0.0
            gmv = 0.0  # Category Topline has no brand-level rows

        share = (gmv / parent_gmv) if parent_gmv else 0.0
        out[key] = {"forecast_share": forecast_share, "trend_share": trend_share,
                    "expert_mom": expert_mom, "expert_yoy": expert_yoy, "expert_flag": expert_flag,
                    "share": share, "gmv": gmv, "is_plug": is_plug,
                    "parent_row": parent_row, "sibling_rows": sibling_rows}
    return out


def _build_expert_brand_gmv_lookup(platform, month_label):
    """Brand-level sibling of _build_expert_gmv_lookup, for Brand Share Cons step 2: rolls the
    Raw expert data file up to (L0, L1, L2, Brand) GMV totals for one platform/month. Same
    empty-dict-not-error behaviour when this platform/month isn't covered yet."""
    agg = _load_raw_expert_agg(platform)
    if agg.empty:
        return {}
    sub = agg[agg["month_label"] == month_label]
    if sub.empty:
        return {}
    totals = sub.groupby(["l0", "l1", "l2", "brand"])["gmv"].sum()
    return {(l0, l1, l2, brand): float(val) for (l0, l1, l2, brand), val in totals.items()}


def _compute_brand_share_forecast(bsc_rows, category_forecast, expert_brand_gmv_lookup=None):
    """Brand Share Cons step 2: one level deeper than _compute_category_topline_forecast - same
    ETS+Trend baseline, same expert-data YoY/MoM alignment (shares _expert_alignment_for_row and
    its guardrails), same proportional reconciliation, same "last sibling is a locked formula"
    convention - but the siblings are BRANDS within one L2 (or flat L0)
    group, and the group's own parent GMV is that L2/L0's just-computed Category Topline number
    (from `category_forecast`, step 1), not a hard total_gmv input.

    Only brand groups with an 'Others' plug row are computed at all - a group without one has no
    cell that can absorb reconciliation, exactly the same rule Manual Change already applies
    (_bsc_others_present) to decide which brand rows are editable in the first place.

    Returns {row_key: {...}} in the exact same shape _compute_category_topline_forecast does, so
    _write_forecast_column and the Brand Validation tab's diagnostics work unchanged on either."""
    others_present = _bsc_others_present(bsc_rows)

    # Resolve each category-level row's OWN just-forecasted GMV (step 1's output) into plain
    # l0/l1/l2 lookup tables, so a brand group's parent GMV can be found the same way
    # _read_report_sheet itself resolves a brand row's parent (L2 if present, else flat L0).
    l0_gmv_fc, l2_gmv_fc = {}, {}
    l0_row_fc, l2_row_fc = {}, {}
    for r in bsc_rows:
        if r["kind"] not in ("l0", "l0_flat", "l2"):
            continue
        data = category_forecast.get(_row_key(r))
        if data is None:
            continue
        if r["kind"] in ("l0", "l0_flat"):
            l0_gmv_fc[r["l0"]] = data["gmv"]
            l0_row_fc[r["l0"]] = r["row_num"]
        else:
            l2_gmv_fc[(r["l0"], r["l1"], r["l2"])] = data["gmv"]
            l2_row_fc[(r["l0"], r["l1"], r["l2"])] = r["row_num"]

    def parent_gmv_for(l0, l1, l2):
        return l2_gmv_fc.get((l0, l1, l2), l0_gmv_fc.get(l0))

    def parent_row_for(l0, l1, l2):
        return l2_row_fc.get((l0, l1, l2), l0_row_fc.get(l0))

    def _bnorm(v):
        return str(v or "").strip().lower()

    # The category row (L2, or flat L0 if no L2) backing each brand group - its OWN prior/year-ago
    # GMV, so a brand group's sanity cap (see resolve_level in _compute_category_topline_forecast,
    # same idea applied here) can be measured relative to how much the CATEGORY itself just moved,
    # not an absolute magnitude in isolation.
    category_row_for = {}
    for r in bsc_rows:
        if r["kind"] == "l2":
            category_row_for[(r["l0"], r["l1"], r["l2"])] = r
        elif r["kind"] in ("l0", "l0_flat"):
            category_row_for.setdefault((r["l0"], None, None), r)

    def category_row_for_group(l0, l1, l2):
        return category_row_for.get((l0, l1, l2)) or category_row_for.get((l0, None, None))

    group_members = {}
    for r in bsc_rows:
        if r["kind"] not in ("brand", "others"):
            continue
        group = (r["l0"], r["l1"], r["l2"])
        if group not in others_present:
            continue  # no locked cell to absorb reconciliation - left untouched, same as edit view
        group_members.setdefault(group, []).append(r)
    last_row_of_group = {g: members[-1]["row_num"] for g, members in group_members.items()}

    forecast_of, trend_of = {}, {}
    expert_mom_of, expert_yoy_of, expert_flag_of = {}, {}, {}
    out = {}
    for group, members in group_members.items():
        l0, l1, l2 = group
        parent_gmv = parent_gmv_for(l0, l1, l2) or 0.0
        cat_row = category_row_for_group(l0, l1, l2)
        parent_growth = None
        if cat_row is not None:
            cutoff_cat = len(cat_row["share"])
            cat_gmv_hist = cat_row.get("gmv") or []
            parent_prior = cat_gmv_hist[cutoff_cat - 1] if 1 <= cutoff_cat <= len(cat_gmv_hist) else None
            parent_yearago = cat_gmv_hist[cutoff_cat - 12] if 12 <= cutoff_cat <= len(cat_gmv_hist) else None
            if parent_yearago and parent_yearago >= EXPERT_MIN_ANCHOR_GMV:
                parent_growth = parent_gmv / parent_yearago - 1
            elif parent_prior and parent_prior >= EXPERT_MIN_ANCHOR_GMV:
                parent_growth = parent_gmv / parent_prior - 1

        joint_members = []
        for r in members:
            forecast_share = _forecast_ets(r["share"])
            if forecast_share is not None:
                forecast_share = max(0.0, forecast_share)
            trend_share = _trend_blend(r["share"])
            if trend_share is not None:
                trend_share = max(0.0, trend_share)
            vals = [v for v in (forecast_share, trend_share) if v is not None]
            base_share = max(0.0, sum(vals) / len(vals)) if vals else 0.0
            forecast_of[r["row_num"]] = forecast_share
            trend_of[r["row_num"]] = trend_share

            cutoff_local = len(r["share"])
            gmv_history = r.get("gmv") or []
            prior_gmv = gmv_history[cutoff_local - 1] if cutoff_local >= 1 and cutoff_local <= len(gmv_history) else None
            yearago_gmv = gmv_history[cutoff_local - 12] if cutoff_local >= 12 and cutoff_local <= len(gmv_history) else None
            # Must match _build_expert_brand_gmv_lookup's own normalization exactly (it lowercases
            # L0_Cat/L1_Cat/L2_Cat/Brand before building its keys) - l0/l1/l2 here come straight
            # from the BSC sheet's real-case text (e.g. "Grooming"), so leaving them un-normalized
            # would silently never match anything in production.
            brand_key = (_bnorm(l0), _bnorm(l1), _bnorm(l2), _bnorm(r["label"]))
            expert_gmv = expert_brand_gmv_lookup.get(brand_key) if expert_brand_gmv_lookup else None

            candidate_share, expert_mom, expert_yoy, needs_review = _expert_alignment_for_row(
                base_share, parent_gmv, prior_gmv, yearago_gmv, expert_gmv)
            expert_mom_of[r["row_num"]] = expert_mom
            expert_yoy_of[r["row_num"]] = expert_yoy
            expert_flag_of[r["row_num"]] = needs_review
            target_gmv = candidate_share * parent_gmv if parent_gmv else 0.0

            if yearago_gmv and yearago_gmv >= EXPERT_MIN_ANCHOR_GMV:
                anchor = yearago_gmv
            elif prior_gmv and prior_gmv >= EXPERT_MIN_ANCHOR_GMV:
                anchor = prior_gmv
            else:
                anchor = math.sqrt(max(target_gmv, EXPERT_MIN_ANCHOR_GMV))

            # Same bounded reconciliation as Category Topline's resolve_level - see its own
            # comments for the full rationale (both the share-point band and the anchor-relative
            # sanity cap exist to stop one brand absorbing an unbounded share of its group's
            # reconciliation gap, whether or not it has its own expert coverage).
            band = MARKET_SHARE_HARD_CAP if (expert_mom is not None or expert_yoy is not None) else NO_EXPERT_RECONCILE_BAND
            lo = max(0.0, (base_share - band) * parent_gmv) if parent_gmv else 0.0
            hi = (base_share + band) * parent_gmv if parent_gmv else 0.0
            sanity_anchor = yearago_gmv if (yearago_gmv and yearago_gmv >= EXPERT_MIN_ANCHOR_GMV) else (
                prior_gmv if (prior_gmv and prior_gmv >= EXPERT_MIN_ANCHOR_GMV) else None)
            if sanity_anchor:
                center = parent_growth if parent_growth is not None else 0.0
                lo = max(lo, sanity_anchor * (1 + center - MAX_PLAUSIBLE_GROWTH_MAGNITUDE))
                hi = min(hi, sanity_anchor * (1 + center + MAX_PLAUSIBLE_GROWTH_MAGNITUDE))
            joint_members.append({"row_num": r["row_num"], "target_gmv": target_gmv, "anchor": anchor,
                                   "lo": lo, "hi": max(hi, lo)})

        resolved, exhausted = _joint_reconcile_group(joint_members, parent_gmv)
        if exhausted:
            for m in joint_members:
                expert_flag_of[m["row_num"]] = True

        for r in members:
            key = _row_key(r)
            is_plug = last_row_of_group[group] == r["row_num"]
            gmv = resolved.get(r["row_num"], 0.0)
            share = (gmv / parent_gmv) if parent_gmv else 0.0
            if is_plug:
                forecast_share = trend_share = expert_mom = expert_yoy = expert_flag = None
                sibling_rows = [m["row_num"] for m in members if m["row_num"] != r["row_num"]]
                parent_row = parent_row_for(l0, l1, l2)
            else:
                forecast_share = forecast_of[r["row_num"]]
                trend_share = trend_of[r["row_num"]]
                expert_mom = expert_mom_of[r["row_num"]]
                expert_yoy = expert_yoy_of[r["row_num"]]
                expert_flag = expert_flag_of[r["row_num"]]
                sibling_rows = None
                parent_row = None
            out[key] = {"forecast_share": forecast_share, "trend_share": trend_share,
                        "expert_mom": expert_mom, "expert_yoy": expert_yoy, "expert_flag": expert_flag,
                        "share": share, "gmv": gmv, "is_plug": is_plug,
                        "parent_row": parent_row, "sibling_rows": sibling_rows}
    return out


def _write_forecast_column(path, sheet_name, month_label, rows, forecast):
    """Writes (or, on a re-run before this platform's Category Topline leaves the analyst stage,
    overwrites) one 'GMV <month_label>' column from a computed forecast - mirrors
    update_reports.py's extend_sheet for a REAL month, except every non-plug cell is a literal
    Python-computed number (there's no spreadsheet-native way to express an ETS forecast) and the
    plug cell is a genuine '=Parent-Sibling-Sibling...' formula, identical in shape to every
    other plug cell this report format already uses - so _is_locked (Manual Change's "which cell
    auto-adjusts" detection) and _resolve_cell both keep working completely unchanged on this
    new column, exactly as they do on every historical one."""
    if not path.exists():
        return False
    wb = openpyxl.load_workbook(path)
    if sheet_name not in wb.sheetnames:
        wb.close()
        return False
    ws = wb[sheet_name]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    col_name = f"GMV {month_label}"
    if col_name in header:
        col_idx = header.index(col_name) + 1
    else:
        month_cols = [i for i, h in enumerate(header) if isinstance(h, str) and h.startswith("GMV ")]
        col_idx = (month_cols[-1] + 1 if month_cols else len(header)) + 1
        ws.cell(row=1, column=col_idx, value=col_name)
    col_letter = get_column_letter(col_idx)

    for r in rows:
        data = forecast.get(_row_key(r))
        if data is None:
            continue
        if data["is_plug"]:
            formula = f"={col_letter}{data['parent_row']}" + "".join(f"-{col_letter}{sib}" for sib in data["sibling_rows"])
            ws.cell(row=r["row_num"], column=col_idx, value=formula)
        else:
            ws.cell(row=r["row_num"], column=col_idx, value=round(data["gmv"], 6))
    wb.save(path)
    wb.close()
    return True


def _find_finalized_tagged_file(platform, month_label):
    """The finalized Monthly Setup tagged file for this exact platform/month label (e.g.
    "Sep-26"), if one exists - the most authoritative expert-data source for that specific month.
    None if that platform/month isn't finalized (or doesn't exist in Monthly Setup's state at
    all - e.g. a historical month predating this whole pipeline)."""
    state = _load_monthly_state()
    for month_key, m in state.get("months", {}).items():
        if month_key.replace("'", "-") != month_label:
            continue
        p = m.get("platforms", {}).get(platform)
        if p and p.get("status") == "finalized" and p.get("final_file"):
            return Path(p["final_file"])
    return None


def _build_current_month_expert_lookup(platform, month_label):
    """'What does expert data say this month's GMV was', generalized to any month Manual Change
    might be reviewing (not just the current in-progress one): the finalized Monthly Setup tagged
    file if this exact platform/month has one (most authoritative), else whatever the Raw expert
    data file itself covers for this month. Empty (not an error) if neither source covers it -
    "no expert data available for this month" is a normal, expected outcome here."""
    final_path = _find_finalized_tagged_file(platform, month_label)
    if final_path is not None:
        lookup = _build_finalized_tagged_gmv_lookup(final_path)
        if lookup:
            return lookup
    return _build_expert_gmv_lookup(platform, month_label)


def _attach_expert_preview(edit_rows, platform, month_label):
    """Attaches 'expert_abs'/'expert_mom'/'expert_yoy' to each Manual Change row, wherever expert
    data is available for this platform/month - confirmed to extend here the same way the
    read-only Category Topline view already shows it, pure expert-data-to-expert-data (current
    month's lookup vs the Raw expert data file's own prior-month/year-ago figures, never Master
    Data) for exactly the reasons established there."""
    current_lookup = _build_current_month_expert_lookup(platform, month_label)
    prior_label = _shift_month_label(month_label, -1)
    yearago_label = _shift_month_label(month_label, -12)
    prior_lookup = _build_expert_gmv_lookup(platform, prior_label) if prior_label else {}
    yearago_lookup = _build_expert_gmv_lookup(platform, yearago_label) if yearago_label else {}
    for r in edit_rows:
        key = _preview_lookup_key(r)
        current = current_lookup.get(key)
        r["expert_abs"] = current
        prior_gmv = prior_lookup.get(key)
        yearago_gmv = yearago_lookup.get(key)
        r["expert_mom"] = (current / prior_gmv - 1) if (current is not None and prior_gmv) else None
        r["expert_yoy"] = (current / yearago_gmv - 1) if (current is not None and yearago_gmv) else None


def _build_edit_rows(rows, raw_cache, gmv_idx, months, month):
    idx = _month_index(months, month)
    parent_of = _parent_gmv_lookup(rows)
    out = []
    for r in rows:
        if r["kind"] not in ("total", "l0", "l0_flat", "l1", "l2"):
            continue  # brand-level rows have no place in this category-level edit view
        key = _row_key(r)
        parent = parent_of[key]
        gmv = r["gmv"][idx]
        if r["kind"] == "total":
            share = 1.0
        else:
            share = (gmv / parent[idx]) if (parent and parent[idx]) else None
        mom_curr = r["growth"][idx]
        mom_prev = r["growth"][idx - 1] if idx >= 1 else None
        yoy = None
        if idx >= 12 and r["gmv"][idx - 12]:
            yoy = (gmv - r["gmv"][idx - 12]) / r["gmv"][idx - 12]
        locked = r["kind"] != "total" and _is_locked(raw_cache, gmv_idx, idx, r["row_num"])
        editable = r["kind"] != "total" and not locked
        out.append({
            "row_key": key, "kind": r["kind"], "level": r["level"], "label": r["label"],
            "l0": r["l0"], "l1": r["l1"], "l2": r["l2"], "row_num": r["row_num"],
            "gmv": gmv, "share": share, "mom_growth_prev": mom_prev, "mom_growth_curr": mom_curr,
            "yoy_growth": yoy, "editable": editable, "locked": locked,
            "group_key": _topline_group_key(r),
        })
    return out


def _group_checks(edit_rows):
    """One check-set per parent group (all L0s under Total; each L0's L1 children; each L1's
    L2 children): shares add to ~100%, nothing negative, exactly one locked (formula) cell."""
    groups = {}
    for r in edit_rows:
        gk = r.get("group_key") or _topline_group_key(r)
        if gk:
            groups.setdefault(gk, []).append(r)
    checks = {}
    for group_key, children in groups.items():
        shares = [c["share"] for c in children if c["share"] is not None]
        shares_sum_ok = abs(sum(shares) - 1.0) < 0.001 if shares else True
        no_negative = all((c["gmv"] or 0) >= -1e-6 for c in children)
        locked_count = sum(1 for c in children if not c["editable"])
        formula_intact = locked_count == 1
        checks[group_key] = {
            "shares_sum_ok": shares_sum_ok, "no_negative": no_negative, "formula_intact": formula_intact,
            "ok": shares_sum_ok and no_negative and formula_intact,
        }
    return checks


def _apply_share_edits(rows, raw_cache, gmv_idx, months, month, edits):
    """Proposed new shares -> {row_key: new_gmv}, including each touched group's locked sibling
    recalculated to absorb the difference (mirroring what the live formula would do in Excel)."""
    idx = _month_index(months, month)
    parent_of = _parent_gmv_lookup(rows)
    by_key = {_row_key(r): r for r in rows}

    changes = {}
    touched_groups = set()
    for e in edits:
        r = by_key.get(e["row_key"])
        if r is None:
            raise HTTPException(400, f"Unknown row '{e['row_key']}'")
        if not e["row_key"] in by_key or r["kind"] == "total":
            raise HTTPException(400, "Total BPC cannot be edited here")
        if _is_locked(raw_cache, gmv_idx, idx, r["row_num"]):
            raise HTTPException(400, f"'{r['label']}' is the auto-adjusting cell for its group and can't be edited directly")
        parent = parent_of[e["row_key"]]
        if parent is None or not parent[idx]:
            raise HTTPException(400, f"Row '{e['row_key']}' has no parent total to derive GMV from")
        new_gmv = e["new_share"] * parent[idx]
        changes[e["row_key"]] = new_gmv
        if r["kind"] == "l1":
            touched_groups.add(("l0", r["l0"]))
        elif r["kind"] == "l2":
            touched_groups.add(("l1", r["l0"], r["l1"]))
        elif r["kind"] in ("l0", "l0_flat"):
            touched_groups.add(("total",))

    for group in touched_groups:
        if group[0] == "total":
            siblings = [r for r in rows if r["kind"] in ("l0", "l0_flat")]
            parent_val = next(r["gmv"][idx] for r in rows if r["kind"] == "total")
        elif group[0] == "l0":
            l0 = group[1]
            siblings = [r for r in rows if r["kind"] == "l1" and r["l0"] == l0]
            parent_val = next(r["gmv"][idx] for r in rows if r["kind"] in ("l0", "l0_flat") and r["l0"] == l0)
        else:
            l0, l1 = group[1], group[2]
            siblings = [r for r in rows if r["kind"] == "l2" and r["l0"] == l0 and r["l1"] == l1]
            parent_val = next(r["gmv"][idx] for r in rows if r["kind"] == "l1" and r["l0"] == l0 and r["l1"] == l1)

        locked_row = None
        total_static = 0.0
        for s in siblings:
            key = _row_key(s)
            if _is_locked(raw_cache, gmv_idx, idx, s["row_num"]):
                locked_row = s
            else:
                total_static += changes.get(key, s["gmv"][idx])
        if locked_row is not None:
            changes[_row_key(locked_row)] = parent_val - total_static

    _cascade_descendants(rows, raw_cache, gmv_idx, idx, changes)
    return changes, idx


def _cascade_descendants(rows, raw_cache, gmv_idx, idx, changes):
    """Any L0/L1 row whose own GMV just changed - directly edited, or the auto-adjusting sibling
    that absorbed someone else's edit one level up - must push that change down into its own
    children, or the level below keeps holding stale absolute GMVs that no longer sum to their own
    (now-changed) parent - invisible to _group_checks, since that only checks each level's shares
    sum to ~100% against whatever parent value it's handed, not whether a parent's displayed GMV
    still equals the sum of its children (confirmed: this is exactly what silently broke Face
    Skin's L2 rows vs its own L1 total after an edit/absorption elsewhere cascaded into it)."""
    l0_children, l1_children, l0_rows, l1_rows = {}, {}, {}, {}
    for r in rows:
        if r["kind"] in ("l0", "l0_flat"):
            l0_rows[r["l0"]] = r
        elif r["kind"] == "l1":
            l0_children.setdefault(r["l0"], []).append(r)
            l1_rows[(r["l0"], r["l1"])] = r
        elif r["kind"] == "l2":
            l1_children.setdefault((r["l0"], r["l1"]), []).append(r)

    for l0, children in l0_children.items():
        l0_row = l0_rows.get(l0)
        if l0_row is not None:
            _cascade_group(l0_row, children, changes, raw_cache, gmv_idx, idx)

    # Runs AFTER the L0->L1 pass above so an L1 row that just got cascaded from its own L0 parent
    # still cascades its own L2 children, using that same final value.
    for (l0, l1), children in l1_children.items():
        l1_row = l1_rows.get((l0, l1))
        if l1_row is not None:
            _cascade_group(l1_row, children, changes, raw_cache, gmv_idx, idx)


def _cascade_group(parent_row, children, changes, raw_cache, gmv_idx, idx):
    """If `parent_row`'s GMV just changed (present in `changes`), rescales `children` (its own
    next-level-down kids) to match: every non-locked child not already explicitly changed is
    scaled by the parent's change ratio, then the one locked/formula child (if any) is recomputed
    via the usual parent-minus-siblings plug - mirroring the touched-groups logic above, one level
    down - so preview/roll-up math stays internally consistent at this level too. The locked
    child's recomputed value is for in-memory use only (checks/display): it is NEVER written to
    disk, since it really is a live '=Parent-Sib-Sib...' formula that recomputes itself once its
    siblings are written - callers filter locked cells out of whatever actually gets saved."""
    parent_key = _row_key(parent_row)
    if parent_key not in changes:
        return
    old_parent_val = parent_row["gmv"][idx]
    new_parent_val = changes[parent_key]
    if not old_parent_val:
        return
    ratio = new_parent_val / old_parent_val
    locked = None
    for c in children:
        if _is_locked(raw_cache, gmv_idx, idx, c["row_num"]):
            locked = c
            continue
        c_key = _row_key(c)
        if c_key not in changes and abs(ratio - 1.0) > 1e-12:
            changes[c_key] = c["gmv"][idx] * ratio
    if locked is not None:
        non_locked_total = sum(
            changes.get(_row_key(c), c["gmv"][idx]) for c in children if _row_key(c) != _row_key(locked)
        )
        changes[_row_key(locked)] = new_parent_val - non_locked_total


class ShareEditIn(BaseModel):
    row_key: str
    new_share: float


class EditRequestIn(BaseModel):
    platform: str
    month: str
    edits: list[ShareEditIn]


def _bsc_others_present(rows):
    """(l0, l1, l2) -> the 'others' row, for every brand group that has one. A group without an
    Others row has no element that can safely absorb an edit, so its brands stay read-only."""
    return {(r["l0"], r["l1"], r["l2"]): r for r in rows if r["kind"] == "others"}


def _build_bsc_edit_rows(rows, raw_cache, gmv_idx, months, month):
    """Brand Share Cons Manual Change view: Total/L0/L1/L2 rows are read-only context (they're
    owned by Category Topline and only ever change via that tab's commit); brand rows are
    editable, scoped to whichever L2 (or flat L0) they belong to, with that group's 'Others' brand
    - when one exists - as the locked cell absorbing the difference."""
    idx = _month_index(months, month)
    parent_of = _parent_gmv_lookup(rows)
    others_present = _bsc_others_present(rows)
    out = []
    for r in rows:
        if r["kind"] not in ("total", "l0", "l0_flat", "l1", "l2", "brand", "others"):
            continue
        key = _row_key(r)
        parent = parent_of[key]
        gmv = r["gmv"][idx]
        if r["kind"] == "total":
            share = 1.0
        else:
            share = (gmv / parent[idx]) if (parent and parent[idx]) else None
        mom_curr = r["growth"][idx]
        mom_prev = r["growth"][idx - 1] if idx >= 1 else None
        yoy = None
        if idx >= 12 and r["gmv"][idx - 12]:
            yoy = (gmv - r["gmv"][idx - 12]) / r["gmv"][idx - 12]

        group_has_others = (r["l0"], r["l1"], r["l2"]) in others_present
        locked = r["kind"] == "others"
        editable = r["kind"] == "brand" and group_has_others and not _is_locked(raw_cache, gmv_idx, idx, r["row_num"])
        group_key = f"BRAND:{r['l0']}:{r['l1']}:{r['l2']}" if (r["kind"] in ("brand", "others") and group_has_others) else None
        out.append({
            "row_key": key, "kind": r["kind"], "level": r["level"], "label": r["label"],
            "l0": r["l0"], "l1": r["l1"], "l2": r["l2"], "row_num": r["row_num"],
            "gmv": gmv, "share": share, "mom_growth_prev": mom_prev, "mom_growth_curr": mom_curr,
            "yoy_growth": yoy, "editable": editable, "locked": locked, "group_key": group_key,
        })
    return out


def _bsc_group_checks(edit_rows):
    """Same shape as Topline's _group_checks, but grouped per brand-editable L2/flat-L0 - groups
    with no Others row (group_key is None on every member) never get an entry here, since there's
    nothing to check: none of their rows are editable in the first place."""
    groups = {}
    for r in edit_rows:
        if r["group_key"]:
            groups.setdefault(r["group_key"], []).append(r)
    checks = {}
    for group_key, children in groups.items():
        shares = [c["share"] for c in children if c["share"] is not None]
        shares_sum_ok = abs(sum(shares) - 1.0) < 0.001 if shares else True
        no_negative = all((c["gmv"] or 0) >= -1e-6 for c in children)
        locked_count = sum(1 for c in children if c["locked"])
        formula_intact = locked_count == 1
        checks[group_key] = {
            "shares_sum_ok": shares_sum_ok, "no_negative": no_negative, "formula_intact": formula_intact,
            "ok": shares_sum_ok and no_negative and formula_intact,
        }
    return checks


def _apply_brand_share_edits(rows, raw_cache, gmv_idx, months, month, edits):
    """Same idea as _apply_share_edits, one level down: proposed new BRAND shares -> {row_key:
    new_gmv}, including each touched L2/flat-L0 group's Others brand recalculated to absorb the
    difference."""
    idx = _month_index(months, month)
    parent_of = _parent_gmv_lookup(rows)
    by_key = {_row_key(r): r for r in rows}
    others_present = _bsc_others_present(rows)

    changes = {}
    touched_groups = set()
    for e in edits:
        r = by_key.get(e["row_key"])
        if r is None:
            raise HTTPException(400, f"Unknown row '{e['row_key']}'")
        if r["kind"] != "brand":
            raise HTTPException(400, f"'{r['label']}' can't be edited directly here")
        group = (r["l0"], r["l1"], r["l2"])
        if group not in others_present:
            raise HTTPException(400, f"'{r['label']}' has no 'Others' brand in its category to absorb the change, so it can't be edited here")
        if _is_locked(raw_cache, gmv_idx, idx, r["row_num"]):
            raise HTTPException(400, f"'{r['label']}' is the auto-adjusting cell for its group and can't be edited directly")
        parent = parent_of[e["row_key"]]
        if parent is None or not parent[idx]:
            raise HTTPException(400, f"Row '{e['row_key']}' has no parent total to derive GMV from")
        new_gmv = e["new_share"] * parent[idx]
        changes[e["row_key"]] = new_gmv
        touched_groups.add(group)

    for group in touched_groups:
        l0, l1, l2 = group
        siblings = [r for r in rows if r["kind"] in ("brand", "others") and r["l0"] == l0 and r["l1"] == l1 and r["l2"] == l2]
        parent_val = parent_of[_row_key(siblings[0])][idx]
        locked_row = others_present[group]
        total_static = 0.0
        for s in siblings:
            if s["kind"] == "others":
                continue
            total_static += changes.get(_row_key(s), s["gmv"][idx])
        changes[_row_key(locked_row)] = parent_val - total_static
    return changes, idx


def _load_bsc_rows_for_edit(platform):
    path = BSC_FILES["all"]
    if not path.exists():
        raise HTTPException(404, "Brand Share Cons workbook not found")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        if platform not in wb.sheetnames:
            raise HTTPException(404, f"Platform '{platform}' not found in Brand Share Cons")
        months = _workbook_months(path, platform)
        rows, raw_cache, gmv_idx, _ = _read_report_sheet(wb[platform], with_units=True)
    finally:
        wb.close()
    return rows, raw_cache, gmv_idx, months


@app.get("/brand-share-cons/edit-view")
def brand_share_cons_edit_view(platform: str, month: str = ""):
    rows, raw_cache, gmv_idx, months = _load_bsc_rows_for_edit(platform)
    target_month = month if month in months else months[-1]
    edit_rows = _build_bsc_edit_rows(rows, raw_cache, gmv_idx, months, target_month)
    checks = _bsc_group_checks(edit_rows)
    return {"months": months, "month": target_month, "rows": edit_rows, "group_checks": checks}


@app.post("/brand-share-cons/edit-preview")
def brand_share_cons_edit_preview(req: EditRequestIn):
    rows, raw_cache, gmv_idx, months = _load_bsc_rows_for_edit(req.platform)
    edits = [e.dict() for e in req.edits]
    changes, idx = _apply_brand_share_edits(rows, raw_cache, gmv_idx, months, req.month, edits)
    by_key = {_row_key(r): r for r in rows}
    parent_of = _parent_gmv_lookup(rows)

    result = []
    for key, new_gmv in changes.items():
        r = by_key[key]
        parent = parent_of[key]
        new_share = (new_gmv / parent[idx]) if (parent and parent[idx]) else None
        prev_gmv = r["gmv"][idx - 1] if idx >= 1 else None
        new_mom_curr = ((new_gmv - prev_gmv) / prev_gmv) if prev_gmv else None
        year_ago_gmv = r["gmv"][idx - 12] if idx >= 12 else None
        new_yoy = ((new_gmv - year_ago_gmv) / year_ago_gmv) if year_ago_gmv else None
        result.append({"row_key": key, "label": r["label"], "kind": r["kind"],
                        "old_gmv": r["gmv"][idx], "new_gmv": new_gmv, "new_share": new_share,
                        "mom_growth_curr": new_mom_curr, "yoy_growth": new_yoy})

    preview_rows = _build_bsc_edit_rows(rows, raw_cache, gmv_idx, months, req.month)
    for pr in preview_rows:
        if pr["row_key"] in changes:
            pr["gmv"] = changes[pr["row_key"]]
            parent = parent_of[pr["row_key"]]
            pr["share"] = (pr["gmv"] / parent[idx]) if (parent and parent[idx]) else pr["share"]
    checks = _bsc_group_checks(preview_rows)
    edit_ok = _edit_is_safe(preview_rows, changes, checks)
    return {"changes": result, "group_checks": checks, "edit_ok": edit_ok}


@app.post("/brand-share-cons/edit-commit")
def brand_share_cons_edit_commit(req: EditRequestIn):
    rows, raw_cache, gmv_idx, months = _load_bsc_rows_for_edit(req.platform)
    edits = [e.dict() for e in req.edits]
    changes, idx = _apply_brand_share_edits(rows, raw_cache, gmv_idx, months, req.month, edits)

    preview_rows = _build_bsc_edit_rows(rows, raw_cache, gmv_idx, months, req.month)
    parent_of = _parent_gmv_lookup(rows)
    for pr in preview_rows:
        if pr["row_key"] in changes:
            pr["gmv"] = changes[pr["row_key"]]
            parent = parent_of[pr["row_key"]]
            pr["share"] = (pr["gmv"] / parent[idx]) if (parent and parent[idx]) else pr["share"]
    checks = _bsc_group_checks(preview_rows)
    if not _edit_is_safe(preview_rows, changes, checks):
        raise HTTPException(400, "Refusing to save: roll-up check failed after this edit. Review the numbers and try again.")

    by_key = {_row_key(r): r for r in rows}
    gmv_col = gmv_idx[idx] + 1
    # Brand-level edits only ever move GMV around WITHIN one L2 (or flat L0) - the L2's own total
    # never changes, so Category Topline (which only tracks L0/L1/L2, never brands) never needs
    # updating here. Only this tab's own two variants get written, and - same rule as Topline's
    # commit - only the cells the user actually edited, never the Others brand's live formula cell.
    edited_only = {e["row_key"]: changes[e["row_key"]] for e in edits}
    _write_share_changes(BSC_FILES["all"], req.platform, gmv_col, by_key, edited_only)
    _write_share_changes(BSC_FILES["md_est"], f"{req.platform} (MD Est)"[:31], gmv_col, by_key, edited_only)

    _append_change_log("Brand Share Cons", req.platform, req.month, changes, set(edited_only.keys()), by_key, parent_of, idx)

    _REPORT_CACHE["bsc_platforms"].clear()
    _REPORT_CACHE["bsc_rows"].clear()
    return {"status": "saved", "changed": list(changes.keys())}


def _load_topline_rows_for_edit(platform):
    path = TOPLINE_FILES["all"]
    if not path.exists():
        raise HTTPException(404, "Category Topline workbook not found")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        if platform not in wb.sheetnames:
            raise HTTPException(404, f"Platform '{platform}' not found in Category Topline")
        months = _workbook_months(path, platform)
        rows, raw_cache, gmv_idx, _ = _read_report_sheet(wb[platform], with_units=False)
    finally:
        wb.close()
    return rows, raw_cache, gmv_idx, months


@app.get("/category-topline/edit-view")
def category_topline_edit_view(platform: str, month: str = ""):
    rows, raw_cache, gmv_idx, months = _load_topline_rows_for_edit(platform)
    target_month = month if month in months else months[-1]
    edit_rows = _build_edit_rows(rows, raw_cache, gmv_idx, months, target_month)
    _attach_expert_preview(edit_rows, platform, target_month)
    checks = _group_checks(edit_rows)
    return {"months": months, "month": target_month, "rows": edit_rows, "group_checks": checks}


@app.post("/category-topline/edit-preview")
def category_topline_edit_preview(req: EditRequestIn):
    rows, raw_cache, gmv_idx, months = _load_topline_rows_for_edit(req.platform)
    edits = [e.dict() for e in req.edits]
    changes, idx = _apply_share_edits(rows, raw_cache, gmv_idx, months, req.month, edits)
    by_key = {_row_key(r): r for r in rows}
    parent_of = _parent_gmv_lookup(rows)
    parent_key_of = _parent_key_lookup(rows)

    # A parent's GMV can itself have changed (directly edited, absorbed as the locked sibling, or
    # cascaded down from ITS OWN parent) - shares must be computed against that NEW value, not the
    # original one `parent_of` always holds, or a cascaded child's share stops summing to 100% of
    # its (also-changed) parent even though the absolute GMVs are perfectly consistent.
    def effective_parent_val(key):
        parent_key = parent_key_of.get(key)
        if parent_key is not None and parent_key in changes:
            return changes[parent_key]
        parent = parent_of.get(key)
        return parent[idx] if parent else None

    result = []
    for key, new_gmv in changes.items():
        r = by_key[key]
        parent_val = effective_parent_val(key)
        new_share = (new_gmv / parent_val) if parent_val else None
        # Only THIS month's cell is changing - the prior-month and year-ago values behind it are
        # untouched literals, so MoM(latest)/YoY can be recomputed live from them without another
        # round trip through _build_edit_rows.
        prev_gmv = r["gmv"][idx - 1] if idx >= 1 else None
        new_mom_curr = ((new_gmv - prev_gmv) / prev_gmv) if prev_gmv else None
        year_ago_gmv = r["gmv"][idx - 12] if idx >= 12 else None
        new_yoy = ((new_gmv - year_ago_gmv) / year_ago_gmv) if year_ago_gmv else None
        result.append({"row_key": key, "label": r["label"], "kind": r["kind"],
                        "old_gmv": r["gmv"][idx], "new_gmv": new_gmv, "new_share": new_share,
                        "mom_growth_curr": new_mom_curr, "yoy_growth": new_yoy})

    preview_rows = _build_edit_rows(rows, raw_cache, gmv_idx, months, req.month)
    for pr in preview_rows:
        if pr["row_key"] in changes:
            pr["gmv"] = changes[pr["row_key"]]
            parent_val = effective_parent_val(pr["row_key"])
            pr["share"] = (pr["gmv"] / parent_val) if parent_val else pr["share"]
    checks = _group_checks(preview_rows)
    edit_ok = _edit_is_safe(preview_rows, changes, checks)
    return {"changes": result, "group_checks": checks, "edit_ok": edit_ok}


def _edit_is_safe(preview_rows, changes, checks):
    """Whether THIS edit is safe to save - scoped to only the groups it actually touched (directly
    edited, auto-absorbed, or cascaded into), never the whole sheet. Flipkart's own source data has
    pre-existing, edit-independent groups that already fail their roll-up check today (e.g. Hair
    Care's and Body Skin's own L2 children already overshoot their L1 parent's GMV, baked into the
    uploaded workbook, nothing to do with any edit) - gating every save on the ENTIRE sheet's
    checks would refuse to save ANY edit anywhere, forever, until that unrelated pre-existing data
    is independently fixed. An edit should only be blocked by a problem IT introduced or left
    behind, never by a standing data issue elsewhere that it never touched."""
    affected = {pr["group_key"] for pr in preview_rows if pr["row_key"] in changes and pr.get("group_key")}
    return all(checks[gk]["ok"] for gk in affected if gk in checks)


def _find_row_nums(ws):
    """row_key -> row_num for one sheet - used to locate the same conceptual row in a DIFFERENT
    workbook (Brand Share Cons has extra brand rows interleaved, so its row numbers for the same
    L0/L1/L2 never match Category Topline's; the All-Platforms/MD-Estimates variants of a given
    file can also differ in which brands/categories are present). Keyed by the same row_key scheme
    as everywhere else (kind+l0+l1+l2+label) so brand rows - several sharing one (l0,l1,l2) - stay
    distinct instead of colliding on a coarser key."""
    lookup = {}
    for r_idx, row in enumerate(ws.iter_rows(min_row=2, max_col=6, values_only=True), start=2):
        if row[0] is None:
            continue
        kind, level, label, l0, l1, l2 = row
        if kind in ("total", "l0", "l0_flat", "l1", "l2", "brand", "others"):
            lookup[_row_key({"kind": kind, "l0": l0, "l1": l1, "l2": l2, "label": label})] = r_idx
    return lookup


def _write_share_changes(path, sheet, gmv_col, by_key, changes):
    if not path.exists():
        return False
    wb2 = openpyxl.load_workbook(path)
    if sheet not in wb2.sheetnames:
        wb2.close()
        return False
    ws2 = wb2[sheet]
    lookup = _find_row_nums(ws2)
    for key in changes:
        r = by_key[key]
        target_row = lookup.get(_row_key(r))
        if target_row is None:
            continue
        ws2.cell(row=target_row, column=gmv_col, value=round(changes[key], 6))
    wb2.save(path)
    wb2.close()
    return True


def _append_change_log(source, platform, month, changes, edited_keys, by_key, parent_of, idx, parent_key_of=None):
    """One row per changed cell (both the user's direct edits AND the locked sibling that
    auto-adjusted to absorb them) - an append-only audit trail of every Manual Change commit,
    independent of the workbooks themselves (which only ever hold the latest value). Shared by
    both Category Topline and Brand Share Cons's Manual Change modes - `source` distinguishes them
    in the log."""
    CHANGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new = not CHANGE_LOG_FILE.exists()
    with open(CHANGE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "source", "platform", "month", "level", "l0", "l1", "l2", "label",
                              "edit_type", "old_gmv", "new_gmv", "old_share", "new_share"])
        ts = datetime.now().isoformat(timespec="seconds")
        for key, new_gmv in changes.items():
            r = by_key[key]
            old_gmv = r["gmv"][idx]
            parent = parent_of[key]
            old_share = (old_gmv / parent[idx]) if (parent and parent[idx]) else None
            parent_key = parent_key_of.get(key) if parent_key_of else None
            new_parent_val = changes[parent_key] if (parent_key and parent_key in changes) else (parent[idx] if parent else None)
            new_share = (new_gmv / new_parent_val) if new_parent_val else None
            writer.writerow([
                ts, source, platform, month, r["kind"], r["l0"] or "", r["l1"] or "", r["l2"] or "", r["label"],
                "direct" if key in edited_keys else "auto-adjusted",
                round(old_gmv, 4), round(new_gmv, 4),
                round(old_share, 6) if old_share is not None else "",
                round(new_share, 6) if new_share is not None else "",
            ])


@app.post("/category-topline/edit-commit")
def category_topline_edit_commit(req: EditRequestIn):
    rows, raw_cache, gmv_idx, months = _load_topline_rows_for_edit(req.platform)
    edits = [e.dict() for e in req.edits]
    changes, idx = _apply_share_edits(rows, raw_cache, gmv_idx, months, req.month, edits)

    preview_rows = _build_edit_rows(rows, raw_cache, gmv_idx, months, req.month)
    parent_of = _parent_gmv_lookup(rows)
    parent_key_of = _parent_key_lookup(rows)
    for pr in preview_rows:
        if pr["row_key"] in changes:
            pr["gmv"] = changes[pr["row_key"]]
            parent_key = parent_key_of.get(pr["row_key"])
            parent = parent_of[pr["row_key"]]
            parent_val = changes[parent_key] if (parent_key and parent_key in changes) else (parent[idx] if parent else None)
            pr["share"] = (pr["gmv"] / parent_val) if parent_val else pr["share"]
    checks = _group_checks(preview_rows)
    if not _edit_is_safe(preview_rows, changes, checks):
        raise HTTPException(400, "Refusing to save: roll-up check failed after this edit. Review the numbers and try again.")

    by_key = {_row_key(r): r for r in rows}
    gmv_col = gmv_idx[idx] + 1
    # `changes` includes every per-group locked/formula sibling too (needed above, to check and
    # preview its resulting value) - those must never be written: they're live formulas, and
    # overwriting one with a plain number would permanently destroy the auto-adjusting behaviour
    # this whole feature depends on. Everything else in `changes` - the user's direct edits AND
    # every cascaded descendant's rescaled GMV - IS a plain literal cell and must be written, or
    # the cascade only ever existed in this response and the on-disk mismatch (e.g. Face Skin's
    # L2 rows vs its own L1 total) would reappear the moment the sheet is reloaded.
    to_write = {
        key: val for key, val in changes.items()
        if not _is_locked(raw_cache, gmv_idx, idx, by_key[key]["row_num"])
    }
    _write_share_changes(TOPLINE_FILES["all"], req.platform, gmv_col, by_key, to_write)
    _write_share_changes(TOPLINE_FILES["md_est"], f"{req.platform} (MD Est)"[:31], gmv_col, by_key, to_write)
    _write_share_changes(BSC_FILES["all"], req.platform, gmv_col, by_key, to_write)
    _write_share_changes(BSC_FILES["md_est"], f"{req.platform} (MD Est)"[:31], gmv_col, by_key, to_write)

    direct_keys = {e["row_key"] for e in edits}
    _append_change_log("Category Topline", req.platform, req.month, changes, direct_keys, by_key, parent_of, idx, parent_key_of)

    _REPORT_CACHE["topline_platforms"].clear()
    _REPORT_CACHE["topline_rows"].clear()
    _REPORT_CACHE["bsc_platforms"].clear()
    _REPORT_CACHE["bsc_rows"].clear()
    return {"status": "saved", "changed": list(changes.keys())}


# --------------------------------------------------------------------------
# Tab: Master Data (read-only, filtered/paginated view of the rebuilt cube)
# --------------------------------------------------------------------------
_MASTER_DATA_CACHE = {"df": None}
_MASTER_DATA_LOCK = threading.Lock()


def _get_master_data_df():
    """Cold load is genuinely slow (~20-30s, confirmed directly) - the startup warm-up thread
    populates this in the background, but a real request can still arrive while that's still in
    progress. The lock makes that request WAIT for the warm-up to finish and reuse its result,
    rather than redundantly re-parsing the same multi-hundred-thousand-row file a second time
    concurrently (confirmed this was happening: a request fired 5s after restart took 58s, far
    worse than a plain cold load alone, from the two loads contending for the same CPU)."""
    if _MASTER_DATA_CACHE["df"] is None:
        with _MASTER_DATA_LOCK:
            if _MASTER_DATA_CACHE["df"] is None:  # re-check: another thread may have just finished
                path = _resolve_master_data_file()
                if path is None:
                    raise HTTPException(404, f"No Master Data .xlsx found in {MASTER_DATA_DIR}")
                df = pd.read_excel(path, sheet_name="Master Data")
                df["_MonthLabel"] = pd.to_datetime(df["Month"]).dt.strftime("%b-%y")
                _MASTER_DATA_CACHE["df"] = df
    return _MASTER_DATA_CACHE["df"]


@app.post("/master-data/reload")
def master_data_reload():
    _MASTER_DATA_CACHE["df"] = None
    df = _get_master_data_df()
    return {"rows": len(df)}


@app.get("/master-data/meta")
def master_data_meta():
    df = _get_master_data_df()
    months_sorted = sorted(df["_MonthLabel"].dropna().unique().tolist(),
                            key=lambda s: datetime.strptime(s, "%b-%y"))
    return {
        "platforms": sorted(df["Platform"].dropna().unique().tolist()),
        "l0": sorted(df["L0_Cat"].dropna().unique().tolist()),
        "l1": sorted(df["L1_Cat"].dropna().unique().tolist()),
        "l2": sorted(df["L2_Cat"].dropna().unique().tolist()),
        "months": months_sorted,
        "total_rows": len(df),
    }


@app.get("/master-data")
def master_data(platform: str = "", l0: str = "", l1: str = "", l2: str = "",
                 brand: str = "", month: str = "", limit: int = 200, offset: int = 0):
    df = _get_master_data_df()
    filtered = df
    if platform:
        filtered = filtered[filtered["Platform"] == platform]
    if l0:
        filtered = filtered[filtered["L0_Cat"] == l0]
    if l1:
        filtered = filtered[filtered["L1_Cat"] == l1]
    if l2:
        filtered = filtered[filtered["L2_Cat"] == l2]
    if brand:
        filtered = filtered[filtered["Brand"].str.contains(brand, case=False, na=False, regex=False)]
    if month:
        filtered = filtered[filtered["_MonthLabel"] == month]

    total = len(filtered)
    limit = max(1, min(limit, 1000))
    page = filtered.iloc[offset: offset + limit].copy()
    page["Month"] = page["_MonthLabel"]
    page = page.drop(columns=["_MonthLabel"])
    # Replace NaN with None so the JSON payload has real nulls, not the string "NaN".
    page = page.astype(object).where(page.notna(), None)
    return {"total": total, "offset": offset, "limit": limit, "rows": page.to_dict(orient="records")}


def _warm_caches_in_background():
    """Master Data's own cold load (~20-30s, confirmed directly: 30.16s on a cold hit, 1.56s on
    the very next identical call) and each platform's Raw expert data file (Flipkart alone can
    take minutes cold) used to hit whichever user made the FIRST real request after any restart -
    a real, repeatedly-reported "the portal has gone slow" complaint that was really just this
    one-time tax landing on whoever happened to click first. Warming both in a background thread
    means the server is reachable immediately (this does not block startup/the first page load),
    but by the time a user actually clicks into a tab, the slow part is already done.

    Called directly from the __main__ launch block below, NOT via @app.on_event("startup") -
    that fires for every TestClient(app) instantiation too (FastAPI's ASGI lifespan events run
    regardless of transport), which would make every single test in the suite spawn this same
    thread against the REAL production Master Data/Raw expert data files - wasteful at best, and
    exactly the kind of "tests touching real production paths" this project has deliberately
    avoided everywhere else. This function only ever runs for an actual `python app.py` launch."""
    def _warm():
        try:
            _get_master_data_df()
        except Exception:
            pass
        for platform in RAW_EXPERT_CONFIG:
            try:
                _load_raw_expert_agg(platform)
            except Exception:
                pass
        # Category Topline/Brand Share Cons have their OWN separate cold-load cost (a direct
        # openpyxl read of these report workbooks, cached in _REPORT_CACHE) - confirmed this was
        # NOT covered by the two warm-ups above (Flipkart's /category-topline still took 9.3s
        # after Master Data + Raw expert data were already warm).
        for variant, path in TOPLINE_FILES.items():
            try:
                for platform in _workbook_platforms(path):
                    _get_topline_rows(variant, platform)
            except Exception:
                pass
        for variant, path in BSC_FILES.items():
            try:
                for platform in _workbook_platforms(path):
                    _get_bsc_rows(variant, platform)
            except Exception:
                pass
    threading.Thread(target=_warm, daemon=True, name="cache-warmup").start()


if __name__ == "__main__":
    import uvicorn

    _warm_caches_in_background()

    # reload=False deliberately: the reload watcher spawns a separate reloader+worker process
    # pair and restarts on any file change it notices in this folder - including OneDrive sync
    # touching the report/master-data files while someone is using the portal, which drops
    # every open connection mid-request. A single stable process is what a shared/demo tool
    # needs; re-enable reload only for active day-to-day code editing, not for showing this to
    # someone.
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
