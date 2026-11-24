"""Collect everything available about an article beyond what its own source returned.

Whatever the source, the same extras are gathered, mostly from Europe PMC, which indexes
PubMed, PMC and preprints under one API and needs no key:

- full text, figure and table captions and the reference list, for open-access papers
  (from PMC's E-utilities, which is fast, falling back to Europe PMC);
- full-text and PDF links, citation count, licence, ORCIDs and affiliations, grants,
  chemicals, plus MeSH terms and keywords when the source had none;
- the reference list (when the full text had none) and the works citing the article.

A step that fails is noted in `details.missing`; it never fails the article itself.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET

import httpx

from . import jats, text
from .models import Article, AuthorDetail, Details, Figure, Grant, Link, Ref
from .sources import europepmc
from .sources.ncbi import eutil

log = logging.getLogger("helix.literature")

MAX_CITATIONS = 1000  # citing works to list; the count is always the full number
# Europe PMC answers most calls in about a second but sometimes hangs; give up on one attempt
# after this long and retry, rather than stalling the whole save.
EPMC_TIMEOUT = httpx.Timeout(20, connect=10)
FULL_TEXT_TIMEOUT = httpx.Timeout(60, connect=10)  # Europe PMC's full text is slower still


def _list(obj: dict | None, *path: str) -> list:
    for key in path:
        obj = (obj or {}).get(key)
    return obj if isinstance(obj, list) else []


_SITES = {"Europe_PMC": "Europe PMC", "PubMedCentral": "PubMed Central", "PubMedCentral_Manuscript": "PMC author manuscript", "DOI": "Publisher"}


def apply_core(r: dict, details: Details) -> Details:
    """Fold a Europe PMC "core" record into the details."""
    links = []
    for u in _list(r, "fullTextUrlList", "fullTextUrl"):
        if not u.get("url"):
            continue
        style = (u.get("documentStyle") or "").lower()
        site = _SITES.get(u.get("site"), u.get("site") or "")
        kind = {"pdf": "PDF", "html": "Full text", "doi": "Publisher page"}.get(style, style.upper() or "Link")
        label = kind if style == "doi" else f"{kind} ({site})" if site else kind
        links.append(Link(label=label, url=u["url"], kind=style, open_access=u.get("availabilityCode") in ("OA", "F")))
    # Open versions first, then PDFs before HTML.
    links.sort(key=lambda link: (not link.open_access, link.kind != "pdf"))

    authors = []
    for a in _list(r, "authorList", "author"):
        name = a.get("fullName") or a.get("collectiveName") or ""
        if not name:
            continue
        aid = a.get("authorId") or {}
        affs = [x.get("affiliation", "").strip() for x in _list(a, "authorAffiliationDetailsList", "authorAffiliation")]
        if not affs and a.get("affiliation"):
            affs = [a["affiliation"].strip()]
        authors.append(AuthorDetail(
            name=name,
            orcid=aid.get("value") if aid.get("type") == "ORCID" else None,
            affiliations=list(dict.fromkeys(x for x in affs if x)),
        ))

    update = {
        "links": links or details.links,
        "author_details": authors or details.author_details,
        "grants": [Grant(agency=g.get("agency") or "", grant_id=g.get("grantId") or "") for g in _list(r, "grantsList", "grant")] or details.grants,
        "chemicals": [c["name"] for c in _list(r, "chemicalList", "chemical") if c.get("name")] or details.chemicals,
        "license": r.get("license") or details.license,
    }
    if r.get("citedByCount") is not None:
        update["cited_by_count"] = int(r["citedByCount"])
    return details.model_copy(update=update)


def fill_article(article: Article, r: dict) -> Article:
    """Fill gaps in the source's own record from a Europe PMC core record. Never overwrites."""
    update = {}
    if not article.pmid and r.get("pmid"):
        update["pmid"] = text.pmid(r["pmid"])
    if not article.pmcid and r.get("pmcid"):
        update["pmcid"] = text.pmcid(r["pmcid"])
    if not article.doi and r.get("doi"):
        update["doi"] = text.doi(r["doi"])
    if not article.mesh:
        update["mesh"] = [m["descriptorName"] for m in _list(r, "meshHeadingList", "meshHeading") if m.get("descriptorName")]
    if not article.keywords:
        update["keywords"] = [text.clean(k) for k in _list(r, "keywordList", "keyword") if k]
    if not article.abstract and r.get("abstractText"):
        update["abstract"] = europepmc._abstract(r["abstractText"])
    if article.open_access is None and r.get("isOpenAccess"):
        update["open_access"] = r["isOpenAccess"] == "Y"
    return article.model_copy(update={k: v for k, v in update.items() if v not in (None, [], "")})


