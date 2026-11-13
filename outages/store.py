"""SQLite storage: every poll, every observation, and the derived outage events."""
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta
from dataclasses import asdict
from pathlib import Path

from .sources import FetchResult, dumps_raw
from .tracker import Decision, Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
  poll_id INTEGER PRIMARY KEY,
  utility TEXT NOT NULL,
  polled_at TEXT NOT NULL,            -- UTC ISO time we asked
  ok INTEGER NOT NULL,                -- 0 = failed: tells "no data" apart from "no outages"
  n_outages INTEGER,
  feed_updated_at TEXT,               -- APS feed timestamp, if given
  raw_path TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  utility TEXT NOT NULL,
  source_id TEXT,
  id_aliases TEXT,                    -- JSON list of earlier ids if the utility re-ticketed it
  kind TEXT,
  start_reported TEXT,
  first_seen TEXT, last_seen TEXT,
  end_lo TEXT, end_hi TEXT,           -- restoration happened between these two polls
  is_open INTEGER,
  lat REAL, lon REAL, area TEXT,
  first_customers INTEGER, peak_customers INTEGER, last_customers INTEGER,
  cause_first TEXT, cause_last TEXT, etr_first TEXT, etr_last TEXT,
  n_polls INTEGER, n_reopen INTEGER,
  in_metro INTEGER, city TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  poll_id INTEGER NOT NULL REFERENCES polls(poll_id),
  event_id TEXT NOT NULL REFERENCES events(event_id),
  action TEXT NOT NULL,               -- new | continuing | reopened | id_changed | closed
  changed TEXT,                       -- which fields moved since the previous poll
  customers INTEGER, etr TEXT, cause TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_open ON events(utility, is_open);
CREATE INDEX IF NOT EXISTS ix_obs_event ON observations(event_id);
"""

EVENT_COLS = [
    "event_id", "utility", "source_id", "id_aliases", "kind", "start_reported", "first_seen",
    "last_seen", "end_lo", "end_hi", "is_open", "lat", "lon", "area", "first_customers",
    "peak_customers", "last_customers", "cause_first", "cause_last", "etr_first", "etr_last",
    "n_polls", "n_reopen", "in_metro", "city"]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    for col, typ in (("in_metro", "INTEGER"), ("city", "TEXT")):  # upgrade older test DBs
        if col not in have:
            con.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
    return con


def _row_to_event(r: sqlite3.Row) -> Event:
    d = dict(r)
    d["id_aliases"] = json.loads(d["id_aliases"] or "[]")
    d["is_open"] = bool(d["is_open"])
    d["in_metro"] = None if d["in_metro"] is None else bool(d["in_metro"])
    return Event(**d)


def load_events(con: sqlite3.Connection, utility: str, polled_at: str
                ) -> tuple[list[Event], list[Event]]:
    """Open events, plus events closed recently enough to be reopened."""
    cutoff = (datetime.fromisoformat(polled_at) - timedelta(hours=2)).isoformat(timespec="seconds")
    open_ = [_row_to_event(r) for r in con.execute(
        "SELECT * FROM events WHERE utility=? AND is_open=1", (utility,))]
    closed = [_row_to_event(r) for r in con.execute(
        "SELECT * FROM events WHERE utility=? AND is_open=0 "
        "AND end_hi >= ? ORDER BY end_hi DESC", (utility, cutoff))]
    return open_, closed


def save_raw(raw_dir: Path, res: FetchResult, polled_at: str) -> str:
    day = polled_at[:10].replace("-", "/")
    stamp = polled_at[11:19].replace(":", "")
    p = raw_dir / res.utility / day / f"{stamp}{'' if res.ok else '_FAILED'}.json.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"polled_at": polled_at, "ok": res.ok, "error": res.error, "data": res.raw}
    p.write_bytes(gzip.compress(dumps_raw(body)))
    return str(p.relative_to(raw_dir))


def record(con: sqlite3.Connection, res: FetchResult, polled_at: str, raw_path: str,
           decisions: list[Decision]) -> int:
    with con:
        cur = con.execute(
            "INSERT INTO polls(utility, polled_at, ok, n_outages, feed_updated_at, raw_path, error)"
            " VALUES (?,?,?,?,?,?,?)",
            (res.utility, polled_at, int(res.ok), len(res.observations) if res.ok else None,
             res.feed_updated_utc, raw_path, res.error))
        poll_id = cur.lastrowid
        for d in decisions:
            e = asdict(d.event)
            e["id_aliases"] = json.dumps(e["id_aliases"])
            e["is_open"] = int(e["is_open"])
            e["in_metro"] = None if e["in_metro"] is None else int(e["in_metro"])
            con.execute(
                f"INSERT OR REPLACE INTO events({','.join(EVENT_COLS)}) "
                f"VALUES ({','.join('?' * len(EVENT_COLS))})", [e[c] for c in EVENT_COLS])
            o = d.obs
            con.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                (poll_id, d.event.event_id, d.action, ",".join(d.changed) or None,
                 o.customers if o else None, o.etr_utc if o else None,
                 o.cause if o else None, o.status if o else None))
    return poll_id
