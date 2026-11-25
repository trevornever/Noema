from __future__ import annotations

import os
import unittest
import uuid

import asyncpg

from noema.postgres_store import PostgresDreamStore

DSN = os.getenv("TEST_POSTGRES_DSN")


class TestPostgresStore(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_and_session_scoped_lineage(self):
        if not DSN:
            self.skipTest("TEST_POSTGRES_DSN not configured")

        store = PostgresDreamStore(DSN)
        await store.open()
        session = "test_" + uuid.uuid4().hex
        root = "root_" + uuid.uuid4().hex
        child = "child_" + uuid.uuid4().hex
        try:
            self.assertTrue(await store.ping())
            await store.insert_node(
                node_id=root,
                session_id=session,
                parent_id=None,
                depth=0,
                mode="cocreate",
                prompt="root prompt",
                narrative="root narrative",
            )
            self.assertEqual(await store.get_node_depth(session, root), 0)

            await store.insert_node(
                node_id=child,
                session_id=session,
                parent_id=root,
                depth=1,
                mode="synthesis",
                prompt="child prompt",
                narrative="child narrative",
            )
            lineage = await store.lineage(session, child, 10)
            self.assertEqual([item["node_id"] for item in lineage], [root, child])
            self.assertIsNone(await store.get_node_depth("other_session", root))

            await store.set_image(child, url=None, status="mocked")
            nodes = await store.session_nodes(session)
            self.assertEqual(len(nodes), 2)
            self.assertEqual(nodes[1]["image_status"], "mocked")

            with self.assertRaises(asyncpg.ForeignKeyViolationError):
                await store.insert_node(
                    node_id="cross_" + uuid.uuid4().hex,
                    session_id="different_session",
                    parent_id=root,
                    depth=1,
                    mode="cocreate",
                    prompt="must be rejected",
                    narrative="cross-session parent edge",
                )
        finally:
            async with store.pool.acquire() as conn:
                await conn.execute("DELETE FROM dream_nodes WHERE session_id=$1", session)
            await store.close()
