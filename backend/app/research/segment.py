"""Sentence splitting, user segments and paper snapshots (PLAN B2.1, B8.1, B8.14).

Models cite segment IDs; code assembles quotes from the stored text (`quote`). One splitter serves user text
and papers. It follows the B8.1 rules (line breaks; `.` `!` `?` `…` followed by whitespace and an uppercase
letter, digit, quote or bracket; Greek `;` and U+037E; pieces under 3 characters merged), with the B8.14
protections applied to both: never inside brackets, after abbreviations, after initials, or inside decimals.

Changing the rules changes segment IDs, so bump SEGMENTER_VERSION (it is stored with each snapshot).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from functools import cached_property
from typing import Iterable, Literal

from app.literature.models import Details

SEGMENTER_VERSION = "seg.v1"
ANSWER_MAX_CHARS = 2000
MIN_PIECE = 3

_TERMINATORS = ".!?…"
_GREEK_TERMINATORS = ";;"
_CLOSERS = "\"'”’»)]}"
_OPENERS = "\"'“‘«„([{"
_PAIRS = {")": "(", "]": "[", "}": "{"}

# Lower-cased tokens (without the final period) that never end a sentence.
_ABBREVIATIONS = {
    "e.g", "i.e", "al", "vs", "cf", "approx", "ca", "resp", "incl", "esp", "viz",
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st",
    "fig", "figs", "tab", "eq", "eqs", "ref", "refs", "suppl", "sect", "chap", "ed", "eds",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
# Tokens that are abbreviations only before a number ("No. 4", "p. 12"), so "No. It stopped." still splits.
_ABBREVIATIONS_BEFORE_DIGIT = {"no", "nos", "nr", "vol", "vols", "p", "pp", "art"}

_GREEK_LETTER = re.compile(r"[Ͱ-Ͽἀ-῿]")
_LATIN_LETTER = re.compile(r"[A-Za-zÀ-ɏ]")
_SPACES = re.compile(r"[ \t\f\v  -​  　]+")
_BLANK_LINE = re.compile(r"\n\s*\n")
_SKIP_TITLE = re.compile(r"acknowledg|contribution|^(?:foot)?notes?\b|authors?'?s?'? notes?", re.IGNORECASE)
_KEEP_IF = re.compile(r"conflict|competing|funding|funded|financial|grant|disclos|sponsor", re.IGNORECASE)


# ---------- splitter ----------

def is_greek(text: str) -> bool:
    """More Greek letters than Latin ones."""
    return len(_GREEK_LETTER.findall(text)) > len(_LATIN_LETTER.findall(text))


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _matched_brackets(line: str) -> dict[int, int]:
    """Open index → close index, for brackets that are closed. Unmatched brackets are ignored."""
    stack: list[tuple[str, int]] = []
    pairs: dict[int, int] = {}
    for i, ch in enumerate(line):
        if ch in "([{":
            stack.append((ch, i))
        elif ch in _PAIRS:
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == _PAIRS[ch]:
                    pairs[stack[depth][1]] = i
                    del stack[depth:]
                    break
    return pairs


def _token_before(line: str, i: int) -> tuple[str, str]:
    """(the word ending at i, the word before it), without leading quotes or brackets."""
    head = line[:i].rstrip()
    words = head.split()
    word = words[-1].lstrip(_OPENERS) if words else ""
    previous = words[-2].lstrip(_OPENERS) if len(words) > 1 else ""
    return word, previous


def _is_initial(word: str) -> bool:
    return len(word) == 1 and word.isalpha() and word.isupper()


def _protected_period(line: str, i: int, next_char: str) -> bool:
    word, previous = _token_before(line, i)
    low = word.casefold()
    if low in _ABBREVIATIONS:
        return True
    if low in _ABBREVIATIONS_BEFORE_DIGIT and next_char.isdigit():
        return True
    if _is_initial(word):
        # "J. Smith", "J. A. Smith", "Mary K. Jones" — but "factor B. Patients" ends a sentence.
        if not previous:
            return True
        if previous.endswith(".") and _is_initial(previous[:-1]):
            return True
        return previous[0].isupper()
    return False


def _split_line(line: str, greek: bool) -> list[str]:
    terminators = _TERMINATORS + (_GREEK_TERMINATORS if greek else ";")
    pairs = _matched_brackets(line)
    closes = set(pairs.values())
    pieces: list[str] = []
    start, depth, i, n = 0, 0, 0, len(line)
    while i < n:
        ch = line[i]
        if i in pairs:
            depth += 1
        elif i in closes:
            depth = max(0, depth - 1)
        elif ch in terminators and depth == 0:
            end = i + 1
            if ch == ".":
                while end < n and line[end] == ".":  # "..." is one terminator
                    end += 1
            ellipsis = end - i > 1
            while end < n and line[end] in _CLOSERS:  # at depth 0 these are unmatched or quotes
                end += 1
            if end < n and line[end].isspace():
                k = end
                while k < n and line[k].isspace():
                    k += 1
                nxt = line[k] if k < n else ""
                starts = bool(nxt) and (nxt.isupper() or nxt.isdigit() or nxt in _OPENERS)
                if starts and not (ch == "." and not ellipsis and _protected_period(line, i, nxt)):
                    pieces.append(line[start:end].strip())
                    start = k
                    i = k
                    continue
            i = end
            continue
        i += 1
    pieces.append(line[start:].strip())
    return [p for p in pieces if p]


def _merge_short(pieces: list[str]) -> list[str]:
    out: list[str] = []
    carry = ""
    for piece in pieces:
        if carry:
            piece = f"{carry} {piece}"
            carry = ""
        if len(piece) < MIN_PIECE:
            if out:
                out[-1] = f"{out[-1]} {piece}"
            else:
                carry = piece
            continue
        out.append(piece)
    if carry:
        out.append(carry)
    return out


def split_sentences(text: str, greek: bool | None = None) -> list[str]:
    if not text or not text.strip():
        return []
    if greek is None:
        greek = is_greek(text)
    pieces: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _normalise(raw)
        if line:
            pieces += _split_line(line, greek)
    return _merge_short(pieces)


def split_user(text: str, greek: bool | None = None) -> list[str]:
    """Problem text, notes and follow-ups (B8.1)."""
    return split_sentences(text, greek)


def split_paper(text: str, greek: bool | None = None) -> list[str]:
    """One paper paragraph (B8.14)."""
    return split_sentences(text, greek)


# ---------- user segments ----------

UserOrigin = Literal["problem", "answer", "followup", "note"]


@dataclass
class UserSegment:
    seg_id: str
    origin: UserOrigin
    text: str
    ref_id: str | None = None
    text_en: str | None = None


def user_segments(problem_text: str, greek: bool | None = None) -> list[UserSegment]:
    return [UserSegment(f"U{n}", "problem", s) for n, s in enumerate(split_user(problem_text, greek), 1)]


def answer_segment(question_id: int, question: str, answer: str) -> UserSegment:
    """One segment "Q: … A: …", at most 2,000 characters (B2.1)."""
    text = f"Q: {_normalise(' '.join(question.split()))} A: {_normalise(' '.join(answer.split()))}"
    if len(text) > ANSWER_MAX_CHARS:
        text = text[:ANSWER_MAX_CHARS - 1].rstrip() + "…"
    return UserSegment(f"A{question_id}", "answer", text, ref_id=str(question_id))


def followup_segment(followup_id: int, text: str) -> UserSegment:
    return UserSegment(f"FU{followup_id}", "followup", " ".join(text.split()), ref_id=str(followup_id))


def note_segments(note_id: int, text: str, greek: bool | None = None) -> list[UserSegment]:
    return [UserSegment(f"N{note_id}.s{n}", "note", s, ref_id=str(note_id))
            for n, s in enumerate(split_user(text, greek), 1)]


# ---------- paper snapshots ----------

SegmentKind = Literal["sentence", "caption", "cell", "footer"]


@dataclass
class PaperSegment:
    seg_id: str
    text: str
    kind: SegmentKind
    section: str  # section title, "Abstract", or the table/figure label
    order: int
    header: bool = False  # table header cell
    label: str = ""  # "Table 2", "Figure 1"
    page: int | None = None  # PDF page (M2)


@dataclass
class Snapshot:
    segments: list[PaperSegment]
    skipped_sections: list[str] = field(default_factory=list)
    has_body: bool = False
    hash: str = ""
    version: str = SEGMENTER_VERSION

    @property
    def coverage(self) -> str:
        """`full` or `abstract_only`; packing may lower `full` to `partial`."""
        return "full" if self.has_body else "abstract_only"

    @cached_property
    def _index(self) -> dict[str, PaperSegment]:
        return {s.seg_id: s for s in self.segments}

    def get(self, seg_id: str) -> PaperSegment:
        return self._index[seg_id]

    def __contains__(self, seg_id: object) -> bool:
        return seg_id in self._index or (isinstance(seg_id, str) and bool(self.row_cells(seg_id)))

    def row_cells(self, row_id: str) -> list[PaperSegment]:
        prefix = row_id + ".c"
        return [s for s in self.segments if s.kind == "cell" and s.seg_id.startswith(prefix)]

    def to_dict(self) -> dict:
        return {"segments": [asdict(s) for s in self.segments], "skipped_sections": list(self.skipped_sections),
                "has_body": self.has_body, "hash": self.hash, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(segments=[PaperSegment(**s) for s in data.get("segments", [])],
                   skipped_sections=list(data.get("skipped_sections", [])), has_body=bool(data.get("has_body")),
                   hash=data.get("hash", ""), version=data.get("version", SEGMENTER_VERSION))


def _skipped(title: str, text: str) -> bool:
    return bool(_SKIP_TITLE.search(title)) and not _KEEP_IF.search(text)


def _hash(segments: Iterable[PaperSegment]) -> str:
    payload = [[s.seg_id, s.text, s.kind, s.header, s.page] for s in segments]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def paper_snapshot(abstract: str, details: Details | None = None) -> Snapshot:
    """Abstract, body sections, figure captions and tables, in document order, with B2.1 IDs.

    Sections are numbered by their position in `details.full_text` (skipped ones keep their number);
    figures and tables are numbered separately in the order they appear.
    """
    details = details or Details()
    out: list[PaperSegment] = []

    def add(seg_id: str, text: str, kind: SegmentKind, section: str, **extra) -> None:
        out.append(PaperSegment(seg_id, text, kind, section, len(out), **extra))

    paragraphs = [p for p in (abstract or "").replace("\r", "\n").split("\n") if p.strip()]
    for p_no, para in enumerate(paragraphs, 1):
        for s_no, sentence in enumerate(split_paper(para), 1):
            add(f"AB.P{p_no}.s{s_no}", sentence, "sentence", "Abstract")

    skipped: list[str] = []
    has_body = False
    for sec_no, section in enumerate(details.full_text, 1):
        if _skipped(section.title, section.text):
            skipped.append(section.title)
            continue
        paras = [p for p in _BLANK_LINE.split(section.text or "") if p.strip()]
        for p_no, para in enumerate(paras, 1):
            for s_no, sentence in enumerate(split_paper(para), 1):
                add(f"S{sec_no}.P{p_no}.s{s_no}", sentence, "sentence", section.title)
                has_body = has_body or section.kind == "body"

    fig_no = table_no = 0
    for fig in details.figures:
        if fig.kind == "table":
            table_no += 1
            label = fig.label or f"Table {table_no}"
            caption = _normalise(" ".join(fig.caption.split()))
            if caption:
                add(f"T{table_no}.cap", caption, "caption", label, label=label)
            for r_no, row in enumerate(fig.rows, 1):
                for c_no, cell in enumerate(row, 1):
                    value = " ".join((cell or "").split())
                    if value:
                        add(f"T{table_no}.r{r_no}.c{c_no}", value, "cell", label, label=label,
                            header=r_no <= fig.head_rows)
            footer = " ".join(fig.footer.split())
            if footer:
                add(f"T{table_no}.foot", footer, "footer", label, label=label)
        else:
            fig_no += 1
            label = fig.label or f"Figure {fig_no}"
            caption = " ".join(fig.caption.split())
            if caption:
                add(f"F{fig_no}.cap", caption, "caption", label, label=label)

    return Snapshot(segments=out, skipped_sections=skipped, has_body=has_body, hash=_hash(out))


# ---------- structure helpers ----------

def part_of(seg_id: str) -> str:
    """`S2.P1.s3` → `S2`; `AB.P1.s1` → `AB`; `T1.r2.c3` → `T1`."""
    return seg_id.split(".", 1)[0]


def block_of(seg_id: str) -> str:
    """The paragraph of a sentence (`S2.P1`) or the row of a cell (`T1.r2`); captions and footers are their own."""
    pieces = seg_id.split(".")
    if len(pieces) == 3:
        return f"{pieces[0]}.{pieces[1]}"
    return seg_id


def quote(snapshot: Snapshot, seg_ids: Iterable[str]) -> str:
    """The cited text in document order: neighbours in one paragraph joined by a space, cells of one row by
    " | ", anything else by " … ". A row ID stands for all its cells. Unknown IDs raise KeyError."""
    chosen: dict[str, PaperSegment] = {}
    for seg_id in seg_ids:
        if seg_id in snapshot._index:
            chosen[seg_id] = snapshot.get(seg_id)
            continue
        cells = snapshot.row_cells(seg_id)
        if not cells:
            raise KeyError(seg_id)
        chosen.update({c.seg_id: c for c in cells})
    ordered = sorted(chosen.values(), key=lambda s: s.order)
    text = ""
    previous: PaperSegment | None = None
    for seg in ordered:
        if previous is None:
            text = seg.text
        elif seg.order == previous.order + 1 and block_of(seg.seg_id) == block_of(previous.seg_id):
            text += (" | " if seg.kind == "cell" else " ") + seg.text
        else:
            text += " … " + seg.text
        previous = seg
    return text
