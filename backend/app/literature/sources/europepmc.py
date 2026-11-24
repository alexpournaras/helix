"""Europe PMC: search for IDs, then fetch one article at a time from /article/{source}/{id}.

Docs: https://europepmc.org/RestfulWebService
No key needed. Covers PubMed, PMC, preprints (PPR), patents and more; each hit has its own
"source" code, so the stored ID is "MED:12345", "PMC:PMC123", "PPR:PPR456" and so on.
"""

import asyncio
import re

import httpx

from .. import text
from ..models import Article, Details, Preview

ID = "europepmc"
NAME = "Europe PMC"
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PAGE = 1000  # the API's maximum page size

# Helix sort key -> (label, Europe PMC sort value). An unknown value makes the API fail, so
# only these are offered. Without a sort value Europe PMC ranks by relevance.
SORTS = {
    "relevance": ("Relevance", None),
    "added": ("Most recently added", "FIRST_IDATE_D desc"),
    "pub_date": ("Publication date, newest first", "P_PDATE_D desc"),
    "pub_date_asc": ("Publication date, oldest first", "P_PDATE_D asc"),
    "cited": ("Most cited", "CITED desc"),
}
DEFAULT_SORT = "relevance"


async def _get(client: httpx.AsyncClient, path: str, params: dict, retries: int = 2, timeout: httpx.Timeout | None = None) -> dict:
    """GET a Europe PMC endpoint, retrying 429s, 5xx answers and timeouts (it is often slow or briefly down)."""
    for attempt in range(retries + 1):
        try:
            response = await client.get(f"{BASE}{path}", params={**params, "format": "json"}, timeout=timeout or client.timeout)
        except httpx.TimeoutException:
            if attempt < retries:
                continue
            raise
        if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()
    raise AssertionError("unreachable")


def _query(term: str, max_results: int) -> tuple[str, int]:
    """A link or ID becomes an exact lookup for that one article."""
    match text.identifier(term):
        case ("ppr", value):
            return f"EXT_ID:{value} AND SRC:PPR", 1
        case ("pmcid", value):
            return f"PMCID:{value}", 1
        case ("pmid", value):
            return f"EXT_ID:{value} AND SRC:MED", 1
        case ("doi", value):
            return f'DOI:"{value}"', 1
    return term, max_results


async def _hits(client: httpx.AsyncClient, term: str, max_results: int, sort: str, result_type: str) -> tuple[int, list[dict]]:
    query, max_results = _query(term, max_results)
    params = {"query": query, "resultType": result_type, "pageSize": min(PAGE, max_results), "cursorMark": "*"}
    if SORTS[sort][1]:
        params["sort"] = SORTS[sort][1]
    hits: dict[str, dict] = {}
    total = 0
    while len(hits) < max_results:
        data = await _get(client, "/search", params)
        total = int(data.get("hitCount") or 0)
        page = (data.get("resultList") or {}).get("result") or []
        for h in page:
            if h.get("source") and h.get("id"):
                hits.setdefault(f"{h['source']}:{h['id']}", h)
        cursor = data.get("nextCursorMark")
        if not page or not cursor or cursor == params["cursorMark"]:
            break
        params["cursorMark"] = cursor
    return total, list(hits.values())[:max_results]


def scoped(query: str) -> str:
    """Research queries cover PubMed and PMC records only (PLAN B8.7)."""
    return f"({query}) AND (SRC:MED OR SRC:PMC)"


async def search_records(client: httpx.AsyncClient, query: str, max_results: int,
                         sort: str = DEFAULT_SORT) -> tuple[int, list[Article]]:
    """(hit count, parsed core records) for a query: abstracts, IDs and flags without a fetch per paper (C11)."""
    total, hits = await _hits(client, query, max_results, sort, "core")
    return total, [parse(h, f"{h['source']}:{h['id']}") for h in hits]


async def count(client: httpx.AsyncClient, query: str) -> int:
    data = await _get(client, "/search", {"query": query, "resultType": "idlist", "pageSize": 1})
    return int(data.get("hitCount") or 0)


