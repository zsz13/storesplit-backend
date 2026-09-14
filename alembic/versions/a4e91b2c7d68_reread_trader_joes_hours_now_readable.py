"""re-read Trader Joe's stores, whose published week has just become readable

**The trap this avoids, which is the same one `d7b21f0c4e93` avoided for Sprouts, reached
by a different route.** `stores.hours_updated_at` gates the weekly store-details phase:
`fresh_store_details` leaves alone any store stamped inside `STORE_DETAILS_TTL_SECONDS`, and
`apply_store_details` stamps **every** attempt, including the ones that found nothing.

Sprouts tripped it by *gaining* `fetch_store_details`. Trader Joe's has had that capability
all along and has been read weekly all along -- what changed is that the read can now
produce an answer. Its locator always published a complete week and never a timezone, and a
wall clock with no zone is not a fact about a store, so every one of those reads ended in
`hours_from_unzoned` returning None and the row being stamped "asked, found nothing usable".
`normalize/timezones.py` now reads the zone off the coordinates the same locator record
carries, so the very same payload resolves to a schedule.

Without this revision the stamp is indistinguishable from a fresh, successful read, and a
deployment that simply upgrades would keep every Trader Joe's store on "Hours not published"
for up to seven more days -- nothing failing, nothing in the logs to say why. The previous
revision's own docstring names Trader Joe's as a retailer deliberately *not* unstamped,
"which has hours but no timezone"; that sentence is what has stopped being true.

**What this does.** Clears `hours_updated_at` for every Trader Joe's store, so the next
scrape reads its details once. Nothing else on the row is touched: an existing `hours`,
`timezone`, `hours_source`, `maps_place_*` or coordinate stays exactly as it is until a
successful read replaces it, because clearing a stamp asks a question and must not throw
away the last answer while it waits.

**Why every Trader Joe's row and not only the ones with no hours.** A Trader Joe's row that
already holds hours holds Google's, bought for a store whose own week was sitting unread in
the locator. Re-reading replaces it with the retailer's own, which is the direction the
hours ladder is built to run in, so those rows are the ones that most want the visit.

**What it costs, stated rather than discovered later.** `hours_updated_at` is one stamp over
two things -- the published-details read *and* the Google *place* lookup, which happen on the
same weekly visit (`fresh_store_details`). So on a deployment that has a `GOOGLE_MAPS_API_KEY`
set, clearing it buys one extra Places *search* per Trader Joe's store, once. That is the
opposite direction from the saving the accompanying change makes on the *schedule* endpoint,
and it is still worth it: it is bounded, it happens once, and the alternative is a fix nobody
can see for a week. The same was true of `d7b21f0c4e93` and is a property of the shared stamp,
not of this revision.

**Adding a retailer later.** Like the revision before it, this is a statement about one
moment. Only Trader Joe's publishes `unzoned_hours` today; an adapter that starts to, or one
whose reads start resolving for some other reason, needs its own revision with its own slug.

Revision ID: a4e91b2c7d68
Revises: f1c9d3a7e5b4
Create Date: 2026-09-13 10:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e91b2c7d68"
down_revision: str | None = "f1c9d3a7e5b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The retailers whose already-published hours became readable in the change this revision
# accompanies. Frozen as literals for the same reason the previous revision froze its own: a
# migration that reached into the live adapter registry would mean something different every
# time the registry changed.
RETAILERS_WITH_NEWLY_READABLE_HOURS: tuple[str, ...] = ("traderjoes",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE stores SET hours_updated_at = NULL "
            "WHERE hours_updated_at IS NOT NULL AND retailer_id IN ("
            "  SELECT id FROM retailers WHERE slug IN :slugs"
            ")"
        ).bindparams(
            sa.bindparam("slugs", value=RETAILERS_WITH_NEWLY_READABLE_HOURS, expanding=True)
        )
    )


def downgrade() -> None:
    """Nothing to undo.

    The upgrade clears a cache stamp; the value it cleared timed an attempt that could not
    have produced hours, and restoring it would only re-hide them. Downgrading the schema
    does not remove the timezone lookup, and the next scrape re-stamps the row anyway.
    """
