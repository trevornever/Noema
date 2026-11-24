from __future__ import annotations

import time
from typing import Any

import asyncpg

from .models import DreamNode, LineageNode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_nodes (
    node_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_id TEXT REFERENCES dream_nodes(node_id) ON DELETE CASCADE,
    depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
    mode TEXT NOT NULL,
    prompt TEXT NOT NULL,
    narrative TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    image_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (image_status IN ('pending','ready','mocked','error','cancelled')),
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_session ON dream_nodes(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON dream_nodes(parent_id);
"""

_COLUMNS = """node_id, session_id, parent_id, depth, mode, prompt, narrative,
              image_url, image_status, created_at"""


class PostgresDreamStore:
    """PostgreSQL implementation of the DreamStore interface."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size,
            command_timeout=30,
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute(_SCHEMA)
        except BaseException:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresDreamStore is not open")
        return self._pool

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except (RuntimeError, asyncpg.PostgresError):
            return False

    async def get_node_depth(self, session_id: str, node_id: str) -> int | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT depth FROM dream_nodes WHERE node_id=$1 AND session_id=$2",
                node_id, session_id,
            )
        return None if value is None else int(value)

    async def lineage(
        self, session_id: str, parent_id: str | None, limit: int
    ) -> list[LineageNode]:
        if parent_id is None or limit <= 0:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE chain(node_id,parent_id,prompt,narrative,hop) AS (
                    SELECT node_id,parent_id,prompt,narrative,0
                    FROM dream_nodes WHERE node_id=$1 AND session_id=$2
                    UNION ALL
                    SELECT n.node_id,n.parent_id,n.prompt,n.narrative,c.hop+1
                    FROM dream_nodes n JOIN chain c ON n.node_id=c.parent_id
                    WHERE c.hop+1 < $3 AND n.session_id=$2
                )
                SELECT node_id,parent_id,prompt,narrative FROM chain ORDER BY hop DESC
                """,
                parent_id, session_id, limit,
            )
        return [
            LineageNode(
                node_id=r["node_id"], parent_id=r["parent_id"],
                prompt=r["prompt"], narrative=r["narrative"],
            ) for r in rows
        ]

    async def insert_node(
        self, *, node_id: str, session_id: str, parent_id: str | None,
        depth: int, mode: str, prompt: str, narrative: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"INSERT INTO dream_nodes ({_COLUMNS}) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,'pending',$8)",
                    node_id, session_id, parent_id, depth, mode, prompt,
                    narrative, time.time(),
                )

    async def set_image(self, node_id: str, *, url: str | None, status: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE dream_nodes SET image_url=$1,image_status=$2 WHERE node_id=$3",
                url, status, node_id,
            )

    async def session_nodes(self, session_id: str) -> list[DreamNode]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM dream_nodes "
                "WHERE session_id=$1 ORDER BY created_at ASC,node_id ASC",
                session_id,
            )
        return [dict(row) for row in rows]  # type: ignore[return-value]
