"""Section-aware packing of a paper snapshot (PLAN B8.14 "Coverage").

Packs follow document order and stay under `max_tokens` (B15 `extraction.max_pack_tokens`, 32k):
- a part (the abstract, a section, a table, a figure caption) joins the current pack if it fits; otherwise it
  starts a fresh pack if it fits in one; otherwise it is split at paragraphs (table rows), then at sentences;
- a table split across packs repeats its heading and header rows in every pack;
- a single sentence larger than the limit gets a pack of its own, flagged `oversize`.
There is no cap on the number of packs unless the profile sets `max_packs_per_paper`; then later packs are
dropped and coverage becomes `partial`. With `priority` (budget, PLAN B17.5 item 4) the parts are chosen in this
order instead: abstract, results (with tables and figure captions), discussion and conclusions, methods, everything
else; the chosen parts are then packed again in document order, and the sections left out are reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import groupby

from app.research import tokens
from app.research.segment import PaperSegment, Snapshot, block_of, part_of
from app.research.settings import QUALITY

DEFAULT_MAX_TOKENS: int = QUALITY["extraction"]["max_pack_tokens"]


@dataclass
class Pack:
    no: int
    seg_ids: list[str]
    text: str
    tokens: int
    oversize: bool = False

    def contains(self, seg_id: str) -> bool:
        """Whether a cited ID was shown in this pack; a row ID counts if any of its cells was."""
        if seg_id in self._ids:
            return True
        prefix = seg_id + ".c"
        return any(i.startswith(prefix) for i in self.seg_ids)

    @property
    def _ids(self) -> set[str]:
        return set(self.seg_ids)

    def summary(self) -> dict:
        """What `paper_reads.packs` stores (the text can be rendered again from the snapshot)."""
        return {"no": self.no, "seg_ids": self.seg_ids, "tokens": self.tokens, "oversize": self.oversize}


@dataclass
class PackResult:
    packs: list[Pack]
    coverage: str  # full / partial / abstract_only
    dropped_ids: list[str] = field(default_factory=list)
    dropped_sections: list[str] = field(default_factory=list)  # titles of parts left out by the pack cap


RESULTS_WORDS = re.compile(r"result|finding|outcome|efficacy|effect|response|adverse|safety|harm", re.I)
DISCUSSION_WORDS = re.compile(r"discussion|conclusion|summary|interpretation|implication", re.I)
METHODS_WORDS = re.compile(r"method|material|patient|participant|population|design|statistic|procedure|setting|"
                           r"intervention|measure|protocol|eligib", re.I)


def part_priority(part: str, seg: PaperSegment) -> int:
    """0 abstract, 1 results/tables/figures, 2 discussion and conclusions, 3 methods, 4 everything else."""
    if part == "AB":
        return 0
    if part[0] in "TF":
        return 1
    title = seg.section or ""
    if RESULTS_WORDS.search(title):
        return 1
    if DISCUSSION_WORDS.search(title):
        return 2
    if METHODS_WORDS.search(title):
        return 3
    return 4


# ---------- rendering ----------

def _heading(part: str, seg: PaperSegment) -> str:
    if part == "AB":
        return "### Abstract"
    title = seg.label if part[0] in "TF" else seg.section
    return f"### {part} {title}".rstrip()


def _row_line(row_id: str, cells: list[PaperSegment], width: int) -> str:
    by_col = {int(c.seg_id.rsplit(".c", 1)[1]): c for c in cells}
    shown = [f"[{by_col[k].seg_id}] {by_col[k].text}" if k in by_col else "—" for k in range(1, width + 1)]
    header = " (header)" if all(c.header for c in cells) else ""
    return f"[{row_id}]{header} " + " | ".join(shown)


def _render_part(part: str, segs: list[PaperSegment]) -> list[str]:
    lines = [_heading(part, segs[0])]
    if part[0] == "T":
        width = max((int(s.seg_id.rsplit(".c", 1)[1]) for s in segs if s.kind == "cell"), default=0)
        for block, group in groupby(segs, key=lambda s: block_of(s.seg_id)):
            items = list(group)
            if items[0].kind == "cell":
                lines.append(_row_line(block, items, width))
            else:
                lines += [f"[{s.seg_id}] {s.text}" for s in items]
        return lines
    first = True
    for _, group in groupby(segs, key=lambda s: block_of(s.seg_id)):
        if not first and part[0] in "AS":
            lines.append("")
        lines += [f"[{s.seg_id}] {s.text}" for s in group]
        first = False
    return lines


def render_segments(segments: list[PaperSegment]) -> str:
    """The text a model sees: one heading per part, one line per sentence, caption or table row."""
    out: list[str] = []
    for part, group in groupby(segments, key=lambda s: part_of(s.seg_id)):
        if out:
            out.append("")
        out += _render_part(part, list(group))
    return "\n".join(out)


# ---------- packing ----------

def _blocks(segs: list[PaperSegment]) -> list[list[PaperSegment]]:
    return [list(g) for _, g in groupby(segs, key=lambda s: block_of(s.seg_id))]


class _Packer:
    def __init__(self, max_tokens: int) -> None:
        self.max = max_tokens
        self.done: list[list[PaperSegment]] = []
        self.current: list[PaperSegment] = []

    def fits(self, segs: list[PaperSegment]) -> bool:
        return tokens.estimate(render_segments(segs)) <= self.max

    def flush(self) -> None:
        if self.current:
            self.done.append(self.current)
            self.current = []

    def place(self, unit: list[PaperSegment], sticky: list[PaperSegment]) -> bool:
        """Add `unit` to the current pack, or to a fresh one (with `sticky` first). False if it fits in neither."""
        if self.fits(self.current + unit):
            self.current += unit
            return True
        fresh = [s for s in sticky if s not in unit] + unit
        if self.fits(fresh):
            self.flush()
            self.current = fresh
            return True
        return False

    def add_part(self, segs: list[PaperSegment]) -> None:
        if self.place(segs, []):
            return
        sticky = [s for s in segs if s.header]  # table header rows travel with every piece of the table
        for block in _blocks(segs):
            if self.place(block, sticky):
                continue
            for seg in block:
                if self.place([seg], sticky):
                    continue
                self.flush()
                self.done.append([seg])  # larger than a pack on its own: oversize


def _pack(parts: list[list[PaperSegment]], max_tokens: int) -> list[list[PaperSegment]]:
    packer = _Packer(max_tokens)
    for segs in parts:
        packer.add_part(segs)
    packer.flush()
    return packer.done


def pack_snapshot(snapshot: Snapshot, max_tokens: int = DEFAULT_MAX_TOKENS,
                  max_packs: int | None = None, priority: bool = False) -> PackResult:
    parts = [list(g) for _, g in groupby(snapshot.segments, key=lambda s: part_of(s.seg_id))]
    done = _pack(parts, max_tokens)
    dropped_sections: list[str] = []
    if priority and max_packs is not None and len(done) > max_packs:
        ranked = sorted(range(len(parts)), key=lambda i: (part_priority(part_of(parts[i][0].seg_id), parts[i][0]), i))
        chosen = _pack([parts[i] for i in ranked], max_tokens)[:max_packs]
        kept_ids = {s.seg_id for pack in chosen for s in pack}
        kept_parts = [p for p in parts if any(s.seg_id in kept_ids for s in p)]
        kept_parts = [[s for s in p if s.seg_id in kept_ids] for p in kept_parts]
        done = _pack(kept_parts, max_tokens)  # document order again, for reading
        dropped_sections = list(dict.fromkeys(
            (p[0].label or p[0].section) for p in parts if not any(s.seg_id in kept_ids for s in p)))

    packs = []
    for no, segs in enumerate(done, 1):
        text = render_segments(segs)
        count = tokens.estimate(text)
        packs.append(Pack(no=no, seg_ids=[s.seg_id for s in segs], text=text, tokens=count,
                          oversize=count > max_tokens))

    coverage = snapshot.coverage
    dropped: list[str] = []
    if max_packs is not None and len(packs) > max_packs:
        packs = packs[:max_packs]
        kept = {i for p in packs for i in p.seg_ids}
        dropped = [s.seg_id for s in snapshot.segments if s.seg_id not in kept]
        if coverage == "full":
            coverage = "partial"
    elif dropped_sections:
        kept = {i for p in packs for i in p.seg_ids}
        dropped = [s.seg_id for s in snapshot.segments if s.seg_id not in kept]
        if coverage == "full":
            coverage = "partial"
    return PackResult(packs=packs, coverage=coverage, dropped_ids=dropped, dropped_sections=dropped_sections)
