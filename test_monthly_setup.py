"""
Automated regression tests for the Monthly Setup / Expert Data Tagging state machine - the part
of the portal that has actually broken in practice (silent status drift between endpoints,
password gates missing on an action, Month Complete letting through an unfinished platform).

Run with: pytest test_monthly_setup.py -v

Never touches the real Portal Data folder or password files - every test gets its own tmp_path,
monkeypatched over app.py's module-level path constants, plus a fake platform list and fake
Master Data months so no real Excel fixtures are needed. Safe to run any time, as often as you
like, in under a second.
"""
import hashlib
import json
import os

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as appmod

ADMIN_PASSWORD = "test-admin-pw"
SL_PASSWORD = "test-sl-pw"
MANAGER_PASSWORD = "test-manager-pw"


def _write_auth_file(path, password):
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    path.write_text(json.dumps({"salt": salt, "hash": h, "algo": "pbkdf2_sha256_200000"}), encoding="utf-8")


def _write_fake_topline_workbook(path):
    """A minimal but structurally real Category Topline workbook - two months of plain-number
    history (no expert-data forecast needed to pass validation) for two platforms, each with a
    Total + two L0 categories, so _compute_category_topline_forecast has a real group (one
    ETS/trend row, one last-sibling plug row) to exercise."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for platform in ("Amazon", "Blinkit"):
        ws = wb.create_sheet(platform)
        # Deliberately far from any month label the other tests in this file lock (Jan'99.. /
        # Jan'50.. /Jan'51..) so set-total-gmv's forecasted month never collides with these
        # fixed "real" historical columns.
        ws.append(["Kind", "Level", "Label", "L0", "L1", "L2", "GMV Oct-30", "GMV Nov-30"])
        ws.append(["total", 0, "Total BPC", None, None, None, 100, 110])
        ws.append(["l0", 1, "Cat A", "Cat A", None, None, 60, 66])
        ws.append(["l0_flat", 1, "Cat B", "Cat B", None, None, 40, 44])
    wb.save(path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    portal_dir = tmp_path / "Portal Data"
    tagged_dir = portal_dir / "Tagged Files"
    tagged_dir.mkdir(parents=True)

    monkeypatch.setattr(appmod, "PORTAL_DATA_DIR", portal_dir)
    monkeypatch.setattr(appmod, "PORTAL_TAGGED_DIR", tagged_dir)
    monkeypatch.setattr(appmod, "MONTHLY_STATE_FILE", portal_dir / "monthly_platform_state.json")
    monkeypatch.setattr(appmod, "ADMIN_AUTH_FILE", portal_dir / "admin_auth.json")
    monkeypatch.setattr(appmod, "SL_AUTH_FILE", portal_dir / "sl_auth.json")
    monkeypatch.setattr(appmod, "MANAGER_AUTH_FILE", portal_dir / "manager_auth.json")
    _write_auth_file(appmod.ADMIN_AUTH_FILE, ADMIN_PASSWORD)
    _write_auth_file(appmod.SL_AUTH_FILE, SL_PASSWORD)
    _write_auth_file(appmod.MANAGER_AUTH_FILE, MANAGER_PASSWORD)

    # Avoid needing a real Category Topline workbook just to get a platform list.
    monkeypatch.setattr(appmod, "_workbook_platforms", lambda path: ["Amazon", "Blinkit"])

    # Avoid needing a real Master Data workbook - a tiny fake frame with just _MonthLabel.
    fake_md = pd.DataFrame({"_MonthLabel": ["Jun-26", "Jul-26", "Aug-26"]})
    monkeypatch.setattr(appmod, "_get_master_data_df", lambda: fake_md)

    # A small real workbook for the Category Topline forecast engine (set-total-gmv) to read
    # from and write into - "md_est" deliberately points at a file that doesn't exist, so
    # _write_forecast_column's dual write there safely no-ops (its own path.exists() guard)
    # instead of needing a second fixture file just to be ignored.
    topline_dir = portal_dir / "Category Topline"
    topline_dir.mkdir(parents=True)
    topline_all_path = topline_dir / "Category Topline - All Platforms.xlsx"
    _write_fake_topline_workbook(topline_all_path)
    monkeypatch.setattr(appmod, "TOPLINE_FILES", {
        "all": topline_all_path,
        "md_est": topline_dir / "Category Topline - MD Estimates.xlsx",
    })
    appmod._REPORT_CACHE["topline_platforms"].clear()
    appmod._REPORT_CACHE["topline_rows"].clear()

    # BSC_FILES must ALWAYS be patched alongside TOPLINE_FILES - set-total-gmv now also writes
    # Category Topline's Total/L0/L1/L2 numbers into Brand Share Cons (see app.py). Pointing both
    # at tmp_path (neither file need actually exist - _write_forecast_column no-ops cleanly on a
    # missing path) is what stopped an earlier real incident: this fixture forgot to patch
    # BSC_FILES once, and a test run genuinely wrote fake month columns into the real production
    # Brand Share Cons workbook because the dict still pointed at it.
    monkeypatch.setattr(appmod, "BSC_FILES", {
        "all": portal_dir / "Brandshare Cons" / "Brand Share Cons - All Platforms.xlsx",
        "md_est": portal_dir / "Brandshare Cons" / "Brand Share Cons - MD Estimates.xlsx",
    })
    appmod._REPORT_CACHE["bsc_platforms"].clear()
    appmod._REPORT_CACHE["bsc_rows"].clear()

    appmod.JOBS.clear()
    return TestClient(appmod.app)


def _lock(client, month, platforms, password=SL_PASSWORD):
    return client.post(f"/monthly-setup/{month}/lock", json={"platforms": platforms, "password": password})


def _fake_completed_job(result=b"draft-bytes"):
    job_id = "fake-job-1"
    appmod.JOBS[job_id] = {"status": "done", "result": result}
    return job_id


# ---------------------------------------------------------------- Lock / Unlock password gate

def test_lock_rejects_missing_or_wrong_password(client):
    assert client.post("/monthly-setup/Jan'99/lock", json={"platforms": {"Amazon": True}}).status_code == 422
    assert _lock(client, "Jan'99", {"Amazon": True}, password="wrong").status_code == 403


def test_lock_with_correct_password_succeeds(client):
    res = _lock(client, "Jan'99", {"Amazon": True, "Blinkit": False})
    assert res.status_code == 200
    body = res.json()
    assert body["locked"] is True
    assert body["platforms"]["Amazon"]["status"] == "pending_upload"
    assert body["platforms"]["Blinkit"]["status"] == "no_expert_data"


def test_lock_and_unlock_use_sl_password_not_admin(client):
    """Regression guard for the exact mixup that prompted this reassignment: Monthly Setup's
    Lock/Edit are SL actions now, Month Complete/Reopen-month are the admin's - using the wrong
    one of the two must fail, not silently succeed."""
    assert _lock(client, "Jan'99", {"Amazon": True}, password=ADMIN_PASSWORD).status_code == 403
    assert _lock(client, "Jan'99", {"Amazon": True}, password=SL_PASSWORD).status_code == 200
    assert client.post("/monthly-setup/Jan'99/unlock", json={"password": ADMIN_PASSWORD}).status_code == 403
    assert client.post("/monthly-setup/Jan'99/unlock", json={"password": SL_PASSWORD}).status_code == 200


def test_unlock_requires_password(client):
    _lock(client, "Jan'99", {"Amazon": True})
    assert client.post("/monthly-setup/Jan'99/unlock", json={"password": "wrong"}).status_code == 403
    res = client.post("/monthly-setup/Jan'99/unlock", json={"password": SL_PASSWORD})
    assert res.status_code == 200
    assert res.json()["locked"] is False


def test_relocking_preserves_progress_for_unchanged_platforms(client):
    _lock(client, "Jan'99", {"Amazon": True})
    client.post(f"/monthly-setup/Jan'99/{'Amazon'}/attach-draft", json={"job_id": _fake_completed_job()})
    # Re-lock with Amazon's flag unchanged - should NOT reset its progress back to pending_upload.
    res = _lock(client, "Jan'99", {"Amazon": True})
    assert res.json()["platforms"]["Amazon"]["status"] == "draft_ready"


# ---------------------------------------------------------------- Full analyst -> SL pipeline

def test_full_happy_path_to_finalized(client):
    _lock(client, "Feb'99", {"Amazon": True})
    job_id = _fake_completed_job()

    r = client.post("/monthly-setup/Feb'99/Amazon/attach-draft", json={"job_id": job_id})
    assert r.json()["status"] == "draft_ready"

    r = client.post("/monthly-setup/Feb'99/Amazon/upload-final", files={"file": ("f.xlsx", b"final-bytes")})
    assert r.json()["status"] == "final_uploaded"

    r = client.post("/monthly-setup/Feb'99/Amazon/send-to-review")
    assert r.json()["status"] == "pending_sl_review"

    # Wrong SL password doesn't move it.
    r = client.post("/monthly-setup/Feb'99/Amazon/finalize", json={"password": "wrong"})
    assert r.status_code == 403
    state = client.get("/monthly-setup/Feb'99/board").json()
    assert state["platforms"]["Amazon"]["status"] == "pending_sl_review"

    r = client.post("/monthly-setup/Feb'99/Amazon/finalize", json={"password": SL_PASSWORD})
    assert r.status_code == 200
    assert r.json()["status"] == "finalized"
    assert r.json()["finalized_at"] is not None


def test_send_back_returns_to_analyst_stage(client):
    _lock(client, "Mar'99", {"Amazon": True})
    client.post("/monthly-setup/Mar'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/Mar'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    client.post("/monthly-setup/Mar'99/Amazon/send-to-review")

    r = client.post("/monthly-setup/Mar'99/Amazon/send-back")
    assert r.json()["status"] == "final_uploaded"
    # Analyst can re-upload again now that it's back from SL.
    r = client.post("/monthly-setup/Mar'99/Amazon/upload-final", files={"file": ("f2.xlsx", b"y")})
    assert r.status_code == 200


def test_reopen_requires_sl_password_and_goes_back_to_final_uploaded(client):
    _lock(client, "Apr'99", {"Amazon": True})
    client.post("/monthly-setup/Apr'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/Apr'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    client.post("/monthly-setup/Apr'99/Amazon/send-to-review")
    client.post("/monthly-setup/Apr'99/Amazon/finalize", json={"password": SL_PASSWORD})

    assert client.post("/monthly-setup/Apr'99/Amazon/reopen", json={"password": "wrong"}).status_code == 403
    r = client.post("/monthly-setup/Apr'99/Amazon/reopen", json={"password": SL_PASSWORD})
    assert r.json()["status"] == "final_uploaded"
    assert r.json()["finalized_at"] is None


# ---------------------------------------------------------------- Illegal transitions rejected

def test_cannot_skip_sl_review_straight_to_finalize(client):
    _lock(client, "May'99", {"Amazon": True})
    client.post("/monthly-setup/May'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/May'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    # Never sent to review - finalize must refuse.
    r = client.post("/monthly-setup/May'99/Amazon/finalize", json={"password": SL_PASSWORD})
    assert r.status_code == 400


def test_cannot_send_to_review_before_final_uploaded(client):
    _lock(client, "Jun'99", {"Amazon": True})
    r = client.post("/monthly-setup/Jun'99/Amazon/send-to-review")
    assert r.status_code == 400


def test_cannot_upload_final_while_pending_sl_review(client):
    _lock(client, "Jul'99", {"Amazon": True})
    client.post("/monthly-setup/Jul'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/Jul'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    client.post("/monthly-setup/Jul'99/Amazon/send-to-review")
    r = client.post("/monthly-setup/Jul'99/Amazon/upload-final", files={"file": ("f2.xlsx", b"y")})
    assert r.status_code == 400


# ---------------------------------------------------------------- Month Complete gating

def test_month_complete_requires_every_expert_platform_finalized(client):
    _lock(client, "Aug'99", {"Amazon": True, "Blinkit": True})
    for platform in ("Amazon", "Blinkit"):
        client.post(f"/monthly-setup/Aug'99/{platform}/attach-draft", json={"job_id": _fake_completed_job()})
        client.post(f"/monthly-setup/Aug'99/{platform}/upload-final", files={"file": ("f.xlsx", b"x")})
        client.post(f"/monthly-setup/Aug'99/{platform}/send-to-review")
    client.post("/monthly-setup/Aug'99/Amazon/finalize", json={"password": SL_PASSWORD})
    # Blinkit still pending_sl_review - Complete must refuse.
    r = client.post("/monthly-setup/Aug'99/complete", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 400

    client.post("/monthly-setup/Aug'99/Blinkit/finalize", json={"password": SL_PASSWORD})
    r = client.post("/monthly-setup/Aug'99/complete", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 200
    assert r.json()["month_complete"] is True


def test_complete_month_blocks_all_further_mutation(client):
    _lock(client, "Sep'99", {"Amazon": True})
    client.post("/monthly-setup/Sep'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/Sep'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    client.post("/monthly-setup/Sep'99/Amazon/send-to-review")
    client.post("/monthly-setup/Sep'99/Amazon/finalize", json={"password": SL_PASSWORD})
    client.post("/monthly-setup/Sep'99/complete", json={"password": ADMIN_PASSWORD})

    assert client.post("/monthly-setup/Sep'99/unlock", json={"password": SL_PASSWORD}).status_code == 400
    assert client.post(
        "/monthly-setup/Sep'99/Amazon/reopen", json={"password": SL_PASSWORD}
    ).status_code == 400
    # Downloads still work even once complete (read-only access stays open).
    r = client.get("/monthly-setup/Sep'99/Amazon/draft")
    assert r.status_code == 200


def test_reopen_month_requires_admin_password(client):
    _lock(client, "Oct'99", {"Amazon": True})
    client.post("/monthly-setup/Oct'99/Amazon/attach-draft", json={"job_id": _fake_completed_job()})
    client.post("/monthly-setup/Oct'99/Amazon/upload-final", files={"file": ("f.xlsx", b"x")})
    client.post("/monthly-setup/Oct'99/Amazon/send-to-review")
    client.post("/monthly-setup/Oct'99/Amazon/finalize", json={"password": SL_PASSWORD})
    client.post("/monthly-setup/Oct'99/complete", json={"password": ADMIN_PASSWORD})

    assert client.post("/monthly-setup/Oct'99/reopen-month", json={"password": "wrong"}).status_code == 403
    r = client.post("/monthly-setup/Oct'99/reopen-month", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 200
    assert r.json()["month_complete"] is False


# ---------------------------------------------------------------- Category Topline review track

def test_category_topline_defaults_to_analyst_for_any_locked_platform(client):
    _lock(client, "Jan'50", {"Amazon": True, "Blinkit": False})
    for platform in ("Amazon", "Blinkit"):  # applies to no-expert-data platforms too
        r = client.get(f"/monthly-setup/Jan'50/{platform}/category-topline")
        assert r.status_code == 200
        assert r.json()["status"] == "analyst"


def test_board_backfills_category_topline_for_never_touched_platforms(client):
    """Regression guard: the board endpoint must include category_topline for every platform
    even if no CT endpoint has ever been called for it yet (e.g. right after locking) - the
    frontend renders a badge straight from this field and silently showed "undefined" otherwise."""
    _lock(client, "Jan'51", {"Amazon": True, "Blinkit": False})
    board = client.get("/monthly-setup/Jan'51/board").json()
    for platform in ("Amazon", "Blinkit"):
        assert "category_topline" in board["platforms"][platform]
        assert board["platforms"][platform]["category_topline"]["status"] == "analyst"


def test_total_gmv_input_only_settable_at_analyst_stage(client):
    _lock(client, "Feb'50", {"Blinkit": False})
    r = client.post("/monthly-setup/Feb'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 123.4})
    assert r.status_code == 200
    assert r.json()["total_gmv_input"] == 123.4

    client.post("/monthly-setup/Feb'50/Blinkit/category-topline/send-to-review")
    r = client.post("/monthly-setup/Feb'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 999})
    assert r.status_code == 400


def test_category_topline_cannot_send_to_review_without_total_gmv_for_no_expert_platform(client):
    _lock(client, "Feb'51", {"Blinkit": False, "Amazon": True})
    r = client.post("/monthly-setup/Feb'51/Blinkit/category-topline/send-to-review")
    assert r.status_code == 400

    # An expert-data platform has no Total GMV concept at all - send-to-review must not require it.
    r2 = client.post("/monthly-setup/Feb'51/Amazon/category-topline/send-to-review")
    assert r2.status_code == 200


def test_category_topline_full_two_step_verification_to_finalized(client):
    _lock(client, "Mar'50", {"Blinkit": False})
    client.post("/monthly-setup/Mar'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 500})
    client.post("/monthly-setup/Mar'50/Blinkit/category-topline/send-to-review")

    # SL approve requires the SL password, not admin or manager.
    assert client.post(
        "/monthly-setup/Mar'50/Blinkit/category-topline/sl-approve", json={"password": ADMIN_PASSWORD}
    ).status_code == 403
    r = client.post("/monthly-setup/Mar'50/Blinkit/category-topline/sl-approve", json={"password": SL_PASSWORD})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_manager_review"

    # Manager finalize requires the Manager password, not SL's.
    assert client.post(
        "/monthly-setup/Mar'50/Blinkit/category-topline/manager-finalize", json={"password": SL_PASSWORD}
    ).status_code == 403
    r = client.post(
        "/monthly-setup/Mar'50/Blinkit/category-topline/manager-finalize", json={"password": MANAGER_PASSWORD}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "finalized"
    assert r.json()["finalized_at"] is not None


def test_category_topline_cannot_skip_sl_or_manager_stage(client):
    _lock(client, "Apr'50", {"Blinkit": False})
    client.post("/monthly-setup/Apr'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 500})
    client.post("/monthly-setup/Apr'50/Blinkit/category-topline/send-to-review")
    # Can't manager-finalize straight from pending_sl_review.
    r = client.post(
        "/monthly-setup/Apr'50/Blinkit/category-topline/manager-finalize", json={"password": MANAGER_PASSWORD}
    )
    assert r.status_code == 400


def test_category_topline_send_back_paths(client):
    _lock(client, "May'50", {"Blinkit": False})
    client.post("/monthly-setup/May'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 500})
    client.post("/monthly-setup/May'50/Blinkit/category-topline/send-to-review")
    client.post("/monthly-setup/May'50/Blinkit/category-topline/sl-approve", json={"password": SL_PASSWORD})

    r = client.post("/monthly-setup/May'50/Blinkit/category-topline/send-back-to-sl")
    assert r.json()["status"] == "pending_sl_review"

    r = client.post("/monthly-setup/May'50/Blinkit/category-topline/send-back-to-analyst")
    assert r.json()["status"] == "analyst"


def test_category_topline_reopen_requires_manager_password_and_goes_to_manager_review(client):
    _lock(client, "Jun'50", {"Blinkit": False})
    client.post("/monthly-setup/Jun'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 500})
    client.post("/monthly-setup/Jun'50/Blinkit/category-topline/send-to-review")
    client.post("/monthly-setup/Jun'50/Blinkit/category-topline/sl-approve", json={"password": SL_PASSWORD})
    client.post("/monthly-setup/Jun'50/Blinkit/category-topline/manager-finalize", json={"password": MANAGER_PASSWORD})

    assert client.post(
        "/monthly-setup/Jun'50/Blinkit/category-topline/reopen", json={"password": SL_PASSWORD}
    ).status_code == 403
    r = client.post("/monthly-setup/Jun'50/Blinkit/category-topline/reopen", json={"password": MANAGER_PASSWORD})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_manager_review"
    assert r.json()["finalized_at"] is None


# ---------------------------------------------------------------- Category Topline forecast engine

def test_resolve_cell_floors_negative_plug_to_zero():
    """A plug cell (parent-minus-siblings) landing a hair below zero from float noise on a
    near-zero 'Others' bucket - observed for real in Big Basket's Category Topline - must floor
    to 0, never surface as a negative GMV."""
    raw_cache = {"G2": 10.0, "G3": 7.0, "G4": "=G2-G3-G5", "G5": 5.0}  # 10 - 7 - 5 = -2
    resolved_cache = {}
    assert appmod._resolve_cell("G4", raw_cache, resolved_cache) == 0.0


def test_compute_category_topline_forecast_floors_oversummed_plug_to_zero():
    """If the OTHER (independently-forecasted) siblings in a group already sum to >= 100% on
    their own, the plug (1 - sum) has nothing left to absorb - it must floor to 0 rather than go
    negative, even though that means the group no longer sums to exactly 100%."""
    rows = [
        {"kind": "total", "row_num": 2, "l0": None, "l1": None, "l2": None, "label": "Total BPC", "share": [1.0]},
        {"kind": "l0", "row_num": 3, "l0": "A", "l1": None, "l2": None, "label": "A", "share": [0.7, 0.75, 0.8]},
        {"kind": "l0", "row_num": 4, "l0": "B", "l1": None, "l2": None, "label": "B", "share": [0.6, 0.65, 0.7]},
        {"kind": "l0_flat", "row_num": 5, "l0": "C", "l1": None, "l2": None, "label": "C", "share": [0.0, 0.0, 0.0]},
    ]
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=100.0)
    key_c = appmod._row_key({"kind": "l0_flat", "l0": "C", "l1": None, "l2": None, "label": "C"})
    assert forecast[key_c]["share"] == 0.0
    assert forecast[key_c]["gmv"] == 0.0


def test_compute_category_topline_forecast_scales_every_sibling_proportionally():
    """Every category in a group - the plug included - gets its own raw ETS+Trend candidate, and
    the whole group must reconcile to sum to exactly 100% - but NOT by an unlimited flat scale
    factor applied regardless of size: confirmed with the user that a category with no expert
    data of its own must still stay "in line with other categories and the market" rather than
    silently absorb however much of a gap reconciliation needs, so each member here is bounded to
    within NO_EXPERT_RECONCILE_BAND (10pp) of its own baseline share. A/B/C's raw candidates
    (0.8/0.7/0.1) sum to 1.6 - a 60-point gap far bigger than the 10pp of room any one of them
    has - so this group CANNOT fully reconcile within its members' bounds: each gets clamped to
    its own floor (A: 0.8-0.10=0.70, B: 0.7-0.10=0.60, C: 0.1-0.10=0.0) and the group is flagged
    as an unresolved conflict (my own real-data check surfaced exactly this failure mode: letting
    the one member with no expert data absorb an unbounded gap pushed it wildly out of line with
    its peers - this synthetic case is deliberately a gap too large for ANY bounded group to
    absorb, to confirm it gets flagged rather than silently forced through)."""
    rows = [
        {"kind": "total", "row_num": 2, "l0": None, "l1": None, "l2": None, "label": "Total BPC", "share": [1.0]},
        {"kind": "l0", "row_num": 3, "l0": "A", "l1": None, "l2": None, "label": "A", "share": [0.7, 0.75, 0.8]},
        {"kind": "l0", "row_num": 4, "l0": "B", "l1": None, "l2": None, "label": "B", "share": [0.6, 0.65, 0.7]},
        {"kind": "l0_flat", "row_num": 5, "l0": "C", "l1": None, "l2": None, "label": "C", "share": [0.1, 0.1, 0.1]},
    ]
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=100.0)
    key_a = appmod._row_key({"kind": "l0", "l0": "A", "l1": None, "l2": None, "label": "A"})
    key_b = appmod._row_key({"kind": "l0", "l0": "B", "l1": None, "l2": None, "label": "B"})
    key_c = appmod._row_key({"kind": "l0_flat", "l0": "C", "l1": None, "l2": None, "label": "C"})

    assert forecast[key_a]["share"] == pytest.approx(0.70)
    assert forecast[key_a]["expert_flag"] is True  # the group's reconciliation conflict is flagged
    assert forecast[key_b]["share"] == pytest.approx(0.60)
    assert forecast[key_b]["expert_flag"] is True
    # C is this group's plug (locked formula cell) - its own diagnostic fields are always hidden
    # in the output regardless (see _compute_category_topline_forecast's docstring), but it's
    # still clamped to its own floor (0.0) internally, same as its siblings.
    assert forecast[key_c]["share"] == pytest.approx(0.0)
    # The group could NOT be fully reconciled within everyone's bounds - shares sum to 1.30, not 1.0.
    assert sum(forecast[k]["share"] for k in (key_a, key_b, key_c)) == pytest.approx(1.30)

    # forecast_share/trend_share stay as each row's own RAW (pre-reconciliation) candidate - a
    # diagnostic of what that category's own history suggested, not the post-reconciliation share.
    assert forecast[key_a]["forecast_share"] == pytest.approx(0.8)


def test_write_forecast_column_plug_formula_evaluates_to_the_scaled_share(tmp_path):
    """The plug's written '=Parent-Sibling-...' formula must evaluate (via the same _resolve_cell
    every read already uses) to the exact proportionally-scaled GMV - not the unscaled raw
    candidate - confirming Manual Change's "edit a sibling, the locked cell adjusts" behaviour
    stays mathematically correct under the new reconciliation method."""
    rows = [
        {"kind": "total", "row_num": 2, "l0": None, "l1": None, "l2": None, "label": "Total BPC", "share": [1.0]},
        {"kind": "l0", "row_num": 3, "l0": "A", "l1": None, "l2": None, "label": "A", "share": [0.7, 0.75, 0.8]},
        {"kind": "l0", "row_num": 4, "l0": "B", "l1": None, "l2": None, "label": "B", "share": [0.6, 0.65, 0.7]},
        {"kind": "l0_flat", "row_num": 5, "l0": "C", "l1": None, "l2": None, "label": "C", "share": [0.1, 0.1, 0.1]},
    ]
    total_gmv = 100.0
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Kind", "Level", "Label", "L0", "L1", "L2"])
    ws.append(["total", 0, "Total BPC", None, None, None])
    ws.append(["l0", 1, "A", "A", None, None])
    ws.append(["l0", 1, "B", "B", None, None])
    ws.append(["l0_flat", 1, "C", "C", None, None])
    path = tmp_path / "plug_formula.xlsx"
    wb.save(path)

    appmod._write_forecast_column(path, "Sheet", "Test-50", rows, forecast)

    wb2 = openpyxl.load_workbook(path, read_only=True, data_only=False)
    ws2 = wb2["Sheet"]
    raw_cache, resolved_cache = {}, {}
    for row_idx in (2, 3, 4, 5):
        raw_cache[f"G{row_idx}"] = ws2.cell(row=row_idx, column=7).value
    c_value = appmod._resolve_cell("G5", raw_cache, resolved_cache)
    key_c = appmod._row_key({"kind": "l0_flat", "l0": "C", "l1": None, "l2": None, "label": "C"})
    assert c_value == pytest.approx(forecast[key_c]["gmv"])
    wb2.close()


def test_set_total_gmv_makes_new_month_appear_in_meta_for_only_that_platform(client):
    """Regression guard: /category-topline/meta's month list used to come from a single
    reference sheet (the workbook's first one), so a forecasted month written to any OTHER
    platform's sheet never showed up in the month picker at all - Save & Compute "worked" but the
    dropdown silently didn't update."""
    before = client.get("/category-topline/meta").json()["months"]
    assert "Oct-50" not in before

    _lock(client, "Oct'50", {"Blinkit": False})
    client.post("/monthly-setup/Oct'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 200})

    after = client.get("/category-topline/meta").json()["months"]
    assert "Oct-50" in after

    # Amazon's own sheet was never touched - it simply doesn't have this month, same as before.
    amazon_data = client.get("/category-topline", params={"variant": "all", "platform": "Amazon", "months": "Oct-50"}).json()
    assert amazon_data["months"] == []


def test_set_total_gmv_writes_forecast_into_workbook_and_merges_into_read_view(client):
    _lock(client, "Jul'50", {"Blinkit": False})
    r = client.post("/monthly-setup/Jul'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["total_gmv_input"] == 200
    assert body["gmv_input_mode"] == "gmv"
    assert body["forecast"]["month_label"] == "Jul-50"

    data = client.get("/category-topline", params={"variant": "all", "platform": "Blinkit"}).json()
    assert "Jul-50" in data["months"]
    pos = data["months"].index("Jul-50")
    rows_by_label = {row["label"]: row for row in data["rows"]}

    total_row = rows_by_label["Total BPC"]
    assert total_row["gmv"][pos] == pytest.approx(200)

    # Cat A keeps its last actual share (0.6, from Nov-30) since under-4-point ETS falls back to
    # the latest value and there's no year-ago month for the trend blend either.
    cat_a = rows_by_label["Cat A"]
    assert cat_a["share"][pos] == pytest.approx(0.6, abs=1e-6)
    assert cat_a["gmv"][pos] == pytest.approx(120, abs=1e-4)  # 200 * 0.6
    assert cat_a["forecast"][pos] == pytest.approx(0.6, abs=1e-6)
    assert cat_a["trend"][pos] == pytest.approx(0.6, abs=1e-6)
    # Forecast/trend are None everywhere else - diagnostic-only, never real history.
    assert all(v is None for i, v in enumerate(cat_a["forecast"]) if i != pos)

    # Cat B is the group's last sibling - the plug, not independently forecast.
    cat_b = rows_by_label["Cat B"]
    assert cat_b["share"][pos] == pytest.approx(0.4, abs=1e-6)
    assert cat_b["gmv"][pos] == pytest.approx(80, abs=1e-4)
    assert cat_b["forecast"][pos] is None
    assert cat_b["trend"][pos] is None


def test_set_total_gmv_plug_cell_is_a_real_formula_not_a_plain_number(client):
    """The last-sibling plug must be written as a live '=Parent-Sibling' formula (not a baked-in
    number) so Manual Change's _is_locked detection - and a real Excel recalculation - both still
    work on this new column exactly as on every historical one."""
    _lock(client, "Aug'50", {"Blinkit": False})
    client.post("/monthly-setup/Aug'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 200})

    wb = openpyxl.load_workbook(appmod.TOPLINE_FILES["all"])
    ws = wb["Blinkit"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    col_idx = header.index("GMV Aug-50") + 1
    cat_a_cell = ws.cell(row=3, column=col_idx).value  # Cat A - forecast, a plain number
    cat_b_cell = ws.cell(row=4, column=col_idx).value  # Cat B - the plug, a formula
    wb.close()
    assert isinstance(cat_a_cell, (int, float))
    assert isinstance(cat_b_cell, str) and cat_b_cell.startswith("=")


def test_set_total_gmv_recompute_uses_real_history_not_its_own_prior_guess(client):
    """Re-running set-total-gmv before sending to review (e.g. the analyst changes their mind)
    must forecast off real history again, not off the first guess it just wrote - otherwise a
    platform's own forecast would drift further from reality on every recompute."""
    _lock(client, "Sep'50", {"Blinkit": False})
    client.post("/monthly-setup/Sep'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 1000})
    r = client.post("/monthly-setup/Sep'50/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 300})
    assert r.status_code == 200

    data = client.get("/category-topline", params={"variant": "all", "platform": "Blinkit"}).json()
    pos = data["months"].index("Sep-50")
    rows_by_label = {row["label"]: row for row in data["rows"]}
    # Still the real Nov-30 share (0.6/0.4), unaffected by the discarded first 1000 GMV guess.
    assert rows_by_label["Cat A"]["share"][pos] == pytest.approx(0.6, abs=1e-6)
    assert rows_by_label["Total BPC"]["gmv"][pos] == pytest.approx(300)


def test_set_total_gmv_mom_and_yoy_modes_resolve_against_actual_history(client):
    _lock(client, "Dec'30", {"Blinkit": False})
    # MoM% off the last real month (Nov-30 Total GMV = 110): +10% -> 121.
    r = client.post("/monthly-setup/Dec'30/Blinkit/category-topline/set-total-gmv", json={"mom_pct": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["gmv_input_mode"] == "mom"
    assert body["total_gmv_input"] == pytest.approx(121)

    _lock(client, "Nov'31", {"Blinkit": False})
    # YoY% needs 12 months of history - this fixture only has 2, so it must fail clearly rather
    # than silently resolving against the wrong month.
    r = client.post("/monthly-setup/Nov'31/Blinkit/category-topline/set-total-gmv", json={"yoy_pct": 5})
    assert r.status_code == 400

    r = client.post("/monthly-setup/Dec'30/Blinkit/category-topline/set-total-gmv", json={"total_gmv": 1, "mom_pct": 2})
    assert r.status_code == 400  # exactly one of the three must be provided


# ---------------------------------------------------------------- Sequential month gating
# (you can't build next month's no-expert-data forecast before this month is actually done, so
# /monthly-setup/months must never offer more than the single next sequential month, and only
# once every already-created month is Complete.)

def test_months_offers_next_month_when_none_created_yet(client):
    r = client.get("/monthly-setup/months").json()
    assert r["months"] == []
    assert r["next_month"] == "Sep'26"  # one past the fixture's fake Master Data latest (Aug'26)


def test_months_hides_next_month_while_current_is_incomplete(client):
    _lock(client, "Sep'26", {"Amazon": False})
    r = client.get("/monthly-setup/months").json()
    assert r["months"] == ["Sep'26"]
    assert r["next_month"] is None


def test_months_offers_next_month_once_current_is_complete(client):
    _lock(client, "Sep'26", {"Amazon": False})
    r = client.post("/monthly-setup/Sep'26/complete", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 200

    r2 = client.get("/monthly-setup/months").json()
    assert r2["months"] == ["Sep'26"]
    assert r2["next_month"] == "Oct'26"


# ---------------------------------------------------------------- State file integrity

def test_creating_same_month_twice_does_not_duplicate_month_order(client):
    client.get("/monthly-setup/Nov'99")
    client.get("/monthly-setup/Nov'99")
    months = client.get("/monthly-setup/months").json()["months"]
    assert months.count("Nov'99") == 1


def test_corrupted_state_file_is_rejected_not_silently_used(client):
    appmod.MONTHLY_STATE_FILE.write_text(
        json.dumps({"months": {"Dec'99": {"platforms": {"Amazon": {"has_expert_data": True}}}}, "month_order": ["Dec'99"]}),
        encoding="utf-8",
    )
    r = client.get("/monthly-setup/Dec'99")
    assert r.status_code == 500


def test_master_data_latest_month_drives_create_window(client):
    r = client.get("/monthly-setup/master-data-latest-month")
    assert r.json()["month"] == "Aug'26"
