"""Explicitly enabled synthetic CPU pilot and its durable job publication records."""

import os

import psycopg
from psycopg.types.json import Jsonb

from umsal_backend import analysis


def grant_versions(db: psycopg.Connection, connection_id: str) -> list[int] | None:
    """Lock the connection and both directional grants in a consistent order."""
    row = db.execute(
        "SELECT status FROM connections WHERE id = %s FOR SHARE", (connection_id,)
    ).fetchone()
    if row is None or row[0] != "ACTIVE":
        return None
    grants = db.execute(
        "SELECT version, analysis_allowed FROM consent_grants "
        "WHERE connection_id = %s ORDER BY subject_id FOR SHARE",
        (connection_id,),
    ).fetchall()
    if len(grants) != 2 or not all(allowed for _, allowed in grants):
        return None
    return [version for version, _ in grants]


def selected(demo: bool, participants: tuple[str, str]) -> bool:
    eligible = set(os.getenv("UMSAL_CPU_PILOT_USERS", "").split(",")) - {""}
    return demo and set(participants) <= eligible


def enqueue(
    db: psycopg.Connection,
    recording_id: str,
    stage: str,
    versions: list[int],
    assignment_version: int = 0,
) -> str:
    job_id = analysis.enqueue(
        db,
        recording_id,
        stage,
        assignment_version,
        pipeline_version=analysis.CPU_PIPELINE_VERSION,
    )
    db.execute(
        "INSERT INTO analysis_outbox (job_id, consent_versions) VALUES (%s, %s)",
        (job_id, Jsonb(versions)),
    )
    return job_id


def audio_snapshot(db: psycopg.Connection, recording_id: str) -> list[int] | None:
    row = db.execute(
        "SELECT o.consent_versions FROM analysis_outbox o "
        "JOIN analysis_jobs j ON j.id = o.job_id "
        "WHERE j.recording_id = %s AND j.stage = 'AUDIO_PREP' AND j.status = 'SUCCEEDED'",
        (recording_id,),
    ).fetchone()
    return row[0] if row else None
