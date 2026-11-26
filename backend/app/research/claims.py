"""Claim code checks and reconciliation (PLAN B8.14), pure functions over a snapshot.

Checks on each extracted claim:
- every sentence ID belongs to what the extractor was shown (its pack, or the whole paper for the whole-paper
  pass); a claim left without a valid ID is rejected;
- the quote is assembled by code in document order (`segment.quote`);
- every number of the effect (value, CI bounds, p) must appear in the cited text, else the effect is cleared;
- `n` must appear in the cited text or the profile's n, else it is cleared;
- unknown hypothesis keys are dropped (a claim without links counts as neutral).

Reconciliation: claims from different extractors match when their sentence-ID sets overlap by ≥ 50% (of the
smaller set) and the token Jaccard of the text is ≥ 0.5; pairs meeting one condition but not the other, with
some overlap of both, are borderline and go to a judge. A cluster keeps the claim citing fewer sentences (then the
shorter one) with `agreement = 2`; the others are members. Unmatched claims keep `agreement = 1`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .schemas import Claim
from .segment import Snapshot, quote

REGISTRY = [re.compile(p) for p in (r"NCT\d{8}", r"ISRCTN\d{8}", r"ACTRN\d{14}", r"ChiCTR[\w-]+", r"DRKS\d{8}",
                                    r"\b\d{4}-\d{6}-\d{2}\b", r"CTRI/[\d/]+\d", r"IRCT\w+")]
MATCH_IDS, MATCH_TEXT = 0.5, 0.5
BORDER_IDS, BORDER_TEXT = 0.25, 0.25
STOP = {"the", "a", "an", "of", "in", "and", "or", "to", "with", "for", "was", "were", "is", "are", "on", "by",
        "at", "as", "that", "than", "from", "be", "this", "these", "patients", "participants"}


# ---------- study identity ----------

def registry_ids(snapshot: Snapshot) -> list[dict]:
    """[{id, sentence_ids}] for every trial registry ID in the snapshot (B8.14 study identity)."""
    found: dict[str, list[str]] = {}
    for s in snapshot.segments:
        for rx in REGISTRY:
            for m in rx.finditer(s.text):
                ids = found.setdefault(m.group(0).rstrip("/"), [])
                if s.seg_id not in ids:
                    ids.append(s.seg_id)
    return [{"id": k, "sentence_ids": v[:4]} for k, v in found.items()]


# ---------- checks ----------

def _number_forms(x: float) -> set[str]:
    forms = {repr(x), f"{x:g}"}
    for d in range(0, 4):
        f = f"{x:.{d}f}"
        if float(f) == x:  # exact forms only (1.5 ↔ 1.50); never a rounding (1.05 is not 1.1)
            forms.add(f)
    out = set()
    for f in forms:
        f = f.rstrip("0").rstrip(".") if "." in f else f
        out |= {f, f.replace(".", ","), f.lstrip("0") if f.startswith("0.") else f}
        if f.startswith("-"):
            out |= {"−" + f[1:], "–" + f[1:]}
    return {o for o in out if o}


def number_in(x: float, text: str) -> bool:
    """`x` is written in `text` (1.50 ↔ 1.5, 0.5 ↔ .5, a decimal comma, a Unicode minus)."""
    numbers = re.findall(r"[−–-]?\d[\d,]*(?:[.,]\d+)?|[−–-]?[.,]\d+", text)
    norm = set()
    for n in numbers:
        n = n.strip().rstrip(",")  # "1,234, and" — the list comma isn't part of the number
        norm.add(n)
        if "," in n and "." not in n and re.fullmatch(r"[−–-]?\d{1,3}(,\d{3})+", n):
            norm.add(n.replace(",", ""))  # 1,234 as a thousands separator
        if "." in n or "," in n:
            dec = n.replace(",", ".")
            if "." in dec:
                dec = dec.rstrip("0").rstrip(".")
            norm |= {dec, dec.replace(".", ",")}
            if dec.startswith("0."):
                norm.add(dec[1:])
    return bool(_number_forms(x) & norm)


def p_in(p: str, text: str) -> bool:
    digits = re.findall(r"\d*[.,]?\d+", p)
    return bool(digits) and all(number_in(float(d.replace(",", ".")), text) for d in digits)


@dataclass
class Checked:
    claim: Claim
    sentence_ids: list[str]
    quote: str
    checks: dict = field(default_factory=dict)


def check_claim(claim: Claim, *, allowed: Iterable[str] | None, snapshot: Snapshot, profile_n: str | None,
                hypothesis_keys: set[str]) -> tuple[Checked | None, str | None]:
    """(checked claim, None) or (None, rejection reason). `allowed` = the IDs shown (None = the whole paper)."""
    shown = set(allowed) if allowed is not None else None

    def ok(sid: str) -> bool:
        if sid not in snapshot:
            return False
        if shown is None or sid in shown:
            return True
        return any(i.startswith(sid + ".c") for i in shown)  # a row ID when its cells were shown
    ids = [s for s in dict.fromkeys(claim.sentence_ids)]
    good = [s for s in ids if ok(s)]
    checks: dict = {}
    if len(good) < len(ids):
        checks["ids_dropped"] = len(ids) - len(good)
    if not good:
        return None, "no valid sentence IDs"
    text = quote(snapshot, good)
    c = claim.model_copy(deep=True)
    if c.effect is not None:
        nums = [v for v in (c.effect.value, c.effect.ci_low, c.effect.ci_high) if v is not None]
        missing = [v for v in nums if not number_in(v, text)]
        if missing or (c.effect.p and not p_in(c.effect.p, text)):
            checks["effect_cleared"] = True
            c.effect = None
    if c.n is not None and not (number_in(float(c.n), text) or (profile_n and number_in(float(c.n), profile_n))):
        checks["n_cleared"] = True
        c.n = None
    links = [l for l in c.links if l.hypothesis_key in hypothesis_keys]
    if len(links) < len(c.links):
        checks["links_dropped"] = len(c.links) - len(links)
    c.links = links
    if not links:
        checks["neutral"] = True
    return Checked(c, good, text, checks), None


# ---------- reconciliation ----------

def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+(?:\.\d+)?", text.casefold()) if t not in STOP}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def id_overlap(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / min(len(sa), len(sb)) if sa and sb else 0.0


def relation(a: Checked, b: Checked) -> str:
    """`match`, `borderline` or `none`."""
    ov, jc = id_overlap(a.sentence_ids, b.sentence_ids), jaccard(a.claim.claim, b.claim.claim)
    if ov >= MATCH_IDS and jc >= MATCH_TEXT:
        return "match"
    if ov >= BORDER_IDS and jc >= BORDER_TEXT and (ov >= MATCH_IDS or jc >= MATCH_TEXT):
        return "borderline"
    return "none"


@dataclass
class Cluster:
    kept: tuple[str, Checked]  # (extractor, claim)
    members: list[tuple[str, Checked]] = field(default_factory=list)

    @property
    def agreement(self) -> int:
        return 2 if self.members else 1  # members always come from another extractor


def _better(a: Checked, b: Checked) -> bool:
    return (len(a.sentence_ids), len(a.claim.claim)) < (len(b.sentence_ids), len(b.claim.claim))


def reconcile(claims: list[tuple[str, Checked]], same: dict[tuple[int, int], bool] | None = None
              ) -> tuple[list[Cluster], list[tuple[int, int]]]:
    """Cluster claims from different extractors. `claims` = [(extractor, claim)]; `same` holds the judge's
    answers for borderline pairs (by index). Returns (clusters, borderline pairs still unjudged)."""
    same = same or {}
    parent = list(range(len(claims)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    unjudged: list[tuple[int, int]] = []
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            if claims[i][0] == claims[j][0]:
                continue  # one extractor's claims never merge with each other
            rel = relation(claims[i][1], claims[j][1])
            if rel == "borderline":
                if (i, j) not in same:
                    unjudged.append((i, j))
                    continue
                rel = "match" if same[(i, j)] else "none"
            if rel == "match":
                ri, rj = find(i), find(j)
                extractors_i = {claims[k][0] for k in range(len(claims)) if find(k) == ri}
                extractors_j = {claims[k][0] for k in range(len(claims)) if find(k) == rj}
                if ri != rj and not extractors_i & extractors_j:  # a cluster holds one claim per extractor
                    parent[rj] = ri
    groups: dict[int, list[int]] = {}
    for i in range(len(claims)):
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for idx in groups.values():
        best = idx[0]
        for k in idx[1:]:
            if _better(claims[k][1], claims[best][1]):
                best = k
        clusters.append(Cluster(claims[best], [claims[k] for k in idx if k != best]))
    return clusters, unjudged
