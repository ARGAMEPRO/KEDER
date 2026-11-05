"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-14
"""
import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ROLE = sa.Enum("CITIZEN", "FORESTER", "MCHS", name="role", native_enum=False, length=32)
STATUS = sa.Enum("CLEAR", "PENDING_VERIFICATION", "CRITICAL_ALERT", "FALSE_ALARM", "CONFIRMED",
                 name="status", native_enum=False, length=32)
SOURCE = sa.Enum("PHOTO", "VIDEO", "RTSP", "MANUAL", name="source", native_enum=False, length=32)
GPSSOURCE = sa.Enum("EXIF", "DEVICE", "MANUAL", "SETTLEMENT", "NONE",
                    name="gpssource", native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("role", ROLE, nullable=False, server_default="CITIZEN"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_is_demo", "users", ["is_demo"])

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("region", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("population", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_settlements_name", "settlements", ["name"])
    op.create_index("ix_settlements_population", "settlements", ["population"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source", SOURCE, nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("gps_source", GPSSOURCE, nullable=False, server_default="NONE"),
        sa.Column("region_name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("image_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("annotated_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("detections", sa.JSON(), nullable=False),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("wind_dir_deg", sa.Float(), nullable=True),
        sa.Column("temp_c", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        sa.Column("weather_fallback", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hazard_geojson", sa.JSON(), nullable=True),
        sa.Column("evacuation", sa.JSON(), nullable=False),
        sa.Column("reporter_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_lat", "incidents", ["lat"])


def downgrade() -> None:
    op.drop_table("incidents")
    op.drop_table("settlements")
    op.drop_table("users")
