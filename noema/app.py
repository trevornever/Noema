from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from starlette.types import Send

from . import __version__
from .config import PACKAGE_DIR, Settings
from .models import ID_PATTERN, DreamTurnRequest
from .orchestrator import EngineMetrics, TurnPlan, new_node_id, orchestrate_turn
from .store import DreamStore
from .postgres_store import PostgresDreamStore

log = logging.getLogger("noema")
INDEX_HTML = PACKAGE_DIR / "static" / "index.html"
SessionId = Annotated[str, Path(pattern=ID_PATTERN)]


class ClosingStreamingResponse(StreamingResponse):
    """Deterministically aclose() the body generator on client disconnect.

    Under ASGI spec >= 2.4 Starlette only notices a disconnect when send() raises and
    then abandons the iterator to GC finalisation. Closing it explicitly guarantees the
    orchestrator's `finally` (image-task cancellation, counters) runs immediately.
    """

    async def stream_response(self, send: Send) -> None:
        iterator = self.body_iterator
        if hasattr(iterator, "aclose"):
            async with aclosing(iterator):  # type: ignore[type-var]
                await super().stream_response(send)
        else:
            await super().stream_response(send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = (\n            PostgresDreamStore(settings.database_url)\n            if settings.database_url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://"))\n            else DreamStore(settings.db_path)\n        )
        await store.open()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        metrics = EngineMetrics()
        app.state.store, app.state.http, app.state.metrics = store, client, metrics
        try:
            yield
        finally:
            await metrics.cancel_all()
            await client.aclose()
            await store.close()

    app = FastAPI(title="Noema Dual-Mode Imagination Studio", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/", include_in_schema=False)
    async def serve_studio() -> FileResponse:
        if not INDEX_HTML.is_file():
            raise HTTPException(status_code=404, detail="Studio UI missing (noema/static/index.html)")
        return FileResponse(INDEX_HTML, media_type="text/html")

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, object]:
        store = request.app.state.store
        metrics: EngineMetrics = request.app.state.metrics
        db_ok = await store.ping()
        return {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "db": db_ok,
            "llm": "live" if settings.llm_api_key else "mock",
            "image": "live" if settings.image_api_key else "mock",
            **metrics.snapshot(),
        }

    @app.post("/api/v1/stream")
    async def stream_turn(request: Request, payload: DreamTurnRequest) -> StreamingResponse:
        store: DreamStore = request.app.state.store
        depth = 0
        if payload.parent_id is not None:
            parent_depth = await store.get_node_depth(payload.session_id, payload.parent_id)
            if parent_depth is None:
                # Reject orphan/cross-session parents before committing the 200 + SSE stream.
                raise HTTPException(status_code=404, detail="parent_id not found in this session")
            depth = parent_depth + 1
        lineage = await store.lineage(payload.session_id, payload.parent_id, settings.lineage_limit)
        plan = TurnPlan(req=payload, node_id=new_node_id(), depth=depth, lineage=lineage)
        return ClosingStreamingResponse(
            orchestrate_turn(
                plan,
                store=store,
                client=request.app.state.http,
                settings=settings,
                metrics=request.app.state.metrics,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/sessions/{session_id}/tree")
    async def get_session_tree(request: Request, session_id: SessionId) -> dict[str, object]:
        nodes = await request.app.state.store.session_nodes(session_id)
        return {"session_id": session_id, "node_count": len(nodes), "nodes": nodes}

    @app.get("/api/v1/sessions/{session_id}/export", response_class=PlainTextResponse)
    async def export_session_markdown(request: Request, session_id: SessionId) -> PlainTextResponse:
        nodes = await request.app.state.store.session_nodes(session_id)
        if not nodes:
            raise HTTPException(status_code=404, detail="No nodes found for session")
        lines = [f"# Noema Session Archive (`{session_id}`)", ""]
        for idx, n in enumerate(nodes, 1):
            parent = n["parent_id"][:8] if n["parent_id"] else "ROOT"
            lines += [
                f"## Node {idx} — `{n['node_id'][:8]}` ({n['mode'].upper()}, depth {n['depth']})",
                f"- **Parent:** `{parent}`",
                f"- **Prompt:** {n['prompt']}",
                "",
                n["narrative"],
                "",
            ]
            if n["image_url"]:
                lines += [f"![Render]({n['image_url']})", ""]
            lines += ["---", ""]
        return PlainTextResponse(
            "\n".join(lines),
            # text/plain so browsers render it in a tab instead of forcing a download.
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="noema-{session_id[:8]}.md"'},
        )

    return app


app = create_app()
