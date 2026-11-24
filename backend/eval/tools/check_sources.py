"""Check that every knowledge source resolves (PLAN Part G.6, 1B.3) and write knowledge/sources.lock.json.

Run from backend/:  .venv/Scripts/python -m eval.tools.check_sources [--only id,id]
Live: a GET per source URL (public guidance pages) and one esummary call for the packs' seed PMIDs. No case text
is sent. Tests read the lock file, so "every source resolves" is enforced offline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from app.literature.sources.ncbi import eutil
from app.research.knowledge import loader

UA = "Mozilla/5.0 (compatible; helix-source-check/1.0)"
NOT_FOUND_HINTS = ("page not found", "page you are looking for", "404 error", "we can't find", "cannot be found")


async def _check_url(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await client.get(url)
    except httpx.HTTPError as exc:
        return {"status": f"error: {type(exc).__name__}"}
    head = r.text[:20000].casefold() if "html" in r.headers.get("content-type", "") else ""
    title = ""
    if "<title" in head:
        title = head.split("<title", 1)[1].split(">", 1)[1].split("</title", 1)[0].strip()[:200]
    if r.status_code != 200:
        return {"status": str(r.status_code), "final_url": str(r.url)}
    if any(h in title for h in NOT_FOUND_HINTS):
        return {"status": "soft-404", "final_url": str(r.url), "title": title}
    return {"status": "ok", "final_url": str(r.url), "title": title}


async def main(only: set[str] | None) -> int:
    sources = loader.load_sources()
    lock = loader.load_lock()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": UA}) as client:
        sem = asyncio.Semaphore(4)

        async def one(src):
            async with sem:
                return src, await _check_url(client, src.url)

        results = await asyncio.gather(*(one(s) for s in sources.values() if not only or s.id in only))
        for src, res in results:
            lock.setdefault("sources", {})[src.id] = {"url": src.url, "checked_at": stamp, **res}
        pmids = sorted({p.pmid for pack in loader.load_packs().values() for p in pack.seed_literature if p.pmid})
        if pmids and not only:
            data = (await eutil(client, "esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})).json()
            result = data.get("result") or {}
            for pmid in pmids:
                rec = result.get(pmid) or {}
                ok = isinstance(rec, dict) and rec.get("title") and "error" not in rec
                lock.setdefault("papers", {})[pmid] = {"status": "ok" if ok else "missing", "checked_at": stamp,
                                                       "title": (rec.get("title") or "")[:200]}
    # Drop entries for sources that no longer exist.
    lock["sources"] = {k: v for k, v in sorted(lock.get("sources", {}).items()) if k in sources}
    loader.LOCK_PATH.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n", "utf-8")
    bad = {k: v for k, v in lock["sources"].items() if v.get("status") != "ok"}
    bad_papers = {k: v for k, v in lock.get("papers", {}).items() if v.get("status") != "ok"}
    for k, v in sorted(lock["sources"].items()):
        print(f"{v['status']:<10} {k:<34} {v.get('title', '')[:70]}")
    print(f"\n{len(lock['sources']) - len(bad)}/{len(lock['sources'])} sources ok; "
          f"{len(lock.get('papers', {})) - len(bad_papers)}/{len(lock.get('papers', {}))} papers ok")
    return 1 if bad or bad_papers else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(asyncio.run(main(set(args.only.split(",")) if args.only else None)))
