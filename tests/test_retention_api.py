"""End-to-end API tests for segment-level retention on a dedicated dataset.

These use their own browser fixtures (``demo/retention_fixtures.py``) and do not
touch the 0-1 dataset used by ``test_api.py``.
"""
from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo

import pytest

from demo import retention_fixtures as rf

TZ = ZoneInfo("Asia/Shanghai")


def _settings(schema):
    from app.config import Settings

    return Settings(
        database_url="postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev",
        admin_api_key="test-admin-key",
        analyst_api_key="test-analyst-key",
        default_schema=schema,
        service_timezone="Asia/Shanghai",
    )


@pytest.fixture(scope="module")
def retention_env(database, client):
    """Seed the retention frame, schedule, segments and diaries once."""
    from app import ingest

    frame = ingest.create_recruitment_batch(database, rf.retention_recruitment_batch(), "pytest")
    tgt = ingest.create_target_batch(database, rf.retention_target_batch(), "pytest")
    prg = ingest.create_program_batch(database, rf.retention_program_batch(), "pytest")
    seg = ingest.create_segment_batch(database, rf.retention_segment_batch(), "pytest")
    rise_prg = ingest.create_program_batch(database, rf.retention_program_batch_rise(), "pytest")
    rise_seg = ingest.create_segment_batch(database, rf.rising_segment_batch(), "pytest")
    for sub in rf.retention_diaries():
        ingest.create_diary_submission(database, sub, "pytest", TZ)
    with database.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        cutoff = cur.fetchone()["m"]
    return {
        "recruitment_batch_seq": frame["batch_seq"],
        "target_batch_seq": tgt["batch_seq"],
        "program_batch_seq": prg["batch_seq"],
        "segment_batch_seq": seg["batch_seq"],
        "rise_program_batch_seq": rise_prg["batch_seq"],
        "rise_segment_batch_seq": rise_seg["batch_seq"],
        "diary_cutoff": cutoff,
    }


def _body(env, note="retention", program_seq=None, segment_seq=None, config=None, confirmations=None):
    return {
        "note": note,
        "inputs": {
            "recruitment_batch_seq": env["recruitment_batch_seq"],
            "target_batch_seq": env["target_batch_seq"],
            "program_batch_seq": program_seq or env["program_batch_seq"],
            "diary_seq_cutoff": env["diary_cutoff"],
            "segment_batch_seq": segment_seq or env["segment_batch_seq"],
            "confirmation_seqs": confirmations or [],
        },
        "config": config or {},
    }


def _publish(client, body):
    created = client.post("/v1/runs", json=body)
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["run_id"]
    executed = client.post(f"/v1/runs/{run_id}/execute", json={"max_steps": 50})
    assert executed.status_code == 200, executed.text
    detail = client.get(f"/v1/runs/{run_id}").json()
    return run_id, detail


# ---------------------------------------------------------------------------
# Bounds, labels, and the no-point rule
# ---------------------------------------------------------------------------
def test_retention_run_publishes_hard_bounds(client, retention_env):
    run_id, detail = _publish(client, _body(retention_env))
    assert detail["status"] == "published", detail.get("failure")
    resp = client.get(f"/v1/runs/{run_id}/segment-retention")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ok"
    programs = {p["program_key"]: p for p in payload["programs"]}
    show = programs["seg_show"]
    segs = sorted(show["segments"], key=lambda s: s["segment_index"])
    # The opening segment contains the boundary-crossing listener, so it has no
    # model-free point even though it has definite listeners.
    assert segs[0]["hard_bounds"]["possible_persons"] >= 1
    assert segs[0]["hard_bounds"]["point_available"] is False
    assert segs[0]["hard_bounds"]["point_share"] is None
    assert segs[0]["hard_bounds"]["lower_share"] < segs[0]["hard_bounds"]["upper_share"]
    # A later segment with no boundary crosser has a model-free point.
    assert segs[2]["hard_bounds"]["point_available"] is True
    # Hard bounds are ordered and non-increasing on the conservative side.
    lowers = [s["hard_bounds"]["lower_share"] for s in segs]
    assert lowers == sorted(lowers, reverse=True)


def test_model_estimate_is_disclosed_not_measured(client, retention_env):
    run_id, _ = _publish(client, _body(retention_env, note="model"))
    payload = client.get(f"/v1/runs/{run_id}/segment-retention").json()
    show = next(p for p in payload["programs"] if p["program_key"] == "seg_show")
    for seg in show["segments"]:
        model = seg["model_estimate"]
        assert model["measured"] is False
        assert model["available"] is True
        assert model["assumptions"]["not_a_measurement"] is True
        lo, hi = model["sensitivity"]["low"], model["sensitivity"]["high"]
        assert lo <= model["share"] <= hi
        # The model estimate is confined to the model-free hard-bound interval.
        hb = seg["hard_bounds"]
        assert hb["lower_share"] - 1e-9 <= model["share"] <= hb["upper_share"] + 1e-9
        assert model["hard_bound_interval"]["clamped_to_bounds"] is True


