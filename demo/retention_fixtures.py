"""Fictional fixtures dedicated to segment-level retention.

Kept separate from ``fixtures.py`` so the existing 0-1 dataset (and its tests)
are untouched. Everything here is constructed, not real.

Scenario
--------
* One-day survey (2026-09-01, Asia/Shanghai), 5-minute recording granularity.
  A record is definite for a segment only when its reported endpoints, padded
  by +/- half a unit, stay wholly inside that segment.
* A 65-person frame spread over every (age x region x sex) target cell so
  bounded raking converges without a special-case target.
* ``seg_show`` on RADIO-S 20:00-21:00 cut into six 15-minute segments. A small
  cohort listens for progressively fewer segments; one listener straddles the
  20:15 boundary (possible, not definite); everyone else returns a filled
  full-day diary with no listening (confirmed no-listening, not missing).
* ``seg_late`` on RADIO-L 23:30-24:30 with a segment that crosses the actual
  midnight boundary, to check the day split.
* ``seg_rise`` on RADIO-R 10:00-10:30: a two-person cohort whose conservative
  retention *rises* from segment 0 to segment 1, used to prove the publisher
  refuses a non-monotone curve.
* A delayed-broadcast confirmation shifts ``seg_show`` by +20 minutes, which
  invalidates the planned boundaries and forces a new analysis version.
* A revised schedule batch moves ``seg_show`` to a different slot, again a new
  version (never an in-place edit of a published report).
"""
from __future__ import annotations

from datetime import date, timedelta

from demo.fixtures import target_batch

TZ = "Asia/Shanghai"
SURVEY_DAY = date(2026, 9, 1)
SURVEY_END = SURVEY_DAY
NEXT_DAY = SURVEY_DAY + timedelta(days=1)

AGE_BANDS = ["18-24", "25-44", "45-64", "65+"]
REGIONS = ["north", "south"]
SEXES = ["F", "M"]

UNIT_MINUTES = 5
BASE_P = 0.01  # every frame person: design weight 100


def _at(day: date, hh: int, mm: int = 0) -> str:
    return f"{day.isoformat()}T{hh:02d}:{mm:02d}:00+08:00"


def _day_window(day: date) -> dict[str, str]:
    return {"start": _at(day, 0), "end": _at(day + timedelta(days=1), 0)}


def _entry(start: str, end: str, station: str, serial: str) -> dict:
    return {"start": start, "end": end, "station": station, "source_serial": serial}


def _t(day: date, hh: int, mm: int, ss: int = 0) -> str:
    """A local timestamp; minutes/seconds may exceed 59 for cross-midnight offsets."""
    return (f"{day.isoformat()}T{hh:02d}:{mm:02d}:{ss:02d}+08:00")


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------
def _generic_persons() -> list[dict]:
    rows = []
    idx = 0
    for age in AGE_BANDS:
        for region in REGIONS:
            for sex in SEXES:
                for _ in range(3):
                    idx += 1
                    rows.append(
                        {
                            "person_id": f"R{idx:03d}",
                            "household_id": f"RH{idx:03d}",
                            "stratum": "urban" if idx % 2 else "rural",
                            "age_band": age,
                            "region": region,
                            "sex": sex,
                            "_records": [],
                        }
                    )
    return rows


