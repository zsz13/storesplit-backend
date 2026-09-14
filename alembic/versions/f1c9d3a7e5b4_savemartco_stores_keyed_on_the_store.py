"""key Lucky and Save Mart stores on the physical store, not on one of its shops

**What changed above this migration.** A Save Mart Companies store row used to be keyed on
the Instacart *shop* id `DefaultShop` answered with. A shop is one fulfilment mode of a
store, and a store has several, so that key identifies a way of buying rather than a
supermarket: the same Lucky in Daly City can arrive as two ids and become two rows, two map
links and two lines in a comparison. Sprouts, which is still keyed that way, has four rows
for two Bay Area stores to show for it. The adapter now keys these two retailers on
`retailerLocationId` -- the physical location, which the same payload has always named -- and
addresses its price queries to the shop.

**Why the old rows cannot simply be renamed.** Nothing in the database maps a shop id to its
location: the mapping lives in a payload from the retailer, and a migration that went and
asked for it would make a schema upgrade depend on a third party being reachable. So the old
rows are removed, and the next scrape recreates each store under its own identity -- with the
name, street, coordinates, timezone and hours this change also gives it, none of which the
old rows had.

**What removing a store costs.** Its current offers and its price history, which are
`store_id` rows in two tables with no cascade. Offers are the current scrape's output and are
rewritten every few minutes, so they cost nothing to lose. Price history for these two
retailers is the real loss, and it is bounded: it is the history of a store identified in a
way this repo has stopped believing in, and keeping it would mean keeping the row it hangs
off. Deleting them in the right order -- history, offers, stores -- is what keeps the foreign
keys satisfied.

Only `lucky` and `savemart` are touched. Every other retailer keys its stores on an id its
own retailer publishes and is not affected; **Sprouts is deliberately not included**, because
its adapter has not changed and re-keying its rows here would delete stores that would come
back exactly as they were.

**The stamp.** `stores.hours_updated_at` gates the weekly store-details read and is written
on every attempt, including the ones that found nothing (see `d7b21f0c4e93`, which explains
why). Both banners have just gained `fetch_store_details`, so any surviving row -- one whose
retailer row exists but whose stores were already re-keyed, or a database restored between
deploys -- would sit behind a stamp meaning "asked, with code that could not answer" for up
to seven days. It is cleared for the same two retailers, which is the revision that bullet
asked any future adapter gaining the capability to write.

Revision ID: f1c9d3a7e5b4
Revises: e4f5a1c6b820
Create Date: 2026-09-12 21:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c9d3a7e5b4"
down_revision: str | None = "e4f5a1c6b820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The retailers whose store identity changed in the release this accompanies, and which
# gained `fetch_store_details` in it. Frozen as literals: a migration that read the live
# adapter registry would mean something different every time the registry changed.
REKEYED_RETAILERS: tuple[str, ...] = ("lucky", "savemart")

_STORES = (
    "SELECT s.id FROM stores s JOIN retailers r ON r.id = s.retailer_id WHERE r.slug IN :slugs"
)


def upgrade() -> None:
    connection = op.get_bind()
    slugs = sa.bindparam("slugs", value=REKEYED_RETAILERS, expanding=True)
    for table in ("price_history", "offers"):
        connection.execute(
            sa.text(f"DELETE FROM {table} WHERE store_id IN ({_STORES})").bindparams(slugs)
        )
    connection.execute(
        sa.text(
            "DELETE FROM stores WHERE id IN ("
            "  SELECT s.id FROM stores s JOIN retailers r ON r.id = s.retailer_id"
            "  WHERE r.slug IN :slugs"
            ")"
        ).bindparams(slugs)
    )
    connection.execute(
        sa.text(
            "UPDATE stores SET hours_updated_at = NULL "
            "WHERE hours_updated_at IS NOT NULL AND retailer_id IN ("
            "  SELECT id FROM retailers WHERE slug IN :slugs"
            ")"
        ).bindparams(slugs)
    )


def downgrade() -> None:
    """Nothing to undo, and nothing that could be.

    The upgrade deleted rows whose identifiers this repo no longer believes identify a store.
    Recreating them would mean inventing shop ids, and the adapter a downgrade steps back to
    rediscovers its stores on its next scrape anyway.
    """
