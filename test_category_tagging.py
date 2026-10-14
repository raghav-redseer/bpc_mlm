"""Regression tests for the category-level expert-data tagging path (Flipkart and similar
platforms whose raw export is pre-aggregated to Category x Sub-cat1 x Sub-cat2 x Brand, not
SKU/product level - see engine/category_mapping.py).

Never touches the real mapping/pending-rules files on disk - every test gets its own tmp_path
monkeypatched over category_mapping.py's module-level path constants and in-memory cache.
"""
import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as appmod
import category_mapping as cm


@pytest.fixture
def cm_env(tmp_path, monkeypatch):
    mapping_dir = tmp_path / "category_mapping_data"
    mapping_dir.mkdir()
    monkeypatch.setattr(cm, "MAPPING_DIR", mapping_dir)
    monkeypatch.setattr(cm, "PENDING_CATEGORY_RULES_FILE", tmp_path / "pending_category_rules.json")
    monkeypatch.setattr(cm, "_mapping_cache", {})
    monkeypatch.setattr(cm, "CATEGORY_LEVEL_PLATFORMS", {"Flipkart"})
    seed = {
        "platform": "Flipkart",
        "source": "test",
        "mappings": {
            "aftershave||aftershave||aftershave": {"L0": "Grooming", "L1": "Hygiene", "L2": "Shaving"},
            "attar||attar||attar": {"L0": "Makeup and Fragrances", "L1": "Fragrance", "L2": "Perfume"},
        },
    }
    cm.save_mapping("Flipkart", seed)
    return cm


@pytest.fixture
def client(monkeypatch):
    appmod.JOBS.clear()
    return TestClient(appmod.app)


# ---------------------------------------------------------------- tag_category_row / mapping

def test_tag_category_row_mapped_combo_is_high_confidence(cm_env):
    result = cm.tag_category_row("Flipkart", "Aftershave", "Aftershave", "Aftershave")
    assert result == {"L0": "Grooming", "L1": "Hygiene", "L2": "Shaving",
                       "method": "Category-Mapped", "confidence": "High", "manual_review": False}


def test_tag_category_row_lookup_is_case_and_whitespace_insensitive(cm_env):
    result = cm.tag_category_row("Flipkart", "  ATTAR ", "attar", "Attar")
    assert result["L0"] == "Makeup and Fragrances"
    assert result["manual_review"] is False


def test_tag_category_row_unmapped_combo_flags_manual_review(cm_env):
    result = cm.tag_category_row("Flipkart", "Totally New Category", "X", "Y")
    assert result["L0"] is None
    assert result["method"] == "Unmapped-Category"
    assert result["manual_review"] is True


# ---------------------------------------------------------------- pending category rules

def test_queue_unmapped_combos_adds_new_and_skips_duplicates(cm_env):
    combos = [("New Cat", "Sub1", "Sub2", 10)]
    added_first = cm.queue_unmapped_combos("Flipkart", combos)
    added_second = cm.queue_unmapped_combos("Flipkart", combos)
    assert added_first == 1
    assert added_second == 0  # already pending - not duplicated on a re-upload
    assert len(cm._load_pending_category_rules()) == 1


def test_approve_category_rules_folds_into_mapping_and_clears_pending(cm_env):
    cm.queue_unmapped_combos("Flipkart", [("New Cat", "Sub1", "Sub2", 10)])
    pending = cm._load_pending_category_rules()
    rule_id = pending[0]["id"]

    approved = cm.approve_category_rules([{"id": rule_id, "l0": "Grooming", "l1": "Hair Care", "l2": "Shampoo"}])

    assert approved == [rule_id]
    assert cm._load_pending_category_rules() == []
    # Live mapping now resolves the combo without a server restart.
    result = cm.tag_category_row("Flipkart", "New Cat", "Sub1", "Sub2")
    assert result == {"L0": "Grooming", "L1": "Hair Care", "L2": "Shampoo",
                       "method": "Category-Mapped", "confidence": "High", "manual_review": False}


def test_reject_category_rules_removes_without_touching_mapping(cm_env):
    cm.queue_unmapped_combos("Flipkart", [("New Cat", "Sub1", "Sub2", 10)])
    rule_id = cm._load_pending_category_rules()[0]["id"]

    rejected_count = cm.reject_category_rules([rule_id])

    assert rejected_count == 1
    assert cm._load_pending_category_rules() == []
    assert cm.tag_category_row("Flipkart", "New Cat", "Sub1", "Sub2")["manual_review"] is True


# ---------------------------------------------------------------- end-to-end via the API