def _special_persons() -> list[dict]:
    def seg_records(pid: str, times: list[tuple[date, int, int, int, int]], station: str) -> list[dict]:
        return [
            _entry(_t(day, hh, mm), _t(eday, ehh, emm), station, f"{pid}-{i}")
            for i, (day, hh, mm, eday, ehh, emm) in enumerate(times)
        ]

    specs: list[tuple[str, list[dict]]] = []
    # G1: definite in segments 0, 1, 2 (padded spans stay inside each segment).
    for pid in ("SH10", "SH11", "SH12", "SH13"):
        specs.append(
            (
                pid,
                seg_records(
                    pid,
                    [
                        (SURVEY_DAY, 20, 3, SURVEY_DAY, 20, 12),
                        (SURVEY_DAY, 20, 18, SURVEY_DAY, 20, 27),
                        (SURVEY_DAY, 20, 33, SURVEY_DAY, 20, 42),
                    ],
                    "RADIO-S",
                ),
            )
        )
    # G2: definite in segments 0 and 1.
    for pid in ("SH20", "SH21", "SH22"):
        specs.append(
            (
                pid,
                seg_records(
                    pid,
                    [
                        (SURVEY_DAY, 20, 3, SURVEY_DAY, 20, 12),
                        (SURVEY_DAY, 20, 18, SURVEY_DAY, 20, 27),
                    ],
                    "RADIO-S",
                ),
            )
        )
    # G3/G4: definite only in segment 0.
    for pid in ("SH30", "SH31", "SH32", "SH40", "SH41"):
        specs.append(
            (pid, seg_records(pid, [(SURVEY_DAY, 20, 3, SURVEY_DAY, 20, 12)], "RADIO-S"))
        )
    # Boundary crosser: possible in segments 0 and 1, definite in neither.
    specs.append(
        (
            "SHCROSS",
            seg_records("SHCROSS", [(SURVEY_DAY, 20, 12, SURVEY_DAY, 20, 18)], "RADIO-S"),
        )
    )
    # Cross-midnight listeners: definite in both late segments.
    for pid in ("SL1", "SL2"):
        specs.append(
            (
                pid,
                seg_records(
                    pid,
                    [
                        (SURVEY_DAY, 23, 35, NEXT_DAY, 0, 5),
                        (NEXT_DAY, 0, 18, NEXT_DAY, 0, 25),
                    ],
                    "RADIO-L",
                ),
            )
        )
    # Rising cohort: SRISE1 definite in both; SRISE2 possible in seg 0, definite in seg 1.
    specs.append(
        (
            "SRISE1",
            seg_records(
                "SRISE1",
                [
                    (SURVEY_DAY, 10, 5, SURVEY_DAY, 10, 10),
                    (SURVEY_DAY, 10, 20, SURVEY_DAY, 10, 25),
                ],
                "RADIO-R",
            ),
        )
    )
    specs.append(
        (
            "SRISE2",
            seg_records(
                "SRISE2",
                [
                    (SURVEY_DAY, 10, 12, SURVEY_DAY, 10, 18),
                    (SURVEY_DAY, 10, 22, SURVEY_DAY, 10, 26),
                ],
                "RADIO-R",
            ),
        )
    )

    # A missing visit: the programme hour is not filled at all, so the person is
    # unassignable (incomplete diary) and is excluded, never counted as silent.
    specs.append(("SMISS", []))
    # An unresolved cross-station conflict during the show: unassignable until
    # an analyst resolves it; never resolved by upload order.
    specs.append(
        (
            "SCONF",
            [
                _entry(_t(SURVEY_DAY, 20, 3), _t(SURVEY_DAY, 20, 20), "RADIO-S", "SCONF-A"),
                _entry(_t(SURVEY_DAY, 20, 10), _t(SURVEY_DAY, 20, 25), "RADIO-X", "SCONF-B"),
            ],
        )
    )

    persons: list[dict] = []
    for i, (pid, records) in enumerate(specs):
        persons.append(
            {
                "person_id": pid,
                "household_id": f"RSH{i:03d}",
                "stratum": "urban" if i % 3 else "rural",
                "age_band": AGE_BANDS[i % len(AGE_BANDS)],
                "region": REGIONS[i % len(REGIONS)],
                "sex": SEXES[i % len(SEXES)],
                "_records": records,
            }
        )
    return persons


def retention_persons() -> list[dict]:
    return _generic_persons() + _special_persons()


def retention_recruitment_batch(batch_ref: str = "retention-frame-v1") -> dict:
    rows = [
        {
            "household_id": p["household_id"],
            "person_id": p["person_id"],
            "stratum": p["stratum"],
            "age_band": p["age_band"],
            "region": p["region"],
            "sex": p["sex"],
            "inclusion_probability": BASE_P,
        }
        for p in retention_persons()
    ]
    return {
        "batch_ref": batch_ref,
        "survey_period": {"start": SURVEY_DAY.isoformat(), "end": SURVEY_END.isoformat()},
        "rows": rows,
    }


def retention_target_batch(batch_ref: str = "retention-targets-v1") -> dict:
    return target_batch(batch_ref)


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------
def retention_program_batch(batch_ref: str = "retention-schedule-v1") -> dict:
    return {
        "batch_ref": batch_ref,
        "timezone": TZ,
        "airings": [
            {
                "program_key": "seg_show",
                "title": "Segment Show",
                "station": "RADIO-S",
                "air_date": SURVEY_DAY.isoformat(),
                "start_local": "20:00",
                "end_local": "21:00",
            },
            {
                "program_key": "seg_late",
                "title": "Late Segment",
                "station": "RADIO-L",
                "air_date": SURVEY_DAY.isoformat(),
                "start_local": "23:30",
                "end_local": "24:30",
            },
        ],
    }


