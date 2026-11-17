import json
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ProviderKeys, settings
from .integrations import Integration, catalog_with_status
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


@app.get("/api/ollama")
def ollama_status():
    """Ping the configured Ollama server and list its installed models."""
    base = store.load().ollama_base_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=1.5) as res:
            data = json.load(res)
        models = sorted(m.get("name", "") for m in data.get("models", []))
        return {"url": base, "reachable": True, "models": models}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"url": base, "reachable": False, "models": [], "error": str(getattr(exc, "reason", exc))}


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
