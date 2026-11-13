"""Decide, poll by poll, whether each outage is new, continuing, restored or back.

Rules (per utility, per successful poll):

1. Same utility + same outage id as an OPEN event        -> "continuing"
2. Same id as an event CLOSED within the grace window    -> "reopened" (same event)
3. No id match, but same reported start time (+-2 min)
   and within 250 m of an open or just-closed event      -> "id_changed" (same event)
4. Otherwise                                             -> "new"
5. An open event that is absent from a SUCCESSFUL poll   -> "closed"; the end time
   lies between its last sighting and this poll.

A failed poll changes nothing: absence of data is never read as "restored".
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from .sources import Observation, in_metro_bbox

GRACE = timedelta(minutes=30)          # a restored outage that reappears within this is the same event
START_TOL = timedelta(minutes=2)
DIST_TOL_M = 250.0


@dataclass
class Event:
    event_id: str
    utility: str
    source_id: str | None
    kind: str
    start_reported: str | None
    first_seen: str
    last_seen: str
    lat: float | None
    lon: float | None
    area: str | None
    first_customers: int | None
    peak_customers: int | None
    last_customers: int | None
    cause_first: str | None
    cause_last: str | None
    etr_first: str | None
    etr_last: str | None
    n_polls: int = 1
    n_reopen: int = 0
    id_aliases: list[str] = field(default_factory=list)
    end_lo: str | None = None          # last poll that saw it
    end_hi: str | None = None          # first successful poll that did not
    is_open: bool = True
    in_metro: bool | None = None       # coarse bbox flag; final clip uses the Census polygon
    city: str | None = None


@dataclass
class Decision:
    action: str                        # new | continuing | reopened | id_changed | closed
    event: Event
    obs: Observation | None = None
    changed: list[str] = field(default_factory=list)  # fields that changed since last poll


def _t(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _dist_m(a: Event, o: Observation) -> float:
    if None in (a.lat, a.lon, o.lat, o.lon):
        return math.inf
    r = 6_371_000.0
    p1, p2 = math.radians(a.lat), math.radians(o.lat)
    dp, dl = p2 - p1, math.radians(o.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _same_outage(e: Event, o: Observation) -> bool:
    es, os_ = _t(e.start_reported), _t(o.start_utc)
    if es is None or os_ is None:
        return False
    return abs(es - os_) <= START_TOL and _dist_m(e, o) <= DIST_TOL_M


def _new_event(o: Observation, polled_at: str) -> Event:
    stamp = polled_at.replace("-", "").replace(":", "")[:13]
    sid = o.source_id or "noid" + hashlib.sha1(f"{o.start_utc}|{o.lat}|{o.lon}".encode()).hexdigest()[:8]
    return Event(
        event_id=f"{o.utility}-{sid}-{stamp}", utility=o.utility, source_id=o.source_id,
        kind=o.kind, start_reported=o.start_utc, first_seen=polled_at, last_seen=polled_at,
        lat=o.lat, lon=o.lon, area=o.area, first_customers=o.customers,
        peak_customers=o.customers, last_customers=o.customers, cause_first=o.cause,
        cause_last=o.cause, etr_first=o.etr_utc, etr_last=o.etr_utc,
        in_metro=in_metro_bbox(o.lat, o.lon), city=o.extra.get("City") if o.extra else None)


def _update(e: Event, o: Observation, polled_at: str) -> list[str]:
    changed = []
    if o.customers != e.last_customers:
        changed.append("customers")
    if o.etr_utc != e.etr_last:
        changed.append("etr")
    if o.cause != e.cause_last:
        changed.append("cause")
    e.last_seen = polled_at
    e.n_polls += 1
    e.last_customers = o.customers
    if o.customers is not None:
        e.peak_customers = max(e.peak_customers or 0, o.customers)
    e.cause_last, e.etr_last = o.cause, o.etr_utc
    if e.start_reported is None:
        e.start_reported = o.start_utc
    return changed


def dedupe(observations: Iterable[Observation]) -> list[Observation]:
    """Keep one observation per (utility, id) within a poll: the one with most customers."""
    best: dict[tuple, Observation] = {}
    for o in observations:
        key = (o.utility, o.source_id) if o.source_id else (o.utility, id(o))
        if key not in best or (o.customers or 0) > (best[key].customers or 0):
            best[key] = o
    return list(best.values())


def reconcile(open_events: list[Event], recent_closed: list[Event],
              observations: list[Observation], polled_at: str, ok: bool) -> list[Decision]:
    """Match one poll's observations for ONE utility against known events."""
    if not ok:
        return []
    now = _t(polled_at)
    by_id = {e.source_id: e for e in open_events if e.source_id}
    closed_by_id: dict[str, Event] = {}
    for e in sorted(recent_closed, key=lambda e: e.end_hi or "", reverse=True):
        if e.source_id and e.end_hi and now - _t(e.end_hi) <= GRACE:
            closed_by_id.setdefault(e.source_id, e)   # most recently closed wins
    matched: set[str] = set()
    decisions: list[Decision] = []
    pending: list[Observation] = []

    # Pass 1: exact id matches.
    for o in dedupe(observations):
        e = by_id.get(o.source_id) if o.source_id else None
        if e and e.event_id not in matched:
            matched.add(e.event_id)
            decisions.append(Decision("continuing", e, o, _update(e, o, polled_at)))
            continue
        e = closed_by_id.get(o.source_id) if o.source_id else None
        if e:
            e.is_open, e.end_lo, e.end_hi = True, None, None
            e.n_reopen += 1
            matched.add(e.event_id)
            decisions.append(Decision("reopened", e, o, _update(e, o, polled_at)))
            continue
        pending.append(o)

    # Pass 2: id changed or missing -> match on reported start time + location.
    candidates = [e for e in open_events if e.event_id not in matched] + \
                 [e for e in recent_closed if e.event_id not in matched and e.end_hi
                  and now - _t(e.end_hi) <= GRACE]
    for o in pending:
        e = next((c for c in candidates if c.event_id not in matched and _same_outage(c, o)), None)
        if e:
            if e.source_id and o.source_id and o.source_id != e.source_id:
                e.id_aliases.append(e.source_id)
                e.source_id = o.source_id
            if not e.is_open:
                e.is_open, e.end_lo, e.end_hi = True, None, None
                e.n_reopen += 1
            matched.add(e.event_id)
            decisions.append(Decision("id_changed", e, o, _update(e, o, polled_at)))
        else:
            e = _new_event(o, polled_at)
            matched.add(e.event_id)
            decisions.append(Decision("new", e, o))

    # Pass 3: open events not seen in this successful poll are restored.
    for e in open_events:
        if e.event_id not in matched and e.is_open:
            e.is_open, e.end_lo, e.end_hi = False, e.last_seen, polled_at
            decisions.append(Decision("closed", e))
    return decisions
