"""Scenario tests for new-vs-continuing matching, run through the real SQLite store."""
import tempfile
import unittest
from pathlib import Path

from outages import store
from outages.sources import FetchResult, parse_aps_layer, parse_srp
from outages.tracker import reconcile

SRP_REC = {  # shape copied from the live SRP feed on 2026-11-13
    "latitude": 33.368225, "longitude": -111.961178, "numberCustomersAffected": 919,
    "outageProblem": "We are investigating the cause of the outage.", "isMaintenanceOutage": False,
    "estimatedUsersImpacted": 0, "crossRoadText": "S 48TH ST TO S KYRENE RD",
    "estimatedRestorationTime": "2026-11-13T02:29:54Z", "outageBegan": "2026-11-13T00:32:20Z",
    "outageId": "261121769"}


def srp(**kw):
    return {**SRP_REC, **kw}


def aps_payload(*feats):  # synthetic: APS had no live outages when the schema was read
    return {"features": [{"attributes": a, "geometry": {"x": a.pop("_x"), "y": a.pop("_y")}}
                         for a in feats]}


class Harness:
    def __init__(self, tmp):
        self.con = store.connect(Path(tmp) / "t.sqlite")

    def poll(self, utility, observations, polled_at, ok=True):
        res = FetchResult(utility, ok, observations, {})
        open_, closed = store.load_events(self.con, utility, polled_at)
        ds = reconcile(open_, closed, observations, polled_at, ok)
        store.record(self.con, res, polled_at, "", ds)
        return sorted(d.action for d in ds)

    def events(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM events ORDER BY first_seen")]


T = ["2026-11-13T00:35:00+00:00", "2026-11-13T00:40:00+00:00", "2026-11-13T00:45:00+00:00",
     "2026-11-13T00:50:00+00:00", "2026-11-13T00:55:00+00:00", "2026-11-13T01:40:00+00:00"]


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(self.tmp.name)

    def tearDown(self):
        self.h.con.close()
        self.tmp.cleanup()

    def test_new_then_continuing_then_closed(self):
        self.assertEqual(self.h.poll("SRP", parse_srp([srp()]), T[0]), ["new"])
        self.assertEqual(self.h.poll("SRP", parse_srp([srp(numberCustomersAffected=400)]), T[1]),
                         ["continuing"])
        self.assertEqual(self.h.poll("SRP", parse_srp([]), T[2]), ["closed"])
        (e,) = self.h.events()
        self.assertEqual((e["peak_customers"], e["last_customers"], e["n_polls"]), (919, 400, 2))
        self.assertEqual((e["end_lo"], e["end_hi"], e["is_open"]), (T[1], T[2], 0))

    def test_failed_poll_does_not_close(self):
        self.h.poll("SRP", parse_srp([srp()]), T[0])
        self.assertEqual(self.h.poll("SRP", [], T[1], ok=False), [])
        self.assertEqual(self.h.events()[0]["is_open"], 1)
        self.assertEqual(self.h.poll("SRP", parse_srp([srp()]), T[2]), ["continuing"])

    def test_flicker_within_grace_reopens_same_event(self):
        self.h.poll("SRP", parse_srp([srp()]), T[0])
        self.h.poll("SRP", parse_srp([]), T[1])
        self.assertEqual(self.h.poll("SRP", parse_srp([srp()]), T[2]), ["reopened"])
        (e,) = self.h.events()
        self.assertEqual((e["is_open"], e["n_reopen"]), (1, 1))

    def test_same_id_after_grace_is_new_event(self):
        self.h.poll("SRP", parse_srp([srp()]), T[0])
        self.h.poll("SRP", parse_srp([]), T[1])
        self.assertEqual(self.h.poll("SRP", parse_srp([srp()]), T[5]), ["new"])
        self.assertEqual(len(self.h.events()), 2)

    def test_reticketed_outage_is_matched_by_start_and_place(self):
        self.h.poll("SRP", parse_srp([srp()]), T[0])
        acts = self.h.poll("SRP", parse_srp([srp(outageId="999", latitude=33.3683)]), T[1])
        self.assertEqual(acts, ["id_changed"])
        (e,) = self.h.events()
        self.assertEqual((e["source_id"], e["id_aliases"]), ("999", '["261121769"]'))

    def test_two_outages_one_restored_one_new(self):
        a, b = srp(), srp(outageId="2", outageBegan="2026-11-13T00:36:00Z", latitude=33.5)
        self.h.poll("SRP", parse_srp([a]), T[0])
        self.assertEqual(self.h.poll("SRP", parse_srp([b]), T[1]), ["closed", "new"])
        self.assertEqual(len(self.h.events()), 2)

    def test_duplicate_ticket_within_poll_counted_once(self):
        self.assertEqual(self.h.poll("SRP", parse_srp([srp(), srp(numberCustomersAffected=5)]), T[0]),
                         ["new"])
        self.assertEqual(self.h.events()[0]["peak_customers"], 919)

    def test_aps_layer_parse_and_match(self):
        feat = dict(Ticket="A1", off=1794529800000, etr=1794537000000, customers=120,
                    City="Phoenix", Cause="Equipment", outagestatus=1, OBJECTID=7,
                    _x=-112.07, _y=33.45)
        obs = parse_aps_layer(aps_payload(dict(feat)), "outage")
        self.assertEqual(obs[0].start_utc, "2026-11-13T07:30:00+00:00")  # Phoenix wall time -> UTC
        self.assertEqual(obs[0].extra["off_ms"], 1794529800000)
        self.assertEqual(self.h.poll("APS", obs, T[0]), ["new"])
        feat2 = dict(feat, customers=80, OBJECTID=99)  # OBJECTID may churn; Ticket is the key
        self.assertEqual(self.h.poll("APS", parse_aps_layer(aps_payload(feat2), "outage"), T[1]),
                         ["continuing"])

    def test_planned_and_metro_flags(self):
        feat = dict(Ticket="P1", off=1794517200000, etr=1794524400000, customers=21,
                    City="Show Low", outagetype="Planned Outage", _x=-110.054, _y=34.2406)
        self.h.poll("APS", parse_aps_layer(aps_payload(feat), "outage"), T[0])
        self.h.poll("SRP", parse_srp([srp(isMaintenanceOutage=True)]), T[0])
        ev = {e["utility"]: e for e in self.h.events()}
        self.assertEqual((ev["APS"]["kind"], ev["APS"]["in_metro"]), ("planned", 0))
        self.assertEqual((ev["SRP"]["kind"], ev["SRP"]["in_metro"]), ("planned", 1))

    def test_utilities_are_independent(self):
        self.h.poll("SRP", parse_srp([srp()]), T[0])
        self.assertEqual(self.h.poll("APS", [], T[1]), [])  # APS poll must not close SRP events
        self.assertEqual(self.h.events()[0]["is_open"], 1)


if __name__ == "__main__":
    unittest.main()
