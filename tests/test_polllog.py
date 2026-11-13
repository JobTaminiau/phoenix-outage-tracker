"""The append-only log must rebuild to the same events the live tracker would produce."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from outages import polllog
from outages.sources import FetchResult, parse_srp

from tests.test_tracker import srp

T = ["2026-11-13T23:55:00+00:00", "2026-11-14T00:00:00+00:00", "2026-11-14T00:05:00+00:00",
     "2026-11-14T00:10:00+00:00"]


def srp_res(records, ok=True):
    return FetchResult("SRP", ok, parse_srp(records) if ok else [],
                       {"getall": records} if ok else {}, error=None if ok else "timeout")


APS_RAW = {"layer0": {"fields": [{"name": "x"}], "spatialReference": {"wkid": 4326},
                      "features": [{"attributes": {"Ticket": "T9", "off": 1794517200000,
                                                   "customers": 10, "outagetype": "Unplanned Outage"},
                                    "geometry": {"x": -112.07, "y": 33.45}}]},
           "layer5": {"features": []}, "layer8": {"features": []},
           "props": {"features": [{"attributes": {"TIMESTAMP": 1794530407000}}]}}


class LogTests(unittest.TestCase):
    def test_append_and_rebuild_across_midnight(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log"
            polllog.append(log, srp_res([srp()]), T[0])
            polllog.append(log, srp_res([], ok=False), T[1])      # failed: must not close
            polllog.append(log, srp_res([srp()]), T[2])
            polllog.append(log, srp_res([]), T[3])                # restored
            aps = FetchResult("APS", True, [], APS_RAW)
            polllog.append(log, aps, T[1])
            self.assertEqual(sorted(p.name for p in log.rglob("*.jsonl")),
                             ["APS.jsonl", "SRP.jsonl", "SRP.jsonl"])
            line = json.loads((log / "2026/11/14/APS.jsonl").read_text().splitlines()[0])
            self.assertNotIn("fields", line["raw"]["layer0"])     # static schema stripped
            counts = polllog.rebuild(log, Path(d) / "o.sqlite")
            self.assertEqual(counts, {"new": 2, "continuing": 1, "closed": 1})
            con = sqlite3.connect(Path(d) / "o.sqlite")
            rows = dict(con.execute("SELECT utility, end_hi FROM events").fetchall())
            self.assertEqual(rows["SRP"], T[3])
            self.assertIsNone(rows["APS"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM polls WHERE ok=0").fetchone()[0], 1)

    def test_truncated_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log"
            p = polllog.append(log, srp_res([srp()]), T[0])
            with p.open("a") as f:
                f.write('{"polled_at": "2026-11-13T23:5')
            self.assertEqual(polllog.rebuild(log, Path(d) / "o.sqlite"), {"new": 1})


if __name__ == "__main__":
    unittest.main()
