"""Explicitly confirmed destructive tools for the literature library."""
import json
from typing import Any

from pydantic import ValidationError

from . import runner
from .db import db
from .models import FetchRequest

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delete_all_literature",
            "description": "Permanently delete every saved literature paper. Use only after Helix has asked for confirmation and the user has directly confirmed the deletion in a later message.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_literature",
            "description": "Start a background import of papers from PubMed, PubMed Central, or Europe PMC. Use only when the user explicitly asks to fetch, import, download, or save literature—not for ordinary searches of the existing library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["pubmed", "pmc", "europepmc"], "default": "pubmed"},
                    "term": {"type": "string", "minLength": 1, "maxLength": 500},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                    "sort": {"type": "string", "enum": ["relevance", "added", "pub_date"], "default": "relevance"},
                    "tags": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 20},
                },
                "required": ["term"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_literature_fetch_run",
            "description": "Check the progress and outcome of a literature import by its fetch run ID.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_literature_fetch",
            "description": "Stop one active literature fetch/import by its run ID. Papers already saved by the run are kept.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_all_literature_fetches",
            "description": "Stop every active literature fetch/import. Papers already saved by the runs are kept.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

NAMES = {tool["function"]["name"] for tool in TOOLS}


def execute(name: str, arguments: str | dict | None) -> dict[str, Any]:
    if isinstance(arguments, dict):
        args = arguments
    else:
        try:
            parsed = json.loads(arguments or "{}")
            args = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            args = {}
    if name == "delete_all_literature":
        if runner.any_running():
            return {"error": "A literature fetch is running. Cancel it or wait for it to finish before deleting papers.", "sources": []}
        deleted = db.delete_all()
        return {"deleted_count": deleted, "scope": "literature_deleted",
                "audit": {"action": "delete_all_literature", "count": deleted}, "sources": []}

    if name == "fetch_literature":
        try:
            source = str(args.get("source") or "pubmed")
            if source not in ("pubmed", "pmc", "europepmc"):
                return {"error": "Choose pubmed, pmc, or europepmc.", "sources": []}
            request = FetchRequest(
                term=args.get("term"), max_results=min(50, int(args.get("max_results") or 5)),
                sort=str(args.get("sort") or "relevance"), tags=args.get("tags") or [],
            )
            run = runner.start(source, request)
            return {"run": run.model_dump(mode="json"), "scope": "literature_fetch_started",
                    "audit": {"action": "fetch_literature", "run_id": run.id, "source": source,
                              "term": request.term, "max_results": request.max_results}, "sources": []}
        except runner.AlreadyRunning as exc:
            return {"error": f"A fetch from that source is already running (run {exc.run_id}).", "sources": []}
        except (ValidationError, ValueError, TypeError) as exc:
            return {"error": str(exc), "sources": []}

    if name == "get_literature_fetch_run":
        try:
            run_id = int(args.get("id"))
        except (TypeError, ValueError):
            return {"error": "A valid fetch run ID is required.", "sources": []}
        run = db.get_run(run_id)
        if run is None:
            return {"error": f"Fetch run {run_id} was not found.", "sources": []}
        return {"run": run.model_dump(mode="json"), "scope": "literature_fetch_status", "sources": []}

    if name == "cancel_literature_fetch":
        try:
            run_id = int(args.get("id"))
        except (TypeError, ValueError):
            return {"error": "A valid fetch run ID is required.", "sources": []}
        if not runner.cancel(run_id):
            return {"error": f"Fetch run {run_id} is not active.", "sources": []}
        return {"cancelled_ids": [run_id], "scope": "literature_fetch_cancelled",
                "audit": {"action": "cancel_literature_fetch", "run_ids": [run_id]}, "sources": []}

    if name == "cancel_all_literature_fetches":
        run_ids = runner.cancel_all()
        return {"cancelled_count": len(run_ids), "cancelled_ids": run_ids, "scope": "literature_fetches_cancelled",
                "audit": {"action": "cancel_all_literature_fetches", "run_ids": run_ids}, "sources": []}

    return {"error": f"Unknown tool: {name}", "sources": []}
