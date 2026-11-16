"""Input ingestion: recruitment, population targets, program schedule and diaries.

All ingestion is append-only. Clinic-style validation runs before anything is
persisted so that a bad upload cannot partially pollute a batch.
"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from .analytics.diary import parse_coverage, parse_entries
from .db import Database
from .errors import conflict, invalid, not_found
from .jsonutil import jb
from .timeutil import parse_aware, slot_interval

DIMENSIONS = ("age_band", "region", "sex")


def _audit(cur, actor: str, action: str, entity: str, entity_id: str | None, details: dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,%s,%s,%s)",
        (actor, action, entity, entity_id, jb(details)),
    )


def _next_batch_seq_from_ref(cur, table: str, batch_ref: str | None) -> int | None:
    if batch_ref is None:
        return None
    cur.execute(f"SELECT batch_seq FROM {table} WHERE batch_ref = %s", (batch_ref,))
    row = cur.fetchone()
    return None if row is None else row["batch_seq"]


# ---------------------------------------------------------------------------
# Recruitment
# ---------------------------------------------------------------------------
def create_recruitment_batch(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    period_start = payload["survey_period"]["start"]
    period_end = payload["survey_period"]["end"]
    if period_end < period_start:
        raise invalid("recruitment_period_order", "survey_period.end must not precede start")
    rows = payload["rows"]
    seen_person: set[str] = set()
    seen_household_person: set[tuple[str, str]] = set()
    normalized = []
    unknown_probability: list[str] = []
    for idx, row in enumerate(rows):
        person = row["person_id"].strip()
        household = row["household_id"].strip()
        if person in seen_person:
            raise invalid(
                "recruitment_duplicate_person",
                "person_id must be unique inside a recruitment batch",
                {"person_id": person, "row": idx},
            )
        seen_person.add(person)
        seen_household_person.add((household, person))
        prob = row.get("inclusion_probability")
        if prob is None:
            unknown_probability.append(person)
        normalized.append(
            {
                "household_id": household,
                "person_id": person,
                "strata": row["stratum"].strip(),
                "age_band": row["age_band"].strip(),
                "region": row["region"].strip(),
                "sex": row["sex"].strip(),
                "inclusion_probability": prob,
                "household_size": row.get("household_size"),
                "notes": row.get("notes"),
            }
        )
    with db.scoped() as conn:
        cur = conn.cursor()
        existing = _next_batch_seq_from_ref(cur, "recruitment_batches", payload.get("batch_ref"))
        if existing is not None:
            return {"batch_seq": existing, "idempotent": True}
        cur.execute(
            """
            INSERT INTO recruitment_batches
                (batch_ref, actor, survey_period_start, survey_period_end, row_count)
            VALUES (%s,%s,%s,%s,%s) RETURNING batch_seq, received_at
            """,
            (payload.get("batch_ref"), actor, period_start, period_end, len(normalized)),
        )
        head = cur.fetchone()
        batch_seq = head["batch_seq"]
        for row in normalized:
            cur.execute(
                """
                INSERT INTO recruitment_rows
                    (batch_seq, household_id, person_id, strata, age_band, region, sex,
                     inclusion_probability, household_size, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    batch_seq,
                    row["household_id"],
                    row["person_id"],
                    row["strata"],
                    row["age_band"],
                    row["region"],
                    row["sex"],
                    row["inclusion_probability"],
                    row["household_size"],
                    row["notes"],
                ),
            )
        _audit(
            cur,
            actor,
            "recruitment.batch.create",
            "recruitment_batch",
            str(batch_seq),
            {
                "row_count": len(normalized),
                "households": len({r["household_id"] for r in normalized}),
                "unknown_probability_persons": unknown_probability,
            },
        )
        conn.commit()
    return {
        "batch_seq": batch_seq,
        "row_count": len(normalized),
        "household_count": len({r["household_id"] for r in normalized}),
        "unknown_probability_persons": unknown_probability,
        "inclusion_probability_required": True,
        "idempotent": False,
    }


