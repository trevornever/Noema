from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from noema.app import create_app
from noema.config import Settings
from noema.models import EngineMode
from noema.providers import ProviderError, stream_llm_tokens, synthesize_image
from noema.store import DreamStore


def fast_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = Settings(
        db_path=str(tmp_path / "noema.db"),
        llm_api_key="",
        image_api_key="",
        mock_token_delay_cocreate=0.0,
        mock_token_delay_synthesis=0.0,
        mock_image_delay_cocreate=0.0,
        mock_image_delay_synthesis=0.0,
        keepalive_s=0.05,
    )
    return replace(base, **overrides)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(fast_settings(tmp_path))) as c:
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    frames = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event, data = "message", []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
        if data:
            frames.append((event, json.loads("\n".join(data))))
    return frames


def run_turn(client: TestClient, **payload: object) -> list[tuple[str, dict]]:
    body = {"mode": "cocreate", "prompt": "a glass cathedral", **payload}
    with client.stream("POST", "/api/v1/stream", json=body) as resp:
        assert resp.status_code == 200, resp.read()
        return parse_sse(resp.read().decode())


def test_full_turn_persists_node_and_image(client: TestClient) -> None:
    frames = run_turn(client, session_id="s1")
    events = [e for e, _ in frames]
    assert events[0] == "meta" and events[-1] == "done"
    assert {"token", "narrative_complete", "image"} <= set(events)
    node_id = frames[0][1]["node_id"]

    tree = client.get("/api/v1/sessions/s1/tree").json()
    assert tree["node_count"] == 1
    node = tree["nodes"][0]
    assert node["node_id"] == node_id
    assert node["depth"] == 0 and node["parent_id"] is None
    assert node["image_status"] == "mocked" and node["image_url"].startswith("https://")
    assert "glass cathedral" in node["narrative"]


def test_branching_depth_and_lineage(client: TestClient) -> None:
    root = run_turn(client, session_id="s2", prompt="root seed")[0][1]["node_id"]
    child = run_turn(client, session_id="s2", parent_id=root, mode="synthesis")[0][1]
    assert child["depth"] == 1 and child["branch_depth"] == 1
    fork = run_turn(client, session_id="s2", parent_id=root)[0][1]
    assert fork["depth"] == 1
    grandchild = run_turn(client, session_id="s2", parent_id=child["node_id"])
    meta = grandchild[0][1]
    assert meta["depth"] == 2
    narrative = "".join(d["text"] for e, d in grandchild if e == "token")
    assert "depth 2" in narrative and "root seed"[:9] not in narrative  # anchors on direct parent
    nodes = client.get("/api/v1/sessions/s2/tree").json()["nodes"]
    assert len(nodes) == 4
    assert sum(1 for n in nodes if n["parent_id"] == root) == 2


def test_unknown_or_cross_session_parent_is_rejected(client: TestClient) -> None:
    other = run_turn(client, session_id="sA")[0][1]["node_id"]
    r = client.post("/api/v1/stream", json={"session_id": "sB", "parent_id": other, "mode": "cocreate", "prompt": "x"})
    assert r.status_code == 404
    r = client.post("/api/v1/stream", json={"session_id": "sB", "parent_id": "nope", "mode": "cocreate", "prompt": "x"})
    assert r.status_code == 404
    assert client.get("/api/v1/sessions/sB/tree").json()["node_count"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "dream", "prompt": "x"},
        {"mode": "cocreate", "prompt": ""},
        {"mode": "cocreate", "prompt": "   "},
        {"mode": "cocreate", "prompt": "x" * 4001},
        {"mode": "cocreate", "prompt": "x", "session_id": "../etc/passwd"},
        {"mode": "cocreate", "prompt": "x", "extra": 1},
    ],
)
def test_validation(client: TestClient, body: dict) -> None:
    assert client.post("/api/v1/stream", json=body).status_code == 422


def test_export_markdown(client: TestClient) -> None:
    assert client.get("/api/v1/sessions/empty/export").status_code == 404
    run_turn(client, session_id="s3", prompt="obsidian lighthouse")
    r = client.get("/api/v1/sessions/s3/export")
    assert r.status_code == 200
    assert "# Noema Session Archive" in r.text and "obsidian lighthouse" in r.text and "![Render](" in r.text