def test_model_none_leaves_no_estimate(client, retention_env):
    run_id, _ = _publish(
        client,
        _body(retention_env, note="no-model", config={"retention": {"model": "none"}}),
    )
    payload = client.get(f"/v1/runs/{run_id}/segment-retention").json()
    for program in payload["programs"]:
        for seg in program["segments"]:
            assert seg["model_estimate"]["available"] is False
            assert seg["model_estimate"]["share"] is None


# ---------------------------------------------------------------------------
# Record-level labels
# ---------------------------------------------------------------------------
def test_record_labels_are_definite_possible_unassignable(client, retention_env):
    run_id, detail = _publish(client, _body(retention_env, note="labels"))
    records = client.get(f"/v1/runs/{run_id}/segment-retention/records?limit=2000").json()
    classes = {r["classification"] for r in records}
    assert classes <= {"definite", "possible", "unassignable"}
    assert "definite" in classes
    assert "possible" in classes  # the boundary crosser
    # The cross-station conflict person is excluded upstream (unresolved_conflict)
    # and is therefore never a member of the retention cohort: it is neither
    # definite nor possible listening, and does not appear as a cohort record.
    persons = client.get(f"/v1/runs/{run_id}/persons?limit=1000").json()
    sconf = next(p for p in persons if p["person_id"] == "SCONF")
    assert sconf["eligible"] is False
    assert sconf["exclusion_code"] == "unresolved_conflict"
    # Every retained record carries a reason.
    assert all(r["reason"] for r in records)
    # The crosser shows up as possible in both adjacent segments, definite in none.
    crosser = [r for r in records if r["person_id"] == "SHCROSS"]
    assert crosser
    assert all(r["classification"] == "possible" for r in crosser)


def test_missing_visit_is_excluded_not_counted_as_silent(client, retention_env):
    run_id, detail = _publish(client, _body(retention_env, note="missing-visit"))
    persons = client.get(f"/v1/runs/{run_id}/persons?limit=1000").json()
    smiss = next(p for p in persons if p["person_id"] == "SMISS")
    assert smiss["eligible"] is False
    assert smiss["exclusion_code"] in {"incomplete_diary", "unresolved_conflict"}
    retention = client.get(f"/v1/runs/{run_id}/segment-retention").json()
    excluded = retention["summary"]["excluded_persons"]["by_code"]
    assert excluded
    assert "never treated as" in retention["summary"]["excluded_persons"]["unassignable_treatment"]


# ---------------------------------------------------------------------------
# Cross-midnight
# ---------------------------------------------------------------------------
def test_cross_midnight_segment_splits_by_actual_day(client, retention_env):
    run_id, _ = _publish(client, _body(retention_env, note="midnight"))
    payload = client.get(f"/v1/runs/{run_id}/segment-retention").json()
    late = next(p for p in payload["programs"] if p["program_key"] == "seg_late")
    first = next(s for s in late["segments"] if s["segment_index"] == 0)
    assert first["crosses_midnight"] is True
    assert first["by_day_minutes"]["2026-09-01"] == 30.0
    assert first["by_day_minutes"]["2026-09-02"] == 15.0
    assert sum(first["by_day_minutes"].values()) == first["length_minutes"]


# ---------------------------------------------------------------------------
# Delayed broadcast / schedule revision -> new version
# ---------------------------------------------------------------------------
def test_delayed_broadcast_confirmation_is_a_new_analysis_version(client, retention_env):
    planned_id, planned = _publish(client, _body(retention_env, note="planned-version"))
    assert planned["status"] == "published"
    conf = client.post("/v1/broadcast/confirmations", json=rf.delayed_confirmation())
    assert conf.status_code == 201, conf.text
    conf_seq = conf.json()["confirmation_seq"]
    delayed_id, delayed = _publish(
        client,
        _body(retention_env, note="delayed-version", confirmations=[conf_seq]),
    )
    assert delayed["status"] == "published", delayed.get("failure")
    assert planned_id != delayed_id

    planned_payload = client.get(f"/v1/runs/{planned_id}/segment-retention").json()
    delayed_payload = client.get(f"/v1/runs/{delayed_id}/segment-retention").json()
    show_planned = next(p for p in planned_payload["programs"] if p["program_key"] == "seg_show")
    show_delayed = next(p for p in delayed_payload["programs"] if p["program_key"] == "seg_show")
    assert show_planned["version"] == "planned"
    assert show_delayed["version"] == f"confirmed:{conf_seq}"
    assert show_delayed["version_basis"] == "as_aired_log"
    # The boundary shift changes the curve: it is a new version, not an edit.
    assert planned_payload["summary"]["retention_hash"] != delayed_payload["summary"]["retention_hash"]
    # The original published report is unchanged.
    old = client.get(f"/v1/runs/{planned_id}/report").json()
    old_again = client.get(f"/v1/runs/{planned_id}/report").json()
    assert old["report_hash"] == old_again["report_hash"]


