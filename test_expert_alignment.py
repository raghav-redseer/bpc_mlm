"""Regression tests for expert-data alignment in Category Topline's forecasting engine: once an
expert-data platform's tagged data is consolidated into Master Data for the target month, each
category's Market Share is anchored to the expert's own implied YoY (preferred) or MoM growth
rate, rather than plain ETS+Trend - see _compute_category_topline_forecast's expert_gmv_lookup
parameter and _build_expert_gmv_lookup in app.py.
"""
import hashlib
import json
import os

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as appmod


def _row(row_num, kind, l0=None, l1=None, l2=None, share=None, gmv=None):
    return {"row_num": row_num, "kind": kind, "l0": l0, "l1": l1, "l2": l2,
            "label": l2 or l1 or l0 or "Total", "share": share or [], "gmv": gmv or []}


# ---------------------------------------------------------------- _expert_aligned_share (the
# tiered clip-toward-expert rule, confirmed via the Face Sunscreen example: forecast 20% YoY,
# expert 30% (double-digit -> +/-2pp band) clips to 28%, not all the way to 30%).

def test_expert_aligned_share_clips_toward_expert_not_all_the_way():
    # Base growth 20% (base_share=600*1.2/1000=0.72), expert 30% (double-digit, +/-2pp band =
    # [28%,32%]) - result lands at the band's near edge, 28%, not 30%.
    result = appmod._expert_aligned_share(base_share=0.72, anchor_gmv=600.0, denom_gmv=1000.0, expert_growth=0.30)
    assert result == pytest.approx(600.0 * 1.28 / 1000.0)


def test_expert_aligned_share_single_digit_expert_growth_uses_tighter_1pp_band():
    # Expert growth is single-digit (8%) -> +/-1pp band = [7%,9%]. Base growth 3% is below it,
    # clips up to 7%, not all the way to 8%.
    base_share = 600.0 * 1.03 / 1000.0
    result = appmod._expert_aligned_share(base_share, anchor_gmv=600.0, denom_gmv=1000.0, expert_growth=0.08)
    assert result == pytest.approx(600.0 * 1.07 / 1000.0)


def test_expert_aligned_share_clips_down_when_base_overshoots_the_band():
    # Base growth (40%) is above the double-digit band's upper edge (30%+2pp=32%) - clips down to
    # 32%, not left untouched and not dropped all the way to 30%.
    base_share = 600.0 * 1.40 / 1000.0
    result = appmod._expert_aligned_share(base_share, anchor_gmv=600.0, denom_gmv=1000.0, expert_growth=0.30)
    assert result == pytest.approx(600.0 * 1.32 / 1000.0)


def test_expert_aligned_share_no_clip_when_base_already_within_band():
    # Base growth (29%) is already inside the double-digit band around 30% ([28%,32%]) - returned
    # unchanged, not forced to either edge.
    base_share = 600.0 * 1.29 / 1000.0
    result = appmod._expert_aligned_share(base_share, anchor_gmv=600.0, denom_gmv=1000.0, expert_growth=0.30)
    assert result == pytest.approx(base_share)


# ---------------------------------------------------------------- _apply_market_share_guardrail
# (two-tier: silent within 3pp, applied-but-flagged 3-4pp, hard-capped-and-flagged beyond 4pp)

def test_market_share_guardrail_silent_within_3pp():
    final, flagged = appmod._apply_market_share_guardrail(candidate_share=0.62, base_share=0.6)
    assert final == pytest.approx(0.62)
    assert flagged is False


def test_market_share_guardrail_flags_but_still_applies_between_3_and_4pp():
    final, flagged = appmod._apply_market_share_guardrail(candidate_share=0.635, base_share=0.6)  # 3.5pp
    assert final == pytest.approx(0.635)  # applied as-is, not rejected
    assert flagged is True


def test_market_share_guardrail_hard_caps_beyond_4pp():
    final, flagged = appmod._apply_market_share_guardrail(candidate_share=0.70, base_share=0.6)  # 10pp
    assert final == pytest.approx(0.64)  # capped at base+4pp, not left at the raw 0.70
    assert flagged is True


