"""Unit tests for segment-level retention: bounds, models, midnight and guards."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.analytics import retention as R
from app.timeutil import local_instant

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 9, 1)
UTC = timezone.utc


def _airing(key, station, start_min, end_min, day=DAY):
    return {
        "program_key": key,
        "title": key,
        "station": station,
        "air_date": day,
        "start_utc": local_instant(day, start_min, TZ),
        "end_utc": local_instant(day, end_min, TZ),
    }


def _bounds(boundaries, unit=5):
    return boundaries


def _person(pid, station, times, weight=1.0, unknown=None):
    return {
        "person_id": pid,
        "weight": weight,
        "records_by_station": {
            station: [
                {
                    "start": local_instant(DAY, s, TZ),
                    "end": local_instant(DAY, e, TZ),
                    "record_id": f"{pid}-{i}",
                    "granularity": 5,
                }
                for i, (s, e) in enumerate(times)
            ]
        },
        "unknown_ranges": unknown or [],
    }


def _segments(spec):
    return {
        ("show", DAY, "S"): [
            {"segment_index": i, "start_offset_min": a, "end_offset_min": b, "label": f"s{i}"}
            for i, (a, b) in enumerate(spec)
        ]
    }


# ---------------------------------------------------------------------------
# Hard bounds / classification
# ---------------------------------------------------------------------------
def test_boundary_crossing_record_widens_interval_without_point():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    # One person wholly inside segment 0 (padded inside), one who straddles the
    # 20:15 boundary, and one confirmed non-listener.
    persons = [
        _person("a", "S", [(20 * 60 + 3, 20 * 60 + 12)]),
        _person("b", "S", [(20 * 60 + 12, 20 * 60 + 18)]),
        _person("c", "S", []),
    ]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    segs = result["airings"][0]["segments"]
    seg0 = segs[0]["hard_bounds"]
    # def a, poss b -> lower 1/3, upper 2/3, no single-point share.
    assert seg0["lower_persons"] == 1
    assert seg0["upper_persons"] == 2
    assert seg0["point_available"] is False
    assert seg0["point_share"] is None
    # Segment 1: neperson is definite there (b straddles, so possible only).
    seg1 = segs[1]["hard_bounds"]
    assert seg1["lower_persons"] == 0
    assert seg1["upper_persons"] == 1
    assert seg1["point_available"] is False


def test_record_wholly_inside_segment_is_definite_point():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    persons = [
        _person("a", "S", [(20 * 60 + 3, 20 * 60 + 12)]),
        _person("b", "S", [(20 * 60 + 18, 20 * 60 + 27)]),
        _person("c", "S", []),
    ]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    segs = result["airings"][0]["segments"]
    # The cohort is the opening-segment audience ({a}); b starts later, so b is
    # not part of the retention cohort.
    assert result["airings"][0]["cohort_persons"] == 1
    assert segs[0]["hard_bounds"]["point_available"] is True
    assert segs[0]["hard_bounds"]["point_share"] == 1.0
    assert segs[1]["hard_bounds"]["point_available"] is True
    assert segs[1]["hard_bounds"]["point_share"] == 0.0
    assert segs[0]["hard_bounds"]["upper_persons"] == segs[0]["hard_bounds"]["lower_persons"]


def test_granularity_padding_can_turn_a_record_possible():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    # Ends at 20:13 -> padded to 20:15:30, crossing into the next segment.
    persons = [_person("a", "S", [(20 * 60 + 3, 20 * 60 + 13)])]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    seg0 = result["airings"][0]["segments"][0]
    assert seg0["hard_bounds"]["point_available"] is False
    assert seg0["hard_bounds"]["possible_persons"] == 1


# ---------------------------------------------------------------------------
# Cross-midnight
# ---------------------------------------------------------------------------
def test_cross_midnight_segment_is_split_by_actual_boundary():
    start = local_instant(DAY, 23 * 60 + 30, TZ)
    end = local_instant(DAY + timedelta(days=1), 30, TZ)
    pieces = R.split_by_actual_day(start, end, TZ)
    assert set(pieces) == {DAY, DAY + timedelta(days=1)}
    night = sum((b - a).total_seconds() / 60 for a, b in pieces[DAY])
    after = sum((b - a).total_seconds() / 60 for a, b in pieces[DAY + timedelta(days=1)])
    assert night == 30.0
    assert after == 30.0


def test_cross_midnight_airing_reports_by_day_split():
    airing = {
        "program_key": "late",
        "title": "late",
        "station": "L",
        "air_date": DAY,
        "start_utc": local_instant(DAY, 23 * 60 + 30, TZ),
        "end_utc": local_instant(DAY, 24 * 60 + 30, TZ),
    }
    boundaries = {
        ("late", DAY, "L"): [
            {"segment_index": 0, "start_offset_min": 0, "end_offset_min": 45},
            {"segment_index": 1, "start_offset_min": 45, "end_offset_min": 60},
        ]
    }
    result = R.compute_retention(
        [airing], boundaries, {}, [_person("a", "L", [])], TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    seg0 = result["airings"][0]["segments"][0]
    assert seg0["crosses_midnight"] is True
    assert seg0["by_day_minutes"][DAY.isoformat()] == 30.0
    assert seg0["by_day_minutes"][(DAY + timedelta(days=1)).isoformat()] == 15.0


# ---------------------------------------------------------------------------
# Confirmed version (delayed broadcast)
# ---------------------------------------------------------------------------
def test_confirmation_shifts_boundaries_and_is_a_new_version():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    conf = {
        ("show", DAY, "S"): {
            "seq": 7,
            "confirmed_start": local_instant(DAY, 20 * 60 + 20, TZ),
            "confirmed_end": local_instant(DAY, 21 * 60 + 20, TZ),
            "basis": "as_aired_log",
        }
    }
    # Listener sits in the *delayed* first segment (never in the planned one).
    persons = [_person("a", "S", [(20 * 60 + 23, 20 * 60 + 32)])]
    result = R.compute_retention(
        [airing], boundaries, conf, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    built = result["airings"][0]
    assert built["version"] == "confirmed:7"
    assert built["version_basis"] == "as_aired_log"
    assert built["confirmation_seq"] == 7
    assert built["delay_minutes"] == 20.0
    seg0 = built["segments"][0]
    assert seg0["hard_bounds"]["lower_persons"] == 1
    assert seg0["hard_bounds"]["point_available"] is True


def test_planned_boundaries_dropped_when_confirmation_shortens_window():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30), (30, 45), (45, 60)])
    conf = {
        ("show", DAY, "S"): {
            "seq": 1,
            "confirmed_start": local_instant(DAY, 20 * 60 + 20, TZ),
            "confirmed_end": local_instant(DAY, 21 * 60 + 20, TZ),
            "basis": "analyst_confirmed",
        }
    }
    result = R.compute_retention(
        [airing], boundaries, conf, [_person("a", "S", [])], TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    assert len(result["airings"][0]["segments"]) == 4


# ---------------------------------------------------------------------------
# Missing visit / conflict -> unassignable, never zero
# ---------------------------------------------------------------------------
def test_conflict_overlap_is_unassignable_not_possible():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    unknown = [(local_instant(DAY, 20 * 60 + 10, TZ), local_instant(DAY, 20 * 60 + 20, TZ))]
    persons = [_person("c", "S", [(20 * 60 + 3, 20 * 60 + 12)], unknown=unknown)]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    # The record overlaps the conflict, so it is unassignable for both segments.
    assert result["airings"][0]["cohort_persons"] == 0
    labels = {(r["person_id"], r["classification"]) for r in result["records"]}
    assert ("c", R.CLASS_UNASSIGNABLE) in labels
    assert all(r["classification"] != R.CLASS_DEFINITE for r in result["records"])


def test_person_with_no_records_and_no_coverage_is_not_in_cohort():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    persons = [
        _person("a", "S", [(20 * 60 + 3, 20 * 60 + 12)]),
        {**_person("missing", "S", []), "coverage": []},
    ]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    # Only the person present in the opening segment is in the cohort.
    assert result["airings"][0]["cohort_persons"] == 1
    assert result["airings"][0]["segments"][0]["hard_bounds"]["lower_persons"] == 1


# ---------------------------------------------------------------------------
# Model estimate: disclosed, bounded, not measured
# ---------------------------------------------------------------------------
def test_model_estimate_is_bounded_and_not_measured():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    persons = [
        _person("a", "S", [(20 * 60 + 3, 20 * 60 + 12)]),
        _person("b", "S", [(20 * 60 + 12, 20 * 60 + 18)]),
    ]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_INTERVAL_CENSORED
    )
    for seg in result["airings"][0]["segments"]:
        model = seg["model_estimate"]
        assert model["available"] is True
        assert model["measured"] is False
        lo, hi = model["sensitivity"]["low"], model["sensitivity"]["high"]
        assert lo <= model["share"] <= hi
        assert seg["hard_bounds"]["lower_share"] - 1e-9 <= model["share"] + 1e-9
        assert model["assumptions"]["not_a_measurement"] is True


def test_model_none_publishes_no_estimate():
    airing = _airing("show", "S", 20 * 60, 21 * 60)
    boundaries = _segments([(0, 15), (15, 30)])
    result = R.compute_retention(
        [airing],
        boundaries,
        {},
        [_person("a", "S", [(20 * 60 + 3, 20 * 60 + 12)])],
        TZ,
        unit_minutes=5,
        model=R.MODEL_NONE,
    )
    assert result["airings"][0]["segments"][0]["model_estimate"]["available"] is False


# ---------------------------------------------------------------------------
# Monotonicity guard
# ---------------------------------------------------------------------------
def test_rising_conservative_curve_is_flagged():
    airing = _airing("rise", "R", 10 * 60, 10 * 60 + 30)
    boundaries = {
        ("rise", DAY, "R"): [
            {"segment_index": 0, "start_offset_min": 0, "end_offset_min": 15},
            {"segment_index": 1, "start_offset_min": 15, "end_offset_min": 30},
        ]
    }
    persons = [
        _person("p1", "R", [(10 * 60 + 5, 10 * 60 + 10), (10 * 60 + 20, 10 * 60 + 25)]),
        _person("p2", "R", [(10 * 60 + 12, 10 * 60 + 18), (10 * 60 + 22, 10 * 60 + 26)]),
    ]
    result = R.compute_retention(
        [airing], boundaries, {}, persons, TZ, unit_minutes=5, model=R.MODEL_NONE
    )
    check = next(c for c in result["checks"] if c["check"] == "retention_monotonicity")
    assert check["status"] == "violated"
    assert check["violations"]
    assert result["monotonicity_satisfied"] is False


def test_require_monotone_false_reports_without_refusing():
    airing = _airing("rise", "R", 10 * 60, 10 * 60 + 30)
    boundaries = {
        ("rise", DAY, "R"): [
            {"segment_index": 0, "start_offset_min": 0, "end_offset_min": 15},
            {"segment_index": 1, "start_offset_min": 15, "end_offset_min": 30},
        ]
    }
    persons = [
        _person("p1", "R", [(10 * 60 + 5, 10 * 60 + 10), (10 * 60 + 20, 10 * 60 + 25)]),
        _person("p2", "R", [(10 * 60 + 12, 10 * 60 + 18), (10 * 60 + 22, 10 * 60 + 26)]),
    ]
    result = R.compute_retention(
        [airing],
        boundaries,
        {},
        persons,
        TZ,
        unit_minutes=5,
        model=R.MODEL_NONE,
        require_monotone=False,
    )
    check = next(c for c in result["checks"] if c["check"] == "retention_monotonicity")
    assert check["status"] == "reported_not_enforced"
    assert result["status"] == "ok"
