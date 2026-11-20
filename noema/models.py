from __future__ import annotations

import uuid
from enum import Enum
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

# Opaque IDs: UUIDs from the client/server, but anything URL-safe and bounded is accepted.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class EngineMode(str, Enum):
    SYNTHESIS = "synthesis"  # deep, reflective analysis + full scene render
    COCREATE = "cocreate"  # rapid iterative branching + progressive visuals


class DreamTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=ID_PATTERN)
    parent_id: str | None = Field(default=None, pattern=ID_PATTERN)
    mode: EngineMode
    prompt: str = Field(..., min_length=1, max_length=4000)


ImageStatus = Literal["pending", "ready", "mocked", "error", "cancelled"]


class ImageResult(TypedDict, total=False):
    status: ImageStatus
    url: str | None
    detail: str


class LineageNode(TypedDict):
    node_id: str
    parent_id: str | None
    prompt: str
    narrative: str


class DreamNode(TypedDict):
    node_id: str
    session_id: str
    parent_id: str | None
    depth: int
    mode: str
    prompt: str
    narrative: str
    image_url: str | None
    image_status: str
    created_at: float
