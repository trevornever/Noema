# Noema — Dual-Mode Imagination & Dream Engine

A FastAPI engine that streams narrative and visual synthesis over Server-Sent Events. Each turn is saved as a node in a branching DAG. SQLite is the direct-run default; PostgreSQL is available through `NOEMA_DATABASE_URL` and is used by Docker Compose. A single process serves the API and the studio UI.

```
noema/
  app.py            FastAPI factory: routes, lifespan, deterministic-close SSE response
  orchestrator.py   per-turn pipeline: meta → tokens → persist → keepalive → image → done
  store.py          SQLite store, recursive-CTE lineage
  postgres_store.py asyncpg PostgreSQL store, recursive-CTE lineage
  config.py         env-driven frozen Settings
  models.py         request/response contracts
  providers/llm.py  OpenAI-compatible streaming adapter (+ zero-key mock)
  providers/image.py  REST/polling image adapter, 429/5xx-only retry (+ zero-key mock)
  static/index.html studio UI (DAG tree, rewind/fork, stop, export)
main.py             uvicorn entry point
stress_test.py      concurrency + chaos harness with server-side drain verification
tests/              pytest suite (API, adapters vs. stubbed upstreams, store)
```

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn main:app --port 8000          # open http://localhost:8000
pytest -q                             # 21 tests
python stress_test.py --users 50 --turns 3
```

With no keys set, both providers run in mock mode, so everything works out of the box.

| Variable | Purpose | Default |
|---|---|---|
| `LLM_MOCK_MODE` | Force mock mode (`true`) or a live OpenAI-compatible endpoint (`false`) | inferred from whether `LLM_API_KEY` is set |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | Any OpenAI-compatible `/chat/completions` endpoint; API key is optional for local endpoints such as Ollama | mock / OpenAI / `gpt-4o-mini` |
| `IMAGE_API_KEY` / `IMAGE_API_URL` | Image provider (direct URL or `polling_url` flow, e.g. BFL/Flux) | mock |
| `IMAGE_AUTH_HEADER` | `Authorization` sends `Bearer <key>`; any other header name (e.g. `x-key`) sends the raw key | `Authorization` |
| `NOEMA_DB_PATH` | SQLite file when PostgreSQL DSN is unset | `./noema_memory.db` |
| `NOEMA_DATABASE_URL` | Optional PostgreSQL DSN (asyncpg) | unset |
| `NOEMA_KEEPALIVE_S` | SSE keepalive interval while a render is in flight | `2.0` |
| `NOEMA_LINEAGE_LIMIT` | Ancestors fed to the LLM as context | `10` |
| `NOEMA_CORS_ORIGINS` | Comma-separated origins | `*` |

## API

* `POST /api/v1/stream` takes `{session_id, parent_id?, mode: cocreate|synthesis, prompt}` and returns an SSE stream of
  `meta → token* → narrative_complete → (: keepalive)* → image → done`, or an `error` frame (`stage: narrative|internal`).
  It returns **404** before streaming if `parent_id` doesn't exist in that session, and **422** for invalid input.
* `GET /api/v1/sessions/{id}/tree` returns all nodes, including `depth` and `image_status` (`pending|ready|mocked|error|cancelled`).
* `GET /api/v1/sessions/{id}/export` returns a Markdown archive.
* `GET /api/v1/health` returns DB status, provider modes, and live counters (`active_streams`, `inflight_image_tasks`).

## Persistence semantics

* Abort mid-narrative: nothing is saved.
* Narrative completes: the node is saved immediately, before the render finishes.
* Abort during the render: the image task is cancelled at once and the node is marked `image_status=cancelled`.

## Fixes over the archived prototype

| Defect in archive | Fix |
|---|---|
| `innerHTML` with prompts, image URLs, and error text: stored XSS | All DOM built through `textContent`/`createElement`; image URLs restricted to http(s) |
| `parent_id` never validated: orphan or cross-session edges, and context leaked across sessions | Pre-stream 404, FK constraint, session-scoped lineage CTE |
| Connection per request with no `busy_timeout`: `database is locked` under concurrent writes | One aiosqlite connection (one serialised thread), write lock, `busy_timeout` |
| Lineage loaded the **entire session** on every turn: O(n) | Recursive CTE bounded by `NOEMA_LINEAGE_LIMIT`, plus a stored `depth` column |
| No sends while waiting on the image, so ASGI ≥2.4 never noticed a disconnect and image tasks ran to completion | Keepalive frames during the render, plus a `StreamingResponse` subclass that `aclose()`s the generator |
| LLM errors, malformed chunks, and empty `choices` crashed the stream mid-response | Typed `ProviderError` that becomes an `error` frame |
| Image adapter retried 4xx and crashed on schema drift | Retries only 408/409/425/429/5xx (jittered backoff, honours `Retry-After`); never raises |
| Node lost if the client left during the render | Node saved when the narrative completes |
| UI ignored HTTP errors; the tree was ordered by time, not hierarchy | `res.ok` handling, DFS tree with lineage highlight, Stop/Esc, Alt+1/2 mode switch |
| Harness only counted client-side results | Checks parent edges, that aborted nodes were not saved, and that **server counters drain to 0** |


## Self-host with Docker

See [DEPLOY_SELF_HOSTED.md](DEPLOY_SELF_HOSTED.md) for the Docker Compose setup.
The starter runs locally in mock mode, persists PostgreSQL data in a named volume,
and offers an optional Ollama profile for local inference without a provider API key. It is intentionally
loopback-bound and is not yet a public multi-user deployment. Authentication and versioned database migrations remain planned follow-on work.


## Database operations

Versioned PostgreSQL migrations are applied transactionally at startup. See
[OPERATIONS.md](OPERATIONS.md) for the SQLite importer, dry-run validation,
backup, and explicitly guarded restore procedure. Do not expose the local
Compose instance publicly; authentication and multi-user authorization remain
prerequisites for internet-facing deployment.