async def _core(client: httpx.AsyncClient, article: Article) -> dict | None:
    # Looking up by ID is much quicker than a search; a DOI alone needs the search.
    if article.pmid or article.pmcid:
        path = f"/article/MED/{article.pmid}" if article.pmid else f"/article/PMC/{article.pmcid}"
        data = await europepmc._get(client, path, {"resultType": "core"}, timeout=EPMC_TIMEOUT)
        return data.get("result")
    if article.doi:
        data = await europepmc._get(client, "/search", {"query": f'DOI:"{article.doi}"', "resultType": "core", "pageSize": 1}, timeout=EPMC_TIMEOUT)
        hits = (data.get("resultList") or {}).get("result") or []
        return hits[0] if hits else None
    return None


async def _full_text(client: httpx.AsyncClient, pmcid: str) -> tuple[ET.Element | None, str]:
    """The article's JATS XML, from PMC first and Europe PMC second. (None, "") when neither has a body."""
    try:
        response = await eutil(client, "efetch.fcgi", {"db": "pmc", "id": pmcid.removeprefix("PMC"), "retmode": "xml"})
        el = jats.article_element(response.content)
        if el is not None and el.find("body") is not None:
            return el, "PubMed Central"
    except Exception as exc:  # noqa: BLE001 - fall back to Europe PMC
        log.info("PMC full text for %s failed: %s", pmcid, exc)
    response = await client.get(f"{europepmc.BASE}/{pmcid}/fullTextXML", timeout=FULL_TEXT_TIMEOUT)
    if response.status_code >= 500:  # briefly down: one more try
        await asyncio.sleep(2)
        response = await client.get(f"{europepmc.BASE}/{pmcid}/fullTextXML", timeout=FULL_TEXT_TIMEOUT)
    if response.status_code == 404:
        return None, ""
    response.raise_for_status()
    el = jats.article_element(response.content)
    return (el, "Europe PMC") if el is not None and el.find("body") is not None else (None, "")


def _epmc_ref(r: dict) -> Ref:
    return Ref(
        title=text.clean(r.get("title") or ""),
        authors=(r.get("authorString") or "").rstrip("."),
        journal=r.get("journalAbbreviation") or r.get("journalTitle") or "",
        year=text.first_year(str(r.get("pubYear") or "")),
        pmid=r.get("id") if r.get("source") == "MED" else text.pmid(r.get("pmid")),
        pmcid=text.pmcid(r.get("pmcid")),
        doi=text.doi(r.get("doi")),
    )


async def _ref_list(client: httpx.AsyncClient, path: str, list_key: str, item_key: str, limit: int) -> list[Ref]:
    out: list[Ref] = []
    page = 1
    while len(out) < limit:
        data = await europepmc._get(client, path, {"page": page, "pageSize": 1000}, timeout=EPMC_TIMEOUT)
        items = _list(data, list_key, item_key)
        out += [_epmc_ref(r) for r in items]
        if len(items) < 1000:
            break
        page += 1
    return out[:limit]


def _epmc_key(article: Article) -> tuple[str, str] | None:
    """How Europe PMC names this article in its /{source}/{id}/references URLs."""
    if article.source == "europepmc":
        src, _, ext_id = article.source_id.partition(":")
        return src, ext_id
    if article.pmid:
        return "MED", article.pmid
    if article.pmcid:
        return "PMC", article.pmcid
    return None


# NCBI publishes open-access articles, images included, in a public AWS bucket. PMC's own
# figure URLs answer browsers with a bot check instead of the image, so the bucket is used.
OA_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"


def _stem(name: str) -> str:
    name = name.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


