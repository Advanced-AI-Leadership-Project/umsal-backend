"""MVP recordings, uploads, analysis jobs, speaker assignments and deletions (D8, #72).

Design: docs/mvp-recording-design.md (5, 9). PostgreSQL holds every state and decides
what may be shown; DynamoDB holds only result bodies (``REC#<recording_id>``) and S3
only the audio (``recordings/<recording_id>/source``).

* ``recordings`` keeps both participants of the connection at upload time, so either
  can still see the status and delete it after a disconnect. Deletion is a tombstone
  (``deleted_at``) first; derived rows stay without content.
* ``upload_sessions`` is one presigned upload per recording; finalize once.
* ``analysis_jobs`` follows the contract stage/status enums. A job is claimed with a
  short lease, so a Lambda that dies after claiming is re-claimed later.
* ``speaker_assignments`` is versioned per recording (optimistic lock).
* ``deletion_jobs`` is one deletion per recording (either participant may ask).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

RECORDING_STATUSES = (
    "UPLOADING",
    "PREPARING_INPUT",
    "QUEUED_AUDIO",
    "PROCESSING_AUDIO",
    "NEEDS_SPEAKER_CONFIRMATION",
    "QUEUED_TEXT",
    "PROCESSING_TEXT",
    "COMPLETED",
    "ABSTAINED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
)
JOB_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "ABSTAINED", "FAILED", "CANCELLED", "EXPIRED")
CONTENT_TYPES = ("audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a")
ID = "^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE recordings (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'rec_' || replace(gen_random_uuid()::text, '-', ''),
            connection_id text COLLATE "C" NOT NULL REFERENCES connections (id),
            uploader_id text COLLATE "C" NOT NULL REFERENCES users (id),
            participant_a_id text COLLATE "C" NOT NULL REFERENCES users (id),
            participant_b_id text COLLATE "C" NOT NULL REFERENCES users (id),
            occurred_at timestamptz NOT NULL,
            filename text CHECK (filename IS NULL OR char_length(filename) BETWEEN 1 AND 200),
            content_type text NOT NULL CHECK (content_type IN ({_in(CONTENT_TYPES)})),
            size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 1 AND 52428800),
            sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{{64}}$'),
            s3_key text NOT NULL UNIQUE,
            s3_version_id text,
            status text NOT NULL DEFAULT 'UPLOADING'
                CHECK (status IN ({_in(RECORDING_STATUSES)})),
            failure_code text,
            speaker_assignment_version integer NOT NULL DEFAULT 0
                CHECK (speaker_assignment_version >= 0),
            speaker_intervals jsonb,
            current_job_id text COLLATE "C",
            demo boolean NOT NULL DEFAULT false,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT recordings_two_people CHECK (participant_a_id < participant_b_id),
            CONSTRAINT recordings_uploader_participates
                CHECK (uploader_id IN (participant_a_id, participant_b_id))
        )
        """
    )
    op.execute(
        "CREATE INDEX recordings_by_participant_a ON recordings "
        "(participant_a_id, created_at DESC, id DESC) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX recordings_by_participant_b ON recordings "
        "(participant_b_id, created_at DESC, id DESC) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX recordings_by_connection ON recordings (connection_id)")
    op.execute(
        """
        CREATE TABLE upload_sessions (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'upl_' || replace(gen_random_uuid()::text, '-', ''),
            recording_id text COLLATE "C" NOT NULL UNIQUE REFERENCES recordings (id),
            owner_id text COLLATE "C" NOT NULL REFERENCES users (id),
            expires_at timestamptz NOT NULL,
            finalized_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX upload_sessions_open ON upload_sessions (owner_id, expires_at) "
        "WHERE finalized_at IS NULL"
    )
    op.execute(
        "CREATE INDEX upload_sessions_expiring ON upload_sessions (expires_at) "
        "WHERE finalized_at IS NULL"
    )
    op.execute(
        f"""
        CREATE TABLE analysis_jobs (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'job_' || replace(gen_random_uuid()::text, '-', ''),
            recording_id text COLLATE "C" NOT NULL REFERENCES recordings (id),
            stage text NOT NULL CHECK (stage IN ('AUDIO_PREP', 'TEXT_ANALYSIS')),
            status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ({_in(JOB_STATUSES)})),
            attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            lease_expires_at timestamptz,
            pipeline_version text NOT NULL,
            assignment_version integer NOT NULL DEFAULT 0 CHECK (assignment_version >= 0),
            failure_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT analysis_jobs_once
                UNIQUE (recording_id, stage, assignment_version, pipeline_version)
        )
        """
    )
    op.execute(
        "CREATE INDEX analysis_jobs_pending ON analysis_jobs (status, created_at) "
        "WHERE status IN ('QUEUED', 'RUNNING')"
    )
    op.execute(
        """
        ALTER TABLE recordings ADD CONSTRAINT recordings_current_job
            FOREIGN KEY (current_job_id) REFERENCES analysis_jobs (id)
        """
    )
    op.execute(
        f"""
        CREATE TABLE speaker_assignments (
            recording_id text COLLATE "C" NOT NULL REFERENCES recordings (id),
            version integer NOT NULL CHECK (version >= 1),
            target_subject_id text COLLATE "C" NOT NULL REFERENCES users (id),
            speaker_id text COLLATE "C" CHECK (speaker_id IS NULL OR speaker_id ~ '{ID}'),
            decision text NOT NULL CHECK (decision IN ('CONFIRMED', 'UNSURE')),
            confirmed_by text COLLATE "C" NOT NULL REFERENCES users (id),
            confirmed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (recording_id, version),
            CONSTRAINT speaker_assignments_decision
                CHECK ((decision = 'CONFIRMED') = (speaker_id IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE deletion_jobs (
            id text COLLATE "C" PRIMARY KEY
                DEFAULT 'del_' || replace(gen_random_uuid()::text, '-', ''),
            scope text NOT NULL DEFAULT 'RECORDING' CHECK (scope = 'RECORDING'),
            recording_id text COLLATE "C" NOT NULL UNIQUE REFERENCES recordings (id),
            requested_by text COLLATE "C" NOT NULL REFERENCES users (id),
            live_deletion_status text NOT NULL DEFAULT 'PENDING'
                CHECK (live_deletion_status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
            live_deleted_at timestamptz,
            last_error_code text CHECK (last_error_code IS NULL OR last_error_code IN
                ('TEMPORARY_STORAGE_ERROR', 'DEPENDENT_JOB_RUNNING', 'BACKUP_POLICY_PENDING')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at timestamptz,
            sweep_after timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX deletion_jobs_open ON deletion_jobs (next_attempt_at) "
        "WHERE live_deletion_status <> 'COMPLETED'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE deletion_jobs")
    op.execute("DROP TABLE speaker_assignments")
    op.execute("ALTER TABLE recordings DROP CONSTRAINT recordings_current_job")
    op.execute("DROP TABLE analysis_jobs")
    op.execute("DROP TABLE upload_sessions")
    op.execute("DROP TABLE recordings")
