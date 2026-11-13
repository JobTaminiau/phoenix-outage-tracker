"""Append-only poll log, and rebuilding the event database by replaying it.

The log is the source of truth: data/log/YYYY/MM/DD/<UTILITY>.jsonl, one line per poll,
holding the raw (slimmed) feed response and whether the poll succeeded. It only ever grows,
so it is safe to commit from an unattended scheduler. The SQLite event database is derived
from it and can be rebuilt at any time, for example after changing the matching rules.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from . import store
from .sources import FetchResult, parse_raw, slim_raw
from .tracker import reconcile


def log_path(log_dir: Path, utility: str, polled_at: str) -> Path:
    y, m, d = polled_at[:10].split("-")
    return log_dir / y / m / d / f"{utility}.jsonl"


def append(log_dir: Path, res: FetchResult, polled_at: str) -> Path:
    p = log_path(log_dir, res.utility, polled_at)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = {"polled_at": polled_at, "utility": res.utility, "ok": res.ok, "error": res.error,
            "n": len(res.observations) if res.ok else None, "raw": slim_raw(res.raw)}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
    return p


def read_all(log_dir: Path) -> Iterator[dict]:
    rows = []
    for p in sorted(log_dir.rglob("*.jsonl")):
        with p.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:  # a truncated last line must not stop a rebuild
                    continue
                rec["_src"] = f"{p.relative_to(log_dir)}:{i + 1}"
                rows.append(rec)
    rows.sort(key=lambda r: (r["polled_at"], r["utility"]))
    yield from rows


def rebuild(log_dir: Path, db_path: Path) -> dict[str, int]:
    """Replay every logged poll, in time order, into a fresh event database."""
    if db_path.exists():
        db_path.unlink()
    con = store.connect(db_path)
    counts: dict[str, int] = {}
    for rec in read_all(log_dir):
        u, t = rec["utility"], rec["polled_at"]
        ok = bool(rec["ok"])
        feed_ts = None
        obs = []
        if ok:
            try:
                obs, feed_ts = parse_raw(u, rec["raw"])
            except Exception as e:  # noqa: BLE001 - an unparseable poll counts as failed
                ok, rec["error"] = False, f"replay parse error: {e!r}"
        res = FetchResult(u, ok, obs, {}, feed_updated_utc=feed_ts, error=rec.get("error"))
        open_, closed = store.load_events(con, u, t)
        decisions = reconcile(open_, closed, obs, t, ok)
        store.record(con, res, t, rec["_src"], decisions)
        for d in decisions:
            counts[d.action] = counts.get(d.action, 0) + 1
    con.close()
    return counts