def _fake_raw_file_bytes():
    df = pd.DataFrame({
        "Category": ["Aftershave", "Attar", "Brand New Thing"],
        "Sub cat 1": ["Aftershave", "Attar", "Sub X"],
        "Sub cat 2": ["Aftershave", "Attar", "Sub Y"],
        "brand": ["BrandA", "BrandB", "BrandC"],
        "Gmv": [1000, 2000, 500],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Raw Data")
    buf.seek(0)
    return buf.getvalue()


def test_category_level_platforms_endpoint_lists_flipkart(cm_env, client):
    resp = client.get("/category-level-platforms")
    assert resp.status_code == 200
    assert resp.json() == {"platforms": ["Flipkart"]}


def test_tag_bulk_category_end_to_end(cm_env, client):
    resp = client.post(
        "/tag-bulk-category",
        files={"file": ("FK_Test.xlsx", _fake_raw_file_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "platform": "Flipkart", "sheet_name": "Raw Data",
            "category_col": "Category", "subcat1_col": "Sub cat 1", "subcat2_col": "Sub cat 2",
            "brand_col": "brand", "save_to_disk": "false",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    import time
    for _ in range(50):
        status = client.get(f"/tag-bulk/{job_id}/status").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status.get("error")
    assert status["summary"]["clean"] == 2
    assert status["summary"]["flagged"] == 1
    assert status["summary"]["manual_review"] == 1

    preview = {(r["Category"], r["Sub cat 1"], r["Sub cat 2"]): r for r in status["preview"]}
    assert preview[("Aftershave", "Aftershave", "Aftershave")]["L2"] == "Shaving"
    assert preview[("Brand New Thing", "Sub X", "Sub Y")]["Manual_Review_Required"] == "Yes"

    # The unmapped combo was queued for review, ready for a human to approve via
    # /pending-category-rules/bulk-approve.
    pending = client.get("/pending-category-rules").json()
    assert len(pending) == 1
    assert pending[0]["category"] == "Brand New Thing"


# ---------------------------------------------------------------- single-tag-column platforms
# (e.g. Amazon: one concatenated "AZ Expert Data"-style tag, no separate Sub-cat1/Sub-cat2 fields)

def _fake_single_tag_file_bytes():
    df = pd.DataFrame({
        "Tag": ["GroomingSkinCareFace", "GroomingSkinCareFace", "Unmapped Tag"],
        "brand": ["BrandA", "BrandB", "BrandC"],
        "Gmv": [1000, 500, 200],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Raw Data")
    buf.seek(0)
    return buf.getvalue()


def test_tag_bulk_category_single_column_platform_blank_subcats_default_to_category_col(cm_env):
    # A mapping keyed "tag||tag||tag" (the convention for a platform with only one native
    # category field) - L2 deliberately None for the lumped/ambiguous case, same as Amazon's
    # real Shampoo+Conditioner / Professional-NA catch-all entries. Calls the job function
    # directly (not through the HTTP endpoint) - this is testing app.py's own fallback logic
    # (blank subcat1_col/subcat2_col -> default to category_col), not the HTTP/multipart layer.
    seed = cm.load_mapping("Flipkart")
    seed["mappings"]["groomingskincareface||groomingskincareface||groomingskincareface"] = {
        "L0": "Grooming", "L1": "Face Skin", "L2": "Face care",
    }
    cm.save_mapping("Flipkart", seed)

    appmod.JOBS.clear()
    job_id = "single_col_test"
    appmod.JOBS[job_id] = {"status": "queued", "done": 0, "total": 0}
    appmod._process_category_bulk_job(
        job_id, _fake_single_tag_file_bytes(), "Amazon_Test.xlsx", "Flipkart", "Raw Data",
        "Tag", "", "", "brand",
        save_to_disk=False, save_folder="", overwrite_existing=False,
    )
    status = appmod.JOBS[job_id]
    assert status["status"] == "done", status.get("error")
    assert status["summary"]["clean"] == 2  # 2 rows of the one mapped tag
    assert status["summary"]["flagged"] == 1  # 1 row of the unmapped tag

    # The "Unique Category Tag Reference" preview should have exactly ONE category column (not
    # three duplicated "Tag" columns) since category_col == subcat1_col == subcat2_col here.
    preview_by_tag = {r["Tag"]: r for r in status["preview"]}
    assert preview_by_tag["GroomingSkinCareFace"]["L2"] == "Face care"
    assert preview_by_tag["Unmapped Tag"]["Manual_Review_Required"] == "Yes"