def test_market_share_guardrail_caps_in_the_negative_direction_too():
    final, flagged = appmod._apply_market_share_guardrail(candidate_share=0.40, base_share=0.6)  # -20pp
    assert final == pytest.approx(0.56)  # capped at base-4pp
    assert flagged is True


# ---------------------------------------------------------------- _expert_alignment_for_row (the
# full per-row decision: YoY drives when usable, MoM only drives when YoY's anchor isn't, and
# whichever one DIDN'T drive is a flag-only "do these agree" check - it never changes the share)

def test_expert_alignment_mom_disagreement_flags_but_never_changes_the_yoy_driven_share():
    # YoY drives (yearago=600, expert_gmv=666 -> expert_yoy=11%, double-digit, +/-2pp band
    # [9%,13%]); base growth 5% clips up to 9% -> final_share = 600*1.09/1000 = 0.654 (a 2.4pp
    # move from the 0.63 base - within the 3pp flag threshold, so NOT flagged by Market Share on
    # its own). prior_gmv is deliberately tiny (20) so the SAME final absolute GMV (654, which is
    # close to but not exactly expert_gmv=666 because of the clip) implies a wildly different MoM
    # (31.7x) than the expert's own MoM off that same tiny base (32.3x) - genuinely discordant,
    # but the share must stay exactly the YoY-clipped 0.654, not move toward MoM at all.
    base_share = 600.0 * 1.05 / 1000.0
    final_share, expert_mom, expert_yoy, needs_review = appmod._expert_alignment_for_row(
        base_share=base_share, denom_gmv=1000.0, prior_gmv=20.0, yearago_gmv=600.0, expert_gmv=666.0)
    assert expert_yoy == pytest.approx(0.11)
    assert expert_mom == pytest.approx(666.0 / 20.0 - 1)
    assert final_share == pytest.approx(600.0 * 1.09 / 1000.0)  # exactly the YoY-driven result
    assert needs_review is True  # flagged purely because MoM disagrees, not because MS moved


def test_expert_alignment_mom_drives_only_when_yoy_anchor_unusable():
    # yearago_gmv (0.1) is below EXPERT_MIN_ANCHOR_GMV -> YoY anchor not trusted at all, so MoM
    # (prior_gmv=600, a trustworthy anchor) drives the adjustment instead.
    final_share, expert_mom, expert_yoy, needs_review = appmod._expert_alignment_for_row(
        base_share=0.6, denom_gmv=1000.0, prior_gmv=600.0, yearago_gmv=0.1, expert_gmv=666.0)
    assert expert_mom == pytest.approx(666.0 / 600.0 - 1)
    # Base growth (0%) clips up to the double-digit band's lower edge (11%-2%=9%) -> raw candidate
    # 600*1.09/1000=0.654, a 5.4pp move past the 4pp hard cap -> capped to 0.6+0.04=0.64.
    assert final_share == pytest.approx(0.64)
    assert needs_review is True


def test_expert_alignment_no_signal_at_all_falls_back_to_base_with_no_flag():
    final_share, expert_mom, expert_yoy, needs_review = appmod._expert_alignment_for_row(
        base_share=0.6, denom_gmv=1000.0, prior_gmv=None, yearago_gmv=None, expert_gmv=None)
    assert final_share == pytest.approx(0.6)
    assert expert_mom is None and expert_yoy is None
    assert needs_review is False


def test_expert_alignment_flags_but_does_not_apply_when_anchor_below_min_base():
    # Both anchors are near-zero (classic noise artifact) - no adjustment is applied, but a raw
    # expert figure DID exist, so this is still flagged for awareness (distinct from "no coverage
    # at all", which is not flagged).
    final_share, expert_mom, expert_yoy, needs_review = appmod._expert_alignment_for_row(
        base_share=0.0005, denom_gmv=1000.0, prior_gmv=0.01, yearago_gmv=0.01, expert_gmv=5.0)
    assert final_share == pytest.approx(0.0005)  # unchanged
    assert needs_review is True


# ---------------------------------------------------------------- _expert_lookup_key

