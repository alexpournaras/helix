import json
import platform
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ProviderKeys, settings
from .integrations import Integration, catalog_with_status
from .providers import ollama
from .store import WorkspaceSettings, store

STARTED_AT = time.time()

app = FastAPI(title=f"{settings.app_name} API", version=settings.version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "python": platform.python_version(),
        "uptime_seconds": round(time.time() - STARTED_AT),
    }


@app.get("/api/overview")
def overview():
    catalog = catalog_with_status()
    # Research counters stay at zero until those modules exist.
    return {
        "projects": 0,
        "datasets": 0,
        "ai_runs": 0,
        "literature": 0,
        "integrations_total": len(catalog),
        "integrations_ready": sum(1 for i in catalog if i.status != "planned"),
        "ai_keys": sorted(k.upper() for k in ProviderKeys().present() if k != "ncbi_api_key"),
    }


@app.get("/api/integrations", response_model=list[Integration])
def list_integrations():
    return catalog_with_status()


# ---------- Ollama ----------

@app.get("/api/integrations/ollama/config", response_model=ollama.OllamaConfig)
def get_ollama_config():
    return ollama.load_config()


@app.put("/api/integrations/ollama/config", response_model=ollama.OllamaConfig)
def put_ollama_config(body: ollama.OllamaConfig):
    return ollama.save_config(body)


@app.get("/api/integrations/ollama/status")
async def get_ollama_status(base_url: str | None = None):
    """Contact the configured server: reachability, version, installed and loaded models.
    Pass `base_url` to test a different address without saving it."""
    config = ollama.load_config()
    if base_url is not None:
        try:
            config = ollama.OllamaConfig(**{**config.model_dump(), "base_url": base_url})
        except ValueError as exc:
            message = exc.errors()[0]["msg"].removeprefix("Value error, ") if hasattr(exc, "errors") else str(exc)
            raise HTTPException(status_code=422, detail=message)
    return await ollama.status(config)


@app.post("/api/integrations/ollama/chat")
async def ollama_chat(body: ollama.ChatRequest):
    """Stream a chat reply as NDJSON events: start, thinking, content, done or error."""
    config = ollama.load_config()

    async def events():
        async for event in ollama.chat_stream(config, body):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@app.get("/api/settings", response_model=WorkspaceSettings)
def get_settings():
    return store.load()


@app.put("/api/settings", response_model=WorkspaceSettings)
def put_settings(body: WorkspaceSettings):
    return store.save(body)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_not_found(path: str):
    raise HTTPException(status_code=404, detail=f"Unknown endpoint /api/{path}")


# Production: serve the built SPA from ../dist with history-API fallback.
dist: Path = settings.web_dist
if dist.is_dir():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