async def search(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[str]]:
    total, hits = await _hits(client, term, max_results, sort, "idlist")
    return total, [f"{h['source']}:{h['id']}" for h in hits]


async def preview(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[Preview]]:
    total, hits = await _hits(client, term, max_results, sort, "lite")
    return total, [
        Preview(
            source_id=f"{h['source']}:{h['id']}",
            url=f"https://europepmc.org/article/{h['source']}/{h['id']}",
            title=text.clean(h.get("title") or ""),
            authors=[a.strip() for a in (h.get("authorString") or "").rstrip(".").split(",") if a.strip()],
            journal=h.get("journalTitle") or h.get("bookOrReportDetails", {}).get("publisher") or "",
            year=text.first_year(h.get("pubYear") or h.get("firstPublicationDate")),
        )
        for h in hits
    ]


async def fetch(client: httpx.AsyncClient, source_id: str) -> tuple[Article, Details] | None:
    from ..enrich import apply_core  # enrich imports this module

    src, _, ext_id = source_id.partition(":")
    data = await _get(client, f"/article/{src}/{ext_id}", {"resultType": "core"})
    result = data.get("result")
    return (parse(result, source_id), apply_core(result, Details())) if result else None


def _list(obj: dict | None, *path: str) -> list:
    for key in path:
        obj = (obj or {}).get(key)
    return obj if isinstance(obj, list) else []


def _abstract(raw: str | None) -> str:
    # Structured abstracts arrive as "<h4>Objective</h4>To provide…"; keep the headings as labels.
    if not raw:
        return ""
    raw = re.sub(r"<h\d[^>]*>(.*?)</h\d>\s*", lambda m: f"\n\n{text.clean(m.group(1))}: ", raw, flags=re.S | re.I)
    parts = (text.clean(part).replace("\n", " ") for part in raw.split("\n\n"))
    return "\n\n".join(part for part in parts if part)


def parse(r: dict, source_id: str) -> Article:
    info = r.get("journalInfo") or {}
    journal = info.get("journal") or {}
    pub_date = text.parse_free_date(r.get("firstPublicationDate")) or text.date(r.get("pubYear"))

    authors = []
    for a in _list(r, "authorList", "author"):
        name = a.get("fullName") or a.get("collectiveName") or " ".join(filter(None, [a.get("lastName"), a.get("initials")]))
        if name:
            authors.append(name.strip())

    src, _, ext_id = source_id.partition(":")
    oa = r.get("isOpenAccess")
    pub_types = [p for p in _list(r, "pubTypeList", "pubType") if p]
    corrections = {(c.get("type") or "").strip().lower() for c in _list(r, "commentCorrectionList", "commentCorrection")}
    return Article(
        source=ID,
        source_id=source_id,
        url=f"https://europepmc.org/article/{src}/{ext_id}",
        pmid=text.pmid(r.get("pmid")),
        pmcid=text.pmcid(r.get("pmcid")),
        doi=text.doi(r.get("doi")),
        title=text.clean(r.get("title")),
        abstract=_abstract(r.get("abstractText")),
        authors=authors,
        journal=(journal.get("title") or journal.get("isoabbreviation") or (r.get("bookOrReportDetails") or {}).get("publisher") or "").strip(),
        year=text.year_of(pub_date),
        pub_date=pub_date,
        volume=str(info.get("volume") or ""),
        issue=str(info.get("issue") or ""),
        pages=str(r.get("pageInfo") or ""),
        keywords=[text.clean(k) for k in _list(r, "keywordList", "keyword") if k],
        mesh=[m["descriptorName"] for m in _list(r, "meshHeadingList", "meshHeading") if m.get("descriptorName")],
        publication_types=pub_types,
        language=(r.get("language") or "").lower(),
        open_access=None if oa is None else oa == "Y",
        retracted="Retracted Publication" in pub_types or "retraction in" in corrections,
        # Only "... in" marks the affected paper; "Expression of concern for" is on the notice (spike X4).
        concern="expression of concern in" in corrections,
    )