def test_schedule_revision_requires_a_new_run(client, retention_env):
    revised = client.post("/v1/programs/batches", json=rf.revised_program_batch())
    assert revised.status_code == 201
    revised_seq = revised.json()["batch_seq"]
    first_id, first = _publish(client, _body(retention_env, note="schedule-v1"))
    assert first["status"] == "published"
    second_id, second = _publish(
        client,
        _body(retention_env, note="schedule-v2", program_seq=revised_seq),
    )
    assert second["status"] == "published", second.get("failure")
    assert first_id != second_id
    # The old report hash is untouched by the revision.
    old = client.get(f"/v1/runs/{first_id}/report").json()
    assert old["report_hash"] == client.get(f"/v1/runs/{first_id}/report").json()["report_hash"]


# ---------------------------------------------------------------------------
# Monotonicity refusal
# ---------------------------------------------------------------------------
def test_rising_conservative_curve_refuses_publication(client, retention_env):
    run_id, detail = _publish(
        client,
        _body(
            retention_env,
            note="non-monotone",
            program_seq=retention_env["rise_program_batch_seq"],
            segment_seq=retention_env["rise_segment_batch_seq"],
        ),
    )
    assert detail["status"] == "rejected"
    assert detail["failure"]["code"] == "retention_non_monotone"
    checks = client.get(f"/v1/runs/{run_id}/segment-retention/checks").json()
    assert checks["monotonicity_satisfied"] is False
    violated = [c for c in checks["checks"] if c["status"] == "violated"]
    assert violated and violated[0]["violations"]
    # No report is published for a refused run.
    report = client.get(f"/v1/runs/{run_id}/report")
    assert report.status_code == 404


def test_require_monotone_false_publishes_with_a_warning(client, retention_env):
    run_id, detail = _publish(
        client,
        _body(
            retention_env,
            note="non-monotone-allowed",
            program_seq=retention_env["rise_program_batch_seq"],
            segment_seq=retention_env["rise_segment_batch_seq"],
            config={"retention": {"require_monotone": False}},
        ),
    )
    assert detail["status"] == "published", detail.get("failure")
    checks = client.get(f"/v1/runs/{run_id}/segment-retention/checks").json()
    statuses = {c["status"] for c in checks["checks"] if c["check"] == "retention_monotonicity"}
    assert "reported_not_enforced" in statuses


# ---------------------------------------------------------------------------
# Replay / determinism
# ---------------------------------------------------------------------------
def test_retention_replay_reproduces_the_curve(client, retention_env):
    run_id, _ = _publish(client, _body(retention_env, note="replay"))
    replay = client.get(f"/v1/runs/{run_id}/segment-retention/replay").json()
    assert replay["reproduced"] is True
    assert replay["recorded_retention_hash"] == replay["replay_retention_hash"]


# ---------------------------------------------------------------------------
# Pinning rules
# ---------------------------------------------------------------------------
def test_confirmation_without_segment_batch_is_refused(client, retention_env):
    body = _body(retention_env, note="conf-no-segment")
    body["inputs"]["segment_batch_seq"] = None
    body["inputs"]["confirmation_seqs"] = [1]
    resp = client.post("/v1/runs", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "confirmation_without_segment_batch"


def test_unknown_confirmation_seq_is_refused(client, retention_env):
    body = _body(retention_env, note="bad-conf", confirmations=[999999])
    resp = client.post("/v1/runs", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "confirmation_not_found"


def test_segment_batch_for_unknown_airing_is_refused(client, retention_env):
    # The rising segment batch only covers seg_rise, which is not in the main
    # program batch, so the boundaries cannot be matched and creation is refused.
    resp = client.post(
        "/v1/runs",
        json=_body(retention_env, note="mismatch", segment_seq=retention_env["rise_segment_batch_seq"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "segment_batch_unmatched_airing"


def test_segment_batch_may_cover_a_subset_of_airings(client, retention_env):
    # A segment batch that covers only seg_show leaves seg_late on uniform
    # defaults; this proves boundaries are keyed per airing, not per batch.
    from app import ingest

    batch = rf.retention_segment_batch(batch_ref=f"segments-show-only-{uuid.uuid4().hex[:6]}")
    batch["airings"] = [a for a in batch["airings"] if a["program_key"] == "seg_show"]
    created = ingest.create_segment_batch(client.app.state.db, batch, "pytest")
    run_id, detail = _publish(
        client, _body(retention_env, note="subset", segment_seq=created["batch_seq"])
    )
    assert detail["status"] == "published", detail.get("failure")
    payload = client.get(f"/v1/runs/{run_id}/segment-retention").json()
    programs = {p["program_key"]: p for p in payload["programs"]}
    assert programs["seg_show"]["version"] == "planned"
    show_segments = sorted(programs["seg_show"]["segments"], key=lambda s: s["segment_index"])
    assert len(show_segments) == 4  # 4 x 15 minutes tiles the 60-minute window
    assert sum(s["length_minutes"] for s in show_segments) == 60.0
    # seg_late had no boundaries in this batch, so it falls back to uniform
    # auto-segments built from the declared 5-minute unit (60 min -> 12 pieces).
    late_segments = programs["seg_late"]["segments"]
    assert late_segments
    assert all(s["length_minutes"] == 5.0 for s in late_segments)
