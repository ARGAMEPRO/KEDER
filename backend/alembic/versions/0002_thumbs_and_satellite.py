"""thumbnail column

Revision ID: 0002_thumbs
Revises: 0001_initial

`source` needs no change: SQLAlchemy 2 renders Enum(native_enum=False) as a plain
VARCHAR (create_constraint defaults to False), so a new member such as SATELLITE
needs no constraint rebuild.
"""
import sqlalchemy as sa

from alembic import op

revision = "0002_thumbs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("thumb_path", sa.String(length=512), nullable=False,
                                         server_default=""))


def downgrade() -> None:
    op.drop_column("incidents", "thumb_path")
