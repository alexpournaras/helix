"""Shared NCBI E-utilities access for PubMed and PMC.

Docs: https://www.ncbi.nlm.nih.gov/books/NBK25501/
NCBI allows 3 requests/second per IP without a key and 10 with NCBI_API_KEY; both
integrations go through one throttle so together they stay under that.
"""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from ...config import ProviderKeys

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "helix"  # NCBI asks callers to identify their tool


class _Throttle:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self, interval: float) -> None:
        async with self._lock:
            delay = self._last + interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


_throttle = _Throttle()


def _key() -> str:
    return ProviderKeys().ncbi_api_key.strip()


async def eutil(client: httpx.AsyncClient, endpoint: str, params: dict, *, retries: int = 2) -> httpx.Response:
    """GET an E-utility, respecting the rate limit and retrying on 429 / 5xx."""
    key = _key()
    query = {**params, "tool": TOOL}
    if key:
        query["api_key"] = key
    interval = 0.11 if key else 0.35
    for attempt in range(retries + 1):
        await _throttle.wait(interval)
        response = await client.get(f"{EUTILS}/{endpoint}", params=query)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
        response.raise_for_status()
        return response
    raise AssertionError("unreachable")


@asynccontextmanager
async def eutil_stream(client: httpx.AsyncClient, endpoint: str, params: dict, *, retries: int = 2) -> AsyncIterator[httpx.Response]:
    """Like eutil, but hands over the open response so the caller can stop reading early."""
    key = _key()
    query = {**params, "tool": TOOL}
    if key:
        query["api_key"] = key
    interval = 0.11 if key else 0.35
    for attempt in range(retries + 1):
        await _throttle.wait(interval)
        async with client.stream("GET", f"{EUTILS}/{endpoint}", params=query) as response:
            if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            yield response
            return


async def _esearch(client: httpx.AsyncClient, db: str, term: str, max_results: int, sort: str | None) -> tuple[int, list[str]]:
    params = {"db": db, "term": term, "retmax": max_results, "retmode": "json"}
    if sort:
        params["sort"] = sort
    data = (await eutil(client, "esearch.fcgi", params)).json().get("esearchresult", {})
    if "ERROR" in data:
        raise ValueError(data["ERROR"])
    return int(data.get("count") or 0), list(data.get("idlist") or [])


def _plain(term: str) -> bool:
    """Free words, with no field tags, quotes, brackets or boolean operators."""
    return not re.search(r'["\[\]()*]|\b(AND|OR|NOT)\b', term)


async def esearch(client: httpx.AsyncClient, db: str, term: str, max_results: int, sort: str | None) -> tuple[int, list[str]]:
    total, ids = await _esearch(client, db, term, max_results, sort)
    # PMC in particular finds nothing for long unquoted phrases such as a pasted title,
    # while the same words in quotes find the article. Try that before giving up.
    if total == 0 and _plain(term) and len(term.split()) > 1:
        total, ids = await _esearch(client, db, f'"{term}"', max_results, sort)
    return total, ids


async def esummary(client: httpx.AsyncClient, db: str, uids: list[str]) -> list[dict]:
    """Short records (title, authors, journal, date) for many UIDs, in the order given."""
    out: dict[str, dict] = {}
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        data = (await eutil(client, "esummary.fcgi", {"db": db, "id": ",".join(chunk), "retmode": "json"})).json()
        result = data.get("result") or {}
        out.update({uid: result[uid] for uid in chunk if isinstance(result.get(uid), dict) and "error" not in result[uid]})
    return [out[uid] for uid in uids if uid in out]


def summary_fields(record: dict) -> dict:
    from .. import text  # local import keeps this module free of model code

    return {
        "title": text.clean(record.get("title") or ""),
        "authors": [a["name"] for a in record.get("authors") or [] if a.get("name") and a.get("authtype", "Author") == "Author"],
        "journal": record.get("fulljournalname") or record.get("source") or "",
        "year": text.first_year(record.get("pubdate") or record.get("epubdate") or record.get("sortpubdate")),
    }


async def find_one(client: httpx.AsyncClient, db: str, query: str) -> tuple[int, list[str]]:
    """Search by an exact ID field, such as "123[pmid]", for a single article."""
    total, ids = await _esearch(client, db, query, 1, None)
    return min(total, 1), ids[:1]
