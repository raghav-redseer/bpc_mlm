"""Lookup-based tagger for platforms whose raw expert-data export is already pre-aggregated to
Category x Sub-cat1 x Sub-cat2 x Brand (e.g. Flipkart), not SKU/product level. Unlike the SKU
engine (pipeline_v6.py - keyword/NLP matching on a free-text product name), there is no text to
parse here: each row's (Category, Sub cat 1, Sub cat 2) triple is looked up verbatim against a
persistent, per-platform mapping table seeded once from that platform's own historical VLOOKUP
sheet and extended over time via the same pending -> approved review flow the SKU engine uses.

Mirrors pipeline_v6.py's shape deliberately (method/confidence/manual_review) so app.py's bulk-tag
job plumbing and the frontend's generic preview table work for both kinds of platform unchanged.
"""
import json
import threading
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
MAPPING_DIR = ENGINE_DIR / "category_mapping_data"
MAPPING_DIR.mkdir(parents=True, exist_ok=True)

PENDING_CATEGORY_RULES_FILE = ENGINE_DIR.parent / "pending_category_rules.json"

# Platforms whose raw expert-data file is pre-aggregated at category level rather than SKU level.
# Add a platform here once its mapping table exists at category_mapping_data/{platform}.json.
# Amazon's raw export has only ONE native category field (a single concatenated "AZ Expert Data"
# tag, not three separate Category/Sub-cat1/Sub-cat2 columns like Flipkart) - its mapping keys
# are built by repeating that one tag three times (see build note in Amazon.json's "source"
# field), so the same 3-part category_key() below works unchanged for both platform shapes.
CATEGORY_LEVEL_PLATFORMS = {"Flipkart", "Amazon"}

_LOCK = threading.RLock()
_mapping_cache = {}  # platform -> {"platform":..., "source":..., "mappings": {key: {"L0":.., "L1":.., "L2":..}}}


def _norm(value):
    return str(value if value is not None else "").strip().lower()


def category_key(category, subcat1, subcat2):
    return "||".join(_norm(v) for v in (category, subcat1, subcat2))


def _mapping_path(platform):
    return MAPPING_DIR / f"{platform}.json"


def load_mapping(platform, force=False):
    with _LOCK:
        if not force and platform in _mapping_cache:
            return _mapping_cache[platform]
        path = _mapping_path(platform)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"platform": platform, "source": None, "mappings": {}}
        _mapping_cache[platform] = data
        return data


def save_mapping(platform, data):
    with _LOCK:
        _mapping_cache[platform] = data
        _mapping_path(platform).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def tag_category_row(platform, category, subcat1, subcat2):
    """Returns the same shape pipeline_v6/tag_sku_bundle_aware uses for a single SKU, so the
    rest of the bulk-tag pipeline (app.py's _process_bulk_job-equivalent) can treat a category
    row identically to a SKU row."""
    mapping = load_mapping(platform)["mappings"]
    key = category_key(category, subcat1, subcat2)
    hit = mapping.get(key)
    if hit is not None:
        return {
            "L0": hit.get("L0"), "L1": hit.get("L1"), "L2": hit.get("L2"),
            "method": "Category-Mapped", "confidence": "High", "manual_review": False,
        }
    return {
        "L0": None, "L1": None, "L2": None,
        "method": "Unmapped-Category", "confidence": "Low", "manual_review": True,
    }


# --------------------------------------------------------------------------
# Pending category rules: an unmapped (Category, Sub cat 1, Sub cat 2) combo found during tagging
# gets queued here for a human to assign an L0/L1/L2 to, mirroring pending_rules.json/
# approved_rules.json for the SKU engine - approving one folds it straight into the platform's
# mapping table (live, no restart needed), it never happens automatically.
# --------------------------------------------------------------------------
def _load_pending_category_rules():
    with _LOCK:
        if not PENDING_CATEGORY_RULES_FILE.exists():
            return []
        return json.loads(PENDING_CATEGORY_RULES_FILE.read_text(encoding="utf-8"))


def _save_pending_category_rules(rules):
    with _LOCK:
        PENDING_CATEGORY_RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")


def queue_unmapped_combos(platform, combos):
    """combos: iterable of (category, subcat1, subcat2, row_count) for keys not already in the
    mapping table or already pending. Returns how many NEW pending entries were added (existing
    ones are left alone, not duplicated on every re-upload)."""
    with _LOCK:
        pending = _load_pending_category_rules()
        existing_keys = {(r["platform"], r["key"]) for r in pending}
        added = 0
        for category, subcat1, subcat2, row_count in combos:
            key = category_key(category, subcat1, subcat2)
            if (platform, key) in existing_keys:
                continue
            pending.append({
                "id": f"{platform}:{key}",
                "platform": platform,
                "key": key,
                "category": category, "subcat1": subcat1, "subcat2": subcat2,
                "row_count": row_count,
                "status": "PENDING REVIEW",
            })
            existing_keys.add((platform, key))
            added += 1
        if added:
            _save_pending_category_rules(pending)
        return added


def approve_category_rules(approvals):
    """approvals: list of {id, l0, l1, l2}. Moves each matching pending entry's mapping into its
    platform's mapping table (live) and removes it from the pending queue."""
    with _LOCK:
        pending = _load_pending_category_rules()
        by_id = {r["id"]: r for r in pending}
        approved_ids = []
        by_platform = {}
        for a in approvals:
            entry = by_id.get(a["id"])
            if entry is None:
                continue
            by_platform.setdefault(entry["platform"], []).append((entry["key"], a))
            approved_ids.append(a["id"])

        for platform, items in by_platform.items():
            data = load_mapping(platform)
            for key, a in items:
                data["mappings"][key] = {"L0": a["l0"].strip(), "L1": a["l1"].strip(), "L2": a["l2"].strip()}
            save_mapping(platform, data)

        remaining = [r for r in pending if r["id"] not in approved_ids]
        _save_pending_category_rules(remaining)
        return approved_ids


def reject_category_rules(ids):
    with _LOCK:
        pending = _load_pending_category_rules()
        ids = set(ids)
        remaining = [r for r in pending if r["id"] not in ids]
        rejected_count = len(pending) - len(remaining)
        if rejected_count:
            _save_pending_category_rules(remaining)
        return rejected_count
