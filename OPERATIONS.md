# Operations: migration, backup, restore

## PostgreSQL schema migrations

Noema applies numbered, transactional PostgreSQL migrations during database
initialization. A PostgreSQL advisory transaction lock serializes migration
startup across multiple app workers. Applied versions are recorded in
`noema_schema_migrations`.

Migration 1 adds a composite unique key and a composite parent foreign key.
This prevents a node in one session from referencing a parent in another
session, even if a caller bypasses the HTTP validation layer.

If legacy data contains cross-session parent links, startup intentionally fails
rather than silently rewriting or deleting those links. Repair or export that
data before restarting.

## SQLite to PostgreSQL

Stop writes to the source SQLite database before importing. The importer
preserves node IDs, session IDs, parent IDs, depth, modes, prompts, narratives,
image URLs/statuses, and timestamps. It validates parent integrity, checks all
target ID conflicts, and writes in one transaction. It never overwrites target
rows.

Example for a local Compose deployment:

```bash
docker compose stop noema
# Copy your SQLite file into the project directory if it is elsewhere.
NOEMA_DATABASE_URL='postgresql://noema:YOUR_PASSWORD@localhost:5432/noema' \
  python -m noema.import_sqlite ./noema_memory.db --dry-run
NOEMA_DATABASE_URL='postgresql://noema:YOUR_PASSWORD@localhost:5432/noema' \
  python -m noema.import_sqlite ./noema_memory.db
docker compose start noema
```

Run the importer from an environment with the project requirements installed
and network access to PostgreSQL. For a Compose-internal command, use the
service hostname `postgres`; from the host, publish a temporary loopback port
or use a one-off container on the Compose network. Do not expose PostgreSQL
publicly just to run an import.

## Backup

```bash
./scripts/backup.sh
```

Backups are PostgreSQL custom-format dumps in `./backups`. The script uses a
restrictive file-creation umask. Copy backups off the machine and test restores
regularly; a backup that has never been restored is not a verified recovery plan.

## Restore (destructive)

Restore replaces matching database objects and data. First take a fresh backup,
then explicitly confirm:

```bash
./scripts/backup.sh
CONFIRM_RESTORE=YES ./scripts/restore.sh ./backups/noema-YYYYMMDDTHHMMSSZ.dump
```

Never run restore against the only copy of important data. Verify application
health and sample sessions after recovery.
