"""Reset and seed a database schema from the fictional fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app import ingest  # noqa: E402
from demo import fixtures  # noqa: E402


def reset_schema(db: Database) -> None:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tablename FROM pg_tables WHERE schemaname = %s
            """,
            (db.schema,),
        )
        tables = [r["tablename"] for r in cur.fetchall()]
        if tables:
            cur.execute(f'DROP TABLE IF EXISTS {", ".join(chr(34)+t+chr(34) for t in tables)} CASCADE')
        conn.commit()
    db.bootstrap()


def seed(db: Database, include_unknown_probability: bool = True) -> dict:
    """Seed batches in a fixed order.

    The first recruitment batch is the *usable* frame: every person has a known
    inclusion probability. A second batch carries the same frame plus one
    person whose probability is unknown, so the refusal path can be shown
    without contaminating the main analysis run.
    """
    actor = "seed"
    clean = ingest.create_recruitment_batch(
        db, fixtures.recruitment_batch(include_unknown_probability=False, batch_ref="frame-v1"), actor
    )
    with_unknown = ingest.create_recruitment_batch(
        db,
        fixtures.recruitment_batch(include_unknown_probability=True, batch_ref="frame-with-unknown"),
        actor,
    )
    tgt = ingest.create_target_batch(db, fixtures.target_batch(), actor)
    prg = ingest.create_program_batch(db, fixtures.program_batch(), actor)
    return {
        "recruitment_batch_seq": clean["batch_seq"],
        "recruitment_unknown_probability_seq": with_unknown["batch_seq"],
        "target_batch_seq": tgt["batch_seq"],
        "program_batch_seq": prg["batch_seq"],
    }


def ingest_diaries(db: Database, tz, include_resubmission: bool = True) -> dict:
    """Insert all initial diaries, then the S701 revision 2 resubmission."""
    count = 0
    for sub in fixtures.diary_submissions():
        ingest.create_diary_submission(db, sub, "seed", tz)
        count += 1
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        initial_cutoff = cur.fetchone()["m"]
    resubmission = None
    if include_resubmission:
        resubmission = ingest.create_diary_submission(db, fixtures.S701_RESUBMISSION, "seed", tz)
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        latest_cutoff = cur.fetchone()["m"]
    return {
        "initial_submissions": count,
        "initial_cutoff": initial_cutoff,
        "resubmission": resubmission,
        "latest_cutoff": latest_cutoff,
    }


def seed_retention(db: Database, tz) -> dict:
    """Seed the separate segment-retention dataset (own frame/schedule/diaries).

    Kept apart from the main dataset so the 0-1 numbers are unchanged. Returns
    the pinned sequences a retention run needs.
    """
    from demo import retention_fixtures as rf

    frame = ingest.create_recruitment_batch(db, rf.retention_recruitment_batch(), "seed")
    tgt = ingest.create_target_batch(db, rf.retention_target_batch(), "seed")
    prg = ingest.create_program_batch(db, rf.retention_program_batch(), "seed")
    seg = ingest.create_segment_batch(db, rf.retention_segment_batch(), "seed")
    rise_prg = ingest.create_program_batch(db, rf.retention_program_batch_rise(), "seed")
    rise_seg = ingest.create_segment_batch(db, rf.rising_segment_batch(), "seed")
    for sub in rf.retention_diaries():
        ingest.create_diary_submission(db, sub, "seed", tz)
    with db.scoped() as conn:
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


def db_has_data(db: Database) -> bool:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM recruitment_batches")
        return cur.fetchone()["c"] > 0


def retention_already_seeded(db: Database) -> bool:
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM segment_batches")
        return cur.fetchone()["c"] > 0


def main() -> None:
    settings = get_settings()
    db = Database(settings)
    db.open()
    if "--reset" in sys.argv:
        reset_schema(db)
    if db_has_data(db):
        # Seeding is insert-only; a second call must not duplicate the frame or
        # collide with existing diary revisions. Use --reset to start over.
        # The retention dataset lives alongside the main one and can still be
        # added to a database that predates it.
        if not retention_already_seeded(db):
            from zoneinfo import ZoneInfo

            retention = seed_retention(db, ZoneInfo(settings.service_timezone))
            print({"retention": retention, "note": "added retention dataset"})
        else:
            print("database already seeded; pass --reset to rebuild the schema")
        db.close()
        return
    seeded = seed(db)
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.service_timezone)
    diaries = ingest_diaries(db, tz)
    retention = seed_retention(db, tz)
    print({"seeded": seeded, "diaries": diaries, "retention": retention})
    db.close()


if __name__ == "__main__":
    main()