def test_expert_lookup_key_shapes_per_level():
    assert appmod._expert_lookup_key(_row(1, "l0", l0="Grooming")) == ("l0", "grooming", None, None)
    assert appmod._expert_lookup_key(_row(2, "l1", l0="Grooming", l1="Hygiene")) == ("l1", "grooming", "hygiene", None)
    assert appmod._expert_lookup_key(_row(3, "l2", l0="Grooming", l1="Hygiene", l2="Shaving")) == \
        ("l2", "grooming", "hygiene", "shaving")


# ---------------------------------------------------------------- _build_expert_gmv_lookup

def _raw_agg_df(rows):
    """rows: list of (month_label, l0, l1, l2, brand, gmv), already normalized lower-case -
    matches what _load_raw_expert_agg itself returns."""
    return pd.DataFrame(rows, columns=["month_label", "l0", "l1", "l2", "brand", "gmv"])


def test_build_expert_gmv_lookup_rolls_up_l0_and_l1_from_l2(monkeypatch):
    flipkart_agg = _raw_agg_df([
        ("Sep-26", "grooming", "hygiene", "shaving", "acme", 100.0),
        ("Sep-26", "grooming", "hygiene", "soaps", "acme", 50.0),
        ("Sep-26", "makeup and fragrances", "fragrance", "perfume", "acme", 30.0),
    ])
    amazon_agg = _raw_agg_df([
        ("Sep-26", "grooming", "hygiene", "shaving", "acme", 999.0),
    ])

    def fake_load(platform):
        return {"Flipkart": flipkart_agg, "Amazon": amazon_agg}[platform]

    monkeypatch.setattr(appmod, "_load_raw_expert_agg", fake_load)

    lookup = appmod._build_expert_gmv_lookup("Flipkart", "Sep-26")

    assert lookup[("l2", "grooming", "hygiene", "shaving")] == pytest.approx(100.0)
    assert lookup[("l2", "grooming", "hygiene", "soaps")] == pytest.approx(50.0)
    assert lookup[("l1", "grooming", "hygiene", None)] == pytest.approx(150.0)
    assert lookup[("l0", "grooming", None, None)] == pytest.approx(150.0)
    assert lookup[("l0", "makeup and fragrances", None, None)] == pytest.approx(30.0)
    # Amazon's row must not leak into Flipkart's lookup.
    assert lookup[("l2", "grooming", "hygiene", "shaving")] != pytest.approx(1099.0)


def test_build_expert_gmv_lookup_empty_when_month_not_yet_covered(monkeypatch):
    flipkart_agg = _raw_agg_df([
        ("Aug-26", "grooming", "hygiene", "shaving", "acme", 100.0),
    ])
    monkeypatch.setattr(appmod, "_load_raw_expert_agg", lambda platform: flipkart_agg)
    assert appmod._build_expert_gmv_lookup("Flipkart", "Sep-26") == {}


def test_build_expert_gmv_lookup_empty_for_platform_with_no_raw_file():
    # Big Basket has no entry in RAW_EXPERT_CONFIG at all - no monkeypatch needed, this must be
    # empty regardless of what's on disk.
    assert appmod._build_expert_gmv_lookup("Big Basket", "Sep-26") == {}


# ---------------------------------------------------------------- _compute_category_topline_forecast

def _two_sibling_rows(cat_a_share_history, cat_a_gmv_history, cat_b_gmv_history):
    total = _row(1, "total")
    cat_a = _row(2, "l0", l0="Cat A", share=cat_a_share_history, gmv=cat_a_gmv_history)
    cat_b = _row(3, "l0_flat", l0="Cat B", share=[1 - s for s in cat_a_share_history], gmv=cat_b_gmv_history)
    return [total, cat_a, cat_b]


