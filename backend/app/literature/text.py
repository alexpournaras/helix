"""Small normalisers shared by the source adapters."""

import html
import re

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def clean(value: str | None) -> str:
    """Strip markup and collapse whitespace, keeping paragraph breaks."""
    if not value:
        return ""
    value = re.sub(r"</?(p|h\d|br|div)\b[^>]*>", "\n", value, flags=re.I)
    value = html.unescape(_TAG.sub("", value))
    lines = (_SPACE.sub(" ", line).strip() for line in value.split("\n"))
    return "\n".join(line for line in lines if line)


def doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value or None


def pmcid(value: str | int | None) -> str | None:
    if value is None or value == "":
        return None
    digits = str(value).strip().upper().removeprefix("PMC")
    return f"PMC{digits}" if digits.isdigit() else None


def pmid(value: str | int | None) -> str | None:
    value = str(value or "").strip()
    return value if value.isdigit() else None


# Links and IDs that point at one article. Checked in order; the first match wins.
_IDENTIFIERS = [
    ("pmcid", re.compile(r"^(?:https?://)?(?:www\.)?(?:pmc\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pmc)/articles/(PMC\d+)\b", re.I)),
    ("pmid", re.compile(r"^(?:https?://)?(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/(\d+)\b", re.I)),
    ("pmcid", re.compile(r"^(?:https?://)?(?:www\.)?europepmc\.org/(?:article|abstract)/PMC/(PMC\d+)\b", re.I)),
    ("pmid", re.compile(r"^(?:https?://)?(?:www\.)?europepmc\.org/(?:article|abstract)/MED/(\d+)\b", re.I)),
    ("ppr", re.compile(r"^(?:https?://)?(?:www\.)?europepmc\.org/(?:article|abstract)/PPR/(PPR\d+)\b", re.I)),
    ("doi", re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)$", re.I)),
    ("pmcid", re.compile(r"^(?:PMCID:\s*)?(PMC\d+)$", re.I)),
    ("pmid", re.compile(r"^PMID:?\s*(\d+)$", re.I)),  # bare digits could be a year, so PMIDs need the prefix
    ("doi", re.compile(r"^(?:doi:\s*)?(10\.\d{4,9}/\S+)$", re.I)),
]


def identifier(term: str) -> tuple[str, str] | None:
    """("pmcid" | "pmid" | "doi" | "ppr", normalised id) when the term is a link to, or an ID of,
    one article; None for an ordinary search."""
    term = term.strip()
    for kind, pattern in _IDENTIFIERS:
        m = pattern.match(term)
        if m:
            value = m.group(1)
            if kind == "pmcid":
                return kind, pmcid(value)
            if kind == "doi":
                return kind, doi(value.rstrip(".,;"))
            return kind, value.upper()
    return None


def first_year(value: str | None) -> int | None:
    m = re.search(r"\b(1[89]|20)\d{2}\b", value or "")
    return int(m.group(0)) if m else None


def month(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip().lower()
    if value.isdigit():
        n = int(value)
        return n if 1 <= n <= 12 else None
    return _MONTHS.get(value[:3])


def date(year: str | int | None, mon: str | int | None = None, day: str | int | None = None) -> str:
    """Build YYYY[-MM[-DD]] from whatever parts are known."""
    y = str(year or "").strip()[:4]
    if not (len(y) == 4 and y.isdigit()):
        return ""
    m = month(str(mon)) if mon else None
    if not m:
        return y
    d = str(day or "").strip()
    if d.isdigit() and 1 <= int(d) <= 31:
        return f"{y}-{m:02d}-{int(d):02d}"
    return f"{y}-{m:02d}"


def parse_free_date(value: str | None) -> str:
    """'2026 Nov 20', '2026 Nov-Dec', '2026/11/01 00:00', '2026-11-01' -> ISO-ish."""
    if not value:
        return ""
    parts = re.split(r"[\s/\-]+", value.strip())
    return date(parts[0], parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None)


def year_of(pub_date: str) -> int | None:
    return int(pub_date[:4]) if pub_date[:4].isdigit() else None
