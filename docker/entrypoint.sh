#!/bin/sh
# Wait for PostgreSQL, then optionally seed once, then start the API.
set -eu

PY="python"
SCHEMA="${AUDIENCE_DB_SCHEMA:-audience}"

echo "[entrypoint] waiting for database"
i=0
until python - <<'PY'
import os, sys
import psycopg
url = os.environ.get("AUDIENCE_DATABASE_URL", "postgresql://ccdev:ccdev@db:5432/ccdev")
try:
    with psycopg.connect(url, connect_timeout=3) as conn:
        conn.execute("SELECT 1")
except Exception as exc:
    print(f"database not ready: {exc}", file=sys.stderr)
    sys.exit(1)
PY
do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "[entrypoint] database still unavailable after 60 attempts" >&2
    exit 1
  fi
  sleep 2
done

if [ "${AUDIENCE_SEED_ON_START:-1}" = "1" ]; then
  echo "[entrypoint] seeding fictional data into schema ${SCHEMA} (idempotent)"
  python scripts/seed.py
fi

exec "$@"
