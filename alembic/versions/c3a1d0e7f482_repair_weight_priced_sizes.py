"""repair the sizes of products a retailer prices by weight

The pricing fix stops a per-pound price being divided by a package weight, but it cannot
undo the rows written while it was. Those rows do not heal on their own, and this is the
migration that repairs them.

**What was wrong.** A weight-priced product's canonical size was taken from whatever weight
could be parsed out of its title or size string -- Target publishes "Boneless & Skinless
Chicken Breast Value Pack - 2.5-5.25lbs - price per lb", so its canonical product was
recorded as a 5.25 lb package. It is not a 5.25 lb package. It is a tray of unknown weight
sold at a rate, and the only size that describes it is one pound, which is what the rate is
quoted against.

**Why a re-scrape is not enough.** `ingest_listing` keeps an existing SKU-to-canonical
mapping deliberately -- it is a matching signal, and re-deciding it every scrape would make
products merge and split under a shopper. So a canonical product created before the fix
keeps its wrong `quantity` and `comparison_quantity` for ever. `comparison_quantity` is what
a basket divides by (`packs_needed`) and what the matcher compares sizes with, so the stale
value quietly misprices baskets and merges products that are not the same size.

**What is repaired, and what is left alone.**

* `canonical_products` whose `attributes->>'sold_by'` is `weight` are set back to one pound,
  with `comparison_quantity` converted into whatever unit that category compares in. `count`
  is cleared: a weighed product has no count.
* `offers` are **not** touched, and this is deliberate. The obvious repair -- restate the
  stored unit price as the rate it always was -- cannot be done safely, because a canonical
  product is shared between retailer SKUs and nothing on an offer row says which basis it was
  priced on. The tempting test ("its unit price equals `price / old comparison quantity`, so
  it was double-divided") is satisfied by *every correctly computed package offer as well*,
  since that is exactly how a package's unit price is calculated. Applying it would relabel a
  genuine package total as a per-pound rate -- the original bug aimed the other way, and
  louder. Offers carry their basis from the adapter and are rewritten by the next scrape of
  their store and category, which is the path that actually knows the answer; until then an
  offer on a repaired product shows the unit price it was collected with.
* `price_history` is **not** rewritten. It is a record of what was collected and when, and
  editing history to match today's understanding is how a record stops being evidence.

The downgrade is deliberately empty of data changes -- see `downgrade()`.

Revision ID: c3a1d0e7f482
Revises: 8fc71eae3f69
Create Date: 2026-09-10 18:12:00.000000
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "c3a1d0e7f482"
down_revision: str | None = "8fc71eae3f69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One pound, in each comparison unit a category can use. Frozen here rather than imported
# from `app.normalize.units`: a migration is a statement about a moment, and one that has
# already run must keep meaning the same thing when a database is rebuilt from scratch years
# later. Importing the live table would let a future change to a conversion factor silently
# alter what this already-applied revision does.
ONE_POUND_IN: dict[str, Decimal] = {"lb": Decimal(1), "oz": Decimal(16)}


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, comparison_unit FROM canonical_products "
            "WHERE attributes ->> 'sold_by' = 'weight' "
            "AND NOT (quantity = 1 AND quantity_unit = 'lb')"
        )
    ).all()
    for canonical_id, comparison_unit in rows:
        size = ONE_POUND_IN.get(comparison_unit)
        if size is None:
            # A weighed product compared in a unit a pound cannot reach is not repairable
            # here, and the ingest would never have written one: leave it exactly as found
            # rather than invent a size for it.
            continue
        connection.execute(
            sa.text(
                "UPDATE canonical_products "
                "SET quantity = 1, quantity_unit = 'lb', count = NULL, comparison_quantity = :size "
                "WHERE id = :id"
            ),
            {"size": size, "id": canonical_id},
        )


def downgrade() -> None:
    """Schema-only, because the data change has no inverse worth having.

    The previous values were a per-pound price divided by a package weight and a package
    weight that described no package. Restoring them would mean recording a wrong size on
    purpose, and the next scrape overwrites the offers either way.
    """
