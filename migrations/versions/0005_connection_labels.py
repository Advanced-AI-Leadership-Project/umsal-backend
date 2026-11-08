"""Per-connection labels: how each participant calls the other (D10, issue #62).

One row per (connection, owner): the name the owner uses for the other participant
of that connection, for example "엄마". A label is visible only to its owner. The
inviter may choose a label before the connection exists; it waits on the invitation
(``inviter_label``) and moves to ``connection_labels`` when the invitation is accepted.

Not enforced in the database: that owner_id is a participant of the connection (the
API writes only for the caller's own connections).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 1-16 characters after trimming, no control characters (the API normalizes first).
# An explicit C0/DEL/C1 range, identical to the API pattern: [[:cntrl:]] depends on the
# server locale and could reject a label the API accepted (a 500 instead of a 422).
LABEL_CHECK = (
    "char_length({col}) BETWEEN 1 AND 16 AND {col} = btrim({col})"
    " AND {col} !~ '[\\x01-\\x1f\\x7f-\\x9f]'"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE connection_labels (
            connection_id text COLLATE "C" NOT NULL REFERENCES connections (id),
            owner_id text COLLATE "C" NOT NULL REFERENCES users (id),
            label text NOT NULL CHECK ({LABEL_CHECK.format(col="label")}),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id, owner_id)
        )
        """
    )
    op.execute(
        f"""
        ALTER TABLE invitations ADD COLUMN inviter_label text
            CHECK (inviter_label IS NULL OR ({LABEL_CHECK.format(col="inviter_label")}))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE invitations DROP COLUMN inviter_label")
    op.execute("DROP TABLE connection_labels")
