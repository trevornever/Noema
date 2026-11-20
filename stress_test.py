"""Concurrency & chaos harness for the Noema engine.

Each virtual user runs a branching chain of turns against /api/v1/stream. A fraction
of turns hang up mid-token (chaos aborts). Afterwards the harness verifies:

  * every completed turn is in the session tree with the correct parent edge,
  * no aborted-mid-token turn was persisted,
  * the server's live counters (active streams, in-flight image tasks) drain to zero,
    i.e. client disconnects leave no zombie work behind.

Exit code is non-zero if any invariant is violated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass(slots=True)
class UserResult:
    worker: int
    completed: int = 0
    aborted: int = 0
    errors: list[str] = field(default_factory=list)
    ttft_ms: list[float] = field(default_factory=list)
    turn_ms: list[float] = field(default_factory=list)


async def iter_sse(resp: httpx.Response):
    """Yield (event, data_dict) for each SSE frame; comments/keepalives are skipped."""
    event, data = "message", []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)


async def run_virtual_user(
    client: httpx.AsyncClient, base: str, worker: int, turns: int, abort_every: int
) -> UserResult:
    res = UserResult(worker=worker)
    session_id = str(uuid.uuid4())
    parent_id: str | None = None
    expected_edges: dict[str, str | None] = {}
    aborted_ids: set[str] = set()

    for turn in range(turns):
        simulate_abort = abort_every > 0 and turn == 0 and worker % abort_every == 0
        payload = {
            "session_id": session_id,
            "parent_id": parent_id,
            "mode": "cocreate" if turn % 2 == 0 else "synthesis",
            "prompt": f"Worker {worker} turn {turn}: crystalline clockwork forest",
        }
        t0 = time.perf_counter()
        node_id: str | None = None
        tokens = 0
        got_done = False
        try:
            async with client.stream("POST", f"{base}/api/v1/stream", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:200]
                    res.errors.append(f"turn {turn}: HTTP {resp.status_code} {body}")
                    break
                async for event, data in iter_sse(resp):
                    if event == "meta":
                        node_id = data["node_id"]
                    elif event == "token":
                        if tokens == 0:
                            res.ttft_ms.append((time.perf_counter() - t0) * 1000)
                        tokens += 1
                        if simulate_abort and tokens >= 3:
                            break  # exits the stream context -> connection is dropped
                    elif event == "error":
                        res.errors.append(f"turn {turn}: server error frame {data}")
                    elif event == "done":
                        got_done = True
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            res.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")
            break

        if simulate_abort:
            res.aborted += 1
            if node_id:
                aborted_ids.add(node_id)
            continue  # parent stays unchanged; next turn forks from the same point
        if not got_done or node_id is None:
            res.errors.append(f"turn {turn}: stream ended without done frame")
            break
        res.completed += 1
        res.turn_ms.append((time.perf_counter() - t0) * 1000)
        expected_edges[node_id] = parent_id
        parent_id = node_id

    try:
        tree = await client.get(f"{base}/api/v1/sessions/{session_id}/tree")
        tree.raise_for_status()
        nodes = {n["node_id"]: n for n in tree.json()["nodes"]}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        res.errors.append(f"tree fetch failed: {exc}")
        return res

    for nid, parent in expected_edges.items():
        if nid not in nodes:
            res.errors.append(f"completed node {nid[:8]} missing from tree")
        elif nodes[nid]["parent_id"] != parent:
            res.errors.append(f"node {nid[:8]} has wrong parent edge")
    for nid in aborted_ids & nodes.keys():
        res.errors.append(f"aborted node {nid[:8]} was persisted")
    return res


async def wait_for_drain(client: httpx.AsyncClient, base: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    health: dict = {}
    while time.monotonic() < deadline:
        health = (await client.get(f"{base}/api/v1/health")).json()
        if health.get("active_streams") == 0 and health.get("inflight_image_tasks") == 0:
            return health
        await asyncio.sleep(0.1)
    return health


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--abort-every", type=int, default=4, help="every Nth user aborts its first turn (0=never)")
    ap.add_argument("--drain-timeout", type=float, default=10.0)
    args = ap.parse_args()
    base = args.url.rstrip("/")

    limits = httpx.Limits(max_connections=args.users * 2, max_keepalive_connections=args.users)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0), limits=limits) as client:
        try:
            (await client.get(f"{base}/api/v1/health")).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Server not reachable at {base}: {exc}", file=sys.stderr)
            return 2

        start = time.perf_counter()
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(run_virtual_user(client, base, i, args.turns, args.abort_every))
                for i in range(args.users)
            ]
        elapsed = time.perf_counter() - start
        results = [t.result() for t in tasks]
        health = await wait_for_drain(client, base, args.drain_timeout)

    ttft = [v for r in results for v in r.ttft_ms]
    turn_ms = [v for r in results for v in r.turn_ms]
    errors = [f"[worker {r.worker}] {e}" for r in results for e in r.errors]
    zombies = health.get("active_streams", -1) != 0 or health.get("inflight_image_tasks", -1) != 0

    print("\n=== NOEMA CONCURRENCY STRESS REPORT ===")
    print(f"Users x turns     : {args.users} x {args.turns}")
    print(f"Total wall time   : {elapsed:.2f}s")
    print(f"Completed turns   : {sum(r.completed for r in results)}")
    print(f"Chaos aborts      : {sum(r.aborted for r in results)}")
    print(f"Errors            : {len(errors)}")
    if ttft:
        print(f"TTFT p50/p95/max  : {pct(ttft, .5):.1f} / {pct(ttft, .95):.1f} / {max(ttft):.1f} ms")
    if turn_ms:
        print(f"Turn mean/p95     : {statistics.fmean(turn_ms):.0f} / {pct(turn_ms, .95):.0f} ms")
    print(
        f"Server after drain: active_streams={health.get('active_streams')} "
        f"inflight_image_tasks={health.get('inflight_image_tasks')} "
        f"aborted={health.get('turns_aborted')} failed={health.get('turns_failed')}"
    )
    for e in errors[:20]:
        print("  !", e)
    if zombies:
        print("FAIL: server did not drain — zombie streams or image tasks remain")
    ok = not errors and not zombies
    print("RESULT            :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