async def _open_data(client: httpx.AsyncClient, pmcid: str) -> dict | None:
    """The bucket's metadata for the latest version of an article, or None when it isn't there."""
    listing = await client.get(OA_BUCKET, params={"list-type": "2", "prefix": f"metadata/{pmcid}."}, timeout=EPMC_TIMEOUT)
    listing.raise_for_status()
    keys = [k.text for k in ET.fromstring(listing.content).iter("{http://s3.amazonaws.com/doc/2006-03-01/}Key") if k.text]
    if not keys:
        return None
    latest = max(keys, key=lambda k: int(k.rsplit(".", 2)[-2]) if k.rsplit(".", 2)[-2].isdigit() else 0)
    meta = await client.get(f"{OA_BUCKET}/{latest}", timeout=EPMC_TIMEOUT)
    meta.raise_for_status()
    return meta.json()


def _https(s3_url: str) -> str:
    return OA_BUCKET + "/" + s3_url.removeprefix("s3://pmc-oa-opendata/").split("?", 1)[0]


def apply_open_data(meta: dict, details: Details) -> Details:
    media = {_stem(u.split("?", 1)[0]): _https(u) for u in meta.get("media_urls") or []}
    figures = [
        f.model_copy(update={"image": media.get(_stem(f.graphic), "")}) if f.graphic else f
        for f in details.figures
    ]
    links = list(details.links)
    if meta.get("pdf_url") and not any(link.url == _https(meta["pdf_url"]) for link in links):
        links.insert(0, Link(label="PDF (PubMed Central)", url=_https(meta["pdf_url"]), kind="pdf", open_access=True))
    return details.model_copy(update={"figures": figures, "links": links})


async def _quiet(coro, label: str, missing: list[str], why: str):
    """Run one lookup; on failure note it in `missing` and return None instead of raising."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - one missing extra never fails the article
        log.warning("%s failed: %s", label, exc)
        missing.append(why)
        return None


async def enrich(client: httpx.AsyncClient, article: Article, details: Details, *,
                 citations: bool = True) -> tuple[Article, Details]:
    """`citations=False` skips the citing-works list (research fetches, PLAN B8.13); references are kept."""
    missing: list[str] = []
    key = _epmc_key(article)
    want_core = article.source != "europepmc"  # Europe PMC's own fetch already used the core record
    want_text = not details.full_text and bool(article.pmcid)

    async def nothing():
        return None

    # These don't depend on each other, so they run at the same time.
    core, text_result, cited = await asyncio.gather(
        _quiet(_core(client, article), f"Europe PMC record for {article.source_id}", missing,
               "Citation count, links, funding and affiliations: Europe PMC didn't answer") if want_core else nothing(),
        _quiet(_full_text(client, article.pmcid), f"Full text for {article.pmcid}", missing,
               "Full text: the download failed") if want_text else nothing(),
        _quiet(_ref_list(client, f"/{key[0]}/{key[1]}/citations", "citationList", "citation", MAX_CITATIONS),
               f"Citations for {article.source_id}", missing, "Cited by: Europe PMC didn't answer")
        if key and citations else nothing(),
    )

    if core:
        article = fill_article(article, core)
        details = apply_core(core, details)
    if text_result and text_result[0] is not None:
        sections, figures, refs = jats.parse(text_result[0])
        details = details.model_copy(update={
            "full_text": sections, "full_text_from": text_result[1],
            "figures": details.figures or figures, "references": details.references or refs,
        })
    if cited is not None:
        details = details.model_copy(update={
            "citations": cited,
            "cited_by_count": details.cited_by_count if details.cited_by_count is not None else len(cited),
        })

    # The full text's own reference list is the most complete; ask Europe PMC only without it.
    key = _epmc_key(article)  # the core record may have supplied a PMID
    if key and not details.references:
        refs = await _quiet(_ref_list(client, f"/{key[0]}/{key[1]}/references", "referenceList", "reference", 5000),
                            f"References for {article.source_id}", missing, "References: Europe PMC didn't answer")
        if refs:
            details = details.model_copy(update={"references": refs})

    # Images for the figures, and PMC's own PDF, for open-access papers.
    if article.pmcid and details.figures or article.pmcid and details.full_text:
        meta = await _quiet(_open_data(client, article.pmcid), f"Open-data record for {article.pmcid}", missing,
                            "Figure images: PMC's open-data store didn't answer")
        if meta:
            details = apply_open_data(meta, details)

    if not details.full_text and not any(m.startswith("Full text") for m in missing):
        missing.append("Full text: not open access" if article.pmcid or article.open_access is False else "Full text: not in PubMed Central")

    return article, details.model_copy(update={"missing": missing})
