import json
import platform
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ProviderKeys, settings
from .chat import router as chat_router
from .notes import router as notes_router
from .integrations import Integration, catalog_with_status
from .literature import runner
from .literature.db import db as literature_db
from .literature.models import (
    SOURCES, FetchRequest, FetchRun, Literature, LiteratureFull, LiteraturePage, Peek, PreviewPage, SourceId, TagRequest,
)
from .providers import ollama, openai
from .store import WorkspaceSettings, store

STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    literature_db.interrupt_stale_runs()
    yield
    await runner.shutdown()


app = FastAPI(title=f"{settings.app_name} API", version=settings.version, lifespan=lifespan)
app.include_router(chat_router)
app.include_router(notes_router)
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
        "literature": literature_db.count(),
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


# ---------- OpenAI ----------

@app.get("/api/integrations/openai/config", response_model=openai.OpenAIConfig)
def get_openai_config():
    return openai.load_config()


@app.put("/api/integrations/openai/config", response_model=openai.OpenAIConfig)
def put_openai_config(body: openai.OpenAIConfig):
    try:
        return openai.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/integrations/openai/status")
async def get_openai_status():
    return await openai.status(openai.load_config())


@app.post("/api/integrations/openai/chat")
async def openai_chat(body: openai.ChatRequest):
    """Stream an OpenAI playground reply as normalized NDJSON events."""
    config = openai.load_config()

    async def events():
        async for event in openai.chat_stream(config, body):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


# ---------- Literature integrations: PubMed, PubMed Central, Europe PMC ----------

@app.post("/api/integrations/{source}/preview", response_model=PreviewPage)
async def preview_fetch(source: SourceId, body: FetchRequest):
    """Search `source` and summarise the hits without saving anything. Each item says whether
    it's already in the library. Save the ones you want by passing their IDs to /fetch."""
    try:
        return await runner.preview(source, body)
    except runner.SearchFailed as exc:
        raise HTTPException(status_code=502, detail=f"Search failed: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/integrations/{source}/peek", response_model=Peek)
async def peek_article(source: SourceId, id: str = Query(min_length=1, max_length=64)):
    """The abstract of one search result, without saving it."""
    try:
        return await runner.peek(source, id)
    except runner.SearchFailed as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't load the abstract: {exc}")


@app.post("/api/integrations/{source}/fetch", response_model=FetchRun, status_code=202)
async def start_fetch(source: SourceId, body: FetchRequest):
    """Search `source` for the term, then fetch and save each article one at a time in the background.
    Poll /api/literature/runs/{id} for progress."""
    try:
        return runner.start(source, body)
    except runner.AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=f"A fetch from this source is already running (run {exc.run_id}).")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/literature/runs", response_model=list[FetchRun])
def list_runs(source: SourceId | None = None, limit: int = Query(20, ge=1, le=200)):
    return literature_db.list_runs(source, limit)


@app.get("/api/literature/runs/{run_id}", response_model=FetchRun)
def get_run(run_id: int):
    run = literature_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such fetch run.")
    return run


@app.post("/api/literature/runs/{run_id}/cancel", response_model=FetchRun)
def cancel_run(run_id: int):
    if not runner.cancel(run_id):
        raise HTTPException(status_code=409, detail="That fetch isn't running.")
    return literature_db.get_run(run_id)


@app.get("/api/literature", response_model=LiteraturePage)
def list_literature(
    q: str = "",
    source: list[SourceId] = Query(default=[]),  # repeat to match any of several
    tag: list[str] = Query(default=[]),
    term: str | None = None,
    run_id: int | None = None,
    sort: str = Query("added", pattern="^(added|newest|oldest|title)$"),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return literature_db.search(q=q, sources=source, tags=tag, term=term, run_id=run_id, sort=sort, limit=limit, offset=offset)


@app.delete("/api/literature")
def delete_all_literature():
    """Empty the library. Refused while a fetch is running, since it would keep saving articles."""
    if runner.any_running():
        raise HTTPException(status_code=409, detail="A fetch is running. Wait for it to finish or cancel it first.")
    return {"deleted": literature_db.delete_all()}


@app.get("/api/literature/facets")
def literature_facets():
    """Values for the library filters."""
    counts = literature_db.counts_by_source()
    return {
        "sources": {s: counts.get(s, 0) for s in SOURCES},
        "tags": literature_db.all_tags(),
        "terms": literature_db.all_terms(),
    }


def _literature_or_404(literature_id: int) -> Literature:
    item = literature_db.get(literature_id)
    if not item:
        raise HTTPException(status_code=404, detail="No such article.")
    return item


@app.get("/api/literature/{literature_id}", response_model=LiteratureFull)
def get_literature(literature_id: int):
    """One article with everything saved for it: full text, references, citations and more."""
    item = literature_db.get_full(literature_id)
    if not item:
        raise HTTPException(status_code=404, detail="No such article.")
    return item


@app.post("/api/literature/{literature_id}/refresh", response_model=LiteratureFull)
async def refresh_literature(literature_id: int):
    """Fetch the article again from its source, with full text, references and citations.
    Can take up to a minute when Europe PMC is slow."""
    item = _literature_or_404(literature_id)
    try:
        await runner.refresh(item.source, item.source_id)
    except runner.SearchFailed as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't refresh the article: {exc}")
    return literature_db.get_full(literature_id)


@app.delete("/api/literature/{literature_id}", status_code=204)
def delete_literature(literature_id: int):
    if not literature_db.delete(literature_id):
        raise HTTPException(status_code=404, detail="No such article.")


@app.post("/api/literature/{literature_id}/tags", response_model=Literature)
def add_tag(literature_id: int, body: TagRequest):
    if not literature_db.add_tag(literature_id, body.name, body.origin):
        raise HTTPException(status_code=404, detail="No such article.")
    return _literature_or_404(literature_id)


@app.delete("/api/literature/{literature_id}/tags/{name}", response_model=Literature)
def remove_tag(literature_id: int, name: str):
    _literature_or_404(literature_id)
    literature_db.remove_tag(literature_id, name)
    return _literature_or_404(literature_id)


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
