"""Consent grants per connection and direction (FR-03, design doc "consent_grants").

One row per (connection, subject): what the subject allows for this connection —
analysis of their speech, which result scopes the recipient may see, and whether the
recipient may see a short verification preview. Rows start fully denied (version 1)
when a connection is created; only the subject can change their own row.

Not enforced in the database (the API writes only derived values): that subject and
recipient are the connection's participants, and that sharing_scopes has no duplicates
(the API stores a canonical, de-duplicated order).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE consent_grants (
            connection_id text COLLATE "C" NOT NULL REFERENCES connections (id),
            subject_id text COLLATE "C" NOT NULL REFERENCES users (id),
            recipient_id text COLLATE "C" NOT NULL REFERENCES users (id),
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            analysis_allowed boolean NOT NULL DEFAULT false,
            sharing_scopes text[] NOT NULL DEFAULT '{}'
                CHECK (sharing_scopes <@ ARRAY['summary', 'repetition', 'evidence']::text[]),
            verification_preview_allowed boolean NOT NULL DEFAULT false,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id, subject_id),
            CONSTRAINT consent_grants_two_people CHECK (subject_id <> recipient_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE consent_grants")
