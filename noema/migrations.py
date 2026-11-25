from __future__ import annotations

import asyncpg

# Versioned, transactional PostgreSQL migrations. Keep each migration as discrete
# statements so asyncpg never has to parse a multi-command prepared statement.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_dream_nodes_session_node "
            "ON dream_nodes(session_id, node_id)",
            "ALTER TABLE dream_nodes DROP CONSTRAINT IF EXISTS dream_nodes_parent_id_fkey",
            "ALTER TABLE dream_nodes ADD CONSTRAINT dream_nodes_session_parent_fkey "
            "FOREIGN KEY (session_id, parent_id) "
            "REFERENCES dream_nodes(session_id, node_id) ON DELETE CASCADE",
        ),
    ),
)


async def apply_migrations(pool: asyncpg.Pool) -> list[int]:
    """Apply pending migrations exactly once, serialized across app workers."""
    applied_now: list[int] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS noema_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            await conn.execute("SELECT pg_advisory_xact_lock(781234991)")
            applied = {
                int(row["version"])
                for row in await conn.fetch("SELECT version FROM noema_schema_migrations")
            }
            for version, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO noema_schema_migrations(version) VALUES($1)", version
                )
                applied_now.append(version)
    return applied_now
