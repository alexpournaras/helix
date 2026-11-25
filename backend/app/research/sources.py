"""Search and fetch for research steps (PLAN B8.8, B8.13), over the literature module.

Steps use `services.sources` when set (tests pass a fake), else `LiteratureSources(services.literature)`.
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


def sources_for(services: Any) -> Sources:
    explicit = getattr(services, "sources", None)
    if explicit is not None:
        return explicit
    return LiteratureSources(getattr(services, "literature", None))
