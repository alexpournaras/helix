"""Search and fetch for research steps (PLAN B8.8, B8.13), over the literature module.

Steps use `services.sources` when set (tests pass a fake), else `LiteratureSources(services.literature)`.
- count: hit counts for query checks (B8.7).
- search: Europe PMC `search_records` (core records, PubMed and PMC only) or PubMed esearch + `fetch_many`.
- fetch: library first (`find_existing`), else the source's fetch + `enrich(citations=False)` +
  `save_article(tag_origin="ai")`; the paper is tagged `inv:{id}` either way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.literature.models import Article, Details

from . import transport

SEARCH_TIMEOUT = 90.0


@dataclass
class Fetched:
    literature_id: int | None
    article: Article
    details: Details
    from_library: bool = False


class Sources(Protocol):
    async def search(self, source: str, query: str, max_results: int) -> tuple[int, list[Article]]: ...
    async def count(self, source: str, query: str) -> int: ...
    async def load(self, literature_id: int) -> Fetched: ...
    async def fetch(self, article: Article, investigation_id: int) -> Fetched: ...


def default_library():
    from app.config import settings as app_settings
    from app.literature.db import LiteratureDB
    root = Path(os.environ.get("HELIX_DATA_DIR") or app_settings.data_dir)
    lib = LiteratureDB(root / "literature.db")
    return lib


class LiteratureSources:
    def __init__(self, library: Any = None):
        self.library = library

    def _lib(self):
        if self.library is None:
            self.library = default_library()
        return self.library

    async def search(self, source: str, query: str, max_results: int) -> tuple[int, list[Article]]:
        from app.literature.sources import europepmc, ncbi, pubmed
        async with transport.client(timeout=SEARCH_TIMEOUT) as client:
            if source == "europepmc":
                return await europepmc.search_records(client, europepmc.scoped(query), max_results)
            if source == "pubmed":
                total, ids = await ncbi.esearch(client, "pubmed", query, max_results, None)
                return total, (await pubmed.fetch_many(client, ids) if ids else [])
        raise ValueError(f"unknown source {source!r}")

    async def count(self, source: str, query: str) -> int:
        """Hit count only (B8.7): Europe PMC idlist with pageSize 1 (scoped), PubMed esearch with retmax 0.
        A query the source rejects raises ValueError."""
        from app.literature.sources import europepmc, pubmed
        async with transport.client(timeout=SEARCH_TIMEOUT) as client:
            if source == "europepmc":
                return await europepmc.count(client, europepmc.scoped(query))
            if source == "pubmed":
                return await pubmed.count(client, query)
        raise ValueError(f"unknown source {source!r}")

    async def load(self, literature_id: int) -> Fetched:
        """A paper already in the library (fetched earlier), with its full text, for reading (B8.14)."""
        full = self._lib().get_full(literature_id)
        if full is None:
            raise LookupError(f"literature item {literature_id} is not in the library")
        return Fetched(literature_id, full, full.details, from_library=True)

    async def references(self, literature_id: int) -> list:
        """Backward references of a library paper (kept by enrich, B8.9)."""
        return await _library_refs(self._lib(), literature_id)

    async def citing(self, literature_id: int, limit: int) -> list:
        """Europe PMC works citing a library paper (forward, B8.9)."""
        from app.literature.enrich import _epmc_key, _ref_list
        full = self._lib().get_full(literature_id)
        key = _epmc_key(full) if full else None
        if not key:
            return []
        async with transport.client(timeout=SEARCH_TIMEOUT) as client:
            return await _ref_list(client, f"/{key[0]}/{key[1]}/citations", "citationList", "citation", limit)

    async def fetch(self, article: Article, investigation_id: int) -> Fetched:
        from app.literature.enrich import enrich
        from app.literature.sources import REGISTRY
        lib = self._lib()
        tag = f"inv:{investigation_id}"
        found = lib.find_existing(article.pmid, article.pmcid, article.doi)
        if found is not None:
            full = lib.get_full(found.id)
            lib.add_tag(found.id, tag, origin="ai")
            return Fetched(found.id, full, full.details, from_library=True)
        async with transport.client(timeout=SEARCH_TIMEOUT) as client:
            got = await REGISTRY[article.source].fetch(client, article.source_id)
            base, details = got if got else (article, Details())
            base, details = await enrich(client, base, details, citations=False)
        literature_id, _ = lib.save_article(base, tags=[tag], details=details, tag_origin="ai")
        return Fetched(literature_id, base, details)


async def _library_refs(lib, literature_id: int) -> list:
    full = lib.get_full(literature_id)
    return list(full.details.references or []) if full and full.details else []


def sources_for(services: Any) -> Sources:
    explicit = getattr(services, "sources", None)
    if explicit is not None:
        return explicit
    return LiteratureSources(getattr(services, "literature", None))
