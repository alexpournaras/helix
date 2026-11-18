from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceId = Literal["pubmed", "pmc", "europepmc"]
SOURCES: tuple[SourceId, ...] = ("pubmed", "pmc", "europepmc")

TagOrigin = Literal["user", "ai"]
RunStatus = Literal["running", "done", "failed", "cancelled", "interrupted"]


class Article(BaseModel):
    """One article exactly as a single source describes it."""

    source: SourceId
    source_id: str  # the source's own key: a PMID, a PMCID, or "MED:123" / "PPR:PPR123" for Europe PMC
    url: str = ""  # the article's page on that source
    pmid: str | None = None
    pmcid: str | None = None  # "PMC" + digits
    doi: str | None = None  # lower-cased
    title: str = ""
    abstract: str = ""
    authors: list[str] = []
    journal: str = ""
    year: int | None = None
    pub_date: str = ""  # YYYY, YYYY-MM or YYYY-MM-DD
    volume: str = ""
    issue: str = ""
    pages: str = ""
    keywords: list[str] = []
    mesh: list[str] = []
    publication_types: list[str] = []
    language: str = ""
    open_access: bool | None = None  # only Europe PMC reports this


class Ref(BaseModel):
    """A cited or citing work, as short as the source gives it."""

    title: str = ""
    authors: str = ""
    journal: str = ""
    year: int | None = None
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    saved: bool = False  # already in the library; worked out when the article is read


class Section(BaseModel):
    title: str = ""  # nested headings joined with " › "
    text: str = ""  # paragraphs separated by blank lines
    kind: Literal["body", "back"] = "body"  # back: acknowledgements, funding, conflicts of interest…


class Figure(BaseModel):
    kind: Literal["figure", "table"] = "figure"
    label: str = ""
    caption: str = ""
    graphic: str = ""  # the file name the article's XML gives for the image
    image: str = ""  # a URL the browser can load, when PMC publishes the image openly
    rows: list[list[str]] = []  # a table's cells, row by row
    head_rows: int = 0  # how many of `rows` are header rows
    footer: str = ""  # a table's footnotes


class Link(BaseModel):
    label: str
    url: str
    kind: str = ""  # "pdf", "html", "doi"…
    open_access: bool | None = None


class AuthorDetail(BaseModel):
    name: str
    orcid: str | None = None
    affiliations: list[str] = []


class Grant(BaseModel):
    agency: str = ""
    grant_id: str = ""


class Details(BaseModel):
    """Everything beyond the citation and abstract. Stored separately and loaded per article."""

    full_text: list[Section] = []
    full_text_from: str = ""  # where the full text came from
    figures: list[Figure] = []
    references: list[Ref] = []
    citations: list[Ref] = []  # works citing this one (the first 1000)
    cited_by_count: int | None = None
    links: list[Link] = []
    author_details: list[AuthorDetail] = []
    grants: list[Grant] = []
    chemicals: list[str] = []
    license: str = ""
    missing: list[str] = []  # what couldn't be fetched this time, and why


class Tag(BaseModel):
    name: str
    origin: TagOrigin = "user"  # "ai" once models start tagging
    created_at: str


class Literature(Article):
    id: int
    tags: list[Tag] = []
    terms: list[str] = []  # search terms whose fetches returned this article
    cited_by_count: int | None = None
    has_full_text: bool = False
    reference_count: int = 0
    created_at: str
    updated_at: str


class LiteratureFull(Literature):
    details: Details = Details()


class Peek(BaseModel):
    abstract: str = ""


class LiteraturePage(BaseModel):
    total: int
    items: list[Literature]


def clean_tag(value: str) -> str:
    return " ".join(value.split())


class FetchRequest(BaseModel):
    term: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=500)
    sort: str = Field(default="relevance", max_length=40)  # one of the source's SORTS keys
    tags: list[str] = Field(default=[], max_length=20)  # added to every article the run saves
    # Picked from a preview: save exactly these source IDs instead of searching again.
    ids: list[str] | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("ids")
    @classmethod
    def _ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        out = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
        if not out:
            raise ValueError("Pick at least one article.")
        if any(len(v) > 64 for v in out):
            raise ValueError("That doesn't look like an article ID.")
        return out

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            value = clean_tag(value)
            if len(value) > 80:
                raise ValueError("Tags can be at most 80 characters.")
            if value and value.casefold() not in {t.casefold() for t in out}:
                out.append(value)
        return out

    @field_validator("term")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a search term.")
        return value


class Preview(BaseModel):
    """One search hit, summarised without fetching the whole record."""

    source_id: str
    url: str
    title: str = ""
    authors: list[str] = []
    journal: str = ""
    year: int | None = None
    saved: bool = False  # already in the library


class PreviewPage(BaseModel):
    source: SourceId
    total: int  # hits the source reports for the term
    items: list[Preview]


class FetchRun(BaseModel):
    id: int
    source: SourceId
    term: str
    max_results: int
    sort: str
    tags: list[str] = []
    status: RunStatus
    total: int | None = None  # hits the source reports for the term
    queued: int = 0  # article IDs this run will fetch
    fetched: int = 0  # saved, new or updated
    new: int = 0
    updated: int = 0
    failed: int = 0
    current: str | None = None  # source_id being fetched right now
    error: str | None = None  # why the run stopped, or the latest per-article failure
    started_at: str
    finished_at: str | None = None


class TagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    origin: TagOrigin = "user"

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = clean_tag(value)
        if not value:
            raise ValueError("A tag needs a name.")
        return value
