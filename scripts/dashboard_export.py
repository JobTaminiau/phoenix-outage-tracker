"""Export tracker results as JSON files for the Phoenix Outage Watch dashboard.

usage: python scripts/dashboard_export.py <data dir with outages.sqlite> <out dir>

Writes {rows, meta} JSON (meta carries this script's own code so readers can check it):
  tracker_events.json  one row per outage event, times in Arizona time (UTC-7)
  tracker_daily.json   one row per day and utility: polls, failed polls, outages started,
                       peak customers out at one time, customer-hours out
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

AZ = timezone(timedelta(hours=-7))
POLL_HOURS = 5 / 60


def az(ts):
    return datetime.fromisoformat(ts).astimezone(AZ).strftime("%Y-%m-%d %H:%M") if ts else None


def main(data_dir: Path, out_dir: Path) -> None:
    con = sqlite3.connect(data_dir / "outages.sqlite")
    con.row_factory = sqlite3.Row
    out_dir.mkdir(parents=True, exist_ok=True)
    code = Path(__file__).read_text(encoding="utf-8")
    meta = [{"label": "dashboard_export.py", "value": code, "type": "code", "lang": "python"},
            {"label": "Input", "value": "phoenix-outage-data log, rebuilt into outages.sqlite"},
            {"label": "Exported", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}]

    # Customer-hours per event: customers out at each poll x 5 minutes.
    ch = {r["event_id"]: r["ch"] for r in con.execute(
        "SELECT event_id, SUM(customers) * ? AS ch FROM observations "
        "WHERE customers IS NOT NULL GROUP BY event_id", (POLL_HOURS,))}
    events = []
    for e in con.execute("SELECT * FROM events ORDER BY first_seen"):
        start = e["start_reported"] or e["first_seen"]
        end = None
        if e["end_lo"] and e["end_hi"]:
            lo, hi = datetime.fromisoformat(e["end_lo"]), datetime.fromisoformat(e["end_hi"])
            end = (lo + (hi - lo) / 2).isoformat()
        hours = None
        if end:
            hours = round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600, 2)
        events.append({
            "event_id": e["event_id"], "utility": e["utility"], "kind": e["kind"],
            "status": "open" if e["is_open"] else "restored",
            "start": az(start), "end": az(end), "hours": hours,
            "peak_customers": e["peak_customers"], "customer_hours": round(ch.get(e["event_id"], 0), 1),
            "city": e["city"], "area": e["area"], "cause": e["cause_last"],
            "lat": e["lat"], "lon": e["lon"], "in_metro": bool(e["in_metro"]) if e["in_metro"] is not None else None,
        })
    (out_dir / "tracker_events.json").write_text(json.dumps({"rows": events, "meta": meta}))

    # Daily health and load, per utility, in Arizona days.
    daily = {}
    for p in con.execute("SELECT poll_id, utility, polled_at, ok FROM polls"):
        d = az(p["polled_at"])[:10]
        r = daily.setdefault((d, p["utility"]), {"date": d, "utility": p["utility"], "polls": 0,
                                                 "failed": 0, "started": 0, "peak_out": 0,
                                                 "customer_hours": 0.0})
        r["polls"] += 1
        r["failed"] += 0 if p["ok"] else 1
    for o in con.execute("SELECT p.utility, p.polled_at, SUM(o.customers) AS out FROM observations o "
                         "JOIN polls p USING(poll_id) WHERE o.action != 'closed' "
                         "GROUP BY o.poll_id"):
        r = daily[(az(o["polled_at"])[:10], o["utility"])]
        r["peak_out"] = max(r["peak_out"], o["out"] or 0)
        r["customer_hours"] += (o["out"] or 0) * POLL_HOURS
    for e in events:
        key = ((e["start"] or "")[:10], e["utility"])
        if key in daily:
            daily[key]["started"] += 1
    rows = sorted(daily.values(), key=lambda r: (r["date"], r["utility"]))
    for r in rows:
        r["customer_hours"] = round(r["customer_hours"], 1)
    (out_dir / "tracker_daily.json").write_text(json.dumps({"rows": rows, "meta": meta}))
    print(f"{len(events)} events, {len(rows)} day-utility rows -> {out_dir}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
