"""ID formats (PLAN B2.1) and the batch-key helper used by the stage machine (B4)."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

# Segment and reference IDs
USER_SENTENCE = re.compile(r"^U[1-9]\d*$")
ANSWER = re.compile(r"^A[1-9]\d*$")
FOLLOWUP = re.compile(r"^FU[1-9]\d*$")
NOTE_SENTENCE = re.compile(r"^N[1-9]\d*\.s[1-9]\d*$")
BODY_SENTENCE = re.compile(r"^S[1-9]\d*\.P[1-9]\d*\.s[1-9]\d*$")
ABSTRACT_SENTENCE = re.compile(r"^AB\.P[1-9]\d*\.s[1-9]\d*$")
TABLE_CAPTION = re.compile(r"^T[1-9]\d*\.cap$")
TABLE_CELL = re.compile(r"^T[1-9]\d*\.r[1-9]\d*\.c[1-9]\d*$")
TABLE_ROW = re.compile(r"^T[1-9]\d*\.r[1-9]\d*$")
TABLE_FOOTER = re.compile(r"^T[1-9]\d*\.foot$")
FIGURE_CAPTION = re.compile(r"^F[1-9]\d*\.cap$")
FIGURE_READING = re.compile(r"^FR[1-9]\d*$")
KEY_SLUG = re.compile(r"^[a-z0-9]+(?:[_.-][a-z0-9]+)*$")

PAPER_PATTERNS = (BODY_SENTENCE, ABSTRACT_SENTENCE, TABLE_CAPTION, TABLE_CELL, TABLE_ROW, TABLE_FOOTER, FIGURE_CAPTION)
USER_PATTERNS = (USER_SENTENCE, ANSWER, FOLLOWUP, NOTE_SENTENCE)


def answer_seg(question_id: int) -> str:
    return f"A{question_id}"


def is_paper_segment(seg_id: str) -> bool:
    return any(p.match(seg_id) for p in PAPER_PATTERNS)


def is_user_segment(seg_id: str) -> bool:
    return any(p.match(seg_id) for p in USER_PATTERNS)


def row_of(cell_id: str) -> str | None:
    """`T2.r3.c4` → `T2.r3`."""
    match = re.match(r"^(T[1-9]\d*\.r[1-9]\d*)\.c[1-9]\d*$", cell_id)
    return match.group(1) if match else None


def slug(text: str, max_len: int = 48) -> str:
    """Stable lowercase key slug for hypotheses and knowledge keys."""
    value = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    return (value[:max_len].rstrip("_")) or "item"


def batch_key(prefix: str, item_ids: Iterable[object]) -> str:
    """`{prefix}:b{hash}` from sorted item IDs, so the same batch always has the same task key (B4)."""
    joined = ",".join(sorted(str(i) for i in item_ids))
    digest = hashlib.sha256(joined.encode()).hexdigest()[:12]
    return f"{prefix}:b{digest}"


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), max(1, size))]
