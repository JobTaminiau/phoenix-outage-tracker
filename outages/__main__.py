"""Command line.

  python -m outages poll                      one poll of both utilities
  python -m outages loop --minutes 30         poll every 5 min for a while (testing)
  python -m outages events --out events.csv   export events
  python -m outages summary                   quick counts

Unattended (GitHub Actions) mode:
  python -m outages log-poll                  poll both utilities, append to data/log only
  python -m outages rebuild                   replay data/log into data/outages.sqlite + events.csv
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import polllog, store
from .sources import FETCHERS, make_session
from .tracker import reconcile


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def poll_once(data_dir: Path, utilities=("APS", "SRP")) -> list[str]:
    con = store.connect(data_dir / "outages.sqlite")
    session = make_session()
    lines = []
    for u in utilities:
        polled_at = now_iso()
        res = FETCHERS[u](session)
        raw_path = store.save_raw(data_dir / "raw", res, polled_at)
        open_, closed = store.load_events(con, u, polled_at)
        decisions = reconcile(open_, closed, res.observations, polled_at, res.ok)
        store.record(con, res, polled_at, raw_path, decisions)
        counts: dict[str, int] = {}
        for d in decisions:
            counts[d.action] = counts.get(d.action, 0) + 1
        status = "ok" if res.ok else f"FAILED {res.error}"
        lines.append(f"{polled_at} {u}: {status}; {len(res.observations)} outages; {counts}")
        time.sleep(2)  # space requests between hosts a little
    con.close()
    return lines


def log_poll(data_dir: Path, utilities=("APS", "SRP")) -> tuple[list[str], bool]:
    session = make_session()
    lines, all_ok = [], True
    for u in utilities:
        polled_at = now_iso()
        res = FETCHERS[u](session)
        polllog.append(data_dir / "log", res, polled_at)
        all_ok &= res.ok
        status = "ok" if res.ok else f"FAILED {res.error}"
        lines.append(f"{polled_at} {u}: {status}; {len(res.observations)} outages")
        time.sleep(2)
    return lines, all_ok


def export_events(data_dir: Path, out: Path) -> int:
    con = store.connect(data_dir / "outages.sqlite")
    rows = con.execute("SELECT * FROM events ORDER BY first_seen, event_id").fetchall()
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(store.EVENT_COLS)
        w.writerows([tuple(r) for r in rows])
    con.close()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="outages")
    ap.add_argument("--data", type=Path, default=Path("data"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    lp = sub.add_parser("loop")
    lp.add_argument("--minutes", type=float, default=30)
    lp.add_argument("--interval", type=int, default=300)
    ev = sub.add_parser("events")
    ev.add_argument("--out", type=Path, default=Path("events.csv"))
    sub.add_parser("summary")
    sub.add_parser("log-poll")
    rb = sub.add_parser("rebuild")
    rb.add_argument("--out", type=Path, default=None, help="events CSV (default data/events.csv)")
    a = ap.parse_args(argv)

    if a.cmd == "poll":
        print("\n".join(poll_once(a.data)))
    elif a.cmd == "loop":
        if a.interval < 300:
            sys.exit("interval below 5 minutes is not allowed (be polite to the utilities)")
        end = time.time() + a.minutes * 60
        while True:
            print("\n".join(poll_once(a.data)), flush=True)
            if time.time() + a.interval > end:
                break
            time.sleep(a.interval + random.uniform(-10, 10))
    elif a.cmd == "events":
        print(f"wrote {export_events(a.data, a.out)} events to {a.out}")
    elif a.cmd == "log-poll":
        lines, _ = log_poll(a.data)
        print("\n".join(lines))
        # Exit 0 even if a feed failed: the failure is logged as data, and the commit must happen.
    elif a.cmd == "rebuild":
        counts = polllog.rebuild(a.data / "log", a.data / "outages.sqlite")
        out = a.out or a.data / "events.csv"
        print(f"replayed: {counts}; wrote {export_events(a.data, out)} events to {out}")
    elif a.cmd == "summary":
        con = store.connect(a.data / "outages.sqlite")
        for r in con.execute("SELECT utility, COUNT(*), SUM(ok), MIN(polled_at), MAX(polled_at) "
                             "FROM polls GROUP BY utility"):
            print("polls", tuple(r))
        for r in con.execute("SELECT utility, is_open, COUNT(*), SUM(peak_customers) "
                             "FROM events GROUP BY utility, is_open"):
            print("events", tuple(r))
        for r in con.execute("SELECT action, COUNT(*) FROM observations GROUP BY action"):
            print("actions", tuple(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
