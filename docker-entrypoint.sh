#!/bin/sh
# Apply migrations, then start the API.
set -e
alembic upgrade head
exec "$@"
