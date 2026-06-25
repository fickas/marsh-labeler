#!/usr/bin/env bash
# Apply any pending schema migrations against the Railway Postgres, then serve.
# Running migrations on boot means a fresh database (or a new migration) is brought
# up to head automatically on deploy -- no manual alembic step in production.
set -euo pipefail

alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
