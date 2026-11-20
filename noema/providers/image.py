from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from ..config import Settings
from ..models import EngineMode, ImageResult

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MAX_BACKOFF_S = 8.0


def _auth_headers(settings: Settings, idempotency_key: str) -> dict[str, str]:
    header = settings.image_auth_header.strip() or "Authorization"
    value = (
        f"Bearer {settings.image_api_key}"
        if header.lower() == "authorization"
        else settings.image_api_key
    )
    return {
        header: value,
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_BACKOFF_S)
        except ValueError:
            pass
    base = min(0.4 * (2**attempt), _MAX_BACKOFF_S)
    return base * (0.5 + random.random() / 2)  # jitter to avoid thundering-herd retries


def _extract_url(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("url", "image_url"):
        if isinstance(body.get(key), str):
            return body[key]
    result = body.get("result")
    if isinstance(result, dict) and isinstance(result.get("sample"), str):
        return result["sample"]
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        url = data[0].get("url")
        if isinstance(url, str):
            return url
    return None


async def _poll(
    client: httpx.AsyncClient, settings: Settings, polling_url: str, headers: dict[str, str]
) -> ImageResult:
    deadline = time.monotonic() + settings.image_poll_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(settings.image_poll_interval_s)
        resp = await client.get(polling_url, headers=headers)
        if resp.status_code in _RETRYABLE_STATUS:
            continue
        resp.raise_for_status()
        pdata = resp.json()
        status = str(pdata.get("status", "")) if isinstance(pdata, dict) else ""
        if status.lower() in {"ready", "succeeded", "completed"}:
            url = _extract_url(pdata)
            if url:
                return ImageResult(status="ready", url=url)
            return ImageResult(status="error", url=None, detail="Provider reported ready without URL")
        if status.lower() in {"error", "failed", "content moderated", "request moderated"}:
            return ImageResult(status="error", url=None, detail=f"Provider status: {status}")
    return ImageResult(status="error", url=None, detail="Image polling timed out")


async def synthesize_image(
    client: httpx.AsyncClient,
    settings: Settings,
    prompt: str,
    mode: EngineMode,
    idempotency_key: str,
) -> ImageResult:
    """Render a frame. Never raises (except CancelledError); failures become status=error."""
    if not settings.image_api_key:
        await asyncio.sleep(
            settings.mock_image_delay_cocreate
            if mode is EngineMode.COCREATE
            else settings.mock_image_delay_synthesis
        )
        return ImageResult(
            status="mocked",
            url=f"https://picsum.photos/seed/{idempotency_key[:10]}/1024/576",
        )

    headers = _auth_headers(settings, idempotency_key)
    style = "Surrealist symbolic dreamscape" if mode is EngineMode.SYNTHESIS else "Concept art frame"
    payload = {"prompt": f"{style}: {prompt}", "aspect_ratio": "16:9"}
    last_detail = "Exhausted retries"

    for attempt in range(settings.image_max_attempts):
        try:
            resp = await client.post(settings.image_api_url, json=payload, headers=headers)
            if resp.status_code in _RETRYABLE_STATUS:
                last_detail = f"HTTP {resp.status_code}"
                if attempt + 1 < settings.image_max_attempts:
                    await asyncio.sleep(_backoff(attempt, resp.headers.get("retry-after")))
                continue
            if resp.status_code >= 400:
                # 4xx (auth, validation, moderation) will not succeed on retry.
                return ImageResult(
                    status="error",
                    url=None,
                    detail=f"HTTP {resp.status_code}: {resp.text[:300]}",
                )
            body = resp.json()
            polling_url = body.get("polling_url") if isinstance(body, dict) else None
            if isinstance(polling_url, str):
                return await _poll(client, settings, polling_url, headers)
            url = _extract_url(body)
            if url:
                return ImageResult(status="ready", url=url)
            return ImageResult(status="error", url=None, detail="Provider response had no image URL")
        except asyncio.CancelledError:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < settings.image_max_attempts:
                await asyncio.sleep(_backoff(attempt, None))
        except (ValueError, KeyError, TypeError) as exc:  # malformed JSON / schema drift
            return ImageResult(status="error", url=None, detail=f"Bad provider payload: {exc}")
    return ImageResult(status="error", url=None, detail=last_detail)