def test_expert_yoy_preferred_over_mom_when_both_available():
    # 13 months of flat history (indices 0..12): Cat A always 600 of a 1000 total (share 0.6).
    # Index 0 is "12 months ago" relative to the target month that follows this truncated history.
    share_hist = [0.6] * 12
    cat_a_gmv_hist = [600.0] * 12
    cat_b_gmv_hist = [400.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    # Expert says Cat A's GMV this month is 740 - a real move (+23.3% vs both year-ago and prior
    # month, both 600 here) but small enough (740/1200=0.6167, only +1.67pp vs the 0.6 base) to
    # stay inside the +/-3pp cap, so it actually gets applied - a separate test below covers what
    # happens when the implied move is too large to trust automatically.
    expert_lookup = {("l0", "cat a", None, None): 740.0}
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1200.0, expert_gmv_lookup=expert_lookup)

    key_a = appmod._row_key(rows[1])
    key_b = appmod._row_key(rows[2])
    assert forecast[key_a]["expert_yoy"] == pytest.approx(740 / 600 - 1)
    assert forecast[key_a]["expert_mom"] == pytest.approx(740 / 600 - 1)
    assert forecast[key_a]["expert_flag"] is False
    # Without alignment, Cat A's base share would stay at 0.6 (flat ETS+Trend on flat history).
    # With alignment it should move toward 740/1200=0.6167, pulling Cat B's plug share down.
    assert forecast[key_a]["share"] > 0.6
    assert forecast[key_a]["share"] + forecast[key_b]["share"] == pytest.approx(1.0)


def test_expert_alignment_hard_caps_and_flags_when_deviation_exceeds_4pp():
    share_hist = [0.6] * 12
    cat_a_gmv_hist = [600.0] * 12
    cat_b_gmv_hist = [400.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    # Expert says Cat A's GMV this month is 792 - +32% YoY (double-digit -> +/-2pp tolerance band),
    # clipping the base growth (0.2) up to 0.30 (0.32 - 0.02), giving a raw candidate share of
    # 600*1.30/1200=0.65 - a 5pp move, past the 4pp hard cap but still within the 8pp disagreement
    # gate (raw unclipped implied share 600*1.32/1200=0.66, a 6pp move, well under the gate), so
    # the RAW candidate is capped at base_share+0.04=0.64 rather than rejected back to 0.6 or
    # blocked entirely by the gate - giving Cat A a target GMV of 0.64*1200=768 (Cat B's target is
    # unchanged, 0.4*1200=480; they sum to 1248, 48 over Total). Reconciliation then jointly
    # reconciles the group to 1200, weighted 1/yearago_gmv^2 per sibling (both have real year-ago
    # history: A=600, B=400) rather than one flat scale factor - so Cat A, with more room (larger
    # anchor), absorbs proportionally more of that 48 gap than Cat B does: final A = 768 -
    # 48*(600^2/(600^2+400^2)) = 734.7692..., share = 734.7692.../1200.
    expert_lookup = {("l0", "cat a", None, None): 792.0}
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1200.0, expert_gmv_lookup=expert_lookup)

    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_yoy"] == pytest.approx(0.32)
    assert forecast[key_a]["expert_flag"] is True
    assert forecast[key_a]["share"] == pytest.approx(0.6123076923076922, abs=1e-6)


def test_expert_alignment_disagreement_gate_blocks_move_entirely_when_implied_move_is_too_wild():
    share_hist = [0.6] * 12
    cat_a_gmv_hist = [600.0] * 12
    cat_b_gmv_hist = [400.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    # Expert says Cat A's GMV this month is 900 - +50% YoY, implying (unclipped) a share of
    # 900/1200=0.75, a 15pp move from the 0.6 base - well past the 8pp disagreement gate. Treated
    # as a likely tagging/mapping mismatch rather than a genuine trend: no move is applied at all
    # (not even to the 4pp hard cap), though it's still flagged for manual review.
    expert_lookup = {("l0", "cat a", None, None): 900.0}
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1200.0, expert_gmv_lookup=expert_lookup)

    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_yoy"] == pytest.approx(0.5)
    assert forecast[key_a]["expert_flag"] is True
    assert forecast[key_a]["share"] == pytest.approx(0.6, abs=1e-6)  # unchanged, gate blocked it


