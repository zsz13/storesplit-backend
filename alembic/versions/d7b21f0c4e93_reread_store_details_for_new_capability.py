"""let a retailer that has just gained store details be read again

**What is wrong without this.** `stores.hours_updated_at` is the gate on the weekly
store-details phase: `fresh_store_details` leaves alone any store stamped inside
`STORE_DETAILS_TTL_SECONDS`, and `apply_store_details` stamps **every** attempt, including
the ones that found nothing. That is deliberate -- without it a store whose page 404s is
re-read every five minutes for ever -- and it is exactly right while an adapter's ability to
read its retailer stays the same.

It stops being right the moment that ability changes. Sprouts has just gained
`fetch_store_details`. Every Sprouts row in an existing database was stamped by the last
scrape that ran *before* it had one: the scrape asked "is this store fresh?", got yes, and
never called the capability that now exists. So the stamp means "asked, with code that could
not answer" while the gate reads it as "asked, and answered recently", and a deployment that
simply upgrades would keep showing "Hours not published" for every Sprouts store for up to
seven more days -- with nothing failing, and nothing in the logs to say why.

**What this does.** Clears `hours_updated_at` for the stores of the retailers named below,
so the next scrape reads their details once. Nothing else on the row is touched: an existing
`hours`, `timezone`, `maps_place_*` or coordinate stays exactly as it is until a successful
read replaces it, because clearing a stamp asks a question and must not throw away the last
answer while it waits.

**Why by retailer and not by "every store with no hours".** A store whose retailer publishes
nothing readable -- Lucky, 99 Ranch, Trader Joe's, which has hours but no timezone -- also
has no hours, and re-reading those every deploy would reintroduce the storm the stamp
prevents. The fact that changed is which *adapters* grew a capability, and that is what is
written down here.

**Adding a retailer later.** This revision is a statement about one moment. A future adapter
that gains `fetch_store_details` needs its own revision with its own slug; editing this one
would not re-run it on any database that has already applied it.

Revision ID: d7b21f0c4e93
Revises: c3a1d0e7f482
Create Date: 2026-09-12 12:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b21f0c4e93"
down_revision: str | None = "c3a1d0e7f482"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The retailers whose adapters gained `fetch_store_details` in the change this revision
# accompanies. Frozen as literals: a migration that reached into the live adapter registry
# would mean something different every time the registry changed.
RETAILERS_WITH_NEW_STORE_DETAILS: tuple[str, ...] = ("sprouts",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE stores SET hours_updated_at = NULL "
            "WHERE hours_updated_at IS NOT NULL AND retailer_id IN ("
            "  SELECT id FROM retailers WHERE slug IN :slugs"
            ")"
        ).bindparams(sa.bindparam("slugs", value=RETAILERS_WITH_NEW_STORE_DETAILS, expanding=True))
    )


def downgrade() -> None:
    """Nothing to undo.

    The upgrade clears a cache stamp; the value it cleared was a timestamp of an attempt
    that could not have succeeded, and restoring it would only re-hide the hours. Downgrading
    the schema does not remove the adapter, and the next scrape re-stamps the row anyway.
    """
