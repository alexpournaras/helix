"""Entry point: python backend/run.py  (honours HELIX_HOST / HELIX_PORT / HELIX_RELOAD)."""

import os
from pathlib import Path

import uvicorn

BACKEND_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        app_dir=str(BACKEND_DIR),
        host=os.environ.get("HELIX_HOST", "127.0.0.1"),
        port=int(os.environ.get("HELIX_PORT", "8000")),
        reload=os.environ.get("HELIX_RELOAD", "1") == "1",
        reload_dirs=[str(BACKEND_DIR / "app")],
        # Vite's proxy keeps connections alive; without a cap, reloads hang waiting for them.
        timeout_graceful_shutdown=1,
    )