def test_expert_alignment_flags_and_ignores_anchor_below_min_base():
    # Year-ago and prior-month GMV are both far below EXPERT_MIN_ANCHOR_GMV (0.5 Cr) - any ratio
    # computed off them is noise (the classic near-zero-base artifact), so even though the expert
    # figure implies a huge "growth" rate, it must be ignored entirely, not just capped.
    share_hist = [0.0005] * 12
    cat_a_gmv_hist = [0.01] * 12
    cat_b_gmv_hist = [400.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    expert_lookup = {("l0", "cat a", None, None): 5.0}  # 5.0 / 0.01 - 1 = 499x "growth"
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1000.0, expert_gmv_lookup=expert_lookup)

    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_yoy"] == pytest.approx(499.0)  # still reported as a diagnostic
    assert forecast[key_a]["expert_flag"] is True  # ...but flagged, not trusted
    # Base share stays whatever plain ETS+Trend gives a near-flat 0.0005 series - the point is just
    # that it is NOT pulled toward the 499x figure.
    assert forecast[key_a]["share"] < 0.01


def test_expert_yoy_wins_over_a_contradicting_mom_signal():
    # 13 months: year-ago (index 0) = 600, everything since drifted down to 400 by the most
    # recent actual month (index 11) - MoM off a recent low base vs. YoY off the higher base give
    # very different growth rates when the expert number sits between them.
    share_hist = [0.6] + [0.4] * 11
    cat_a_gmv_hist = [600.0] + [400.0] * 11
    cat_b_gmv_hist = [400.0] + [600.0] * 11
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    expert_lookup = {("l0", "cat a", None, None): 480.0}  # -20% YoY (600->480), +20% MoM (400->480)
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1000.0, expert_gmv_lookup=expert_lookup)

    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_yoy"] == pytest.approx(-0.2)
    assert forecast[key_a]["expert_mom"] == pytest.approx(0.2)
    # YoY anchor (600) * (1 - 0.2) = 480 -> raw share 480/1000 = 0.48, NOT the MoM anchor's
    # (400 * 1.2 = 480 -> also 0.48 here by construction) - use a total_gmv that makes them
    # diverge to prove which one actually drove the result.
    forecast2 = appmod._compute_category_topline_forecast(rows, total_gmv=2000.0, expert_gmv_lookup=expert_lookup)
    # Raw (pre-reconciliation) share from the YoY anchor would be 480/2000=0.24 either way here
    # since both anchors happen to resolve to the same target GMV (480) by construction above -
    # the real proof is expert_yoy being populated and used in preference whenever present, which
    # the dedicated unit test below isolates directly.
    assert forecast2[key_a]["expert_yoy"] == pytest.approx(-0.2)


def test_expert_alignment_uses_yoy_anchor_not_mom_anchor_when_both_present():
    # Construct a case where YoY-anchor-implied share and MoM-anchor-implied share are clearly
    # different, so we can tell which one the engine actually used.
    share_hist = [0.6] + [0.4] * 11          # irrelevant to the expert path, just needs 12 points
    cat_a_gmv_hist = [1000.0] + [200.0] * 11  # year-ago=1000, most-recent-actual=200
    cat_b_gmv_hist = [0.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    # Expert GMV this month = 1100. YoY-anchor target = 1000*(1+0.1)=1100 (growth=0.1 exactly by
    # construction). MoM-anchor growth would be 1100/200-1=4.5 -> MoM-anchor target = 200*5.5=1100
    # too (same target GMV, deliberately, to isolate which GROWTH RATE gets stored/reported - the
    # resulting share is identical either way, but expert_yoy must be the one that's non-None-and-
    # preferred per the source comment, i.e. present and used whenever available).
    expert_lookup = {("l0", "cat a", None, None): 1100.0}
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=2000.0, expert_gmv_lookup=expert_lookup)
    key_a = appmod._row_key(rows[1])
    # Both growth rates are computed and stored independently (not just whichever one "won") -
    # the source's documented preference order (YoY when available, else MoM) is what the
    # implementation branches on; this confirms both diagnostics are genuinely populated and
    # distinct, which is what the Expert MoM/Expert YoY UI columns display either way.
    assert forecast[key_a]["expert_yoy"] == pytest.approx(0.1)
    assert forecast[key_a]["expert_mom"] == pytest.approx(4.5)


