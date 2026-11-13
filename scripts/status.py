"""Write a short Markdown status page from the rebuilt event database."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

con = sqlite3.connect(sys.argv[1])
q = lambda s: con.execute(s).fetchall()  # noqa: E731
print(f"# Outage tracker status\n\nUpdated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC\n")
log_mb = sum(f.stat().st_size for f in Path(sys.argv[1]).parent.joinpath("log").rglob("*.jsonl")) / 1e6
print(f"Log size on disk: {log_mb:.1f} MB (consider a new yearly data repo past ~1,000 MB)\n")
print("| Utility | Polls | Failed | First poll | Last poll |\n| --- | --- | --- | --- | --- |")
for u, n, ok, a, b in q("SELECT utility, COUNT(*), SUM(ok), MIN(polled_at), MAX(polled_at) "
                        "FROM polls GROUP BY utility"):
    print(f"| {u} | {n} | {n - ok} | {a} | {b} |")
print("\n| Utility | Kind | Events | In metro box | Open now |\n| --- | --- | --- | --- | --- |")
for r in q("SELECT utility, kind, COUNT(*), SUM(in_metro), SUM(is_open) FROM events "
           "GROUP BY utility, kind"):
    print("| " + " | ".join(str(x) for x in r) + " |")