def retention_program_batch_rise(batch_ref: str = "retention-schedule-rise") -> dict:
    return {
        "batch_ref": batch_ref,
        "timezone": TZ,
        "airings": [
            {
                "program_key": "seg_rise",
                "title": "Rising Retention",
                "station": "RADIO-R",
                "air_date": SURVEY_DAY.isoformat(),
                "start_local": "10:00",
                "end_local": "10:30",
            }
        ],
    }


def revised_program_batch(batch_ref: str = "retention-schedule-v2") -> dict:
    """A schedule revision: seg_show moves from 20:00-21:00 to 20:10-21:10."""
    batch = retention_program_batch(batch_ref)
    for airing in batch["airings"]:
        if airing["program_key"] == "seg_show":
            airing["start_local"] = "20:10"
            airing["end_local"] = "21:10"
    return batch


# ---------------------------------------------------------------------------
# Segment boundaries
# ---------------------------------------------------------------------------
def _segments(count: int, length: int, labels: list[str] | None = None) -> list[dict]:
    return [
        {
            "start_offset_min": i * length,
            "end_offset_min": (i + 1) * length,
            "label": labels[i] if labels and i < len(labels) else f"segment_{i}",
        }
        for i in range(count)
    ]


def retention_segment_batch(batch_ref: str = "segments-v1") -> dict:
    return {
        "batch_ref": batch_ref,
        "unit_minutes": UNIT_MINUTES,
        "granularity_note": "listening records are reported to the nearest 5 minutes",
        "airings": [
            {
                "program_key": "seg_show",
                "air_date": SURVEY_DAY.isoformat(),
                "station": "RADIO-S",
                "segments": _segments(4, 15, ["open", "talk", "ad", "close"]),
            },
            {
                "program_key": "seg_late",
                "air_date": SURVEY_DAY.isoformat(),
                "station": "RADIO-L",
                # One 45-minute segment that crosses the real midnight boundary.
                "segments": [
                    {"start_offset_min": 0, "end_offset_min": 45, "label": "across_midnight"},
                    {"start_offset_min": 45, "end_offset_min": 60, "label": "after_midnight"},
                ],
            },
        ],
    }


def rising_segment_batch(batch_ref: str = "segments-rising-v1") -> dict:
    return {
        "batch_ref": batch_ref,
        "unit_minutes": UNIT_MINUTES,
        "granularity_note": "two 15-minute segments",
        "airings": [
            {
                "program_key": "seg_rise",
                "air_date": SURVEY_DAY.isoformat(),
                "station": "RADIO-R",
                "segments": [
                    {"start_offset_min": 0, "end_offset_min": 15, "label": "early"},
                    {"start_offset_min": 15, "end_offset_min": 30, "label": "late"},
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------
def delayed_confirmation() -> dict:
    """seg_show confirmed as aired 20 minutes late (20:20-21:20)."""
    return {
        "program_key": "seg_show",
        "air_date": SURVEY_DAY.isoformat(),
        "station": "RADIO-S",
        "confirmed_start": _at(SURVEY_DAY, 20, 20),
        "confirmed_end": _at(SURVEY_DAY, 21, 20),
        "basis": "as_aired_log",
        "note": "studio feed delay",
    }


# ---------------------------------------------------------------------------
# Diaries
# ---------------------------------------------------------------------------
def _coverage_for(person_id: str) -> list[dict]:
    if person_id == "SMISS":
        # Missing visit: 20:00-21:00 of the programme day is left unfilled.
        return [
            {"start": _at(SURVEY_DAY, 0), "end": _at(SURVEY_DAY, 20)},
            {"start": _at(SURVEY_DAY, 21), "end": _at(NEXT_DAY, 0)},
        ]
    return [_day_window(SURVEY_DAY)]


def retention_diaries() -> list[dict]:
    out = []
    for person in retention_persons():
        out.append(
            {
                "person_id": person["person_id"],
                "household_id": person["household_id"],
                "revision": 1,
                "submitted_at": _at(NEXT_DAY, 8),
                "coverage": _coverage_for(person["person_id"]),
                "entries": [
                    {
                        "start": r["start"],
                        "end": r["end"],
                        "station": r["station"],
                        "source_serial": r["source_serial"],
                    }
                    for r in person["_records"]
                ],
            }
        )
    return out
