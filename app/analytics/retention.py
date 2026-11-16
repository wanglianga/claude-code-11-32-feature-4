"""Segment-level retention for coarse listening records.

Problem
-------
Listening records arrive at a fixed granularity (for example five minutes).
When such a record crosses an advertising boundary (a *segment* boundary) the
record cannot say which side of the boundary the listener stopped on. A
planned schedule can also be invalidated by an actual delayed broadcast, so
segment boundaries must come from a **confirmed** version when one exists.

Two things are kept strictly apart and never blurred together:

1. **Model-free hard bounds.** A record whose worst-case span (reported
   endpoints widened by half the recording granularity) lies entirely inside
   one segment is a *definite* attribution. A record that straddles a boundary
   is only a *possible* attribution: it widens the interval, but is never
   turned into a point share.  So a segment with boundary-crossing records has
   ``lower``/``upper`` but **no** model-free point.
2. **An explicitly declared interval-censored model.** Reported start/end are
   treated as interval-censored observations of the true start/stop, with a
   declared within-granularity placement rule. It produces a smoother estimate
   but is labelled ``measured: false`` and carries its parameters plus a
   sensitivity band. It is never presented as a measurement.

Definitions
-----------
* A *segment* is a half-open ``[start, end)`` inside one confirmed airing.
* Each record is labelled, per airing it touches, ``definite`` (wholly inside
  one segment), ``possible`` (straddles a boundary) or ``unassignable``
  (overlaps an unresolved cross-station conflict, or cannot be tied to the
  airing at all, e.g. a missing visit).
* The **retention cohort** is the set of persons present in the opening
  segment (definite or possible there). Retention at segment ``i`` is the share
  of that cohort listening during segment ``i``:
  - ``lower``  = share whose record is definite for the segment,
  - ``upper``  = share whose record is definite *or* possible for the segment,
  - ``estimate`` = expected share covered under the interval-censored model,
    which lies between ``lower`` and ``upper``.
* Cross-midnight segments are split by the *actual* local day boundary before
  any day total is produced.
* Within one airing the conservative (lower-bound) retention must not rise
  from one segment to the next. If it does, the publisher refuses the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from ..timeutil import local_instant, merge

CLASS_DEFINITE = "definite"
CLASS_POSSIBLE = "possible"
CLASS_UNASSIGNABLE = "unassignable"

PLACEMENT_AT_START = "at_start"
PLACEMENT_AT_END = "at_end"

MODEL_NONE = "none"
MODEL_INTERVAL_CENSORED = "interval_censored"


@dataclass
class Segment:
    program_key: str
    air_date: date
    station: str
    index: int
    start: datetime
    end: datetime
    version: str
    basis: str
    label: str | None = None

    @property
    def length_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class Airing:
    program_key: str
    title: str
    station: str
    air_date: date
    planned_start: datetime
    planned_end: datetime
    effective_start: datetime
    effective_end: datetime
    version: str
    basis: str
    confirmation_seq: int | None = None
    segments: list[Segment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def delay_minutes(self) -> float:
        return (self.effective_start - self.planned_start).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# Small interval helpers
# ---------------------------------------------------------------------------
def _minutes(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60.0)


def _intersects(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a1 > b0 and a0 < b1


def _clip(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> tuple[datetime, datetime] | None:
    s, e = max(a0, b0), min(a1, b1)
    return (s, e) if e > s else None


def _contains(outer0: datetime, outer1: datetime, inner0: datetime, inner1: datetime) -> bool:
    return outer0 <= inner0 and inner1 <= outer1


def split_by_actual_day(
    start: datetime, end: datetime, tz: ZoneInfo
) -> dict[date, list[tuple[datetime, datetime]]]:
    """Split ``[start, end)`` at every real local midnight it crosses.

    The local day is resolved at each cursor position, so the split follows the
    actual boundary (including a DST change) rather than assuming a fixed 24h.
    """
    out: dict[date, list[tuple[datetime, datetime]]] = {}
    cursor = start
    while cursor < end:
        local_day = cursor.astimezone(tz).date()
        next_midnight = local_instant(local_day + timedelta(days=1), 0, tz)
        piece_end = min(end, next_midnight)
        if piece_end <= cursor:  # defensive: never emit an empty day piece
            break
        out.setdefault(local_day, []).append((cursor, piece_end))
        cursor = piece_end
    return out


def _auto_boundaries(duration_minutes: float, unit: float) -> list[tuple[float, float]]:
    if unit <= 0:
        unit = duration_minutes or 1.0
    bounds: list[tuple[float, float]] = []
    offset = 0.0
    while offset < duration_minutes - 1e-9:
        end = min(offset + unit, duration_minutes)
        bounds.append((offset, end))
        offset = end
    return bounds


# ---------------------------------------------------------------------------
# Effective versions and boundaries
# ---------------------------------------------------------------------------
def build_airings(
    airings: list[dict[str, Any]],
    boundaries: dict[tuple[str, date, str], list[dict[str, Any]]],
    confirmations: dict[tuple[str, date, str], dict[str, Any]],
    tz: ZoneInfo,
    unit_minutes: float,
) -> list[Airing]:
    """Attach effective boundaries to each planned airing.

    A pinned confirmation for ``(program_key, air_date, station)`` replaces the
    planned start/end; declared segment offsets are then measured from the
    confirmed start. Without a confirmation the planned schedule is the
    explicitly specified version and is used as-is.
    """
    out: list[Airing] = []
    for raw in airings:
        key = (raw["program_key"], raw["air_date"], raw["station"])
        conf = confirmations.get(key)
        if conf is not None:
            eff_start, eff_end = conf["confirmed_start"], conf["confirmed_end"]
            version = f"confirmed:{conf['seq']}"
            basis = conf.get("basis", "analyst_confirmed")
            delay = (eff_start - raw["start_utc"]).total_seconds() / 60.0
            notes = [f"effective version is confirmed (delay {delay:+.1f} min vs planned)"]
        else:
            eff_start, eff_end = raw["start_utc"], raw["end_utc"]
            version = "planned"
            basis = "planned_schedule"
            notes = ["no confirmation pinned; the explicitly specified planned schedule is used"]

        airing = Airing(
            program_key=raw["program_key"],
            title=raw.get("title", raw["program_key"]),
            station=raw["station"],
            air_date=raw["air_date"],
            planned_start=raw["start_utc"],
            planned_end=raw["end_utc"],
            effective_start=eff_start,
            effective_end=eff_end,
            version=version,
            basis=basis,
            confirmation_seq=None if conf is None else conf["seq"],
            notes=notes,
        )

        declared = boundaries.get(key)
        if declared:
            duration = _minutes(eff_start, eff_end)
            for row in sorted(declared, key=lambda r: r["segment_index"]):
                off0 = float(row["start_offset_min"])
                off1 = float(row["end_offset_min"])
                if off0 >= duration - 1e-9:
                    airing.notes.append(
                        f"declared segment {row['segment_index']} starts outside the effective "
                        f"window ({off0:.1f} min >= {duration:.1f} min) and was dropped"
                    )
                    continue
                seg_start = eff_start + timedelta(minutes=off0)
                seg_end = eff_start + timedelta(minutes=off1)
                if seg_end > eff_end + timedelta(microseconds=1):
                    airing.notes.append(
                        f"declared segment {row['segment_index']} truncated at the effective end"
                    )
                    seg_end = eff_end
                if seg_end <= seg_start:
                    continue
                airing.segments.append(
                    Segment(
                        program_key=raw["program_key"],
                        air_date=raw["air_date"],
                        station=raw["station"],
                        index=int(row["segment_index"]),
                        start=seg_start,
                        end=seg_end,
                        version=version,
                        basis=basis,
                        label=row.get("label"),
                    )
                )
        else:
            airing.notes.append("no declared boundaries; uniform segments from the recording unit")
            for idx, (off0, off1) in enumerate(
                _auto_boundaries(_minutes(eff_start, eff_end), unit_minutes)
            ):
                airing.segments.append(
                    Segment(
                        program_key=raw["program_key"],
                        air_date=raw["air_date"],
                        station=raw["station"],
                        index=idx,
                        start=eff_start + timedelta(minutes=off0),
                        end=eff_start + timedelta(minutes=off1),
                        version=version,
                        basis=basis,
                    )
                )
        out.append(airing)
    return out


# ---------------------------------------------------------------------------
# Record spans and labelling
# ---------------------------------------------------------------------------
def record_spans(
    record: dict[str, Any], granularity: float
) -> dict[str, tuple[datetime, datetime]]:
    """Return ``certain`` / ``reported`` / ``maximal`` spans for one record.

    ``certain``  = certainly listened (endpoints pulled in by half a unit),
    ``reported`` = the raw reported interval,
    ``maximal``  = widest interval consistent with the report (padded by half a unit).
    """
    half = timedelta(minutes=max(0.0, granularity) / 2.0)
    start, end = record["start"], record["end"]
    certain = (start + half, end - half)
    if certain[1] <= certain[0]:
        # A record shorter than the recording unit has no certain interior.
        certain = (start, start)
    return {"certain": certain, "reported": (start, end), "maximal": (start - half, end + half)}


def _record_unknown_overlap(
    record: dict[str, Any], granularity: float, unknown_ranges: list[tuple[datetime, datetime]]
) -> bool:
    maximal = record_spans(record, granularity)["maximal"]
    return any(_intersects(maximal[0], maximal[1], u0, u1) for u0, u1 in unknown_ranges)


def label_record(
    record: dict[str, Any], airing: Airing, granularity: float
) -> tuple[str, list[int], str]:
    """Label one record against one airing.

    Returns ``(classification, touched_segment_indices, reason)``.
    """
    maximal = record_spans(record, granularity)["maximal"]
    touched = [
        seg.index
        for seg in airing.segments
        if _intersects(maximal[0], maximal[1], seg.start, seg.end)
    ]
    if not touched:
        return CLASS_UNASSIGNABLE, [], "record does not overlap this airing"
    if _record_unknown_overlap(record, granularity, record.get("_unknown_ranges", [])):
        return CLASS_UNASSIGNABLE, touched, "record overlaps an unresolved cross-station conflict"
    if not record.get("_version_usable", True):
        return CLASS_UNASSIGNABLE, touched, "no usable confirmed broadcast version for this airing"

    containing = [
        seg.index
        for seg in airing.segments
        if _contains(seg.start, seg.end, maximal[0], maximal[1])
    ]
    if containing:
        return (
            CLASS_DEFINITE,
            [containing[0]],
            "worst-case record span lies entirely inside one segment",
        )
    return (
        CLASS_POSSIBLE,
        touched,
        "record straddles a segment or programme boundary; attribution is ambiguous",
    )


# ---------------------------------------------------------------------------
# Interval-censored model
# ---------------------------------------------------------------------------
def _expected_overlap_probability(
    record: dict[str, Any], segment: Segment, granularity: float
) -> float:
    """P(true listening interval overlaps the segment), endpoints uniform in their cells.

    This keeps the model on the same scale as the hard bounds: both are shares of
    the cohort who listened during the segment. It smooths the 0/1 attribution,
    but it is an estimate under a declared rule, never a measurement.
    """
    half = max(0.0, granularity) / 2.0
    s_lo = record["start"] - timedelta(minutes=half)
    s_hi = record["start"] + timedelta(minutes=half)
    e_lo = record["end"] - timedelta(minutes=half)
    e_hi = record["end"] + timedelta(minutes=half)
    steps = 9
    overlap = 0
    for i in range(steps):
        sx = s_lo + (s_hi - s_lo) * (i + 0.5) / steps
        for j in range(steps):
            ey = e_lo + (e_hi - e_lo) * (j + 0.5) / steps
            if ey > sx and _intersects(sx, ey, segment.start, segment.end):
                overlap += 1
    return overlap / (steps * steps)


def _extreme_overlap_probability(
    record: dict[str, Any], segment: Segment, granularity: float, placement: str
) -> float:
    """Overlap probability under a corner in the rounding cell (deterministic 0/1).

    ``at_end`` shrinks both endpoints to their latest/earliest consistent values
    (least overlap); ``at_start`` expands them (most overlap). These bracket the
    uniform-placement estimate.
    """
    spans = record_spans(record, granularity)
    span = spans["maximal"] if placement == PLACEMENT_AT_START else spans["certain"]
    return 1.0 if (span[1] > span[0] and _intersects(span[0], span[1], segment.start, segment.end)) else 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _weight_sum(items: list[dict[str, Any]]) -> float:
    return sum(i["weight"] for i in items if i.get("weight") is not None)


def compute_retention(
    airings: list[dict[str, Any]],
    boundaries: dict[tuple[str, date, str], list[dict[str, Any]]],
    confirmations: dict[tuple[str, date, str], dict[str, Any]],
    persons: list[dict[str, Any]],
    tz: ZoneInfo,
    *,
    unit_minutes: float,
    model: str = MODEL_INTERVAL_CENSORED,
    require_monotone: bool = True,
    monotonicity_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Compute per-segment hard bounds, an optional model estimate and record labels.

    ``persons`` each carry ``person_id``, ``weight`` (or ``None``),
    ``records_by_station`` and ``unknown_ranges``. Only eligible persons should
    be supplied; missing diaries are already excluded upstream.
    """
    built = build_airings(airings, boundaries, confirmations, tz, unit_minutes)

    per_airing: list[dict[str, Any]] = []
    record_rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    for airing in built:
        if not airing.segments:
            checks.append(
                {
                    "check": "retention_segments_present",
                    "program_key": airing.program_key,
                    "station": airing.station,
                    "air_date": airing.air_date.isoformat(),
                    "status": "violated",
                    "reason": "no effective segment boundaries fall inside the airing",
                }
            )
            continue

        # person -> segment -> state; plus record-level labels
        person_segments: dict[str, dict[int, str]] = {}
        person_records: dict[str, list[dict[str, Any]]] = {}
        for person in persons:
            pid = person["person_id"]
            unknown = person.get("unknown_ranges", [])
            records = [
                r
                for r in person["records_by_station"].get(airing.station, [])
                if _intersects(
                    *record_spans(r, r.get("granularity", unit_minutes))["maximal"],
                    airing.effective_start,
                    airing.effective_end,
                )
            ]
            states: dict[int, str] = {}
            labelled: list[dict[str, Any]] = []
            for rec in records:
                gran = rec.get("granularity", unit_minutes)
                rec_with_context = {**rec, "_unknown_ranges": unknown, "_version_usable": True}
                cls, touched, reason = label_record(rec_with_context, airing, gran)
                labelled.append(
                    {
                        "record": rec,
                        "classification": cls,
                        "touched": touched,
                        "reason": reason,
                        "granularity": gran,
                    }
                )
                for seg in touched:
                    # definite only wins for its containing segment; a possible
                    # record is possible for every segment it touches.
                    prev = states.get(seg)
                    if cls == CLASS_DEFINITE:
                        states[seg] = CLASS_DEFINITE
                    elif cls == CLASS_POSSIBLE and prev != CLASS_DEFINITE:
                        states[seg] = CLASS_POSSIBLE
            person_segments[pid] = states
            person_records[pid] = labelled

        cohort = [
            p for p in persons if person_segments[p["person_id"]].get(0) in (CLASS_DEFINITE, CLASS_POSSIBLE)
        ]
        cohort_n = len(cohort)
        cohort_w = _weight_sum(cohort)

        segment_payloads: list[dict[str, Any]] = []
        lower_series: list[float] = []
        lower_counts: list[int] = []
        for seg in airing.segments:
            definite = [
                p
                for p in cohort
                if person_segments[p["person_id"]].get(seg.index) == CLASS_DEFINITE
            ]
            possible = [
                p
                for p in cohort
                if person_segments[p["person_id"]].get(seg.index) == CLASS_POSSIBLE
            ]
            absent = cohort_n - len(definite) - len(possible)
            lower_n, upper_n = len(definite), len(definite) + len(possible)
            lower_w, upper_w = _weight_sum(definite), _weight_sum(definite) + _weight_sum(possible)

            model_block = {
                "available": False,
                "measured": False,
                "kind": None,
                "share": None,
                "sensitivity": None,
                "assumptions": None,
            }
            if model == MODEL_INTERVAL_CENSORED and cohort:
                def coverage(fn) -> float:
                    # Unweighted person-share, on the same basis as the published
                    # hard bounds (lower_share / upper_share), so the model can be
                    # clamped inside them without a basis mismatch.
                    vals = []
                    for p in cohort:
                        frac = 0.0
                        for rec in person_records[p["person_id"]]:
                            if seg.index not in rec["touched"]:
                                continue
                            if rec["classification"] == CLASS_UNASSIGNABLE:
                                continue
                            frac += fn(rec["record"], seg, rec["granularity"])
                        vals.append(min(1.0, frac))
                    return fmean(vals) if vals else 0.0

                uniform = coverage(_expected_overlap_probability)
                at_end = coverage(
                    lambda r, s, g: _extreme_overlap_probability(r, s, g, PLACEMENT_AT_END)
                )
                at_start = coverage(
                    lambda r, s, g: _extreme_overlap_probability(r, s, g, PLACEMENT_AT_START)
                )
                # The model must stay inside the model-free hard bounds. A record
                # that is definite under the worst-case span is definite listening,
                # so its model probability is at least the lower bound; a record
                # that is not even possible cannot contribute above the upper
                # bound. Clamping keeps the published estimate honest.
                lower_share = lower_n / cohort_n if cohort_n else 0.0
                upper_share = upper_n / cohort_n if cohort_n else 0.0

                def _clamp(value: float) -> float:
                    return min(max(value, lower_share), upper_share)

                share = _clamp(uniform)
                sens_lo = _clamp(min(at_end, at_start))
                sens_hi = _clamp(max(at_end, at_start))
                model_block = {
                    "available": True,
                    "measured": False,
                    "kind": "interval_censored_overlap_probability",
                    "basis": "unweighted person share of the retention cohort",
                    "share": round(share, 6),
                    "sensitivity": {
                        "low": round(min(sens_lo, share), 6),
                        "high": round(max(sens_hi, share), 6),
                    },
                    "hard_bound_interval": {
                        "low": round(lower_share, 6),
                        "high": round(upper_share, 6),
                        "clamped_to_bounds": True,
                    },
                    "assumptions": {
                        "observed": "reported record start/end rounded to the recording unit",
                        "censoring": "each true endpoint lies within +/- unit/2 of the reported one",
                        "placement_rule": "endpoints uniform within the rounding cell",
                        "sensitivity_rule": "at_end = lower, at_start = upper endpoint placement",
                        "clamping_rule": "estimate is confined to the model-free hard-bound interval",
                        "not_a_measurement": True,
                    },
                }

            point_available = len(possible) == 0
            by_day = {
                day.isoformat(): round(sum(_minutes(a, b) for a, b in pieces), 3)
                for day, pieces in split_by_actual_day(seg.start, seg.end, tz).items()
            }
            segment_payloads.append(
                {
                    "program_key": airing.program_key,
                    "title": airing.title,
                    "station": airing.station,
                    "air_date": airing.air_date.isoformat(),
                    "segment_index": seg.index,
                    "label": seg.label,
                    "start_utc": seg.start.isoformat(),
                    "end_utc": seg.end.isoformat(),
                    "length_minutes": round(seg.length_minutes, 3),
                    "by_day_minutes": by_day,
                    "crosses_midnight": len(by_day) > 1,
                    "version": airing.version,
                    "version_basis": airing.basis,
                    "confirmation_seq": airing.confirmation_seq,
                    "delay_minutes": round(airing.delay_minutes, 3),
                    "cohort_persons": cohort_n,
                    "cohort_weighted": round(cohort_w, 3) if cohort_w else None,
                    "hard_bounds": {
                        "lower_persons": lower_n,
                        "upper_persons": upper_n,
                        "definite_persons": lower_n,
                        "possible_persons": len(possible),
                        "absent_persons": absent,
                        "lower_share": round(lower_n / cohort_n, 6) if cohort_n else None,
                        "upper_share": round(upper_n / cohort_n, 6) if cohort_n else None,
                        "lower_weighted": round(lower_w, 3) if cohort_w else None,
                        "upper_weighted": round(upper_w, 3) if cohort_w else None,
                        "lower_weighted_share": round(lower_w / cohort_w, 6) if cohort_w else None,
                        "upper_weighted_share": round(upper_w / cohort_w, 6) if cohort_w else None,
                        "point_available": point_available,
                        "point_share": (round(lower_n / cohort_n, 6) if (point_available and cohort_n) else None),
                    },
                    "model_estimate": model_block,
                    "basis": {
                        "method": "model_free_point" if point_available else "model_free_bounds_only",
                        "granularity_minutes": unit_minutes,
                        "version": airing.version,
                        "version_basis": airing.basis,
                        "cohort_definition": "persons present in the opening segment",
                        "why": (
                            "every record touching this segment is wholly inside it, so the count is definite"
                            if point_available
                            else "boundary-crossing records are present: they widen the interval and no "
                            "model-free single-point share is published"
                        ),
                    },
                }
            )
            lower_series.append(round(lower_n / cohort_n, 6) if cohort_n else 0.0)
            lower_counts.append(lower_n)

        # --- monotonicity guard on the conservative series -------------------
        violations: list[dict[str, Any]] = []
        for i in range(1, len(lower_series)):
            if lower_series[i] > lower_series[i - 1] + monotonicity_tolerance:
                violations.append(
                    {
                        "previous_segment": airing.segments[i - 1].index,
                        "segment": airing.segments[i].index,
                        "previous_lower_persons": lower_counts[i - 1],
                        "lower_persons": lower_counts[i],
                        "explanation": (
                            "dropping uncertain listeners independently per segment makes the "
                            "conservative retention rise, which a retention curve cannot do"
                        ),
                    }
                )
        status = "ok"
        if violations and require_monotone:
            status = "violated"
        elif violations:
            status = "reported_not_enforced"
        checks.append(
            {
                "check": "retention_monotonicity",
                "program_key": airing.program_key,
                "station": airing.station,
                "air_date": airing.air_date.isoformat(),
                "status": status,
                "require_monotone": require_monotone,
                "lower_bound_series": lower_series,
                "violations": violations,
            }
        )

        # --- record-level labels --------------------------------------------
        for person in persons:
            pid = person["person_id"]
            for labelled in person_records[pid]:
                rec = labelled["record"]
                cls = labelled["classification"]
                touched = labelled["touched"]
                if cls == CLASS_UNASSIGNABLE:
                    row_segments = [None]
                else:
                    row_segments = touched
                for seg_index in row_segments:
                    frac = None
                    if seg_index is not None and cls != CLASS_UNASSIGNABLE:
                        seg = airing.segments[seg_index]
                        frac = round(
                            _expected_overlap_probability(rec, seg, labelled["granularity"]), 6
                        )
                    record_rows.append(
                        {
                            "program_key": airing.program_key,
                            "air_date": airing.air_date,
                            "station": airing.station,
                            "person_id": pid,
                            "record_id": rec.get("record_id"),
                            "record_start_utc": rec["start"],
                            "record_end_utc": rec["end"],
                            "granularity_minutes": labelled["granularity"],
                            "classification": cls,
                            "segment_index": seg_index,
                            "coverage_fraction": frac,
                            "reason": labelled["reason"],
                        }
                    )

        included_segments: dict[int, int] = {}
        for person in persons:
            for seg_index in person_segments[person["person_id"]]:
                included_segments[seg_index] = included_segments.get(seg_index, 0) + 1

        per_airing.append(
            {
                "program_key": airing.program_key,
                "title": airing.title,
                "station": airing.station,
                "air_date": airing.air_date.isoformat(),
                "version": airing.version,
                "version_basis": airing.basis,
                "confirmation_seq": airing.confirmation_seq,
                "delay_minutes": round(airing.delay_minutes, 3),
                "planned_start_utc": airing.planned_start.isoformat(),
                "planned_end_utc": airing.planned_end.isoformat(),
                "effective_start_utc": airing.effective_start.isoformat(),
                "effective_end_utc": airing.effective_end.isoformat(),
                "notes": airing.notes,
                "cohort_persons": cohort_n,
                "cohort_weighted": round(cohort_w, 3) if cohort_w else None,
                "segments": segment_payloads,
            }
        )

    any_violation = any(c["status"] == "violated" for c in checks)
    return {
        "unit_minutes": unit_minutes,
        "model": model,
        "airings": per_airing,
        "records": record_rows,
        "checks": checks,
        "monotonicity_satisfied": not any_violation,
        "status": "violated" if any_violation else "ok",
    }


def retention_hash_payload(result: dict[str, Any]) -> dict[str, Any]:
    """The replay payload: the computed curve and record labels, canonically."""
    return {
        "unit_minutes": result["unit_minutes"],
        "model": result["model"],
        "airings": [
            {
                "program_key": a["program_key"],
                "station": a["station"],
                "air_date": a["air_date"],
                "version": a["version"],
                "delay_minutes": a["delay_minutes"],
                "segments": [
                    {
                        "segment_index": s["segment_index"],
                        "start_utc": s["start_utc"],
                        "end_utc": s["end_utc"],
                        "hard_bounds": s["hard_bounds"],
                        "model_share": s["model_estimate"].get("share"),
                    }
                    for s in a["segments"]
                ],
            }
            for a in result["airings"]
        ],
        "checks": result["checks"],
        "record_labels": [
            {
                "person_id": r["person_id"],
                "program_key": r["program_key"],
                "station": r["station"],
                "air_date": r["air_date"],
                "start": r["record_start_utc"].isoformat(),
                "end": r["record_end_utc"].isoformat(),
                "classification": r["classification"],
                "segment_index": r["segment_index"],
            }
            for r in result["records"]
        ],
    }
