from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import aiosqlite

from .postgres_store import PostgresDreamStore

COLUMNS = (
    "node_id", "session_id", "parent_id", "depth", "mode", "prompt",
    "narrative", "image_url", "image_status", "created_at",
)


async def import_database(source: Path, dsn: str, *, dry_run: bool = False) -> int:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")

    store = PostgresDreamStore(dsn)
    await store.open()
    try:
        async with aiosqlite.connect(source) as db:
            db.row_factory = aiosqlite.Row
            exists = await db.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dream_nodes'"
            )
            if not exists:
                raise RuntimeError("Source SQLite file has no dream_nodes table")

            invalid = await db.execute_fetchall(
                """
                SELECT n.node_id
                FROM dream_nodes n
                LEFT JOIN dream_nodes p ON p.node_id = n.parent_id
                WHERE n.parent_id IS NOT NULL
                  AND (p.node_id IS NULL OR p.session_id <> n.session_id)
                LIMIT 1
                """
            )
            if invalid:
                raise RuntimeError(
                    f"Source contains an orphan or cross-session parent: {invalid[0][0]}"
                )
            rows = await db.execute_fetchall(
                "SELECT " + ", ".join(COLUMNS) + " FROM dream_nodes ORDER BY created_at, node_id"
            )

        records = [tuple(row[column] for column in COLUMNS) for row in rows]
        if not records:
            print("Source database is empty; nothing to import.")
            return 0

        ids = [record[0] for record in records]
        async with store.pool.acquire() as conn:
            conflicts = await conn.fetch(
                "SELECT node_id FROM dream_nodes WHERE node_id = ANY($1::text[])",
                ids,
            )
            if conflicts:
                sample = ", ".join(row["node_id"] for row in conflicts[:5])
                raise RuntimeError(
                    f"Import stopped: {len(conflicts)} node ID conflict(s), e.g. {sample}. "
                    "No rows were changed."
                )
            if dry_run:
                print(f"DRY RUN: {len(records)} nodes validated; PostgreSQL target has no ID conflicts.")
                return len(records)

            async with conn.transaction():
                await conn.copy_records_to_table(
                    "dream_nodes",
                    records=records,
                    columns=COLUMNS,
                )
        print(f"Imported {len(records)} nodes in one PostgreSQL transaction.")
        return len(records)
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely copy a Noema SQLite database into PostgreSQL."
    )
    parser.add_argument("sqlite_file", type=Path, help="Path to the source .db file")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate source and target conflicts without writing data",
    )
    args = parser.parse_args()
    dsn = os.getenv("NOEMA_DATABASE_URL", "")
    if not dsn.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
        parser.error("Set NOEMA_DATABASE_URL to a PostgreSQL connection string")
    asyncio.run(import_database(args.sqlite_file, dsn, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
