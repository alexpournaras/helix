"""research.sqlite3: schema (PLAN B3), migrations and data access.

Everything here is synchronous; async callers use ``asyncio.to_thread``. One connection per
operation (or per explicit transaction) keeps the module safe to use from worker threads.
JSON is stored as TEXT, times as ISO UTC strings.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 3

# Columns that hold JSON. Rows read through `_row` are decoded automatically.
JSON_COLUMNS = {
    "complaints", "systems", "special_populations", "settings_snapshot", "data", "check_results", "entry_terms",
    "tree_numbers", "queries", "candidate_ids", "selected_ids", "attributes", "sentence_ids", "votes", "seg_ids",
    "options", "targets", "answer", "rules_evaluated", "rule_hits", "panel", "features_explained",
    "features_unexplained", "discriminators", "ratings", "rank_key", "sources", "authors", "pub_types", "query_ids",
    "hypothesis_keys", "score_breakdown", "skipped_sections", "segments", "packs", "profile", "appraisal_votes",
    "appraisal", "study_identity", "applicability", "quality_flags", "registry_ids", "pages", "effect", "checks",
    "confirmation", "claim_ids", "computed", "novelty", "draft_data", "issues", "support_checks", "resolutions",
    "final_check", "readiness", "payload", "limitations", "population",
}

_TABLES = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    problem_text        TEXT NOT NULL,
    problem_version     INTEGER NOT NULL DEFAULT 1,
    problem_lang        TEXT,
    problem_text_en     TEXT,
    complaints          TEXT NOT NULL DEFAULT '[]',
    systems             TEXT NOT NULL DEFAULT '[]',
    special_populations TEXT NOT NULL DEFAULT '[]',
    knowledge_version   INTEGER NOT NULL DEFAULT 0,
    synthetic           INTEGER NOT NULL DEFAULT 0,
    profile             TEXT NOT NULL DEFAULT 'quality',
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','waiting_user','paused','complete','cancelled','failed')),
    outcome             TEXT CHECK (outcome IS NULL OR outcome IN
                        ('findings','insufficient_evidence','incomplete','safety_stop')),
    waiting_for         TEXT CHECK (waiting_for IS NULL OR waiting_for IN
                        ('problem_rewrite','answers','safety_ack','plan_approval','selection_approval')),
    stage               TEXT NOT NULL DEFAULT 'intake',
    return_stage        TEXT,
    round               INTEGER NOT NULL DEFAULT 1,
    review_round        INTEGER NOT NULL DEFAULT 0,
    segment_no          INTEGER NOT NULL DEFAULT 0,
    segment_started_at  TEXT,
    settings_snapshot   TEXT NOT NULL DEFAULT '{}',
    safety_level        TEXT,
    stop_reason         TEXT,
    error               TEXT,
    active_seconds      REAL NOT NULL DEFAULT 0,
    active_since        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_segments (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    seg_id           TEXT NOT NULL,
    origin           TEXT NOT NULL,
    ref_id           TEXT,
    text             TEXT NOT NULL,
    text_en          TEXT,
    PRIMARY KEY (investigation_id, seg_id)
);

CREATE TABLE IF NOT EXISTS case_briefs (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    data             TEXT NOT NULL,
    source           TEXT,
    check_results    TEXT,
    prompt_version   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (investigation_id, version)
);

CREATE TABLE IF NOT EXISTS mesh_cache (
    term          TEXT PRIMARY KEY COLLATE NOCASE,
    descriptor_ui TEXT,
    name          TEXT,
    entry_terms   TEXT NOT NULL DEFAULT '[]',
    tree_numbers  TEXT NOT NULL DEFAULT '[]',
    scope_note    TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grounding_runs (
    id                INTEGER PRIMARY KEY,
    investigation_id  INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    complaint_key     TEXT NOT NULL,
    mesh_ui           TEXT,
    population        TEXT NOT NULL DEFAULT 'general',
    queries           TEXT NOT NULL DEFAULT '[]',
    candidate_ids     TEXT NOT NULL DEFAULT '[]',
    selected_ids      TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL CHECK (status IN ('ok','gap','failed')),
    gap_reason        TEXT,
    knowledge_version INTEGER,
    UNIQUE (investigation_id, complaint_key, population)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN
                     ('red_flag','differential','history_item','evaluation','management_topic','vocabulary')),
    key              TEXT NOT NULL,
    text             TEXT NOT NULL,
    attributes       TEXT NOT NULL DEFAULT '{}',
    population       TEXT,
    origin           TEXT NOT NULL CHECK (origin IN ('curated','guideline','proposed')),
    pack_ref         TEXT,
    trust            TEXT NOT NULL CHECK (trust IN ('curated','verified','partial','proposed')),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','rejected','superseded'))
);

CREATE TABLE IF NOT EXISTS knowledge_attestations (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES knowledge_items (id) ON DELETE CASCADE,
    read_id       INTEGER REFERENCES paper_reads (id) ON DELETE CASCADE,
    literature_id INTEGER,
    sentence_ids  TEXT NOT NULL DEFAULT '[]',
    quote         TEXT NOT NULL DEFAULT '',
    verdict       TEXT,
    votes         TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS knowledge_coverage (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    item_id          INTEGER NOT NULL REFERENCES knowledge_items (id) ON DELETE CASCADE,
    decision         TEXT NOT NULL CHECK (decision IN ('hypothesis','excluded')),
    hypothesis_id    INTEGER REFERENCES hypotheses (id) ON DELETE CASCADE,
    reason           TEXT,
    seg_ids          TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (investigation_id, item_id)
);

CREATE TABLE IF NOT EXISTS clarifying_questions (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    research_round   INTEGER NOT NULL DEFAULT 1,
    qround           INTEGER NOT NULL DEFAULT 1,
    origin           TEXT NOT NULL CHECK (origin IN ('intake','safety','grounding','gaps')),
    item_id          INTEGER REFERENCES knowledge_items (id) ON DELETE SET NULL,
    key              TEXT NOT NULL,
    text             TEXT NOT NULL,
    why              TEXT,
    answer_type      TEXT NOT NULL DEFAULT 'text',
    options          TEXT NOT NULL DEFAULT '[]',
    priority         INTEGER NOT NULL DEFAULT 2,
    expected_impact  REAL,
    targets          TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','skipped')),
    answer           TEXT,
    answered_at      TEXT,
    UNIQUE (investigation_id, key)
);

CREATE TABLE IF NOT EXISTS safety_reviews (
    id                INTEGER PRIMARY KEY,
    investigation_id  INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    brief_version     INTEGER NOT NULL,
    knowledge_version INTEGER NOT NULL,
    rules_evaluated   TEXT NOT NULL DEFAULT '[]',
    rule_level        TEXT,
    rule_hits         TEXT NOT NULL DEFAULT '[]',
    panel             TEXT NOT NULL DEFAULT '[]',
    level             TEXT NOT NULL,
    recommendation    TEXT,
    acknowledged_at   TEXT,
    UNIQUE (investigation_id, brief_version, knowledge_version)
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id                   INTEGER PRIMARY KEY,
    investigation_id     INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    key                  TEXT NOT NULL,
    title                TEXT NOT NULL,
    description          TEXT,
    mechanism            TEXT,
    kind                 TEXT,
    item_id              INTEGER REFERENCES knowledge_items (id) ON DELETE SET NULL,
    features_explained   TEXT NOT NULL DEFAULT '[]',
    features_unexplained TEXT NOT NULL DEFAULT '[]',
    discriminators       TEXT NOT NULL DEFAULT '[]',
    prior                TEXT,
    prior_reason         TEXT,
    status               TEXT NOT NULL DEFAULT 'active',
    retired_reason       TEXT,
    round_added          INTEGER NOT NULL DEFAULT 1,
    origin               TEXT,
    direction            TEXT,
    certainty            TEXT,
    case_fit             TEXT,
    ratings              TEXT,
    rank_key             TEXT,
    evidence_version     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (investigation_id, key)
);

CREATE TABLE IF NOT EXISTS plan_uncovered (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    feature_key      TEXT NOT NULL,
    reason           TEXT,
    round            INTEGER NOT NULL,
    PRIMARY KEY (investigation_id, feature_key, round)
);

CREATE TABLE IF NOT EXISTS research_queries (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    hypothesis_id    INTEGER REFERENCES hypotheses (id) ON DELETE CASCADE,
    round            INTEGER NOT NULL,
    purpose          TEXT NOT NULL DEFAULT 'research' CHECK (purpose IN ('research','grounding')),
    source           TEXT NOT NULL,
    query            TEXT NOT NULL,
    query_type       TEXT,
    rationale        TEXT,
    origin           TEXT,
    hit_count        INTEGER,
    attempts         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    error            TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    round_found      INTEGER NOT NULL,
    purpose          TEXT NOT NULL DEFAULT 'research' CHECK (purpose IN ('research','grounding')),
    sources          TEXT NOT NULL DEFAULT '[]',
    source           TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    pmid             TEXT,
    pmcid            TEXT,
    doi              TEXT,
    openalex_id      TEXT,
    s2_id            TEXT,
    title_norm       TEXT,
    title            TEXT NOT NULL DEFAULT '',
    abstract         TEXT NOT NULL DEFAULT '',
    authors          TEXT NOT NULL DEFAULT '[]',
    journal          TEXT,
    year             INTEGER,
    pub_types        TEXT NOT NULL DEFAULT '[]',
    language         TEXT,
    open_access      INTEGER,
    full_text_likely INTEGER,
    oa_pdf_url       TEXT,
    cited_by         INTEGER,
    retracted        INTEGER NOT NULL DEFAULT 0,
    concern_flag     INTEGER NOT NULL DEFAULT 0,
    first_rank       INTEGER,
    query_ids        TEXT NOT NULL DEFAULT '[]',
    found_via        TEXT NOT NULL DEFAULT 'search' CHECK (found_via IN ('search','snowball','seed','grounding')),
    literature_id    INTEGER,
    status           TEXT NOT NULL DEFAULT 'new',
    UNIQUE (investigation_id, purpose, source, source_id)
);
CREATE INDEX IF NOT EXISTS candidates_pmid ON candidates (investigation_id, pmid);
CREATE INDEX IF NOT EXISTS candidates_doi ON candidates (investigation_id, doi);

CREATE TABLE IF NOT EXISTS snowball_edges (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    from_candidate   INTEGER NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    to_candidate     INTEGER NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('reference','citation')),
    via              TEXT,
    PRIMARY KEY (from_candidate, to_candidate, kind)
);

CREATE TABLE IF NOT EXISTS screenings (
    candidate_id     INTEGER PRIMARY KEY REFERENCES candidates (id) ON DELETE CASCADE,
    prefilter_reason TEXT,
    votes            TEXT NOT NULL DEFAULT '[]',
    relevance        INTEGER,
    hypothesis_keys  TEXT NOT NULL DEFAULT '[]',
    design           TEXT,
    decision         TEXT,
    reason           TEXT,
    score            REAL,
    score_breakdown  TEXT,
    selected_round   INTEGER,
    selected_reason  TEXT
);

CREATE TABLE IF NOT EXISTS paper_reads (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    candidate_id     INTEGER REFERENCES candidates (id) ON DELETE CASCADE,
    literature_id    INTEGER,
    round            INTEGER NOT NULL,
    purpose          TEXT NOT NULL DEFAULT 'research' CHECK (purpose IN ('research','grounding','both')),
    scope            TEXT CHECK (scope IS NULL OR scope IN ('full','partial','abstract_only')),
    fulltext_source  TEXT,
    parser           TEXT,
    coverage         REAL,
    skipped_sections TEXT NOT NULL DEFAULT '[]',
    segments         TEXT,
    segments_hash    TEXT,
    packs            TEXT,
    profile          TEXT,
    appraisal_votes  TEXT,
    appraisal        TEXT,
    study_identity   TEXT,
    applicability    TEXT,
    quality_flags    TEXT NOT NULL DEFAULT '[]',
    reread           INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS studies (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('primary','review_sr','review_narrative','guideline')),
    label            TEXT,
    registry_ids     TEXT NOT NULL DEFAULT '[]',
    dataset          TEXT,
    round            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS study_members (
    study_id INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    read_id  INTEGER NOT NULL REFERENCES paper_reads (id) ON DELETE CASCADE,
    PRIMARY KEY (study_id, read_id)
);

CREATE TABLE IF NOT EXISTS review_inclusions (
    review_study_id   INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    included_study_id INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    matched_by        TEXT,
    PRIMARY KEY (review_study_id, included_study_id)
);

CREATE TABLE IF NOT EXISTS evidence_claims (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    read_id          INTEGER NOT NULL REFERENCES paper_reads (id) ON DELETE CASCADE,
    literature_id    INTEGER,
    pass             TEXT,
    extractor_model  TEXT,
    cluster_id       INTEGER,
    agreement        INTEGER CHECK (agreement IS NULL OR agreement IN (1, 2)),
    pack_no          INTEGER,
    claim            TEXT NOT NULL,
    sentence_ids     TEXT NOT NULL DEFAULT '[]',
    quote            TEXT NOT NULL DEFAULT '',
    pages            TEXT,
    section_title    TEXT,
    evidence_type    TEXT,
    population       TEXT,
    design           TEXT,
    n                INTEGER,
    effect           TEXT,
    limitations      TEXT,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','rejected','superseded')),
    verification     TEXT CHECK (verification IS NULL OR verification IN ('supported','partial')),
    checks           TEXT,
    escalated        INTEGER NOT NULL DEFAULT 0,
    adjudicated      INTEGER NOT NULL DEFAULT 0,
    consequential    INTEGER NOT NULL DEFAULT 0,
    confirmation     TEXT,
    round            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_votes (
    id         INTEGER PRIMARY KEY,
    claim_id   INTEGER NOT NULL REFERENCES evidence_claims (id) ON DELETE CASCADE,
    stage      TEXT NOT NULL CHECK (stage IN ('verify','escalate','adjudicate','confirm')),
    model      TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    checks     TEXT,
    reason     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_links (
    claim_id      INTEGER NOT NULL REFERENCES evidence_claims (id) ON DELETE CASCADE,
    hypothesis_id INTEGER NOT NULL REFERENCES hypotheses (id) ON DELETE CASCADE,
    origin        TEXT NOT NULL CHECK (origin IN ('extract','mapping')),
    direction     TEXT,
    strength      TEXT,
    case_match    TEXT,
    reason        TEXT,
    PRIMARY KEY (claim_id, hypothesis_id, origin)
);

CREATE TABLE IF NOT EXISTS rejected_items (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    step             TEXT NOT NULL,
    ref              TEXT,
    raw              TEXT,
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_fit (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    round            INTEGER NOT NULL,
    feature_key      TEXT NOT NULL,
    hypothesis_id    INTEGER NOT NULL REFERENCES hypotheses (id) ON DELETE CASCADE,
    fit              TEXT NOT NULL CHECK (fit IN ('explains','compatible','against','unknown')),
    claim_ids        TEXT NOT NULL DEFAULT '[]',
    reason           TEXT,
    copied           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (investigation_id, round, feature_key, hypothesis_id)
);

CREATE TABLE IF NOT EXISTS hypothesis_analyses (
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    hypothesis_id    INTEGER NOT NULL REFERENCES hypotheses (id) ON DELETE CASCADE,
    round            INTEGER NOT NULL,
    data             TEXT NOT NULL,
    copied           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (investigation_id, hypothesis_id, round)
);

CREATE TABLE IF NOT EXISTS gap_reports (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    round            INTEGER NOT NULL,
    computed         TEXT,
    panel            TEXT,
    novelty          TEXT,
    decision         TEXT,
    reason           TEXT,
    UNIQUE (investigation_id, round)
);

CREATE TABLE IF NOT EXISTS followups (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    question         TEXT NOT NULL,
    segment_no       INTEGER NOT NULL,
    round_started    INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id                 INTEGER PRIMARY KEY,
    investigation_id   INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    version            INTEGER NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('full','safety','record')),
    status             TEXT NOT NULL DEFAULT 'draft',
    draft_data         TEXT,
    data               TEXT,
    issues             TEXT NOT NULL DEFAULT '[]',
    support_checks     TEXT NOT NULL DEFAULT '[]',
    resolutions        TEXT NOT NULL DEFAULT '[]',
    final_check        TEXT,
    review_incomplete  INTEGER NOT NULL DEFAULT 0,
    readiness          TEXT,
    markdown           TEXT,
    followup_id        INTEGER REFERENCES followups (id) ON DELETE SET NULL,
    UNIQUE (investigation_id, version)
);

CREATE TABLE IF NOT EXISTS research_tasks (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    step             TEXT NOT NULL,
    key              TEXT NOT NULL,
    payload          TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','done','failed','skipped','cancelled')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 4,
    restarts         INTEGER NOT NULL DEFAULT 0,
    not_before       TEXT,
    error            TEXT,
    error_kind       TEXT,
    skip_reason      TEXT,
    priority         INTEGER NOT NULL DEFAULT 100,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    UNIQUE (investigation_id, key)
);
CREATE INDEX IF NOT EXISTS tasks_status ON research_tasks (status, priority, id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                  INTEGER PRIMARY KEY,
    investigation_id    INTEGER REFERENCES investigations (id) ON DELETE CASCADE,
    task_id             INTEGER,
    step                TEXT,
    role                TEXT,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT,
    cache_key           TEXT,
    cached              INTEGER NOT NULL DEFAULT 0,
    attempt_kind        TEXT NOT NULL DEFAULT 'first',
    input_tokens        INTEGER,
    cached_input_tokens INTEGER,
    output_tokens       INTEGER,
    latency_ms          INTEGER,
    cost                REAL,
    status              TEXT NOT NULL,
    error_kind          TEXT,
    error               TEXT,
    request_excerpt     TEXT,
    response            TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_calls_cache ON llm_calls (cache_key, status);
CREATE INDEX IF NOT EXISTS llm_calls_inv ON llm_calls (investigation_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    at               TEXT NOT NULL,
    level            TEXT NOT NULL DEFAULT 'info',
    stage            TEXT,
    kind             TEXT NOT NULL,
    message          TEXT NOT NULL DEFAULT '',
    data             TEXT
);

CREATE TABLE IF NOT EXISTS note_outbox (
    id               INTEGER PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES investigations (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,
    artifact_key     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    UNIQUE (investigation_id, kind, artifact_key)
);

-- Validated model responses, keyed by the B5.1 cache key. Global: a cached call is reusable
-- across retries and restarts of the same investigation.
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key  TEXT PRIMARY KEY,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Ordered migrations from version N-1 to N. Version 1 is the base schema above.
MIGRATIONS: dict[int, list[str]] = {
    # 2 (task 1R.2, PLAN B17.6): grounding reuse. A global cache of guideline knowledge per complaint × population
    # × versions: guideline items, attestations and the guideline snapshots; never user segments or case facts.
    2: [
        """CREATE TABLE IF NOT EXISTS knowledge_cache (
            id            INTEGER PRIMARY KEY,
            key           TEXT NOT NULL UNIQUE,
            complaint_key TEXT NOT NULL,
            mesh_ui       TEXT,
            population    TEXT NOT NULL,
            versions      TEXT NOT NULL,
            status        TEXT NOT NULL,          -- ok / gap
            gap_reason    TEXT,
            payload       TEXT NOT NULL,          -- {queries, sources, items}
            created_at    TEXT NOT NULL
        )""",
        "ALTER TABLE grounding_runs ADD COLUMN reused_from INTEGER",
    ],
    # 3 (task 1R.4, PLAN B17.5 item 5): single-verifier mode records `second` and `audit` votes. SQLite can't
    # change a CHECK constraint, so the table is rebuilt with the same columns.
    3: [
        """CREATE TABLE claim_votes_v3 (
            id         INTEGER PRIMARY KEY,
            claim_id   INTEGER NOT NULL REFERENCES evidence_claims (id) ON DELETE CASCADE,
            stage      TEXT NOT NULL CHECK (stage IN ('verify','escalate','second','audit','adjudicate','confirm')),
            model      TEXT NOT NULL,
            verdict    TEXT NOT NULL,
            checks     TEXT,
            reason     TEXT,
            created_at TEXT NOT NULL
        )""",
        "INSERT INTO claim_votes_v3 (id, claim_id, stage, model, verdict, checks, reason, created_at) "
        "SELECT id, claim_id, stage, model, verdict, checks, reason, created_at FROM claim_votes",
        "DROP TABLE claim_votes",
        "ALTER TABLE claim_votes_v3 RENAME TO claim_votes",
    ],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key, value in item.items():
        if key in JSON_COLUMNS and isinstance(value, str):
            try:
                item[key] = json.loads(value)
            except ValueError:
                pass
    return item


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        if isinstance(value, (dict, list, tuple)):
            out[key] = dumps(value)
        elif isinstance(value, bool):
            out[key] = int(value)
        else:
            out[key] = value
    return out


class ResearchDB:
    """Data access for research.sqlite3. Safe to share across threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._ready = False

    # ---------- connections ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA secure_delete = ON")
        return conn

    def init(self) -> None:
        with self._init_lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                self._migrate(conn)
            finally:
                conn.close()
            self._ready = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            current = 0
            if has_meta:
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                current = int(row["value"]) if row else 0
            if current == 0:
                for statement in _split_sql(_TABLES):
                    conn.execute(statement)
                current = 1
            for version in sorted(v for v in MIGRATIONS if v > current):
                for statement in MIGRATIONS[version]:
                    conn.execute(statement)
                current = version
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"research.sqlite3 schema {current} is newer than this code ({SCHEMA_VERSION}).")
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(current),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (BEGIN IMMEDIATE … COMMIT, rolled back on any exception)."""
        self.init()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self.init()
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---------- generic helpers ----------

    def query(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict]:
        with self.read() as conn:
            return [_decode(r) for r in conn.execute(sql, params)]

    def one(self, sql: str, params: Iterable[Any] | dict = ()) -> dict | None:
        with self.read() as conn:
            return _decode(conn.execute(sql, params).fetchone())

    def schema_version(self) -> int:
        row = self.one("SELECT value FROM meta WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    @staticmethod
    def insert(conn: sqlite3.Connection, table: str, values: dict[str, Any], *, or_ignore: bool = False) -> int | None:
        values = _encode(values)
        cols = ", ".join(values)
        marks = ", ".join(f":{k}" for k in values)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        cur = conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", values)
        return cur.lastrowid if cur.rowcount else None

    @staticmethod
    def update(conn: sqlite3.Connection, table: str, row_id: int, values: dict[str, Any]) -> None:
        values = _encode(values)
        sets = ", ".join(f"{k} = :{k}" for k in values)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = :_id", {**values, "_id": row_id})

    # ---------- investigations ----------

    def create_investigation(self, problem_text: str, *, profile: str, settings_snapshot: dict,
                             synthetic: bool = False, title: str = "") -> int:
        stamp = now()
        with self.tx() as conn:
            return self.insert(conn, "investigations", {
                "title": title, "problem_text": problem_text, "profile": profile,
                "settings_snapshot": settings_snapshot, "synthetic": synthetic, "status": "active",
                "stage": "intake", "segment_no": 0, "segment_started_at": stamp, "active_since": stamp,
                "created_at": stamp, "updated_at": stamp,
            })

    def investigation(self, investigation_id: int) -> dict | None:
        return self.one("SELECT * FROM investigations WHERE id = ?", (investigation_id,))

    def list_investigations(self) -> list[dict]:
        return self.query(
            "SELECT id, title, status, outcome, stage, waiting_for, profile, synthetic, systems, created_at, updated_at "
            "FROM investigations ORDER BY id DESC"
        )

    def update_investigation(self, investigation_id: int, conn: sqlite3.Connection | None = None, **values: Any) -> None:
        values["updated_at"] = now()
        if conn is not None:
            self.update(conn, "investigations", investigation_id, values)
            return
        with self.tx() as c:
            self.update(c, "investigations", investigation_id, values)

    def set_stage(self, investigation_id: int, expected: str, new: str, conn: sqlite3.Connection | None = None,
                  **extra: Any) -> bool:
        """Compare-and-set the stage (B4). Returns False if another writer moved it first."""
        values = _encode({**extra, "stage": new, "updated_at": now()})
        sets = ", ".join(f"{k} = :{k}" for k in values)
        sql = f"UPDATE investigations SET {sets} WHERE id = :_id AND stage = :_expected"
        params = {**values, "_id": investigation_id, "_expected": expected}
        if conn is not None:
            return conn.execute(sql, params).rowcount == 1
        with self.tx() as c:
            return c.execute(sql, params).rowcount == 1

    def delete_investigation(self, investigation_id: int) -> bool:
        with self.tx() as conn:
            return conn.execute("DELETE FROM investigations WHERE id = ?", (investigation_id,)).rowcount == 1

    # ---------- tasks ----------

    def queue_task(self, conn: sqlite3.Connection, investigation_id: int, step: str, key: str,
                   payload: dict | None = None, *, priority: int = 100, max_attempts: int = 4) -> bool:
        """INSERT OR IGNORE by (investigation, key). Returns True if a new task was queued."""
        cur = conn.execute(
            "INSERT OR IGNORE INTO research_tasks (investigation_id, step, key, payload, priority, max_attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (investigation_id, step, key, dumps(payload or {}), priority, max_attempts, now()),
        )
        return cur.rowcount == 1

    def claim_task(self, *, exclude_ids: Iterable[int] = (), investigation_ids: Iterable[int] | None = None) -> dict | None:
        """Atomically move the next runnable task to `running`. Only tasks of active investigations run."""
        exclude = list(exclude_ids)
        stamp = now()
        with self.tx() as conn:
            sql = (
                "SELECT t.* FROM research_tasks t JOIN investigations i ON i.id = t.investigation_id "
                "WHERE t.status = 'queued' AND i.status = 'active' AND (t.not_before IS NULL OR t.not_before <= ?)"
            )
            params: list[Any] = [stamp]
            if exclude:
                sql += f" AND t.id NOT IN ({','.join('?' * len(exclude))})"
                params += exclude
            if investigation_ids is not None:
                ids = list(investigation_ids)
                if not ids:
                    return None
                sql += f" AND t.investigation_id IN ({','.join('?' * len(ids))})"
                params += ids
            sql += " ORDER BY t.priority, t.id LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE research_tasks SET status = 'running', attempts = attempts + 1, started_at = ? "
                "WHERE id = ? AND status = 'queued'", (stamp, row["id"]),
            ).rowcount
            if not updated:
                return None
            return _decode(conn.execute("SELECT * FROM research_tasks WHERE id = ?", (row["id"],)).fetchone())

    def finish_task(self, conn: sqlite3.Connection, task_id: int, status: str = "done", **values: Any) -> None:
        self.update(conn, "research_tasks", task_id, {**values, "status": status, "finished_at": now()})

    def task(self, task_id: int) -> dict | None:
        return self.one("SELECT * FROM research_tasks WHERE id = ?", (task_id,))

    def tasks(self, investigation_id: int, *, step: str | None = None, statuses: Iterable[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM research_tasks WHERE investigation_id = ?"
        params: list[Any] = [investigation_id]
        if step:
            sql += " AND step = ?"
            params.append(step)
        if statuses:
            sts = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(sts))})"
            params += sts
        return self.query(sql + " ORDER BY id", params)

    def open_task_count(self, investigation_id: int, conn: sqlite3.Connection | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM research_tasks WHERE investigation_id = ? AND status IN ('queued','running')"
        if conn is not None:
            return conn.execute(sql, (investigation_id,)).fetchone()["n"]
        return self.one(sql, (investigation_id,))["n"]

    def requeue_running(self, *, count_restart: bool) -> list[dict]:
        """Startup recovery / graceful shutdown (B11). Returns the tasks that were re-queued."""
        with self.tx() as conn:
            rows = [_decode(r) for r in conn.execute("SELECT * FROM research_tasks WHERE status = 'running'")]
            for row in rows:
                if count_restart:
                    conn.execute(
                        "UPDATE research_tasks SET status = 'queued', restarts = restarts + 1, "
                        "attempts = MAX(attempts - 1, 0), started_at = NULL WHERE id = ?", (row["id"],))
                else:
                    conn.execute(
                        "UPDATE research_tasks SET status = 'queued', attempts = MAX(attempts - 1, 0), started_at = NULL "
                        "WHERE id = ?", (row["id"],))
            return rows

    # ---------- events, rejections ----------

    def event(self, investigation_id: int, kind: str, message: str = "", *, level: str = "info",
              stage: str | None = None, data: Any = None, conn: sqlite3.Connection | None = None) -> None:
        values = {"investigation_id": investigation_id, "at": now(), "level": level, "stage": stage,
                  "kind": kind, "message": message, "data": data}
        if conn is not None:
            self.insert(conn, "events", values)
            return
        with self.tx() as c:
            self.insert(c, "events", values)

    def reject(self, conn: sqlite3.Connection, investigation_id: int, step: str, ref: str | None, raw: Any,
               reason: str) -> None:
        self.insert(conn, "rejected_items", {
            "investigation_id": investigation_id, "step": step, "ref": ref,
            "raw": raw if isinstance(raw, str) or raw is None else dumps(raw), "reason": reason, "created_at": now(),
        })

    # ---------- model calls, cache ----------

    # ---------- grounding reuse (PLAN B17.6) ----------

    def knowledge_cache_get(self, key: str, max_age_days: float, gap_max_age_days: float) -> dict | None:
        """The cached grounding for `key` if it is young enough: `ok` within max_age_days, `gap` within
        gap_max_age_days."""
        row = self.one("SELECT * FROM knowledge_cache WHERE key = ?", (key,))
        if row is None:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
        limit = gap_max_age_days if row["status"] == "gap" else max_age_days
        if age.total_seconds() > float(limit) * 86400:
            return None
        out = dict(row)
        for col in ("payload", "versions"):  # `payload` is decoded by `one` (JSON_COLUMNS); `versions` isn't
            if isinstance(out[col], str):
                out[col] = json.loads(out[col])
        return out

    def knowledge_cache_put(self, key: str, *, complaint_key: str, mesh_ui: str | None, population: str,
                            versions: dict, status: str, gap_reason: str | None, payload: dict) -> int:
        with self.tx() as conn:
            conn.execute("DELETE FROM knowledge_cache WHERE key = ?", (key,))
            cur = conn.execute(
                "INSERT INTO knowledge_cache (key, complaint_key, mesh_ui, population, versions, status, gap_reason, "
                "payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, complaint_key, mesh_ui, population, json.dumps(versions, sort_keys=True), status, gap_reason,
                 json.dumps(payload, ensure_ascii=False), now()))
            return int(cur.lastrowid)

    def knowledge_cache_clear(self) -> int:
        with self.tx() as conn:
            return conn.execute("DELETE FROM knowledge_cache").rowcount

    def knowledge_cache_stats(self) -> dict:
        row = self.one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM knowledge_cache")
        return {"entries": row["n"], "oldest": row["oldest"]}

    def log_call(self, values: dict[str, Any]) -> int:
        with self.tx() as conn:
            return self.insert(conn, "llm_calls", {**values, "created_at": now()})

    def cache_get(self, cache_key: str) -> str | None:
        row = self.one("SELECT response FROM llm_cache WHERE cache_key = ?", (cache_key,))
        return row["response"] if row else None

    def cache_put(self, cache_key: str, response: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO llm_cache (cache_key, response, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (cache_key) DO UPDATE SET response = excluded.response", (cache_key, response, now()))

    # ---------- usage (per run segment) ----------

    def usage(self, investigation_id: int, segment_started_at: str | None = None) -> dict[str, Any]:
        """Token, cost and call counts from llm_calls, plus candidate counts and active time (B3)."""
        params: list[Any] = [investigation_id]
        since = ""
        if segment_started_at:
            since = " AND created_at >= ?"
            params.append(segment_started_at)
        calls = self.one(
            "SELECT COUNT(*) AS calls, SUM(cached) AS cached_calls, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(cost),0) AS cost FROM llm_calls WHERE investigation_id = ?" + since, params)
        by_provider = self.query(
            "SELECT provider, model, COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cost),0) AS cost FROM llm_calls "
            "WHERE investigation_id = ?" + since + " GROUP BY provider, model ORDER BY provider, model", params)
        cand = self.one(
            "SELECT COUNT(*) AS candidates, "
            "SUM(CASE WHEN s.decision IS NOT NULL THEN 1 ELSE 0 END) AS screened "
            "FROM candidates c LEFT JOIN screenings s ON s.candidate_id = c.id WHERE c.investigation_id = ?",
            (investigation_id,))
        reads = self.one("SELECT COUNT(*) AS n FROM paper_reads WHERE investigation_id = ? AND status = 'done'",
                         (investigation_id,))
        inv = self.investigation(investigation_id) or {}
        active = float(inv.get("active_seconds") or 0)
        since_t = parse_time(inv.get("active_since"))
        if since_t is not None and inv.get("status") == "active":
            active += (datetime.now(timezone.utc) - since_t).total_seconds()
        return {
            "calls": calls["calls"], "cached_calls": calls["cached_calls"] or 0,
            "input_tokens": calls["input_tokens"], "cached_input_tokens": calls["cached_input_tokens"],
            "output_tokens": calls["output_tokens"], "tokens": calls["input_tokens"] + calls["output_tokens"],
            "cost": round(calls["cost"], 6), "by_model": by_provider,
            "candidates": cand["candidates"], "screened": cand["screened"] or 0, "papers_read": reads["n"],
            "active_seconds": round(active, 1),
        }


def _split_sql(script: str) -> list[str]:
    """Split the schema script into statements (it contains no semicolons inside literals)."""
    statements = []
    for chunk in script.split(";"):
        lines = [line for line in chunk.splitlines() if not line.strip().startswith("--")]
        text = "\n".join(lines).strip()
        if text:
            statements.append(text)
    return statements
