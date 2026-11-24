"""MeSH lookup through E-utilities, cached, and MeSH tree number → Helix body system (PLAN B0, B8.1, C36).

Lookup rules (spike X8):
1. A descriptor UI (`D014012`) is looked up directly.
2. Otherwise an exact heading search, `"{term}"[MeSH Terms]` (MeSH maps entry terms here too).
3. Otherwise a free search, accepted only if the term is one of the descriptor's entry terms. Without this rule
   "palpitations" resolved to *Panic Disorder*.
4. Otherwise the term is unverified and the caller drops it.
Results, misses included, are cached in `mesh_cache` (global). Misses are retried after NEGATIVE_TTL_DAYS.
Only vocabulary terms are ever sent, never case text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import httpx
import yaml

from app.literature.sources.ncbi import eutil
from app.research import transport
from app.research.db import ResearchDB, now

MAPPING_PATH = Path(__file__).resolve().parent / "knowledge" / "mesh_systems.yaml"
NEGATIVE_TTL_DAYS = 30
MAX_UIDS = 5
_UI = re.compile(r"^D\d{6,9}$", re.IGNORECASE)


@dataclass
class Descriptor:
    ui: str
    name: str
    entry_terms: list[str] = field(default_factory=list)
    tree_numbers: list[str] = field(default_factory=list)
    scope_note: str = ""

    @property
    def systems(self) -> list[str]:
        return systems_for(self.tree_numbers)


def normalise_term(term: str) -> str:
    return " ".join((term or "").split()).strip()


# ---------- mapping ----------

@lru_cache(maxsize=4)
def load_mapping(path: Path = MAPPING_PATH) -> dict:
    data = yaml.safe_load(Path(path).read_text("utf-8")) or {}
    validate_mapping(data)
    return data


def validate_mapping(data: dict) -> None:
    """Every prefix is a tree-number prefix and maps to a declared system or null."""
    systems = set((data.get("systems") or {}).keys())
    if not systems:
        raise ValueError("mesh_systems.yaml: no systems")
    for prefix, system in (data.get("prefixes") or {}).items():
        if not re.fullmatch(r"[A-Z]\d{2}(\.\d{3})*", str(prefix)):
            raise ValueError(f"mesh_systems.yaml: bad prefix {prefix!r}")
        if system is not None and system not in systems:
            raise ValueError(f"mesh_systems.yaml: {prefix} maps to unknown system {system!r}")


def systems_for(tree_numbers: Iterable[str], mapping: dict | None = None) -> list[str]:
    """Union over tree numbers of the system of the longest matching prefix (null → none), in first-seen order."""
    table = mapping or load_mapping()
    prefixes = sorted((table.get("prefixes") or {}).items(), key=lambda kv: -len(kv[0]))
    out: list[str] = []
    for tree in tree_numbers:
        for prefix, system in prefixes:
            if tree == prefix or tree.startswith(prefix + "."):
                if system and system not in out:
                    out.append(system)
                break
    return out


def union_systems(descriptors: Iterable[Descriptor], proposed: Iterable[str] = ()) -> list[str]:
    """B8.1: mapped systems of the verified descriptors ∪ the systems the model proposed (known ones only)."""
    known = set(load_mapping()["systems"])
    out: list[str] = []
    for system in [s for d in descriptors for s in d.systems] + [p for p in proposed if p in known]:
        if system not in out:
            out.append(system)
    return out


# ---------- E-utilities ----------

def _descriptor(record: dict) -> Descriptor | None:
    ui = str(record.get("ds_meshui") or "")
    if not ui.startswith("D"):
        return None  # supplementary concepts (C…) and qualifiers (Q…) aren't descriptors
    terms = [t for t in record.get("ds_meshterms") or [] if t]
    trees = [link["treenum"] for link in record.get("ds_idxlinks") or [] if link.get("treenum")]
    return Descriptor(ui=ui, name=terms[0] if terms else "", entry_terms=terms, tree_numbers=trees,
                      scope_note=(record.get("ds_scopenote") or "").strip())


async def _search(client: httpx.AsyncClient, query: str) -> list[Descriptor]:
    found = await eutil(client, "esearch.fcgi", {"db": "mesh", "term": query, "retmode": "json", "retmax": MAX_UIDS})
    uids = (found.json().get("esearchresult") or {}).get("idlist") or []
    if not uids:
        return []
    summary = await eutil(client, "esummary.fcgi", {"db": "mesh", "id": ",".join(uids), "retmode": "json"})
    result = summary.json().get("result") or {}
    records = [result[u] for u in result.get("uids") or [] if isinstance(result.get(u), dict)]
    return [d for d in (_descriptor(r) for r in records) if d]


def _named(descriptors: list[Descriptor], term: str) -> list[Descriptor]:
    low = term.casefold()
    return [d for d in descriptors if any(t.casefold() == low for t in d.entry_terms)]


async def _resolve(client: httpx.AsyncClient, term: str) -> Descriptor | None:
    if _UI.match(term):
        hits = [d for d in await _search(client, f"{term.upper()}[MeSH Unique ID]") if d.ui == term.upper()]
        return hits[0] if hits else None
    heading = await _search(client, f'"{term}"[MeSH Terms]')
    if heading:
        return (_named(heading, term) or heading)[0]
    named = _named(await _search(client, term), term)
    return named[0] if named else None


# ---------- cache ----------

def _cached(db: ResearchDB, term: str) -> tuple[bool, Descriptor | None]:
    row = db.one("SELECT * FROM mesh_cache WHERE term = ?", (term,))
    if not row:
        return False, None
    if not row["descriptor_ui"]:
        fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - fetched > timedelta(days=NEGATIVE_TTL_DAYS):
            return False, None
        return True, None
    return True, Descriptor(ui=row["descriptor_ui"], name=row["name"] or "", entry_terms=_list(row["entry_terms"]),
                            tree_numbers=_list(row["tree_numbers"]), scope_note=row["scope_note"] or "")


def _list(value) -> list[str]:
    """ResearchDB decodes JSON columns already; raw rows hold the JSON text."""
    return list(value) if isinstance(value, list) else json.loads(value or "[]")


def _store(db: ResearchDB, term: str, d: Descriptor | None) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO mesh_cache (term, descriptor_ui, name, entry_terms, tree_numbers, scope_note, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (term) DO UPDATE SET descriptor_ui = excluded.descriptor_ui, "
            "name = excluded.name, entry_terms = excluded.entry_terms, tree_numbers = excluded.tree_numbers, "
            "scope_note = excluded.scope_note, fetched_at = excluded.fetched_at",
            (term, d.ui if d else None, d.name if d else None, json.dumps(d.entry_terms if d else [], ensure_ascii=False),
             json.dumps(d.tree_numbers if d else []), d.scope_note if d else None, now()))


# ---------- public ----------

async def lookup(term: str, db: ResearchDB, *, client: httpx.AsyncClient | None = None,
                 refresh: bool = False) -> Descriptor | None:
    """The verified descriptor for a term or UI, or None (unverified: the caller drops it)."""
    term = normalise_term(term)
    if not term:
        return None
    if not refresh:
        hit, descriptor = _cached(db, term)
        if hit:
            return descriptor
    if client is None:
        async with transport.client(timeout=60) as own:
            descriptor = await _resolve(own, term)
    else:
        descriptor = await _resolve(client, term)
    _store(db, term, descriptor)
    return descriptor


async def verify(terms: Iterable[str], db: ResearchDB, *,
                 client: httpx.AsyncClient | None = None) -> tuple[list[Descriptor], list[str]]:
    """(verified descriptors without duplicates, unverified terms) for a complaint's MeSH candidates."""
    verified: dict[str, Descriptor] = {}
    dropped: list[str] = []
    for term in dict.fromkeys(normalise_term(t) for t in terms):
        if not term:
            continue
        descriptor = await lookup(term, db, client=client)
        if descriptor is None:
            dropped.append(term)
        else:
            verified.setdefault(descriptor.ui, descriptor)
    return list(verified.values()), dropped
