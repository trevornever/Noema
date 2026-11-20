from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .models import DreamTurnRequest, ImageResult, LineageNode
from .providers import ProviderError, stream_llm_tokens, synthesize_image
from .store import DreamStore

log = logging.getLogger("noema.orchestrator")


@dataclass(slots=True)
class EngineMetrics:
    """Live resource counters. Must return to zero once all clients are gone."""

    active_streams: int = 0
    inflight_image_tasks: int = 0
    turns_completed: int = 0
    turns_aborted: int = 0
    turns_failed: int = 0
    _image_tasks: set[asyncio.Task[ImageResult]] = field(default_factory=set)

    def track_image(self, task: asyncio.Task[ImageResult]) -> None:
        self._image_tasks.add(task)
        self.inflight_image_tasks = len(self._image_tasks)

        def _done(t: asyncio.Task[ImageResult]) -> None:
            self._image_tasks.discard(t)
            self.inflight_image_tasks = len(self._image_tasks)

        task.add_done_callback(_done)

    async def cancel_all(self) -> None:
        tasks = list(self._image_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self) -> dict[str, int]:
        return {
            "active_streams": self.active_streams,
            "inflight_image_tasks": self.inflight_image_tasks,
            "turns_completed": self.turns_completed,
            "turns_aborted": self.turns_aborted,
            "turns_failed": self.turns_failed,
        }


def sse_frame(event: str, data: dict[str, Any]) -> str:
    # json.dumps never emits raw newlines, so a single `data:` line is always valid SSE.
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


SSE_KEEPALIVE = ": keepalive\n\n"


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """Everything validated before the HTTP 200 is committed, so bad input gets a real 4xx."""

    req: DreamTurnRequest
    node_id: str
    depth: int
    lineage: list[LineageNode]


async def orchestrate_turn(
    plan: TurnPlan,
    *,
    store: DreamStore,
    client: httpx.AsyncClient,
    settings: Settings,
    metrics: EngineMetrics,
) -> AsyncIterator[str]:
    req, node_id = plan.req, plan.node_id
    metrics.active_streams += 1
    image_task: asyncio.Task[ImageResult] = asyncio.create_task(
        synthesize_image(client, settings, req.prompt, req.mode, idempotency_key=node_id),
        name=f"noema-image-{node_id[:8]}",
    )
    metrics.track_image(image_task)
    persisted = False
    finished = False

    try:
        yield sse_frame(
            "meta",
            {
                "session_id": req.session_id,
                "node_id": node_id,
                "parent_id": req.parent_id,
                "mode": req.mode.value,
                "depth": plan.depth,
                "branch_depth": len(plan.lineage),
            },
        )

        parts: list[str] = []
        try:
            async for token in stream_llm_tokens(client, settings, req.mode, req.prompt, plan.lineage):
                parts.append(token)
                yield sse_frame("token", {"text": token})
        except ProviderError as exc:
            log.warning("LLM failure on node %s: %s", node_id, exc)
            metrics.turns_failed += 1
            finished = True
            yield sse_frame("error", {"stage": "narrative", "detail": str(exc)})
            return

        narrative = "".join(parts).strip()
        await store.insert_node(
            node_id=node_id,
            session_id=req.session_id,
            parent_id=req.parent_id,
            depth=plan.depth,
            mode=req.mode.value,
            prompt=req.prompt,
            narrative=narrative,
        )
        persisted = True
        yield sse_frame("narrative_complete", {"node_id": node_id})

        # Keepalive comments while the render is in flight: keeps proxies from idling the
        # connection out AND forces a send, which is how ASGI 2.4 servers surface disconnects.
        while True:
            done, _ = await asyncio.wait({image_task}, timeout=settings.keepalive_s or None)
            if done:
                break
            yield SSE_KEEPALIVE

        result = image_task.result()
        await store.set_image(node_id, url=result.get("url"), status=result.get("status", "error"))
        metrics.turns_completed += 1
        finished = True
        yield sse_frame("image", dict(result))
        yield sse_frame("done", {"node_id": node_id})
    except Exception:
        # Store/internal failure mid-stream: headers are already sent, so report in-band.
        log.exception("Turn %s failed", node_id)
        metrics.turns_failed += 1
        finished = True
        yield sse_frame("error", {"stage": "internal", "detail": "Internal engine failure"})
    finally:
        metrics.active_streams -= 1
        if not finished:
            metrics.turns_aborted += 1
        if not image_task.done():
            image_task.cancel()
            if persisted:
                try:
                    await asyncio.shield(store.set_image(node_id, url=None, status="cancelled"))
                except Exception:  # best effort; never mask the original exit reason
                    log.exception("Failed to mark node %s image as cancelled", node_id)


def new_node_id() -> str:
    return str(uuid.uuid4())
