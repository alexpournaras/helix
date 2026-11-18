"""PubMed: esearch for PMIDs, then efetch one article at a time (PubMed XML)."""

import xml.etree.ElementTree as ET

import httpx

from .. import text
from ..models import Article, Details, Preview
from .ncbi import esearch, esummary, eutil, find_one, summary_fields

ID = "pubmed"
NAME = "PubMed"


# Helix sort key -> (label, E-utilities sort value). Without a sort value, E-utilities returns
# the most recently added records first, which is not the same as the website's "Best match".
SORTS = {
    "relevance": ("Best match", "relevance"),
    "added": ("Most recently added", None),
    "pub_date": ("Publication date, newest first", "pub_date"),
    "author": ("First author, A–Z", "Author"),
    "journal": ("Journal, A–Z", "JournalName"),
}
DEFAULT_SORT = "relevance"


async def search(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[str]]:
    match text.identifier(term):
        case ("pmid", value):
            return 1, [value]
        case ("pmcid", value):
            return await find_one(client, "pubmed", f"{value}[pmc]")
        case ("doi", value):
            return await find_one(client, "pubmed", f'"{value}"[doi]')
        case ("ppr", _):
            return 0, []  # a Europe PMC preprint, not in PubMed
        case _:
            return await esearch(client, "pubmed", term, max_results, SORTS[sort][1])


async def preview(client: httpx.AsyncClient, term: str, max_results: int, sort: str) -> tuple[int, list[Preview]]:
    total, ids = await search(client, term, max_results, sort)
    records = await esummary(client, "pubmed", ids) if ids else []
    return total, [
        Preview(source_id=r["uid"], url=f"https://pubmed.ncbi.nlm.nih.gov/{r['uid']}/", **summary_fields(r)) for r in records
    ]


async def fetch(client: httpx.AsyncClient, source_id: str) -> tuple[Article, Details] | None:
    response = await eutil(client, "efetch.fcgi", {"db": "pubmed", "id": source_id, "retmode": "xml"})
    node = ET.fromstring(response.content).find("PubmedArticle")
    return (parse(node), Details()) if node is not None else None


def _text(node: ET.Element | None) -> str:
    return text.clean("".join(node.itertext())) if node is not None else ""


def _date(node: ET.Element | None) -> str:
    if node is None:
        return ""
    if node.find("Year") is not None:
        return text.date(node.findtext("Year"), node.findtext("Month"), node.findtext("Day"))
    return text.parse_free_date(node.findtext("MedlineDate"))  # e.g. "2026 Nov-Dec"


def parse(node: ET.Element) -> Article:
    citation = node.find("MedlineCitation")
    article = citation.find("Article")
    journal = article.find("Journal")
    issue = journal.find("JournalIssue") if journal is not None else None

    sections = []
    for part in article.findall("Abstract/AbstractText"):
        body = _text(part)
        label = part.get("Label")
        if body:
            sections.append(f"{label}: {body}" if label and label.upper() != "UNLABELLED" else body)

    authors = []
    for a in article.findall("AuthorList/Author"):
        if a.findtext("CollectiveName"):
            authors.append(_text(a.find("CollectiveName")))
        elif a.findtext("LastName"):
            authors.append(" ".join(filter(None, [a.findtext("LastName"), a.findtext("Initials")])))

    ids = {i.get("IdType"): (i.text or "").strip() for i in node.findall("PubmedData/ArticleIdList/ArticleId")}
    doi = ids.get("doi") or next(
        (e.text for e in article.findall("ELocationID") if e.get("EIdType") == "doi"), None
    )

    pub_date = _date(issue.find("PubDate") if issue is not None else None) or _date(article.find("ArticleDate"))
    pmid = citation.findtext("PMID", "").strip()

    return Article(
        source=ID,
        source_id=pmid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        pmid=text.pmid(pmid),
        pmcid=text.pmcid(ids.get("pmc")),
        doi=text.doi(doi),
        title=_text(article.find("ArticleTitle")) or _text(article.find("VernacularTitle")),
        abstract="\n\n".join(sections),
        authors=authors,
        journal=(journal.findtext("Title") or journal.findtext("ISOAbbreviation") or "").strip() if journal is not None else "",
        year=text.year_of(pub_date),
        pub_date=pub_date,
        volume=(issue.findtext("Volume") or "").strip() if issue is not None else "",
        issue=(issue.findtext("Issue") or "").strip() if issue is not None else "",
        pages=(article.findtext("Pagination/MedlinePgn") or "").strip(),
        keywords=[_text(k) for k in citation.findall("KeywordList/Keyword") if _text(k)],
        mesh=[_text(m) for m in citation.findall("MeshHeadingList/MeshHeading/DescriptorName") if _text(m)],
        publication_types=[_text(p) for p in article.findall("PublicationTypeList/PublicationType") if _text(p)],
        language=(article.findtext("Language") or "").strip().lower(),
    )
