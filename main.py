"""Entry point: `uvicorn main:app --port 8000` or `python main.py`."""

from __future__ import annotations

import os

from noema.app import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("NOEMA_HOST", "127.0.0.1"),
        port=int(os.getenv("NOEMA_PORT", "8000")),
        log_level=os.getenv("NOEMA_LOG_LEVEL", "info"),
    )