def test_root_serves_studio_and_health(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "Noema Studio" in r.text
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["llm"] == "mock" and h["active_streams"] == 0


def test_llm_failure_emits_error_frame_and_persists_nothing(tmp_path: Path) -> None:
    settings = fast_settings(tmp_path, llm_api_key="k", llm_base_url="http://127.0.0.1:9")
    with TestClient(create_app(settings)) as c:
        frames = run_turn(c, session_id="s4")
        assert frames[-1][0] == "error" and frames[-1][1]["stage"] == "narrative"
        assert c.get("/api/v1/sessions/s4/tree").json()["node_count"] == 0
        h = c.get("/api/v1/health").json()
        assert h["turns_failed"] == 1 and h["inflight_image_tasks"] == 0


def test_keepalive_during_slow_render(tmp_path: Path) -> None:
    settings = fast_settings(tmp_path, mock_image_delay_cocreate=0.3, keepalive_s=0.05)
    with TestClient(create_app(settings)) as c:
        with c.stream("POST", "/api/v1/stream", json={"mode": "cocreate", "prompt": "x"}) as resp:
            raw = resp.read().decode()
    assert raw.count(": keepalive") >= 2
    assert parse_sse(raw)[-1][0] == "done"


# ---------- provider adapters against stubbed upstreams ----------


def _sse_body(*chunks: object) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def test_llm_adapter_parses_openai_stream() -> None:
    body = _sse_body(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": []},  # usage-only chunk
        {"choices": [{"delta": {"content": " void"}}]},
    )
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["payload"] = json.loads(req.content)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def go() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            s = Settings(llm_api_key="sk", llm_base_url="https://llm.test/v1", db_path=":memory:")
            lineage = [{"node_id": "a", "parent_id": None, "prompt": "p0", "narrative": "n0"}]
            return [t async for t in stream_llm_tokens(http, s, EngineMode.SYNTHESIS, "p1", lineage)]

    assert asyncio.run(go()) == ["Hello", " void"]
    assert seen["auth"] == "Bearer sk"
    roles = [m["role"] for m in seen["payload"]["messages"]]  # type: ignore[index]
    assert roles == ["system", "user", "assistant", "user"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "bad key"}),
        httpx.Response(200, content=b"data: {not json}\n\n"),
        httpx.Response(200, content=b'data: {"error": {"message": "overloaded"}}\n\n'),
    ],
)
def test_llm_adapter_raises_provider_error(response: httpx.Response) -> None:
    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
            s = Settings(llm_api_key="sk", db_path=":memory:")
            async for _ in stream_llm_tokens(http, s, EngineMode.COCREATE, "p", []):
                pass

    with pytest.raises(ProviderError):
        asyncio.run(go())


def _img_settings(**kw: object) -> Settings:
    return replace(
        Settings(db_path=":memory:"),
        image_api_key="ik",
        image_api_url="https://img.test/gen",
        image_poll_interval_s=0.0,
        **kw,
    )


def test_image_adapter_retries_5xx_then_polls() -> None:
    calls = {"post": 0, "poll": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, json={"polling_url": "https://img.test/poll/1"})
        calls["poll"] += 1
        if calls["poll"] < 2:
            return httpx.Response(200, json={"status": "Pending"})
        return httpx.Response(200, json={"status": "Ready", "result": {"sample": "https://cdn.test/x.png"}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await synthesize_image(http, _img_settings(), "p", EngineMode.SYNTHESIS, "idem-1")

    assert asyncio.run(go()) == {"status": "ready", "url": "https://cdn.test/x.png"}
    assert calls == {"post": 2, "poll": 2}


def test_image_adapter_does_not_retry_4xx_and_uses_custom_header() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(400, text="moderated")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await synthesize_image(
                http, _img_settings(image_auth_header="x-key"), "p", EngineMode.COCREATE, "idem-2"
            )

    out = asyncio.run(go())
    assert out["status"] == "error" and "400" in out["detail"]
    assert len(calls) == 1
    assert calls[0].headers["x-key"] == "ik" and calls[0].headers["idempotency-key"] == "idem-2"


def test_image_adapter_survives_malformed_payload() -> None:
    async def go():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"<html>"))
        async with httpx.AsyncClient(transport=transport) as http:
            return await synthesize_image(http, _img_settings(), "p", EngineMode.COCREATE, "k")

    assert asyncio.run(go())["status"] == "error"


def test_store_lineage_is_bounded_and_session_scoped(tmp_path: Path) -> None:
    async def go() -> None:
        store = DreamStore(str(tmp_path / "s.db"))
        await store.open()
        try:
            parent = None
            for i in range(25):
                await store.insert_node(
                    node_id=f"n{i}", session_id="s", parent_id=parent, depth=i,
                    mode="cocreate", prompt=f"p{i}", narrative=f"n{i}",
                )
                parent = f"n{i}"
            chain = await store.lineage("s", "n24", limit=10)
            assert [n["node_id"] for n in chain] == [f"n{i}" for i in range(15, 25)]
            assert await store.lineage("other", "n24", limit=10) == []
            assert await store.get_node_depth("s", "n24") == 24
            assert await store.get_node_depth("other", "n24") is None
        finally:
            await store.close()

    asyncio.run(go())
