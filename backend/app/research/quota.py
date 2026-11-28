"""Daily quota ledger (PLAN B17.1, B17.5 item 1, task 1R.0).

OpenAI's complimentary quota (2.5M tokens per UTC day for the small models, data-sharing traffic only) is not a
provider-side limit: usage beyond it is billed, and a request that crosses it is billed in full. Helix enforces its
own no-overage ceiling (`quota.daily_ceiling_tokens`, 2.4M) here, before every provider call:

  reserve   estimated input (× 1.1) + the call's max output, in one BEGIN IMMEDIATE transaction; refused when
            used + held reservations + external adjustment + request would pass the ceiling → `QuotaExceeded`
            (retry after the next 00:00 UTC reset);
  settle    the reservation is replaced by the provider's reported usage;
  release   a call that failed without usage (an HTTP error with a status) frees its reservation;
  keep      a call that may have been billed (timeout, dropped stream) is booked at its full reservation.

The ledger is one SQLite file shared by every process (the API, eval runs, spikes, tools): `HELIX_QUOTA_DB`, else
`backend/data/quota.sqlite3` — deliberately not the data folder, because eval runs use temporary data folders.
Only calls that really reach a provider are counted: fake providers and the replay, blocked and mock transports
never touch the totals. Reservations still held after `STALE_SECONDS` (a crashed process) are booked in full.

    npm run research:quota                 # today's used / reserved / remaining and the next reset
    npm run research:quota -- --add 50000  # tokens used outside Helix on the same project today
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .providers.base import ModelError

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "quota.sqlite3"
STALE_SECONDS = 3600          # > the providers' 1,800 s deadline
INPUT_MARGIN = 1.1            # the character estimate (tokens.py) can be low
CASE_START_NEED = 2_000_000   # expected need of a full investigation with a new complaint (B17.1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_days (
    provider     TEXT NOT NULL,
    day_utc      TEXT NOT NULL,
    used_input   INTEGER NOT NULL DEFAULT 0,
    used_output  INTEGER NOT NULL DEFAULT 0,
    external     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider, day_utc)
);
CREATE TABLE IF NOT EXISTS quota_reservations (
    id               INTEGER PRIMARY KEY,
    provider         TEXT NOT NULL,
    day_utc          TEXT NOT NULL,
    tokens           INTEGER NOT NULL,
    status           TEXT NOT NULL,            -- held / settled / released / kept
    used_input       INTEGER,
    used_output      INTEGER,
    investigation_id INTEGER,
    task_id          INTEGER,
    role             TEXT,
    model            TEXT,
    created_at       TEXT NOT NULL,
    settled_at       TEXT
);
CREATE INDEX IF NOT EXISTS quota_res_held ON quota_reservations (provider, day_utc, status);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def day_of(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d")


def next_reset(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc) + timedelta(days=1)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class QuotaExceeded(ModelError):
    """The call would pass the day's ceiling. `retry_after` = seconds to the next 00:00 UTC reset."""

    def __init__(self, message: str, *, retry_after: float, reset_at: str):
        super().__init__("quota", message, retry_after=retry_after)
        self.reset_at = reset_at


@dataclass
class Status:
    provider: str
    day_utc: str
    used: int
    reserved: int
    external: int
    ceiling: int | None
    next_reset: str

    @property
    def remaining(self) -> int | None:
        if self.ceiling is None:
            return None
        return max(0, self.ceiling - self.used - self.reserved - self.external)

    def case_can_start(self, need: int = CASE_START_NEED) -> bool:
        return self.remaining is None or self.remaining >= need