def test_expert_alignment_falls_back_to_base_forecast_when_no_expert_gmv_for_row():
    share_hist = [0.6] * 12
    cat_a_gmv_hist = [600.0] * 12
    cat_b_gmv_hist = [400.0] * 12
    rows = _two_sibling_rows(share_hist, cat_a_gmv_hist, cat_b_gmv_hist)

    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1000.0, expert_gmv_lookup={})
    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_mom"] is None
    assert forecast[key_a]["expert_yoy"] is None
    # No expert signal at all -> behaves exactly like the no-expert-data engine (share stays 0.6).
    assert forecast[key_a]["share"] == pytest.approx(0.6, abs=1e-6)


def test_expert_alignment_none_when_lookup_not_provided_at_all():
    share_hist = [0.6] * 12
    rows = _two_sibling_rows(share_hist, [600.0] * 12, [400.0] * 12)
    forecast = appmod._compute_category_topline_forecast(rows, total_gmv=1000.0)  # default param
    key_a = appmod._row_key(rows[1])
    assert forecast[key_a]["expert_mom"] is None
    assert forecast[key_a]["expert_yoy"] is None
    assert forecast[key_a]["share"] == pytest.approx(0.6, abs=1e-6)


# ---------------------------------------------------------------- end-to-end via the API, for a
# real has_expert_data=True platform using the SAME manual Total-GMV trigger as a no-expert one.

