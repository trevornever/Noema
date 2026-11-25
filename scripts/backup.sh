#!/usr/bin/env sh
set -eu

BACKUP_DIR="${NOEMA_BACKUP_DIR:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
umask 077
FILE="$BACKUP_DIR/noema-$STAMP.dump"

docker compose exec -T postgres pg_dump -U noema -d noema -Fc > "$FILE"
test -s "$FILE"
printf 'Backup created: %s\n' "$FILE"