class Ledger:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] = utcnow):
        self.path = Path(path)
        self.clock = clock
        self._init_lock = threading.Lock()
        self._ready = False

    # ---------- storage ----------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            with self._init_lock:
                if not self._ready:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(self.path, timeout=30)
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.executescript(SCHEMA)
                    finally:
                        conn.close()
                    self._ready = True
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")  # one writer at a time across threads and processes
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _day_row(self, conn: sqlite3.Connection, provider: str, day: str) -> sqlite3.Row:
        conn.execute("INSERT OR IGNORE INTO quota_days (provider, day_utc, updated_at) VALUES (?, ?, ?)",
                     (provider, day, _iso(self.clock())))
        return conn.execute("SELECT * FROM quota_days WHERE provider = ? AND day_utc = ?", (provider, day)).fetchone()

    def _expire_stale(self, conn: sqlite3.Connection) -> None:
        """Reservations held past STALE_SECONDS belong to a crashed process: book them in full (cautious)."""
        cutoff = _iso(self.clock() - timedelta(seconds=STALE_SECONDS))
        for r in conn.execute("SELECT * FROM quota_reservations WHERE status = 'held' AND created_at < ?",
                              (cutoff,)).fetchall():
            self._book(conn, r, r["tokens"], 0, "kept")

    def _book(self, conn: sqlite3.Connection, r: sqlite3.Row, used_in: int, used_out: int, status: str) -> None:
        self._day_row(conn, r["provider"], r["day_utc"])
        conn.execute("UPDATE quota_days SET used_input = used_input + ?, used_output = used_output + ?, updated_at = ? "
                     "WHERE provider = ? AND day_utc = ?",
                     (used_in, used_out, _iso(self.clock()), r["provider"], r["day_utc"]))
        conn.execute("UPDATE quota_reservations SET status = ?, used_input = ?, used_output = ?, settled_at = ? "
                     "WHERE id = ?", (status, used_in, used_out, _iso(self.clock()), r["id"]))

    def _held(self, conn: sqlite3.Connection, provider: str, day: str) -> int:
        row = conn.execute("SELECT COALESCE(SUM(tokens), 0) FROM quota_reservations "
                           "WHERE provider = ? AND day_utc = ? AND status = 'held'", (provider, day)).fetchone()
        return int(row[0])

    # ---------- operations ----------

    def reserve(self, provider: str, tokens: int, ceiling: int | None, **meta: Any) -> int:
        now = self.clock()
        day = day_of(now)
        with self._tx() as conn:
            self._expire_stale(conn)
            row = self._day_row(conn, provider, day)
            used = row["used_input"] + row["used_output"] + row["external"]
            held = self._held(conn, provider, day)
            if ceiling is not None and used + held + tokens > ceiling:
                reset = next_reset(now)
                raise QuotaExceeded(
                    f"Daily token ceiling for {provider} reached ({used + held:,} used or reserved of {ceiling:,}; "
                    f"this call needs up to {tokens:,}). Resumes after {_iso(reset)}.",
                    retry_after=(reset - now).total_seconds() + 5, reset_at=_iso(reset))
            cur = conn.execute(
                "INSERT INTO quota_reservations (provider, day_utc, tokens, status, investigation_id, task_id, role, "
                "model, created_at) VALUES (?, ?, ?, 'held', ?, ?, ?, ?, ?)",
                (provider, day, int(tokens), meta.get("investigation_id"), meta.get("task_id"), meta.get("role"),
                 meta.get("model"), _iso(now)))
            return int(cur.lastrowid)

    def _finish(self, reservation_id: int, used_in: int, used_out: int, status: str) -> None:
        with self._tx() as conn:
            r = conn.execute("SELECT * FROM quota_reservations WHERE id = ?", (reservation_id,)).fetchone()
            if r is None or r["status"] != "held":
                return  # already settled (e.g. expired as stale)
            if status == "released":
                conn.execute("UPDATE quota_reservations SET status = 'released', settled_at = ? WHERE id = ?",
                             (_iso(self.clock()), reservation_id))
            else:
                self._book(conn, r, used_in if status == "settled" else r["tokens"],
                           used_out if status == "settled" else 0, status)

    def settle(self, reservation_id: int, input_tokens: int, output_tokens: int) -> None:
        self._finish(reservation_id, int(input_tokens or 0), int(output_tokens or 0), "settled")

    def release(self, reservation_id: int) -> None:
        self._finish(reservation_id, 0, 0, "released")

    def keep(self, reservation_id: int) -> None:
        self._finish(reservation_id, 0, 0, "kept")

    def add_external(self, provider: str, tokens: int) -> None:
        with self._tx() as conn:
            day = day_of(self.clock())
            self._day_row(conn, provider, day)
            conn.execute("UPDATE quota_days SET external = external + ?, updated_at = ? WHERE provider = ? AND day_utc = ?",
                         (int(tokens), _iso(self.clock()), provider, day))

    def status(self, provider: str, ceiling: int | None) -> Status:
        now = self.clock()
        day = day_of(now)
        with self._tx() as conn:
            self._expire_stale(conn)
            row = self._day_row(conn, provider, day)
            held = self._held(conn, provider, day)
        return Status(provider=provider, day_utc=day, used=row["used_input"] + row["used_output"], reserved=held,
                      external=row["external"], ceiling=ceiling, next_reset=_iso(next_reset(now)))


# ---------- process-wide default ----------

_default: Ledger | None = None
_default_lock = threading.Lock()


def ledger_path() -> Path:
    env = os.environ.get("HELIX_QUOTA_DB", "").strip()
    return Path(env) if env else DEFAULT_PATH


def default_ledger() -> Ledger:
    """The shared ledger for this process (re-resolved when HELIX_QUOTA_DB changes, e.g. in tests)."""
    global _default
    path = ledger_path()
    with _default_lock:
        if _default is None or _default.path != path:
            _default = Ledger(path)
        return _default


def ceiling_for(settings: Any, provider: str) -> int | None:
    """The day's ceiling for `provider`, or None when the provider isn't under a quota (B17.1)."""
    cfg = getattr(settings, "quota", None) or {}
    if provider not in (cfg.get("providers") or []):
        return None
    value = cfg.get("daily_ceiling_tokens")
    return int(value) if value else None


def counted(provider_obj: Any) -> bool:
    """Only real adapters on the real network count: not fake providers, nor replay / blocked / mock transports."""
    if not getattr(provider_obj, "quota_counted", False):
        return False
    from . import transport
    current = transport.current_transport()
    return current is None or isinstance(current, transport.RecordTransport)


def _local_reset(iso: str) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return t.strftime("%Y-%m-%d %H:%M %Z").strip()


def describe(st: Status) -> str:
    if st.ceiling is None:
        return f"{st.provider}: no ceiling configured; used {st.used:,} today (UTC {st.day_utc})"
    return (f"{st.provider} UTC {st.day_utc}: used {st.used:,} + reserved {st.reserved:,} + outside Helix "
            f"{st.external:,} of {st.ceiling:,}; remaining {st.remaining:,}; next reset {_local_reset(st.next_reset)}; "
            f"a full case can start: {'yes' if st.case_can_start() else 'no'}")


def main(argv: list[str] | None = None) -> int:
    from .settings import SettingsStore
    from app.config import settings as app_settings
    parser = argparse.ArgumentParser(description="Helix daily token quota (PLAN B17.1)")
    parser.add_argument("--add", type=int, help="record tokens used outside Helix on the same project today")
    parser.add_argument("--provider", default="openai")
    args = parser.parse_args(argv)
    research = SettingsStore(Path(app_settings.data_dir) / "research.json").load()
    ledger = default_ledger()
    if args.add:
        ledger.add_external(args.provider, args.add)
    print(f"ledger {ledger.path}")
    print(describe(ledger.status(args.provider, ceiling_for(research, args.provider))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
