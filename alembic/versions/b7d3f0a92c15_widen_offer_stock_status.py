"""room for the longest stock wording a retailer actually has

`offers.stock_status` keeps the retailer's own words beside the normalized state, so that a
surprising verdict can be traced to the payload that produced it. At 30 characters it was
narrower than strings this codebase already generates: Whole Foods' documented
`OUT_OF_STOCK_ONLINE` makes `availability=OUT_OF_STOCK_ONLINE`, which is 32. That had two
bad endings and no good one -- either the insert fails and takes the whole store-and-category
batch of offers with it, or the column keeps `availability=OUT_OF_STOCK_ONLI`, a token no
payload ever contained, which is worse than useless in the one column whose entire job is to
be quotable evidence.

60 covers every wording any registered adapter can build today (the longest observed in a
real run is 26) with room for a qualifier. The ingest still bounds what it writes, because a
diagnostic must never be what fails a scrape -- but at this width the bound is a backstop
rather than something that fires.

Widening a `varchar` limit is a catalogue-only change in PostgreSQL: no table rewrite, no
scan, no lock beyond the brief `ACCESS EXCLUSIVE` on the DDL itself. Nothing is backfilled
because nothing was lost: every stored value already fits, and the next scrape rewrites each
offer it sees anyway.

The downgrade truncates in SQL rather than failing, since by then the column may hold values
the narrower type cannot take.

Revision ID: b7d3f0a92c15
Revises: a4e91b2c7d68
Create Date: 2026-09-13 17:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3f0a92c15"
down_revision: str | None = "a4e91b2c7d68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "offers",
        "stock_status",
        existing_type=sa.String(length=30),
        type_=sa.String(length=60),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "offers",
        "stock_status",
        existing_type=sa.String(length=60),
        type_=sa.String(length=30),
        existing_nullable=True,
        postgresql_using="left(stock_status, 30)",
    )
