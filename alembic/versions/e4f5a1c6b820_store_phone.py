"""a store's own telephone number

Retailers that publish an address publish a number beside it, and until now StoreSplit threw
it away. It is the thing a shopper reaches for when a store they were sent to turns out to be
shut, or when an item the comparison says is in stock is not on the shelf -- the two moments
where the rest of this product has run out of answers.

Nullable, and no backfill: a number is a fact a retailer states, and there is nothing to
derive one from. The stores that have one get it on their next weekly details read.

Revision ID: e4f5a1c6b820
Revises: d7b21f0c4e93
Create Date: 2026-09-12 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a1c6b820"
down_revision: str | None = "d7b21f0c4e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("phone", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "phone")
