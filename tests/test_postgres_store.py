from __future__ import annotations

import os
import uuid

import pytest

from noema.postgres_store import PostgresDreamStore

DSN = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN not configured")


@pytest.mark.asyncio
async def test_postgres_store_round_trip_and_session_scoped_lineage():
    store = PostgresDreamStore(DSN)
    await store.open()
    session = "test_" + uuid.uuid4().hex
    root = "root_" + uuid.uuid4().hex
    child = "child_" + uuid.uuid4().hex
    try:
        assert await store.ping()
        await store.insert_node(
            node_id=root, session_id=session, parent_id=None, depth=0,
            mode="cocreate", prompt="root prompt", narrative="root narrative",
        )
        assert await store.get_node_depth(session, root) == 0
        await store.insert_node(
            node_id=child, session_id=session, parent_id=root, depth=1,
            mode="synthesis", prompt="child prompt", narrative="child narrative",
        )
        lineage = await store.lineage(session, child, 10)
        assert [item["node_id"] for item in lineage] == [root, child]
        assert await store.get_node_depth("other_session", root) is None
        await store.set_image(child, url=None, status="mocked")
        nodes = await store.session_nodes(session)
        assert len(nodes) == 2
        assert nodes[1]["image_status"] == "mocked"
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM dream_nodes WHERE session_id=$1", session)
        await store.close()
