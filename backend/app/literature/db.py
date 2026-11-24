"""SQLite storage for literature, tags and fetch runs (backend/data/literature.db).

Each integration keeps its own rows: an article is unique per (source, source_id), so the
same paper fetched from PubMed and Europe PMC is stored twice, linked by pmid/pmcid/doi.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..config import settings
from . import text
from .models import Article, Details, FetchRun, Literature, LiteratureFull, LiteraturePage, Tag

TAG_ORIGINS = ("user", "ai")

SCHEMA = """
CREATE TABLE IF NOT EXISTS literature (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    url               TEXT NOT NULL DEFAULT '',
    pmid              TEXT,
    pmcid             TEXT,
    doi               TEXT,
    title             TEXT NOT NULL DEFAULT '',
    abstract          TEXT NOT NULL DEFAULT '',
    authors           TEXT NOT NULL DEFAULT '[]',
    journal           TEXT NOT NULL DEFAULT '',
    year              INTEGER,
    pub_date          TEXT NOT NULL DEFAULT '',
    volume            TEXT NOT NULL DEFAULT '',
    issue             TEXT NOT NULL DEFAULT '',
    pages             TEXT NOT NULL DEFAULT '',
    keywords          TEXT NOT NULL DEFAULT '[]',
    mesh              TEXT NOT NULL DEFAULT '[]',
    publication_types TEXT NOT NULL DEFAULT '[]',
    language          TEXT NOT NULL DEFAULT '',
    open_access       INTEGER,
    retracted         INTEGER NOT NULL DEFAULT 0,
    concern           INTEGER NOT NULL DEFAULT 0,
    cited_by_count    INTEGER,
    has_full_text     INTEGER NOT NULL DEFAULT 0,
    reference_count   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS literature_pmid ON literature (pmid);
CREATE INDEX IF NOT EXISTS literature_pmcid ON literature (pmcid);
CREATE INDEX IF NOT EXISTS literature_doi ON literature (doi);

-- Full text, references, citations and the rest: large, so only loaded for one article at a time.
CREATE TABLE IF NOT EXISTS literature_details (
    literature_id INTEGER PRIMARY KEY REFERENCES literature (id) ON DELETE CASCADE,
    data          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS literature_tags (
    literature_id INTEGER NOT NULL REFERENCES literature (id) ON DELETE CASCADE,
    name          TEXT NOT NULL COLLATE NOCASE,
    origin        TEXT NOT NULL DEFAULT 'user',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (literature_id, name)
);
CREATE INDEX IF NOT EXISTS literature_tags_name ON literature_tags (name);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    term        TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    sort        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,
    total       INTEGER,
    queued      INTEGER NOT NULL DEFAULT 0,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    current     TEXT,
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_hits (
    run_id        INTEGER NOT NULL REFERENCES fetch_runs (id) ON DELETE CASCADE,
    literature_id INTEGER NOT NULL REFERENCES literature (id) ON DELETE CASCADE,
    rank          INTEGER NOT NULL,
    PRIMARY KEY (run_id, literature_id)
);
CREATE INDEX IF NOT EXISTS fetch_hits_literature ON fetch_hits (literature_id);
"""

LIST_FIELDS = ("authors", "keywords", "mesh", "publication_types")
ARTICLE_FIELDS = [f for f in Article.model_fields]
RUN_FIELDS = [f for f in FetchRun.model_fields if f != "id"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LiteratureDB:
    def __init__(self, path: Path):
        self.path = path
        self._write = threading.Lock()
        self._ready = False

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if not self._ready:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                self._migrate(conn)
                self._ready = True
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(fetch_runs)")}
        if "tags" not in run_cols:
            conn.execute("ALTER TABLE fetch_runs ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        lit_cols = {r["name"] for r in conn.execute("PRAGMA table_info(literature)")}
        for col, ddl in (
            ("cited_by_count", "INTEGER"),
            ("has_full_text", "INTEGER NOT NULL DEFAULT 0"),
            ("reference_count", "INTEGER NOT NULL DEFAULT 0"),
            ("retracted", "INTEGER NOT NULL DEFAULT 0"),
            ("concern", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in lit_cols:
                conn.execute(f"ALTER TABLE literature ADD COLUMN {col} {ddl}")

    # ---------- literature ----------

    def save_article(
        self, article: Article, run_id: int | None = None, rank: int = 0, tags: list[str] = (), details: Details | None = None,
        tag_origin: str = "user",
    ) -> tuple[int, bool]:
        """Insert or refresh one article, adding `tags` to it with `tag_origin` ("user" or "ai"). Existing tags
        and created_at survive a refresh. Returns (id, created)."""
        if tag_origin not in TAG_ORIGINS:
            raise ValueError(f"tag_origin must be one of {TAG_ORIGINS}")
        values = article.model_dump()
        for f in ("retracted", "concern"):
            values[f] = int(values[f])
        for f in LIST_FIELDS:
            values[f] = json.dumps(values[f], ensure_ascii=False)
        if values["open_access"] is not None:
            values["open_access"] = int(values["open_access"])
        stamp = now()
        cols = ", ".join(ARTICLE_FIELDS)
        marks = ", ".join(f":{f}" for f in ARTICLE_FIELDS)
        updates = ", ".join(f"{f} = excluded.{f}" for f in ARTICLE_FIELDS if f not in ("source", "source_id"))
        with self._write, self._conn() as conn:
            existed = conn.execute(
                "SELECT 1 FROM literature WHERE source = ? AND source_id = ?", (article.source, article.source_id)
            ).fetchone()
            row = conn.execute(
                f"INSERT INTO literature ({cols}, created_at, updated_at) VALUES ({marks}, :stamp, :stamp) "
                f"ON CONFLICT (source, source_id) DO UPDATE SET {updates}, updated_at = :stamp RETURNING id",
                {**values, "stamp": stamp},
            ).fetchone()
            if run_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO fetch_hits (run_id, literature_id, rank) VALUES (?, ?, ?)", (run_id, row["id"], rank)
                )
            conn.executemany(
                "INSERT OR IGNORE INTO literature_tags (literature_id, name, origin, created_at) VALUES (?, ?, ?, ?)",
                [(row["id"], name, tag_origin, stamp) for name in tags],
            )
            if details is not None:
                conn.execute(
                    "INSERT INTO literature_details (literature_id, data) VALUES (?, ?) "
                    "ON CONFLICT (literature_id) DO UPDATE SET data = excluded.data",
                    (row["id"], details.model_dump_json()),
                )
                conn.execute(
                    "UPDATE literature SET cited_by_count = ?, has_full_text = ?, reference_count = ? WHERE id = ?",
                    (details.cited_by_count, int(bool(details.full_text)), len(details.references), row["id"]),
                )
        return row["id"], existed is None

    def _hydrate(self, conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Literature]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        tags: dict[int, list[Tag]] = {i: [] for i in ids}
        for t in conn.execute(
            f"SELECT literature_id, name, origin, created_at FROM literature_tags WHERE literature_id IN ({marks}) ORDER BY created_at, name",
            ids,
        ):
            tags[t["literature_id"]].append(Tag(name=t["name"], origin=t["origin"], created_at=t["created_at"]))
        terms: dict[int, list[str]] = {i: [] for i in ids}
        for t in conn.execute(
            f"SELECT DISTINCT h.literature_id, r.term FROM fetch_hits h JOIN fetch_runs r ON r.id = h.run_id "
            f"WHERE h.literature_id IN ({marks}) ORDER BY r.id",
            ids,
        ):
            if t["term"] not in terms[t["literature_id"]]:
                terms[t["literature_id"]].append(t["term"])
        out = []
        for r in rows:
            data = dict(r)
            for f in LIST_FIELDS:
                data[f] = json.loads(data[f] or "[]")
            if data["open_access"] is not None:
                data["open_access"] = bool(data["open_access"])
            out.append(Literature(**data, tags=tags[r["id"]], terms=terms[r["id"]]))
        return out

    def find_existing(self, pmid: str | None = None, pmcid: str | None = None, doi: str | None = None) -> Literature | None:
        """The saved copy of a paper, matched by PMID, then PMCID, then DOI (PLAN B8.13 "library first").
        Among copies from several sources, the one with full text wins, then the oldest."""
        keys = [("pmid", text.pmid(pmid)), ("pmcid", text.pmcid(pmcid)), ("doi", text.doi(doi))]
        with self._conn() as conn:
            for column, value in keys:
                if not value:
                    continue
                row = conn.execute(
                    f"SELECT * FROM literature WHERE {column} = ? ORDER BY has_full_text DESC, id LIMIT 1", (value,)
                ).fetchone()
                if row:
                    return self._hydrate(conn, [row])[0]
        return None

    def get(self, literature_id: int) -> Literature | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM literature WHERE id = ?", (literature_id,)).fetchone()
            return self._hydrate(conn, [row])[0] if row else None

    def get_full(self, literature_id: int) -> LiteratureFull | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM literature WHERE id = ?", (literature_id,)).fetchone()
            if not row:
                return None
            item = self._hydrate(conn, [row])[0]
            d = conn.execute("SELECT data FROM literature_details WHERE literature_id = ?", (literature_id,)).fetchone()
            details = Details.model_validate_json(d["data"]) if d else Details()
            details = self._mark_saved(conn, details)
            return LiteratureFull(**item.model_dump(), details=details)

    @staticmethod
    def _mark_saved(conn: sqlite3.Connection, details: Details) -> Details:
        """Flag references and citing works that are already in the library, from any source."""
        refs = [*details.references, *details.citations]
        pmids = list({r.pmid for r in refs if r.pmid})
        pmcids = list({r.pmcid for r in refs if r.pmcid})
        have_pmid, have_pmcid = set(), set()
        for i in range(0, len(pmids), 500):
            chunk = pmids[i:i + 500]
            have_pmid |= {r[0] for r in conn.execute(f"SELECT pmid FROM literature WHERE pmid IN ({','.join('?' * len(chunk))})", chunk)}
        for i in range(0, len(pmcids), 500):
            chunk = pmcids[i:i + 500]
            have_pmcid |= {r[0] for r in conn.execute(f"SELECT pmcid FROM literature WHERE pmcid IN ({','.join('?' * len(chunk))})", chunk)}

        def mark(rs):
            return [r.model_copy(update={"saved": (r.pmid in have_pmid) or (r.pmcid in have_pmcid)}) for r in rs]

        return details.model_copy(update={"references": mark(details.references), "citations": mark(details.citations)})

    def search(
        self,
        *,
        q: str = "",
        sources: list[str] = (),
        tags: list[str] = (),
        term: str | None = None,
        run_id: int | None = None,
        sort: str = "added",
        limit: int = 25,
        offset: int = 0,
    ) -> LiteraturePage:
        where, params = [], []
        if q.strip():
            like = f"%{q.strip()}%"
            where.append("(l.title LIKE ? OR l.abstract LIKE ? OR l.authors LIKE ? OR l.journal LIKE ? OR l.pmid = ? OR l.pmcid = ? OR l.doi = ?)")
            params += [like, like, like, like, q.strip(), q.strip().upper(), q.strip().lower()]
        # Several sources or tags match articles with any of them.
        if sources:
            where.append(f"l.source IN ({','.join('?' * len(sources))})")
            params += list(sources)
        if tags:
            where.append(
                f"EXISTS (SELECT 1 FROM literature_tags t WHERE t.literature_id = l.id AND t.name IN ({','.join('?' * len(tags))}))"
            )
            params += list(tags)
        if term:
            where.append("EXISTS (SELECT 1 FROM fetch_hits h JOIN fetch_runs r ON r.id = h.run_id WHERE h.literature_id = l.id AND r.term = ? COLLATE NOCASE)")
            params.append(term)
        if run_id is not None:
            where.append("EXISTS (SELECT 1 FROM fetch_hits h WHERE h.literature_id = l.id AND h.run_id = ?)")
            params.append(run_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = {
            "added": "l.id DESC",
            "newest": "l.pub_date DESC, l.id DESC",
            "oldest": "CASE WHEN l.pub_date = '' THEN 1 ELSE 0 END, l.pub_date ASC, l.id",
            "title": "l.title COLLATE NOCASE, l.id",
        }.get(sort, "l.id DESC")
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM literature l {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT l.* FROM literature l {clause} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()
            return LiteraturePage(total=total, items=self._hydrate(conn, rows))

    def delete(self, literature_id: int) -> bool:
        with self._write, self._conn() as conn:
            return conn.execute("DELETE FROM literature WHERE id = ?", (literature_id,)).rowcount > 0

    def saved_ids(self, source: str, source_ids: list[str]) -> set[str]:
        if not source_ids:
            return set()
        with self._conn() as conn:
            marks = ",".join("?" * len(source_ids))
            rows = conn.execute(
                f"SELECT source_id FROM literature WHERE source = ? AND source_id IN ({marks})", [source, *source_ids]
            )
            return {r[0] for r in rows}

    def delete_all(self) -> int:
        """Delete every article; tags and fetch hits go with them. Fetch run history is kept."""
        with self._write, self._conn() as conn:
            return conn.execute("DELETE FROM literature").rowcount

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM literature").fetchone()[0]

    def counts_by_source(self) -> dict[str, int]:
        with self._conn() as conn:
            return {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) AS n FROM literature GROUP BY source")}

    # ---------- tags ----------

    def add_tag(self, literature_id: int, name: str, origin: str = "user") -> bool:
        with self._write, self._conn() as conn:
            if not conn.execute("SELECT 1 FROM literature WHERE id = ?", (literature_id,)).fetchone():
                return False
            conn.execute(
                "INSERT OR IGNORE INTO literature_tags (literature_id, name, origin, created_at) VALUES (?, ?, ?, ?)",
                (literature_id, name, origin, now()),
            )
            return True

    def remove_tag(self, literature_id: int, name: str) -> bool:
        with self._write, self._conn() as conn:
            return conn.execute(
                "DELETE FROM literature_tags WHERE literature_id = ? AND name = ?", (literature_id, name)
            ).rowcount > 0

    def all_tags(self) -> list[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT name, COUNT(*) AS count, SUM(origin = 'ai') AS ai FROM literature_tags GROUP BY name COLLATE NOCASE ORDER BY count DESC, name"
                )
            ]

    def all_terms(self) -> list[str]:
        with self._conn() as conn:
            return [r[0] for r in conn.execute("SELECT term FROM fetch_runs GROUP BY term COLLATE NOCASE ORDER BY MAX(id) DESC")]

    # ---------- fetch runs ----------

    def create_run(self, source: str, term: str, max_results: int, sort: str, tags: list[str] = ()) -> FetchRun:
        with self._write, self._conn() as conn:
            row = conn.execute(
                "INSERT INTO fetch_runs (source, term, max_results, sort, tags, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?) RETURNING *",
                (source, term, max_results, sort, json.dumps(list(tags), ensure_ascii=False), now()),
            ).fetchone()
            return _run(row)

    def update_run(self, run_id: int, **fields) -> None:
        unknown = set(fields) - set(RUN_FIELDS)
        if unknown:
            raise ValueError(f"unknown run fields: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._write, self._conn() as conn:
            conn.execute(f"UPDATE fetch_runs SET {sets} WHERE id = ?", [*fields.values(), run_id])

    def get_run(self, run_id: int) -> FetchRun | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM fetch_runs WHERE id = ?", (run_id,)).fetchone()
            return _run(row) if row else None

    def list_runs(self, source: str | None = None, limit: int = 20) -> list[FetchRun]:
        with self._conn() as conn:
            if source:
                rows = conn.execute("SELECT * FROM fetch_runs WHERE source = ? ORDER BY id DESC LIMIT ?", (source, limit))
            else:
                rows = conn.execute("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (limit,))
            return [_run(r) for r in rows]

    def interrupt_stale_runs(self) -> None:
        """Runs left 'running' by a previous process will never finish."""
        with self._write, self._conn() as conn:
            conn.execute(
                "UPDATE fetch_runs SET status = 'interrupted', current = NULL, finished_at = ? WHERE status = 'running'", (now(),)
            )


def _run(row: sqlite3.Row) -> FetchRun:
    return FetchRun(**{**dict(row), "tags": json.loads(row["tags"] or "[]")})


db = LiteratureDB(settings.data_dir / "literature.db")
