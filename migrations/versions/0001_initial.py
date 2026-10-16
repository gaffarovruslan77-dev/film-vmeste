"""Начальная схема

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "movies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("wikidata_id", sa.String(32), nullable=False, unique=True),
        sa.Column("kinopoisk_id", sa.Integer),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("year", sa.Integer),
        sa.Column("duration_min", sa.Integer),
        sa.Column("genres", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("popularity", sa.Integer, nullable=False),
        sa.Column("description", sa.Text),
    )
    op.create_index("ix_movies_kinopoisk_id", "movies", ["kinopoisk_id"])
    op.create_index("ix_movies_genres", "movies", ["genres"], postgresql_using="gin")
    op.create_index("ix_movies_popularity", "movies", [sa.text("popularity DESC")])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("external_id", sa.String(128)),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.UniqueConstraint("provider", "external_id", name="uq_users_external"),
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("owner_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filters", postgresql.JSONB, nullable=False),
        sa.Column("stop_on_match", sa.Boolean, nullable=False),
        sa.Column(
            "candidate_ids", postgresql.ARRAY(sa.Integer), nullable=False, server_default="{}"
        ),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
    )

    op.create_table(
        "participants",
        sa.Column(
            "room_id",
            sa.BigInteger,
            sa.ForeignKey("rooms.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("joined_at", TZ, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
    )

    op.create_table(
        "swipes",
        sa.Column(
            "room_id",
            sa.BigInteger,
            sa.ForeignKey("rooms.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("movie_id", sa.Integer, sa.ForeignKey("movies.id"), primary_key=True),
        sa.Column("liked", sa.Boolean, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
    )

    op.create_table(
        "matches",
        sa.Column(
            "room_id",
            sa.BigInteger,
            sa.ForeignKey("rooms.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("movie_id", sa.Integer, sa.ForeignKey("movies.id"), primary_key=True),
        sa.Column("created_at", TZ, nullable=False),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("published_at", TZ),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    for table in ("outbox", "matches", "swipes", "participants", "rooms", "users", "movies"):
        op.drop_table(table)
