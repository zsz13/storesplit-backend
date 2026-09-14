"""offer availability and product url hygiene

Adds the normalized availability an offer now carries, and clears product URLs that were
never URLs. Existing offers become `unknown` rather than `in_stock`: nothing recorded what
those scrapes actually saw, and guessing is the bug this column exists to prevent. The next
scrape of a store replaces them with the retailer's own answer.

**Operational note.** Search and basket comparison default to in-stock only, so between this
upgrade and the next scrape those surfaces return nothing for already-collected ZIPs, even
though `offers` is full. That is the honest reading of "we do not know", not a fault: run
`POST /scrape` (or `scripts/scrape.py --zip ...`) after upgrading. The UI says which of the
two situations it is in -- `SearchResponse.offers_before_filter` distinguishes "nothing
collected" from "the filter hid it all".

The URL cleanup removes values a browser could not follow -- notably the stringified
`{'id': ..., 'canonicalUrl': None, ...}` objects the Lucky adapter used to persist, which
the frontend resolved against its own origin. Wrong-host URLs are not detectable in
portable SQL; the scrape service now re-checks every URL against the retailer's own host
before writing, so they cannot be reintroduced.

Revision ID: 774a717a7963
Revises: 19cc31159d59
Create Date: 2026-09-08 19:17:17.429315
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "774a717a7963"
down_revision: str | None = "19cc31159d59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "offers",
        sa.Column("availability", sa.String(length=20), server_default="unknown", nullable=False),
    )
    op.create_index("ix_offers_availability", "offers", ["availability"], unique=False)
    op.execute(
        "UPDATE retailer_products SET product_url = NULL "
        "WHERE product_url IS NOT NULL AND product_url NOT LIKE 'https://%'"
    )


def downgrade() -> None:
    # The cleared URLs are not restored: they were unusable, and a scrape rebuilds them.
    op.drop_index("ix_offers_availability", table_name="offers")
    op.drop_column("offers", "availability")
