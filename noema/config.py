from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def _env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: str = field(
        default_factory=lambda: os.getenv("NOEMA_DB_PATH", str(PROJECT_DIR / "noema_memory.db"))
    )
    database_url: str = field(default_factory=lambda: os.getenv("NOEMA_DATABASE_URL", ""))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list("NOEMA_CORS_ORIGINS", "*")
    )
    lineage_limit: int = field(default_factory=lambda: _env_int("NOEMA_LINEAGE_LIMIT", 10))
    keepalive_s: float = field(default_factory=lambda: _env_float("NOEMA_KEEPALIVE_S", 2.0))
    # Mock-provider pacing (seconds per token / per render). 0 is valid for tests.
    mock_token_delay_cocreate: float = field(
        default_factory=lambda: _env_float("NOEMA_MOCK_TOKEN_DELAY_COCREATE", 0.025)
    )
    mock_token_delay_synthesis: float = field(
        default_factory=lambda: _env_float("NOEMA_MOCK_TOKEN_DELAY_SYNTHESIS", 0.045)
    )
    mock_image_delay_cocreate: float = field(
        default_factory=lambda: _env_float("NOEMA_MOCK_IMAGE_DELAY_COCREATE", 0.6)
    )
    mock_image_delay_synthesis: float = field(
        default_factory=lambda: _env_float("NOEMA_MOCK_IMAGE_DELAY_SYNTHESIS", 1.2)
    )

    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    )
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    llm_read_timeout_s: float = field(
        default_factory=lambda: _env_float("LLM_READ_TIMEOUT_S", 30.0)
    )

    image_api_key: str = field(default_factory=lambda: os.getenv("IMAGE_API_KEY", ""))
    image_api_url: str = field(
        default_factory=lambda: os.getenv(
            "IMAGE_API_URL", "https://api.example-image-provider.com/v1/generate"
        )
    )
    # "Authorization" → sends "Bearer <key>". Any other header (e.g. BFL's "x-key") → raw key.
    image_auth_header: str = field(
        default_factory=lambda: os.getenv("IMAGE_AUTH_HEADER", "Authorization")
    )
    image_poll_interval_s: float = field(
        default_factory=lambda: _env_float("IMAGE_POLL_INTERVAL_S", 0.5)
    )
    image_poll_timeout_s: float = field(
        default_factory=lambda: _env_float("IMAGE_POLL_TIMEOUT_S", 60.0)
    )
    image_max_attempts: int = field(default_factory=lambda: _env_int("IMAGE_MAX_ATTEMPTS", 3))
