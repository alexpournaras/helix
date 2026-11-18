"""Fetch runs: search a source for a term, then fetch and save each article one at a time.

A run is a background task, so it keeps going if the browser navigates away; progress is
written to the database after every article and the UI polls it. One run per source at a time.
"""

import asyncio
import logging

import httpx

from . import enrich
from .db import db, now
from .models import FetchRequest, FetchRun, Peek, PreviewPage, SourceId
from .sources import REGISTRY

log = logging.getLogger("helix.literature")

MAX_CONSECUTIVE_FAILURES = 5  # stop early when the source is clearly down
TIMEOUT = httpx.Timeout(30, connect=10)

_tasks: dict[int, asyncio.Task] = {}  # run id -> task
_active: dict[str, int] = {}  # source -> running run id


class AlreadyRunning(Exception):
    def __init__(self, run_id: int):
        self.run_id = run_id


def describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return "The source is rate-limiting requests (429). Try again in a minute."
        return f"The source answered with an error ({code})."
    if isinstance(exc, httpx.TimeoutException):
        return "The source didn't respond in time."
    if isinstance(exc, httpx.ConnectError):
        return "Couldn't reach the source. Check the internet connection."
    if isinstance(exc, httpx.HTTPError):
        return f"Network error: {exc.__class__.__name__}"
    return str(exc) or exc.__class__.__name__


def _check_sort(source: SourceId, request: FetchRequest) -> None:
    sorts = REGISTRY[source].SORTS
    if request.sort not in sorts:
        raise ValueError(f"{REGISTRY[source].NAME} can't sort by “{request.sort}”. Choose one of: {', '.join(sorts)}.")


class SearchFailed(Exception):
    pass


async def preview(source: SourceId, request: FetchRequest) -> PreviewPage:
    """Search and summarise the hits without saving anything, so they can be reviewed first."""
    _check_sort(source, request)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "Helix/0.1"}) as client:
            total, items = await REGISTRY[source].preview(client, request.term, request.max_results, request.sort)
    except Exception as exc:  # noqa: BLE001 - shown to the user as a failed search
        log.warning("%s preview failed: %s", source, exc)
        raise SearchFailed(describe(exc)) from exc
    saved = await asyncio.to_thread(db.saved_ids, source, [i.source_id for i in items])
    return PreviewPage(source=source, total=total, items=[i.model_copy(update={"saved": i.source_id in saved}) for i in items])


async def peek(source: SourceId, source_id: str) -> Peek:
    """One search result's abstract, for reading before deciding to save it."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "Helix/0.1"}) as client:
            fetched = await REGISTRY[source].fetch(client, source_id)
    except Exception as exc:  # noqa: BLE001
        raise SearchFailed(describe(exc)) from exc
    return Peek(abstract=fetched[0].abstract if fetched else "")


async def refresh(source: SourceId, source_id: str) -> None:
    """Fetch one saved article again with everything around it, e.g. after Europe PMC was down.
    Tags and the saved date are kept."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "Helix/0.1"}) as client:
            fetched = await REGISTRY[source].fetch(client, source_id)
            if fetched is None:
                raise ValueError(f"{source_id} came back empty")
            article, details = await enrich.enrich(client, *fetched)
    except Exception as exc:  # noqa: BLE001
        raise SearchFailed(describe(exc)) from exc
    await asyncio.to_thread(db.save_article, article, None, 0, (), details)


def start(source: SourceId, request: FetchRequest) -> FetchRun:
    _check_sort(source, request)
    if source in _active:
        raise AlreadyRunning(_active[source])
    max_results = len(request.ids) if request.ids else request.max_results
    run = db.create_run(source, request.term, max_results, request.sort, request.tags)
    _active[source] = run.id
    task = asyncio.create_task(_run(run.id, source, request))
    _tasks[run.id] = task

    def _cleanup(_):
        _tasks.pop(run.id, None)
        if _active.get(source) == run.id:
            _active.pop(source, None)

    task.add_done_callback(_cleanup)
    return run


def any_running() -> bool:
    return bool(_active)


def cancel(run_id: int) -> bool:
    task = _tasks.get(run_id)
    if not task:
        return False
    task.cancel()
    return True


async def _run(run_id: int, source: SourceId, request: FetchRequest) -> None:
    module = REGISTRY[source]
    counts = {"fetched": 0, "new": 0, "updated": 0, "failed": 0}
    last_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "Helix/0.1"}) as client:
            try:
                if request.ids:  # picked from a preview: no need to search again
                    total, ids = len(request.ids), request.ids
                else:
                    total, ids = await module.search(client, request.term, request.max_results, request.sort)
            except Exception as exc:  # noqa: BLE001 - report any search failure on the run
                log.warning("%s search failed: %s", source, exc)
                db.update_run(run_id, status="failed", error=f"Search failed: {describe(exc)}", finished_at=now())
                return
            await asyncio.to_thread(db.update_run, run_id, total=total, queued=len(ids))

            streak = 0
            for rank, source_id in enumerate(ids, 1):
                await asyncio.to_thread(db.update_run, run_id, current=source_id)
                try:
                    fetched = await module.fetch(client, source_id)
                    if fetched is None:
                        raise ValueError(f"{source_id} came back empty")
                    article, details = await enrich.enrich(client, *fetched)
                    _, created = await asyncio.to_thread(db.save_article, article, run_id, rank, request.tags, details)
                    counts["fetched"] += 1
                    counts["new" if created else "updated"] += 1
                    streak = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad article shouldn't stop the run
                    log.warning("%s fetch %s failed: %s", source, source_id, exc)
                    counts["failed"] += 1
                    streak += 1
                    last_error = f"{source_id}: {describe(exc)}"
                    if streak >= MAX_CONSECUTIVE_FAILURES:
                        db.update_run(run_id, **counts, status="failed", current=None, finished_at=now(),
                                      error=f"Stopped after {streak} failures in a row. Last: {last_error}")
                        return
                await asyncio.to_thread(db.update_run, run_id, **counts, error=last_error)

        db.update_run(run_id, **counts, status="done", current=None, error=last_error, finished_at=now())
    except asyncio.CancelledError:
        db.update_run(run_id, **counts, status="cancelled", current=None, error=last_error, finished_at=now())
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("%s run %s crashed", source, run_id)
        db.update_run(run_id, **counts, status="failed", current=None, error=describe(exc), finished_at=now())


async def shutdown() -> None:
    for task in list(_tasks.values()):
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks.values(), return_exceptions=True)
