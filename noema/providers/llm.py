from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from ..config import Settings
from ..models import EngineMode, LineageNode

_SYSTEM_PROMPTS: dict[EngineMode, str] = {
    EngineMode.SYNTHESIS: (
        "You are Noema, an architectural dream synthesizer. "
        "Respond with vivid, symbolic, existential clarity."
    ),
    EngineMode.COCREATE: (
        "You are Noema in rapid co-creation mode. "
        "Evolve the visual and narrative scene concisely and dynamically."
    ),
}


class ProviderError(RuntimeError):
    """Upstream model failure that should surface to the client as an `error` frame."""


def build_messages(mode: EngineMode, prompt: str, lineage: list[LineageNode]) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": _SYSTEM_PROMPTS[mode]}]
    for node in lineage:
        messages.append({"role": "user", "content": node["prompt"]})
        if node["narrative"]:
            messages.append({"role": "assistant", "content": node["narrative"]})
    messages.append({"role": "user", "content": prompt})
    return messages


async def _mock_tokens(
    settings: Settings, mode: EngineMode, prompt: str, lineage: list[LineageNode]
) -> AsyncIterator[str]:
    anchor = f"branching from '{lineage[-1]['prompt'][:28]}...'" if lineage else "from root void"
    text = (
        f"[{mode.value.upper()} // depth {len(lineage)} // {anchor}] "
        f'The architecture crystallizes around "{prompt}" — refracting latent symbols '
        f"into resonant spatial planes where memory and structure converge."
    )
    delay = (
        settings.mock_token_delay_cocreate
        if mode is EngineMode.COCREATE
        else settings.mock_token_delay_synthesis
    )
    words = text.split(" ")
    for i, word in enumerate(words):
        yield word if i == len(words) - 1 else word + " "
        await asyncio.sleep(delay)


async def stream_llm_tokens(
    client: httpx.AsyncClient,
    settings: Settings,
    mode: EngineMode,
    prompt: str,
    lineage: list[LineageNode],
) -> AsyncIterator[str]:
    """Yield narrative deltas from an OpenAI-compatible /chat/completions SSE stream.

    Raises ProviderError for any upstream/transport/protocol failure. Never raises
    raw httpx/JSON errors so the orchestrator can emit a typed `error` frame.
    """
    if settings.llm_mock_mode:
        async for tok in _mock_tokens(settings, mode, prompt, lineage):
            yield tok
        return

    payload = {
        "model": settings.llm_model,
        "messages": build_messages(mode, prompt, lineage),
        "stream": True,
        "temperature": 0.85 if mode is EngineMode.SYNTHESIS else 0.7,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    timeout = httpx.Timeout(connect=5.0, read=settings.llm_read_timeout_s, write=10.0, pool=5.0)

    try:
        async with client.stream(
            "POST",
            f"{settings.llm_base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread())[:500].decode("utf-8", "replace")
                raise ProviderError(f"LLM HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"LLM sent malformed chunk: {data[:200]!r}") from exc
                if isinstance(chunk, dict) and "error" in chunk:
                    raise ProviderError(f"LLM stream error: {str(chunk['error'])[:300]}")
                choices = chunk.get("choices") if isinstance(chunk, dict) else None
                # Usage-only / keepalive chunks carry an empty choices array.
                if not choices:
                    continue
                delta = (choices[0] or {}).get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
    except httpx.TimeoutException as exc:
        raise ProviderError(f"LLM timed out: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"LLM transport failure: {type(exc).__name__}: {exc}") from exc
