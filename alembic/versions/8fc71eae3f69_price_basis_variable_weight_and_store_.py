"""price basis, variable weight and a store's Google place

Adds the fields that make a price self-describing, and the ones that let a store be pointed
at rather than merely located.

`offers.price_basis` says what `price` is an amount *of*: `package` (a total for one
package), `lb`, `oz` or `each`. Without it a price cannot be read back: Target publishes
`current_retail: 2.59` for a tray of chicken it sells at $2.59 **per pound**, and reading
that as a pack total then dividing by the tray's 5.25 lb upper weight produced $0.49/lb --
five times too cheap, and therefore ranked first. Every existing row is `package`, which is
what every retailer means unless it says otherwise and what the old code assumed everywhere;
the server default makes that explicit rather than implied.

`offers.max_total_price` is the ceiling a retailer publishes for a variable-weight item
(Target's `formatted_max_item_price`). It is copied, never computed: Target's own maximum for
a 2.5-5.25 lb tray at $2.59/lb is $12.95, not the $13.60 that multiplication gives.

`retailer_products.min_weight` / `max_weight` / `weight_unit` hold the weight span the
retailer published for the package ("2.5-5.25lbs"). NULL together is the ordinary state.
They are stored beside the price rather than folded into it because a variable-weight tray
has no single size, and its upper end is precisely the number that must not be divided into
an already-per-pound price.

`stores.maps_place_url` / `maps_place_id` / `maps_source` / `maps_updated_at` hold a Google
Maps link to the *business*, resolved once during the weekly store-details read and cached,
never built per request. Target publishes `miscellaneous.google_cid` and Safeway publishes
`googlePlaceId` on their own store pages, so those two are places the retailer itself
identified. NULL is the normal state for everyone else and means "no specific place has been
verified"; the API then falls back to an address search naming the retailer, which is a
worse link than a place and a far better one than the wrong shop.

Every column is nullable or defaulted and additive, so the upgrade takes no table rewrite and
the downgrade loses only these columns. No existing row is rewritten: a price collected
before anything recorded its basis really was read as a package total, so `package` is not a
backfilled guess but the value that price was computed with.

Revision ID: 8fc71eae3f69
Revises: b9433dc64d10
Create Date: 2026-09-10 17:40:44.271338
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8fc71eae3f69"
down_revision: str | None = "b9433dc64d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "offers",
        sa.Column(
            "price_basis",
            sa.String(length=10),
            server_default="package",
            nullable=False,
        ),
    )
    op.add_column(
        "offers", sa.Column("max_total_price", sa.Numeric(precision=10, scale=2), nullable=True)
    )
    op.add_column(
        "retailer_products",
        sa.Column("min_weight", sa.Numeric(precision=12, scale=4), nullable=True),
    )
    op.add_column(
        "retailer_products",
        sa.Column("max_weight", sa.Numeric(precision=12, scale=4), nullable=True),
    )
    op.add_column(
        "retailer_products", sa.Column("weight_unit", sa.String(length=20), nullable=True)
    )
    op.add_column("stores", sa.Column("maps_place_url", sa.String(length=500), nullable=True))
    op.add_column("stores", sa.Column("maps_place_id", sa.String(length=128), nullable=True))
    op.add_column("stores", sa.Column("maps_source", sa.String(length=100), nullable=True))
    op.add_column("stores", sa.Column("maps_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "maps_updated_at")
    op.drop_column("stores", "maps_source")
    op.drop_column("stores", "maps_place_id")
    op.drop_column("stores", "maps_place_url")
    op.drop_column("retailer_products", "weight_unit")
    op.drop_column("retailer_products", "max_weight")
    op.drop_column("retailer_products", "min_weight")
    op.drop_column("offers", "max_total_price")
    op.drop_column("offers", "price_basis")
