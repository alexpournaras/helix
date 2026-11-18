"""PubMed Central: esearch for PMC IDs, then efetch one article at a time (JATS XML).

PMC's efetch returns the whole article as JATS XML: the citation details come from <front>,
and for open-access papers the full text, figures and references come from the rest.
"""

import xml.etree.ElementTree as ET

import httpx

from .. import jats, text
from ..models import Article, Details, Preview
from .ncbi import esearch, esummary, eutil, find_one, summary_fields

ID = "pmc"
NAME = "PubMed Central"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


# PMC's E-utilities only know "relevance"; with no sort value they return the most recently added.
SORTS = {
    "relevance": ("Best match", "relevance"),
    "added": ("Most recently added", None),
}
DEFAULT_SORT = "relevance"


async def search(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[str]]:
    match text.identifier(term):
        case ("pmcid", value):
            return 1, [value]
        case ("pmid", value):
            total, uids = await find_one(client, "pmc", f"{value}[pmid]")
        case ("doi", value):
            total, uids = await find_one(client, "pmc", f'"{value}"[doi]')
        case ("ppr", _):
            return 0, []  # a Europe PMC preprint, not in PMC
        case _:
            total, uids = await esearch(client, "pmc", term, max_results, SORTS[sort][1])
    return total, [f"PMC{uid}" for uid in uids]


async def preview(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[Preview]]:
    total, ids = await search(client, term, max_results, sort)
    records = await esummary(client, "pmc", [i.removeprefix("PMC") for i in ids]) if ids else []
    return total, [
        Preview(source_id=f"PMC{r['uid']}", url=f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{r['uid']}/", **summary_fields(r))
        for r in records
    ]


async def fetch(client: httpx.AsyncClient, source_id: str) -> tuple[Article, Details] | None:
    """The whole article: citation details from <front>, and full text, figures and
    references when PMC has the body (open-access papers)."""
    uid = source_id.upper().removeprefix("PMC")
    response = await eutil(client, "efetch.fcgi", {"db": "pmc", "id": uid, "retmode": "xml"})
    root = jats.article_element(response.content)
    front = root.find("front") if root is not None else None
    if front is None:
        return None
    article = parse(front, dict(root.attrib), source_id)
    sections, figures, refs = jats.parse(root)
    details = Details(full_text=sections, full_text_from="PubMed Central" if sections else "", figures=figures, references=refs)
    return article, details


def _text(node: ET.Element | None) -> str:
    return text.clean("".join(node.itertext())) if node is not None else ""


def _initials(given: str) -> str:
    return "".join(part[0] for part in given.replace("-", " ").replace(".", " ").split() if part[0].isalpha())


def _authors(meta: ET.Element) -> list[str]:
    out = []
    for contrib in meta.findall("contrib-group/contrib"):
        if contrib.get("contrib-type", "author") != "author":
            continue
        name = contrib.find("name")
        if name is not None:
            given = name.find("given-names")
            initials = (given.get("initials") if given is not None else None) or _initials(_text(given))
            out.append(" ".join(filter(None, [_text(name.find("surname")), initials.replace(".", "")])))
        elif contrib.find("collab") is not None:
            out.append(_text(contrib.find("collab")))
    return [a for a in out if a]


def _pub_date(meta: ET.Element) -> str:
    dates = {}
    for d in meta.findall("pub-date"):
        kind = d.get("pub-type") or d.get("date-type") or ""
        if d.get("publication-format") == "electronic" and kind == "pub":
            kind = "epub"
        dates.setdefault(kind, d)
    for kind in ("ppub", "epub", "pub", "collection", ""):
        d = dates.get(kind)
        if d is not None and d.findtext("year"):
            return text.date(d.findtext("year"), d.findtext("month"), d.findtext("day"))
    d = next(iter(dates.values()), None)
    return text.date(d.findtext("year"), d.findtext("month"), d.findtext("day")) if d is not None else ""


def _abstract(meta: ET.Element) -> str:
    candidates = meta.findall("abstract")
    node = next((a for a in candidates if not a.get("abstract-type")), candidates[0] if candidates else None)
    if node is None:
        return ""
    secs = node.findall("sec")
    if not secs:
        return "\n\n".join(p for p in (_text(p) for p in node.findall("p")) if p) or _text(node)
    parts = []
    for sec in secs:
        title = _text(sec.find("title"))
        body = " ".join(p for p in (_text(p) for p in sec.findall("p")) if p)
        if body:
            parts.append(f"{title}: {body}" if title else body)
    return "\n\n".join(parts)


def parse(front: ET.Element, article_attrs: dict[str, str], source_id: str) -> Article:
    meta = front.find("article-meta")
    journal = front.find("journal-meta")
    ids = {i.get("pub-id-type"): (i.text or "").strip() for i in meta.findall("article-id")}
    pmcid = text.pmcid(ids.get("pmcid") or ids.get("pmc")) or text.pmcid(source_id)
    pub_date = _pub_date(meta)
    first, last = (meta.findtext("fpage") or "").strip(), (meta.findtext("lpage") or "").strip()
    pages = f"{first}-{last}" if first and last and first != last else first or (meta.findtext("elocation-id") or "").strip()

    article_type = article_attrs.get("article-type", "").replace("-", " ")
    return Article(
        source=ID,
        source_id=pmcid,
        url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        pmid=text.pmid(ids.get("pmid")),
        pmcid=pmcid,
        doi=text.doi(ids.get("doi")),
        title=_text(meta.find("title-group/article-title")),
        abstract=_abstract(meta),
        authors=_authors(meta),
        journal=_text(journal.find("journal-title-group/journal-title")) if journal is not None else "",
        year=text.year_of(pub_date),
        pub_date=pub_date,
        volume=(meta.findtext("volume") or "").strip(),
        issue=(meta.findtext("issue") or "").strip(),
        pages=pages,
        keywords=[k for k in (_text(k) for k in meta.findall("kwd-group/kwd")) if k],
        publication_types=[article_type] if article_type else [],
        language=article_attrs.get(XML_LANG, "").lower(),
    )
