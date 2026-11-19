"""Read-only AI tools for Helix's saved literature library."""

import json
from typing import Any

from .db import db

MAX_FULL_TEXT_CHARS = 60_000
MAX_FIGURE_DATA_CHARS = 30_000

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_literature",
            "description": "List papers already saved in the Helix literature library without requiring a search query. Use for requests like list papers, show all papers, or what is in the literature library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
                    "sort": {"type": "string", "enum": ["added", "newest", "oldest", "title"], "default": "added"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_literature",
            "description": "Search papers already saved in Helix by text, exact tag, or both. When the user says a paper is tagged with a value, put that exact value in tag rather than query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional title words, author, journal, PMID, PMCID, DOI, or subject terms."},
                    "tag": {"type": "string", "description": "Optional exact Helix tag name. Tag matching is case-insensitive."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_literature_tags",
            "description": "List all tag names in the Helix literature library and the number of papers using each tag.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_literature",
            "description": "Read only the abstract and bibliographic metadata of one saved paper. This is the default reading tool. It never returns full text, figures, tables, references, or citations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "description": "The Helix literature ID returned by a literature tool."},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_literature_full_text",
            "description": "Read the saved full-text sections of one paper. Use only when the user explicitly asks for the full text, whole paper, article body, detailed section analysis, or to read everything.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1, "description": "Helix literature ID."}},
                "required": ["id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_literature_figures",
            "description": "Read stored figure captions, image URLs, and table cells. Use only when the user explicitly asks for figures, images, tables, or to read everything. This provides captions/data, not visual pixel inspection.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1, "description": "Helix literature ID."}},
                "required": ["id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_literature_references",
            "description": "Read stored references and citing works. Use only when the user explicitly asks for references, citations, related works, or to read everything.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1, "description": "Helix literature ID."}},
                "required": ["id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_literature",
            "description": "List the papers most recently saved in the Helix literature library.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
                "additionalProperties": False,
            },
        },
    },
]


def _source(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title or f"Untitled literature #{item.id}",
        "source": item.source,
        "source_id": item.source_id,
        "url": item.url,
        "year": item.year,
    }


def _summary(item) -> dict[str, Any]:
    return {
        **_source(item),
        "authors": item.authors[:8],
        "journal": item.journal,
        "abstract_excerpt": item.abstract[:800],
        "has_full_text": item.has_full_text,
        "tags": [tag.name for tag in item.tags],
    }


def _list_summary(item) -> dict[str, Any]:
    return {
        **_source(item),
        "authors": item.authors[:4],
        "journal": item.journal,
        "tags": [tag.name for tag in item.tags],
        "has_full_text": item.has_full_text,
    }