def load_recruitment(cur, batch_seq: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM recruitment_batches WHERE batch_seq = %s", (batch_seq,))
    batch = cur.fetchone()
    if batch is None:
        raise not_found("recruitment_batch_not_found", f"recruitment batch {batch_seq} not found")
    cur.execute("SELECT * FROM recruitment_rows WHERE batch_seq = %s ORDER BY person_id", (batch_seq,))
    rows = cur.fetchall()
    return {**batch, "rows": rows}


# ---------------------------------------------------------------------------
# Population targets
# ---------------------------------------------------------------------------
def create_target_batch(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    total = int(payload["population_total"])
    margins = payload["margins"]
    missing = [d for d in DIMENSIONS if d not in margins]
    if missing:
        raise invalid(
            "target_dimension_missing",
            "every analysis dimension needs known margins",
            {"missing": missing, "required": list(DIMENSIONS)},
        )
    extra = [d for d in margins if d not in DIMENSIONS]
    if extra:
        raise invalid("target_dimension_unknown", "unknown target dimension", {"extra": extra})
    dimension_totals = {}
    for dim, cats in margins.items():
        if not cats:
            raise invalid("target_dimension_empty", f"dimension {dim} has no categories")
        s = 0.0
        for cat, value in cats.items():
            if value is None or value < 0:
                raise invalid("target_value_invalid", f"target for {dim}={cat} must be non-negative")
            s += float(value)
        dimension_totals[dim] = round(s, 6)
    conflicting = {
        dim: v for dim, v in dimension_totals.items() if abs(v - total) > 1e-6
    }
    if conflicting:
        raise invalid(
            "target_totals_inconsistent",
            "dimension margins must all sum to the same population total",
            {"population_total": total, "dimension_totals": dimension_totals, "conflicting": conflicting},
        )
    with db.scoped() as conn:
        cur = conn.cursor()
        existing = _next_batch_seq_from_ref(cur, "target_batches", payload.get("batch_ref"))
        if existing is not None:
            return {"batch_seq": existing, "idempotent": True, "dimension_totals": dimension_totals}
        cur.execute(
            """
            INSERT INTO target_batches (batch_ref, actor, population_total, dimensions, dimension_totals)
            VALUES (%s,%s,%s,%s,%s) RETURNING batch_seq
            """,
            (payload.get("batch_ref"), actor, total, jb(margins), jb(dimension_totals)),
        )
        batch_seq = cur.fetchone()["batch_seq"]
        _audit(cur, actor, "target.batch.create", "target_batch", str(batch_seq), {"dimension_totals": dimension_totals})
        conn.commit()
    return {"batch_seq": batch_seq, "population_total": total, "dimension_totals": dimension_totals, "idempotent": False}


def load_targets(cur, batch_seq: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM target_batches WHERE batch_seq = %s", (batch_seq,))
    row = cur.fetchone()
    if row is None:
        raise not_found("target_batch_not_found", f"target batch {batch_seq} not found")
    return row


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------
def create_program_batch(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    try:
        tz = ZoneInfo(payload.get("timezone") or "Asia/Shanghai")
    except ZoneInfoNotFoundError as exc:
        raise invalid("timezone_unknown", "unknown IANA timezone", {"timezone": payload.get("timezone")}) from exc
    airings = []
    seen = set()
    for idx, item in enumerate(payload["airings"]):
        key = (item["program_key"], item["station"], item["air_date"], item["start_local"])
        if key in seen:
            raise invalid(
                "program_duplicate_airing",
                "duplicate airing inside batch",
                {"row": idx, "program_key": item["program_key"], "station": item["station"]},
            )
        seen.add(key)
        try:
            start_utc, end_utc = slot_interval(
                item["air_date"], item["start_local"], item["end_local"], tz
            )
        except ValueError as exc:
            raise invalid("program_slot_invalid", str(exc), {"row": idx}) from exc
        airings.append(
            {
                "program_key": item["program_key"],
                "title": item["title"],
                "station": item["station"],
                "air_date": item["air_date"],
                "start_local": item["start_local"],
                "end_local": item["end_local"],
                "start_utc": start_utc,
                "end_utc": end_utc,
            }
        )
    with db.scoped() as conn:
        cur = conn.cursor()
        existing = _next_batch_seq_from_ref(cur, "program_batches", payload.get("batch_ref"))
        if existing is not None:
            return {"batch_seq": existing, "idempotent": True}
        cur.execute(
            """
            INSERT INTO program_batches (batch_ref, actor, timezone, row_count)
            VALUES (%s,%s,%s,%s) RETURNING batch_seq
            """,
            (payload.get("batch_ref"), actor, str(tz), len(airings)),
        )
        batch_seq = cur.fetchone()["batch_seq"]
        for a in airings:
            cur.execute(
                """
                INSERT INTO program_airings
                    (batch_seq, program_key, title, station, air_date, start_local, end_local,
                     start_utc, end_utc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    batch_seq,
                    a["program_key"],
                    a["title"],
                    a["station"],
                    a["air_date"],
                    a["start_local"],
                    a["end_local"],
                    a["start_utc"],
                    a["end_utc"],
                ),
            )
        _audit(
            cur,
            actor,
            "program.batch.create",
            "program_batch",
            str(batch_seq),
            {"rows": len(airings), "programs": sorted({a["program_key"] for a in airings})},
        )
        conn.commit()
    return {"batch_seq": batch_seq, "airing_count": len(airings), "timezone": str(tz), "idempotent": False}


def load_programs(cur, batch_seq: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM program_batches WHERE batch_seq = %s", (batch_seq,))
    batch = cur.fetchone()
    if batch is None:
        raise not_found("program_batch_not_found", f"program batch {batch_seq} not found")
    cur.execute(
        "SELECT * FROM program_airings WHERE batch_seq = %s ORDER BY start_utc, program_key, station",
        (batch_seq,),
    )
    return {**batch, "airings": cur.fetchall()}


# ---------------------------------------------------------------------------
# Diary submissions
# ---------------------------------------------------------------------------
def create_diary_submission(db: Database, payload: dict[str, Any], actor: str, tz: ZoneInfo) -> dict[str, Any]:
    submitted_at = parse_aware(payload["submitted_at"], "submitted_at")
    coverage = parse_coverage(payload.get("coverage", []))
    entries = parse_entries(payload.get("entries", []), payload["revision"])
    scope = payload.get("replacement_scope") or {"type": "append"}
    if scope.get("type") == "replace":
        if not scope.get("start") or not scope.get("end"):
            raise invalid("diary_scope_invalid", "replace scope requires start and end")
        ss = parse_aware(scope["start"], "replacement_scope.start")
        se = parse_aware(scope["end"], "replacement_scope.end")
        if se <= ss:
            raise invalid("diary_scope_invalid", "replacement_scope.end must be after start")
        local_s, local_e = ss.astimezone(tz), se.astimezone(tz)
        if local_s.date() != local_e.date():
            s_mid = (local_s.hour, local_s.minute, local_s.second, local_s.microsecond) == (0, 0, 0, 0)
            e_mid = (local_e.hour, local_e.minute, local_e.second, local_e.microsecond) == (0, 0, 0, 0)
            if not (s_mid and e_mid):
                raise invalid(
                    "diary_partial_day_replacement",
                    "replacement spanning several survey days must cover whole survey days",
                    {"scope_start_local": local_s.isoformat(), "scope_end_local": local_e.isoformat()},
                )

    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT submission_seq, revision, household_id
            FROM diary_submissions WHERE person_id = %s
            ORDER BY submission_seq DESC LIMIT 1
            """,
            (payload["person_id"],),
        )
        prev = cur.fetchone()
        if prev is not None and payload["revision"] <= prev["revision"]:
            raise conflict(
                "diary_revision_not_increasing",
                "revision must strictly increase over the latest submission",
                {"previous_revision": prev["revision"], "previous_seq": prev["submission_seq"]},
            )
        if prev is not None and prev["household_id"] != payload["household_id"]:
            raise invalid("diary_household_mismatch", "household_id must match earlier submissions")

        cur.execute(
            """
            INSERT INTO diary_submissions
                (person_id, household_id, revision, replacement_scope, coverage, submitted_at, actor, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING submission_seq
            """,
            (
                payload["person_id"],
                payload["household_id"],
                payload["revision"],
                jb(scope),
                jb(payload.get("coverage", [])),
                submitted_at,
                actor,
                jb(payload),
            ),
        )
        submission_seq = cur.fetchone()["submission_seq"]

        superseded = 0
        if scope.get("type") == "replace":
            cur.execute(
                """
                UPDATE diary_entries
                   SET superseded_by_submission_seq = %s
                 WHERE person_id = %s
                   AND superseded_by_submission_seq IS NULL
                   AND start_utc < %s AND end_utc > %s
                """,
                (submission_seq, payload["person_id"], scope["end"], scope["start"]),
            )
            superseded = cur.rowcount
            cur.execute(
                """
                DELETE FROM diary_coverage_windows
                 WHERE submission_seq = %s AND start_utc < %s AND end_utc > %s
                """,
                (submission_seq, scope["end"], scope["start"]),
            )
            # Coverage windows are informational; exact bookkeeping happens at
            # run time against the raw submission history.

        for entry in entries:
            cur.execute(
                """
                INSERT INTO diary_entries
                    (submission_seq, person_id, household_id, start_raw, end_raw, start_utc,
                     end_utc, station, source_serial, revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING entry_id
                """,
                (
                    submission_seq,
                    payload["person_id"],
                    payload["household_id"],
                    entry["start_raw"],
                    entry["end_raw"],
                    entry["start"],
                    entry["end"],
                    entry["station"],
                    entry["source_serial"],
                    entry["revision"],
                ),
            )
            entry["entry_id"] = cur.fetchone()["entry_id"]

        for s, e in coverage:
            cur.execute(
                "INSERT INTO diary_coverage_windows (submission_seq, start_utc, end_utc) VALUES (%s,%s,%s)",
                (submission_seq, s, e),
            )

        _audit(
            cur,
            actor,
            "diary.submission.create",
            "diary_submission",
            str(submission_seq),
            {
                "person_id": payload["person_id"],
                "revision": payload["revision"],
                "scope": scope,
                "entries": len(entries),
                "superseded_entries": superseded,
            },
        )
        conn.commit()
    return {
        "submission_seq": submission_seq,
        "person_id": payload["person_id"],
        "revision": payload["revision"],
        "entry_count": len(entries),
        "coverage_window_count": len(coverage),
        "superseded_entry_rows": superseded,
        "replacement_scope": scope,
    }


def load_submissions(cur, seq_cutoff: int) -> dict[str, list[dict[str, Any]]]:
    cur.execute(
        """
        SELECT * FROM diary_submissions WHERE submission_seq <= %s
        ORDER BY person_id, submission_seq
        """,
        (seq_cutoff,),
    )
    submissions = cur.fetchall()
    if not submissions:
        return {}
    cur.execute(
        """
        SELECT * FROM diary_entries WHERE submission_seq <= %s
        ORDER BY submission_seq, entry_id
        """,
        (seq_cutoff,),
    )
    entries_by_sub: dict[int, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        entries_by_sub.setdefault(row["submission_seq"], []).append(row)
    by_person: dict[str, list[dict[str, Any]]] = {}
    for sub in submissions:
        rows = entries_by_sub.get(sub["submission_seq"], [])
        by_person.setdefault(sub["person_id"], []).append(
            {
                "submission_seq": sub["submission_seq"],
                "person_id": sub["person_id"],
                "household_id": sub["household_id"],
                "revision": sub["revision"],
                "replacement_scope": sub["replacement_scope"],
                "coverage": sub["coverage"],
                "submitted_at": sub["submitted_at"],
                "entries": [
                    {
                        "entry_id": r["entry_id"],
                        "start": r["start_raw"],
                        "end": r["end_raw"],
                        "station": r["station"],
                        "source_serial": r["source_serial"],
                        "revision": r["revision"],
                    }
                    for r in rows
                ],
            }
        )
    return by_person


# ---------------------------------------------------------------------------
# Segment boundaries (segment-level retention)
# ---------------------------------------------------------------------------
def create_segment_batch(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Store a declared segment-boundary batch for one or more airings.

    Boundaries are expressed as minute offsets from each airing's *effective*
    start, so the same offsets apply to the planned schedule and to a confirmed
    (delayed) version. A run pins the batch sequence plus the confirmation
    serials it treated as authoritative.
    """
    unit = float(payload["unit_minutes"])
    if unit <= 0:
        raise invalid("segment_unit_invalid", "unit_minutes must be positive")
    rows: list[dict[str, Any]] = []
    for idx, airing in enumerate(payload["airings"]):
        specs = sorted(airing["segments"], key=lambda s: float(s["start_offset_min"]))
        expected = 0.0
        for seg_no, spec in enumerate(specs):
            start = float(spec["start_offset_min"])
            end = float(spec["end_offset_min"])
            if end <= start:
                raise invalid(
                    "segment_boundary_invalid",
                    "segment end_offset_min must exceed start_offset_min",
                    {"airing": idx, "segment": seg_no},
                )
            if abs(start - expected) > 1e-9:
                raise invalid(
                    "segment_boundary_gap",
                    "segments must be contiguous from offset 0",
                    {"airing": idx, "segment": seg_no, "expected_start": expected, "start": start},
                )
            expected = end
            rows.append(
                {
                    "program_key": airing["program_key"],
                    "air_date": airing["air_date"],
                    "station": airing["station"],
                    "segment_index": seg_no,
                    "start_offset_min": start,
                    "end_offset_min": end,
                    "label": spec.get("label"),
                }
            )
    with db.scoped() as conn:
        cur = conn.cursor()
        existing = _next_batch_seq_from_ref(cur, "segment_batches", payload.get("batch_ref"))
        if existing is not None:
            return {"batch_seq": existing, "idempotent": True}
        cur.execute(
            """
            INSERT INTO segment_batches (batch_ref, actor, unit_minutes, granularity_note, row_count)
            VALUES (%s,%s,%s,%s,%s) RETURNING batch_seq
            """,
            (payload.get("batch_ref"), actor, unit, payload.get("granularity_note"), len(rows)),
        )
        batch_seq = cur.fetchone()["batch_seq"]
        for row in rows:
            cur.execute(
                """
                INSERT INTO segment_boundaries
                    (batch_seq, program_key, air_date, station, version, segment_index,
                     start_offset_min, end_offset_min, label)
                VALUES (%s,%s,%s,%s,'planned',%s,%s,%s,%s)
                """,
                (
                    batch_seq,
                    row["program_key"],
                    row["air_date"],
                    row["station"],
                    row["segment_index"],
                    row["start_offset_min"],
                    row["end_offset_min"],
                    row["label"],
                ),
            )
        _audit(
            cur,
            actor,
            "segment.batch.create",
            "segment_batch",
            str(batch_seq),
            {"rows": len(rows), "airings": len(payload["airings"]), "unit_minutes": unit},
        )
        conn.commit()
    return {
        "batch_seq": batch_seq,
        "airing_count": len(payload["airings"]),
        "segment_count": len(rows),
        "unit_minutes": unit,
        "idempotent": False,
    }


def load_segment_batch(cur, batch_seq: int) -> dict[str, Any]:
    cur.execute("SELECT * FROM segment_batches WHERE batch_seq = %s", (batch_seq,))
    batch = cur.fetchone()
    if batch is None:
        raise not_found("segment_batch_not_found", f"segment batch {batch_seq} not found")
    cur.execute(
        "SELECT * FROM segment_boundaries WHERE batch_seq = %s ORDER BY air_date, program_key, station, segment_index",
        (batch_seq,),
    )
    return {**batch, "boundaries": cur.fetchall()}


# ---------------------------------------------------------------------------
# Broadcast confirmations (as-aired versions)
# ---------------------------------------------------------------------------
def create_confirmation(db: Database, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Register the confirmed as-aired version of one airing (append-only).

    The start/end instants are stored as UTC. If the confirmation differs from
    the planned slot, every analysis that pins this serial uses the confirmed
    version, producing a *new* analysis version rather than silently editing an
    existing published report.
    """
    start = parse_aware(payload["confirmed_start"], "confirmed_start")
    end = parse_aware(payload["confirmed_end"], "confirmed_end")
    if end <= start:
        raise invalid("confirmation_order", "confirmed_end must be after confirmed_start")
    basis = payload.get("basis") or "analyst_confirmed"
    if basis not in ("as_aired_log", "encoder_log", "analyst_confirmed"):
        raise invalid("confirmation_basis_invalid", "unknown confirmation basis", {"basis": basis})
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO broadcast_confirmations
                (program_key, air_date, station, confirmed_start_utc, confirmed_end_utc,
                 basis, note, actor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (program_key, air_date, station, confirmed_start_utc, confirmed_end_utc)
            DO NOTHING
            RETURNING confirmation_seq
            """,
            (
                payload["program_key"],
                payload["air_date"],
                payload["station"],
                start,
                end,
                basis,
                payload.get("note"),
                actor,
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT confirmation_seq FROM broadcast_confirmations
                 WHERE program_key=%s AND air_date=%s AND station=%s
                   AND confirmed_start_utc=%s AND confirmed_end_utc=%s
                """,
                (payload["program_key"], payload["air_date"], payload["station"], start, end),
            )
            confirmation_seq = cur.fetchone()["confirmation_seq"]
            idempotent = True
        else:
            confirmation_seq = row["confirmation_seq"]
            idempotent = False
            _audit(
                cur,
                actor,
                "confirmation.create",
                "broadcast_confirmation",
                str(confirmation_seq),
                {"program_key": payload["program_key"], "start": start.isoformat(), "end": end.isoformat()},
            )
        conn.commit()
    return {
        "confirmation_seq": confirmation_seq,
        "program_key": payload["program_key"],
        "air_date": payload["air_date"],
        "station": payload["station"],
        "confirmed_start": start.isoformat().replace("+00:00", "Z"),
        "confirmed_end": end.isoformat().replace("+00:00", "Z"),
        "basis": basis,
        "idempotent": idempotent,
        "affects": "new runs only",
    }


def load_confirmations(
    cur, seqs: list[int]
) -> tuple[dict[tuple[str, Any, str], dict[str, Any]], list[int]]:
    """Load the pinned confirmations, keyed by (program_key, air_date, station).

    If two pinned serials target the same airing the later serial wins; callers
    that care should not pin conflicting versions for one airing. Returns the
    keyed version map plus the list of serials actually found.
    """
    if not seqs:
        return {}, []
    cur.execute(
        """
        SELECT confirmation_seq, program_key, air_date, station, confirmed_start_utc,
               confirmed_end_utc, basis, note
          FROM broadcast_confirmations
         WHERE confirmation_seq = ANY(%s)
         ORDER BY confirmation_seq
        """,
        (list(seqs),),
    )
    rows = cur.fetchall()
    out: dict[tuple[str, Any, str], dict[str, Any]] = {}
    for r in rows:
        out[(r["program_key"], r["air_date"], r["station"])] = {
            "seq": r["confirmation_seq"],
            "confirmed_start": r["confirmed_start_utc"],
            "confirmed_end": r["confirmed_end_utc"],
            "basis": r["basis"],
            "note": r["note"],
        }
    return out, [r["confirmation_seq"] for r in rows]
