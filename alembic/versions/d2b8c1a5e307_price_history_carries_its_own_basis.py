"""a history row says what its number means, and the series index says where it lives

`price_history` stored five numbers and a timestamp, and nothing that said what any of them
were quoted *per*. That was survivable while nothing read the table; it is not survivable now
that a shopper can open a chart of it. `2.59` is a fair total for a tray of chicken and a
fair rate for a pound of it, and a chart that plots the two on one axis is not a chart of
anything. `offers` has carried `price_basis` beside its price since `8fc71eae3f69` for
exactly this reason -- this brings history up to the same standard.

**The two columns are copied, not joined to.** The obvious alternative -- read the basis off
the offer when the chart is drawn -- fails in both directions. An offer is deleted the moment
the retailer stops listing the product (`expire_stale_offers`), taking the only explanation
of its own history with it; and an offer that survives has been overwritten with *today's*
basis, which is the wrong answer for a row written before a retailer switched from a package
total to a rate per pound. A history row is read long after the offer that produced it has
changed or gone, so it has to be self-describing.

**Backfill, and why the column is nullable.** Existing rows take the basis and unit label of
the offer for the same (retailer product, store) where one still exists -- that offer is the
same collection path that wrote the row, and for the overwhelming majority of rows it is the
same scrape. Rows whose offer has since been expired keep **NULL** in both.

`offers.price_basis` is `NOT NULL DEFAULT 'package'` and that is right there: an adapter that
said nothing was looking at a package total, which is what a retailer publishes unless it
says otherwise. Copying that default onto a *backfilled* history row would be a different
thing entirely -- there was nobody to ask, and `package` is not an absence. A reader renders
it as "$2.59 for the pack", and over a per-pound rate that is the precise sentence
`8fc71eae3f69` was written to stop. So the column is nullable, an un-backfilled row says
"not recorded", and the API sends `price_basis: null` rather than a guess. Every row written
by a scrape from here on carries the real answer.

**Nothing is rewritten but the two new columns.** `scraped_at`, the prices and the unit
prices are untouched, and no row is deleted or merged. History is a record of what was
collected and when; editing it to match today's understanding is how a record stops being
evidence (see `c3a1d0e7f482`, which said the same thing and left the table alone).

**The index.** `ix_price_history_product_store` was `(retailer_product_id, store_id)`, built
for the scrape's "what did this cost last time" lookup. Reading a chart asks a different
question -- one series, one time window -- so the range column joins the key, and the old
index is dropped rather than kept beside it: it is a strict prefix of the new one, so every
plan that used it uses this one.

**Operationally.** PostgreSQL 17, and `docker-entrypoint.sh` runs `alembic upgrade head`
before the API starts, so the columns exist before anything reads them; code from before this
revision simply does not select them, so the two directions are compatible either way.
Everything here is cheap on this table and deliberately not written for a table it is not:
adding a column with a *constant* default has been metadata-only since PostgreSQL 11 (no
rewrite, no scan), the backfill is one correlated `UPDATE` over a few thousand rows rather
than a batched job, and the index is built non-concurrently inside the migration's
transaction -- `CREATE INDEX CONCURRENTLY` cannot run in one, and this is a local
single-writer MVP where the brief `SHARE` lock costs nothing. If `price_history` ever grows
past the point where a full-table `UPDATE` is a pause somebody notices, split the backfill
out and build the index concurrently outside the transaction; that is a different migration,
not a knob on this one.

Revision ID: d2b8c1a5e307
Revises: b7d3f0a92c15
Create Date: 2026-09-13 18:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2b8c1a5e307"
down_revision: str | None = "b7d3f0a92c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("price_history", sa.Column("price_basis", sa.String(length=10), nullable=True))
    op.add_column(
        "price_history", sa.Column("unit_price_unit", sa.String(length=20), nullable=True)
    )

    # Correlated on the pair that identifies a series, which is also the offers table's own
    # unique key -- so this matches at most one offer per history row.
    op.execute(
        """
        UPDATE price_history AS ph
           SET price_basis = o.price_basis,
               unit_price_unit = o.unit_price_unit
          FROM offers AS o
         WHERE o.retailer_product_id = ph.retailer_product_id
           AND o.store_id = ph.store_id
        """
    )

    op.drop_index("ix_price_history_product_store", table_name="price_history")
    op.create_index(
        "ix_price_history_series",
        "price_history",
        ["retailer_product_id", "store_id", "scraped_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_price_history_series", table_name="price_history")
    op.create_index(
        "ix_price_history_product_store",
        "price_history",
        ["retailer_product_id", "store_id"],
    )
    op.drop_column("price_history", "unit_price_unit")
    op.drop_column("price_history", "price_basis")
