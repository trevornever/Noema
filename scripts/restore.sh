#!/usr/bin/env sh
set -eu

if [ "${CONFIRM_RESTORE:-}" != "YES" ]; then
  echo "Refusing destructive restore. Set CONFIRM_RESTORE=YES to continue." >&2
  exit 2
fi
if [ "$#" -ne 1 ] || [ ! -s "$1" ]; then
  echo "Usage: CONFIRM_RESTORE=YES $0 path/to/noema.dump" >&2
  exit 2
fi

echo "This replaces objects in the configured Noema PostgreSQL database."
docker compose exec -T postgres pg_restore --clean --if-exists --no-owner -U noema -d noema < "$1"
echo "Restore completed. Verify /api/v1/health and inspect session data."
