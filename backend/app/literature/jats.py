"""Full text from JATS XML, the format PMC and Europe PMC publish articles in.

Only the parts worth reading are kept: section headings with their paragraphs, figure and
table captions, back matter (acknowledgements, funding, conflicts of interest…) and the
reference list. Markup inside paragraphs is flattened to plain text.
"""

import xml.etree.ElementTree as ET

XLINK = "{http://www.w3.org/1999/xlink}href"
MAX_TABLE_ROWS = 300  # keep huge supplementary-style tables from bloating the record

from . import text
from .models import Figure, Ref, Section

# Elements whose text would only clutter a paragraph.
_SKIP_INLINE = {"table-wrap", "fig", "disp-formula", "supplementary-material", "graphic", "media", "alternatives"}
_FN_TITLES = {
    "conflict": "Conflicts of interest",
    "coi-statement": "Conflicts of interest",
    "financial-disclosure": "Funding",
    "supported-by": "Funding",
    "con": "Author contributions",
    "data-availability": "Data availability",
}


def _flat(node: ET.Element | None) -> str:
    """Text of a node, skipping embedded tables, figures and formulas."""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(el: ET.Element) -> None:
        if el.tag in _SKIP_INLINE:
            if el.tail:
                parts.append(el.tail)
            return
        if el.text:
            parts.append(el.text)
        for child in el:
            walk(child)
        if el.tail and el is not node:
            parts.append(el.tail)

    walk(node)
    return text.clean("".join(parts)).replace("\n", " ")


def _paragraphs(sec: ET.Element) -> list[str]:
    out = []
    for child in sec:
        if child.tag == "p":
            p = _flat(child)
            if p:
                out.append(p)
        elif child.tag in ("list", "def-list", "disp-quote", "boxed-text"):
            for p in child.iter("p"):
                t = _flat(p)
                if t:
                    out.append(f"• {t}" if child.tag == "list" else t)
    return out


def _sections(parent: ET.Element, kind: str, trail: tuple[str, ...] = ()) -> list[Section]:
    out: list[Section] = []
    for sec in parent.findall("sec"):
        title = _flat(sec.find("title"))
        path = (*trail, title) if title else trail
        paras = _paragraphs(sec)
        if paras:
            out.append(Section(title=" › ".join(path), text="\n\n".join(paras), kind=kind))
        out += _sections(sec, kind, path)
    return out


def _table(wrap: ET.Element) -> tuple[list[list[str]], int, str]:
    table = wrap.find(".//table")
    if table is None:
        return [], 0, ""
    head = [tr for tr in table.findall("thead/tr")]
    body = [tr for tr in table.findall("tbody/tr")] or [tr for tr in table.findall("tr")]
    rows = [[_flat(cell) for cell in tr if cell.tag in ("td", "th")] for tr in [*head, *body][:MAX_TABLE_ROWS]]
    foot = wrap.find("table-wrap-foot")
    footer = " ".join(p for p in (_flat(c) for c in foot) if p) if foot is not None else ""
    return [r for r in rows if any(r)], len(head), footer


def _figures(root: ET.Element) -> list[Figure]:
    out = []
    for el in root.iter():
        if el.tag not in ("fig", "table-wrap"):
            continue
        caption = el.find("caption")
        body = " ".join(p for p in (_flat(c) for c in caption) if p) if caption is not None else ""
        label = _flat(el.find("label"))
        graphic = next((g.get(XLINK, "") for g in el.iter("graphic") if g.get(XLINK)), "")
        rows, head_rows, footer = _table(el) if el.tag == "table-wrap" else ([], 0, "")
        if label or body or graphic or rows:
            out.append(Figure(
                kind="figure" if el.tag == "fig" else "table", label=label, caption=body,
                graphic=graphic, rows=rows, head_rows=head_rows, footer=footer,
            ))
    return out


def _ref(ref: ET.Element) -> Ref | None:
    cit = ref.find("element-citation")
    if cit is None:
        cit = ref.find("mixed-citation")
    if cit is None:
        cit = ref.find("citation")
    if cit is None:
        return None
    names = []
    for n in cit.iter("name"):
        surname, given = _flat(n.find("surname")), _flat(n.find("given-names"))
        if surname:
            names.append(f"{surname} {given.replace('.', '').replace(' ', '')}".strip())
    for c in cit.iter("collab"):
        names.append(_flat(c))
    ids = {p.get("pub-id-type"): (p.text or "").strip() for p in cit.iter("pub-id")}
    title = _flat(cit.find("article-title")) or _flat(cit.find("chapter-title"))
    journal = _flat(cit.find("source"))
    if not title and not journal:  # unstructured: keep the whole citation as its title
        title = _flat(cit)
    authors = ", ".join(names[:6]) + (" et al." if len(names) > 6 else "")
    return Ref(
        title=title,
        authors=authors,
        journal=journal if title != _flat(cit) else "",
        year=text.first_year(_flat(cit.find("year"))),
        pmid=text.pmid(ids.get("pmid")),
        pmcid=text.pmcid(ids.get("pmcid") or ids.get("pmc")),
        doi=text.doi(ids.get("doi")),
    )


def parse(article: ET.Element) -> tuple[list[Section], list[Figure], list[Ref]]:
    """(full text sections, figures and tables, references) for one <article> element."""
    sections: list[Section] = []
    body = article.find("body")
    if body is not None:
        lead = _paragraphs(body)  # text straight under <body>, before any section
        if lead:
            sections.append(Section(title="", text="\n\n".join(lead)))
        sections += _sections(body, "body")

    back = article.find("back")
    if back is not None:
        for ack in back.findall("ack"):
            paras = _paragraphs(ack) or [_flat(ack)]
            title = _flat(ack.find("title")) or "Acknowledgements"
            if any(paras):
                sections.append(Section(title=title, text="\n\n".join(p for p in paras if p), kind="back"))
        sections += _sections(back, "back")
        for notes in back.findall("notes"):
            title = _flat(notes.find("title"))
            paras = _paragraphs(notes)
            if paras:
                sections.append(Section(title=title or "Notes", text="\n\n".join(paras), kind="back"))
            sections += _sections(notes, "back", (title,) if title else ())
        for fn in back.iter("fn"):
            kind = (fn.get("fn-type") or "").lower()
            if kind in _FN_TITLES:
                body_text = "\n\n".join(_paragraphs(fn)) or _flat(fn)
                if body_text:
                    sections.append(Section(title=_FN_TITLES[kind], text=body_text, kind="back"))

    figures = _figures(article)
    refs = [r for r in (_ref(ref) for ref in article.iter("ref")) if r]
    return sections, figures, refs


def article_element(xml: bytes) -> ET.Element | None:
    root = ET.fromstring(xml)
    return root if root.tag == "article" else root.find(".//article")
