from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite

from .models import DreamNode, LineageNode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_nodes (
    node_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    parent_id    TEXT REFERENCES dream_nodes(node_id) ON DELETE CASCADE,
    depth        INTEGER NOT NULL DEFAULT 0,
    mode         TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    narrative    TEXT NOT NULL DEFAULT '',
    image_url    TEXT,
    image_status TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_session ON dream_nodes(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nodes_parent  ON dream_nodes(parent_id);
"""

_NODE_COLUMNS = (
    "node_id, session_id, parent_id, depth, mode, prompt, narrative, "
    "image_url, image_status, created_at"
)


class StoreClosedError(RuntimeError):
    pass


class DreamStore:
    """Single-connection async SQLite DAG store.

    aiosqlite executes every statement on one dedicated thread, so a single shared
    connection serialises access without cross-connection WAL lock contention
    ("database is locked") that a connection-per-request design hits under load.
    The write lock keeps each mutation + commit atomic with respect to other writers.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._db is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA busy_timeout=5000;")
            await db.execute("PRAGMA foreign_keys=ON;")
            await db.executescript(_SCHEMA)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._db = db

    async def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            await db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise StoreClosedError("DreamStore is not open")
        return self._db

    async def ping(self) -> bool:
        try:
            async with self.db.execute("SELECT 1") as cur:
                return (await cur.fetchone()) is not None
        except (StoreClosedError, aiosqlite.Error):
            return False

    async def get_node_depth(self, session_id: str, node_id: str) -> int | None:
        """Depth of node_id if it exists *in this session*; None otherwise."""
        async with self.db.execute(
            "SELECT depth FROM dream_nodes WHERE node_id = ? AND session_id = ?",
            (node_id, session_id),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else int(row["depth"])

    async def lineage(self, session_id: str, parent_id: str | None, limit: int) -> list[LineageNode]:
        """Up to `limit` ancestors ending at parent_id, oldest first. O(limit), not O(session)."""
        if parent_id is None or limit <= 0:
            return []
        async with self.db.execute(
            """
            WITH RECURSIVE chain(node_id, parent_id, prompt, narrative, hop) AS (
                SELECT node_id, parent_id, prompt, narrative, 0
                FROM dream_nodes WHERE node_id = ? AND session_id = ?
                UNION ALL
                SELECT n.node_id, n.parent_id, n.prompt, n.narrative, c.hop + 1
                FROM dream_nodes n JOIN chain c ON n.node_id = c.parent_id
                WHERE c.hop + 1 < ? AND n.session_id = ?
            )
            SELECT node_id, parent_id, prompt, narrative FROM chain ORDER BY hop DESC
            """,
            (parent_id, session_id, limit, session_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            LineageNode(
                node_id=r["node_id"],
                parent_id=r["parent_id"],
                prompt=r["prompt"],
                narrative=r["narrative"],
            )
            for r in rows
        ]

    async def insert_node(
        self,
        *,
        node_id: str,
        session_id: str,
        parent_id: str | None,
        depth: int,
        mode: str,
        prompt: str,
        narrative: str,
    ) -> None:
        async with self._write_lock:
            try:
                await self.db.execute(
                    f"INSERT INTO dream_nodes ({_NODE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?)",
                    (node_id, session_id, parent_id, depth, mode, prompt, narrative, time.time()),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def set_image(self, node_id: str, *, url: str | None, status: str) -> None:
        async with self._write_lock:
            try:
                await self.db.execute(
                    "UPDATE dream_nodes SET image_url = ?, image_status = ? WHERE node_id = ?",
                    (url, status, node_id),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def session_nodes(self, session_id: str) -> list[DreamNode]:
        async with self.db.execute(
            f"SELECT {_NODE_COLUMNS} FROM dream_nodes WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [DreamNode(**dict(r)) for r in rows]  # type: ignore[typeddict-item]