def _arguments(value: str | dict | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _limit(value: Any, default: int = 5) -> int:
    try:
        return min(10, max(1, int(value or default)))
    except (TypeError, ValueError):
        return default


def _literature_id(args: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    try:
        return int(args.get("id")), None
    except (TypeError, ValueError):
        return None, {"error": "A valid literature ID is required.", "sources": []}


def _get(args: dict[str, Any]):
    literature_id, error = _literature_id(args)
    if error:
        return None, error
    item = db.get_full(literature_id)
    if item is None:
        return None, {"error": f"Literature #{literature_id} was not found.", "sources": []}
    return item, None


def execute(name: str, raw_arguments: str | dict | None) -> dict[str, Any]:
    """Execute an allow-listed tool and return JSON-safe data for the model and UI."""
    args = _arguments(raw_arguments)
    if name == "list_literature":
        try:
            limit = min(50, max(1, int(args.get("limit") or 25)))
        except (TypeError, ValueError):
            limit = 25
        sort = str(args.get("sort") or "added")
        if sort not in ("added", "newest", "oldest", "title"):
            sort = "added"
        page = db.search(sort=sort, limit=limit)
        return {"total": page.total, "returned": len(page.items), "truncated": page.total > len(page.items),
                "papers": [_list_summary(item) for item in page.items], "scope": "library",
                "sources": [_source(item) for item in page.items]}

    if name == "search_literature":
        query = str(args.get("query") or "").strip()
        tag = str(args.get("tag") or "").strip()
        limit = _limit(args.get("limit"))
        if not query and not tag:
            return {"error": "A text query or exact tag is required.", "sources": []}
        page = db.search(q=query, tags=[tag] if tag else [], sort="newest", limit=limit)
        return {"query": query or None, "tag": tag or None, "total": page.total, "papers": [_summary(item) for item in page.items],
                "scope": "search", "sources": [_source(item) for item in page.items]}

    if name == "list_literature_tags":
        return {"tags": db.all_tags(), "scope": "tags", "sources": []}

    if name == "list_recent_literature":
        limit = _limit(args.get("limit"))
        page = db.search(sort="added", limit=limit)
        return {"total": page.total, "papers": [_summary(item) for item in page.items], "scope": "search",
                "sources": [_source(item) for item in page.items]}

    if name == "get_literature":
        item, error = _get(args)
        if error:
            return error
        if not item.abstract.strip():
            return {"error": f"Literature #{item.id} has no saved abstract.", "scope": "abstract_unavailable",
                    "sources": [_source(item)]}
        return {
            "paper": {
                **_summary(item),
                "abstract": item.abstract,
                "keywords": item.keywords,
                "mesh": item.mesh,
                "publication_types": item.publication_types,
                "available": {
                    "full_text": item.has_full_text,
                    "figures_and_tables": len(item.details.figures),
                    "references": len(item.details.references),
                    "citations": len(item.details.citations),
                },
            },
            "scope": "abstract",
            "audit": {"scope": "abstract", "complete": True, "characters": len(item.abstract)},
            "sources": [_source(item)],
        }

    if name == "get_literature_full_text":
        item, error = _get(args)
        if error:
            return error
        if not item.details.full_text:
            return {"error": f"Literature #{item.id} has no saved full text. Do not claim to have read it.",
                    "scope": "full_text_unavailable", "sources": [_source(item)]}
        sections, used, truncated = [], 0, False
        for section in item.details.full_text:
            heading = section.title or "Untitled section"
            block = f"## {heading}\n{section.text}".strip()
            remaining = MAX_FULL_TEXT_CHARS - used
            if remaining <= 0:
                truncated = True
                break
            if len(block) > remaining:
                block = block[:remaining]
                truncated = True
            sections.append({"title": heading, "text": block.removeprefix(f"## {heading}\n")})
            used += len(block)
        return {
            "paper": {
                "id": item.id,
                "title": item.title,
                "full_text_from": item.details.full_text_from,
                "full_text_available": True,
                "full_text_sections": sections,
                "full_text_truncated": truncated,
                "full_text_characters_returned": used,
                "license": item.details.license,
            },
            "scope": "full_text",
            "audit": {"scope": "full_text", "complete": not truncated, "sections": len(sections), "characters": used},
            "sources": [_source(item)],
        }

    if name == "get_literature_figures":
        item, error = _get(args)
        if error:
            return error
        if not item.details.figures:
            return {"error": f"Literature #{item.id} has no stored figures or tables.",
                    "scope": "figures_unavailable", "sources": [_source(item)]}
        figures, used, truncated = [], 0, False
        for figure in item.details.figures:
            data = {
                "kind": figure.kind, "label": figure.label, "caption": figure.caption,
                "image_url": figure.image, "table_rows": figure.rows, "table_header_rows": figure.head_rows,
                "footer": figure.footer,
            }
            encoded = json.dumps(data, ensure_ascii=False)
            if used + len(encoded) > MAX_FIGURE_DATA_CHARS:
                truncated = True
                break
            figures.append(data)
            used += len(encoded)
        return {"paper": {"id": item.id, "title": item.title, "figures_and_tables": figures,
                           "data_truncated": truncated, "note": "Image URLs and captions are provided; image pixels were not inspected."},
                "scope": "figures", "audit": {"scope": "figures", "complete": not truncated, "items": len(figures),
                                                 "pixels_inspected": False}, "sources": [_source(item)]}

    if name == "get_literature_references":
        item, error = _get(args)
        if error:
            return error
        if not item.details.references and not item.details.citations:
            return {"error": f"Literature #{item.id} has no stored references or citations.",
                    "scope": "references_unavailable", "sources": [_source(item)]}
        references_truncated = len(item.details.references) > 200
        citations_truncated = len(item.details.citations) > 200
        return {"paper": {"id": item.id, "title": item.title,
                           "references": [ref.model_dump() for ref in item.details.references[:200]],
                           "citations": [ref.model_dump() for ref in item.details.citations[:200]],
                           "references_truncated": references_truncated,
                           "citations_truncated": citations_truncated},
                "scope": "references", "audit": {"scope": "references",
                    "complete": not references_truncated and not citations_truncated,
                    "references": min(200, len(item.details.references)), "citations": min(200, len(item.details.citations))},
                "sources": [_source(item)]}

    return {"error": f"Unknown tool: {name}", "sources": []}


def tool_event(name: str, result: dict[str, Any]) -> dict[str, Any]:
    labels = {
        "list_literature": "Listed the literature library",
        "search_literature": "Searched the literature library",
        "list_literature_tags": "Listed literature tags",
        "get_literature": "Read a saved paper",
        "get_literature_full_text": "Read saved full text",
        "get_literature_figures": "Read figures and tables",
        "get_literature_references": "Read references and citations",
        "list_recent_literature": "Listed recent literature",
    }
    return {"type": "tool", "name": name, "label": labels.get(name, name), "sources": result.get("sources", [])}
