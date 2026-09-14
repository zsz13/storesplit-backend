"""store hours and offer store context

Adds what an offer row needs to name the store it belongs to and point at it.

`stores.timezone` / `hours` / `hours_source` / `hours_updated_at` hold opening hours as the
retailer publishes them, refreshed at scrape time and cached. NULL hours are the normal
state for a retailer that publishes none on a surface StoreSplit is
allowed to read (Raley's serves its store details from a robots-disallowed `/api`), and the
UI reads that as "Hours unavailable" rather than filling in a plausible 9-to-9.

`offers.store_context` records the store the retailer itself echoed back when it priced the
offer -- Whole Foods' `storeId`, Raley's `currentStoreNumber`. Existing rows are NULL: they
were written before anything checked, and back-filling them from the store they are attached
to would manufacture exactly the proof this column exists to demand.

Every column is nullable and additive, so the upgrade takes no table rewrite and the
downgrade loses only these columns.

Revision ID: b9433dc64d10
Revises: 774a717a7963
Create Date: 2026-09-10 01:26:16.121132
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9433dc64d10"
down_revision: str | None = "774a717a7963"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("offers", sa.Column("store_context", sa.String(length=64), nullable=True))
    op.add_column("stores", sa.Column("timezone", sa.String(length=64), nullable=True))
    op.add_column("stores", sa.Column("hours", sa.JSON(), nullable=True))
    op.add_column("stores", sa.Column("hours_source", sa.String(length=100), nullable=True))
    op.add_column(
        "stores", sa.Column("hours_updated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stores", "hours_updated_at")
    op.drop_column("stores", "hours_source")
    op.drop_column("stores", "hours")
    op.drop_column("stores", "timezone")
    op.drop_column("offers", "store_context")