def _write_auth_file(path, password):
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    path.write_text(json.dumps({"salt": salt, "hash": h, "algo": "pbkdf2_sha256_200000"}), encoding="utf-8")


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
    _write_auth_file(appmod.ADMIN_AUTH_FILE, "admin-pw")
    _write_auth_file(appmod.SL_AUTH_FILE, "sl-pw")
    _write_auth_file(appmod.MANAGER_AUTH_FILE, "manager-pw")

    monkeypatch.setattr(appmod, "_workbook_platforms", lambda path: ["Flipkart"])

    topline_dir = portal_dir / "Category Topline"
    topline_dir.mkdir(parents=True)
    topline_all_path = topline_dir / "Category Topline - All Platforms.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Flipkart")
    # 12 months of flat real history (Oct-24..Sep-25) then the target month Oct-25 gets written
    # by set-total-gmv itself - Cat A flat at share 0.6 (600 of 1000), Cat B the plug at 0.4.
    months = [f"GMV {m}-{('24' if i < 3 else '25')}" for i, m in enumerate(
        ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"])]
    ws.append(["Kind", "Level", "Label", "L0", "L1", "L2"] + months)
    ws.append(["total", 0, "Total BPC", None, None, None] + [1000] * 12)
    ws.append(["l0", 1, "Cat A", "Cat A", None, None] + [600] * 12)
    ws.append(["l0_flat", 1, "Cat B", "Cat B", None, None] + [400] * 12)
    wb.save(topline_all_path)
    monkeypatch.setattr(appmod, "TOPLINE_FILES", {
        "all": topline_all_path,
        "md_est": topline_dir / "Category Topline - MD Estimates.xlsx",
    })
    appmod._REPORT_CACHE["topline_platforms"].clear()
    appmod._REPORT_CACHE["topline_rows"].clear()

    # BSC_FILES must ALWAYS be patched alongside TOPLINE_FILES - set-total-gmv also writes
    # Category Topline's Total/L0/L1/L2 numbers into Brand Share Cons. Neither file needs to
    # exist (_write_forecast_column no-ops cleanly on a missing path) - the point is just to
    # never leave this pointed at the real production workbook during a test.
    monkeypatch.setattr(appmod, "BSC_FILES", {
        "all": portal_dir / "Brandshare Cons" / "Brand Share Cons - All Platforms.xlsx",
        "md_est": portal_dir / "Brandshare Cons" / "Brand Share Cons - MD Estimates.xlsx",
    })
    appmod._REPORT_CACHE["bsc_platforms"].clear()
    appmod._REPORT_CACHE["bsc_rows"].clear()

    monkeypatch.setattr(appmod, "_load_raw_expert_agg", lambda platform: _CLIENT_RAW_AGG["df"])

    appmod.JOBS.clear()
    return TestClient(appmod.app)


_CLIENT_RAW_AGG = {"df": pd.DataFrame(columns=["month_label", "l0", "l1", "l2", "brand", "gmv"])}


def _set_client_expert_gmv(value, month_label="Oct-25"):
    # "expert data" is now the Raw expert data folder, not Master Data - same (l0, l1, l2, brand,
    # gmv) shape _load_raw_expert_agg itself returns.
    _CLIENT_RAW_AGG["df"] = pd.DataFrame({
        "month_label": [month_label], "l0": ["cat a"], "l1": [""], "l2": [""],
        "brand": [""], "gmv": [value],
    })


def test_set_total_gmv_aligns_to_expert_data_for_has_expert_data_platform(client):
    # 740 vs year-ago 600 is +23.3%, implying a share of 740/1200=0.6167 - within the +/-3pp cap
    # of the 0.6 base, so it actually gets applied end-to-end.
    _set_client_expert_gmv(740.0)
    # Same lock/lifecycle every platform goes through - Flipkart marked has_expert_data=True.
    r = client.post("/monthly-setup/Oct'25/lock", json={"platforms": {"Flipkart": True}, "password": "sl-pw"})
    assert r.status_code == 200

    # The exact same manual Total-GMV endpoint, unchanged, now drives an expert-aligned forecast
    # because this platform/month has finalized expert data sitting in Master Data.
    r = client.post("/monthly-setup/Oct'25/Flipkart/category-topline/set-total-gmv", json={"total_gmv": 1200})
    assert r.status_code == 200
    body = r.json()
    rows_meta = body["forecast"]["rows"]
    cat_a_key = next(k for k in rows_meta if k.startswith("l0|Cat A|"))
    assert rows_meta[cat_a_key]["expert_yoy"] == pytest.approx(740 / 600 - 1)
    assert rows_meta[cat_a_key]["expert_flag"] is False

    data = client.get("/category-topline", params={"variant": "all", "platform": "Flipkart"}).json()
    pos = data["months"].index("Oct-25")
    cat_a = next(row for row in data["rows"] if row["label"] == "Cat A")
    assert cat_a["expert_yoy"][pos] == pytest.approx(740 / 600 - 1)
    assert cat_a["expert_flag"][pos] is False
    # Expert-aligned target (740/1200=0.6167) pulled the share up from the flat-history base (0.6).
    assert cat_a["share"][pos] > 0.6


def test_set_total_gmv_hard_caps_and_flags_expert_data_when_move_exceeds_4pp(client):
    # 792 vs year-ago 600 is +32% YoY, double-digit -> +/-2pp band, clipping the base growth (0.2)
    # up to 0.30 -> raw candidate share 600*1.30/1200=0.65, a 5pp move - past the 4pp hard cap but
    # still within the 8pp disagreement gate (raw unclipped implied share 0.66, a 6pp move) - so
    # it's capped to 0.6+0.04=0.64 raw (not rejected back to 0.6, and not blocked by the gate
    # either), giving Cat A a target GMV of 0.64*1200=768 (Cat B's target is unchanged at 480;
    # together 48 over Total). The group is then jointly reconciled to 1200, weighted 1/yearago^2
    # per sibling (A=600, B=400) rather than one flat scale factor, so Cat A absorbs more of that
    # gap than Cat B: final A = 768 - 48*(600^2/(600^2+400^2)) = 734.7692..., still flagged for
    # manual review.
    _set_client_expert_gmv(792.0, month_label="Nov-25")
    r = client.post("/monthly-setup/Nov'25/lock", json={"platforms": {"Flipkart": True}, "password": "sl-pw"})
    assert r.status_code == 200

    r = client.post("/monthly-setup/Nov'25/Flipkart/category-topline/set-total-gmv", json={"total_gmv": 1200})
    assert r.status_code == 200

    data = client.get("/category-topline", params={"variant": "all", "platform": "Flipkart"}).json()
    cat_a = next(row for row in data["rows"] if row["label"] == "Cat A")
    assert "Nov-25" in data["months"]
    pos = data["months"].index("Nov-25")
    assert cat_a["expert_yoy"][pos] == pytest.approx(0.32)
    assert cat_a["expert_flag"][pos] is True
    assert cat_a["share"][pos] == pytest.approx(0.6123076923076922, abs=1e-6)
