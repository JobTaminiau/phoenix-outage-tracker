"""Fetch and normalize the APS and SRP public outage feeds.

Both feeds are public and need no login. Each fetch returns a FetchResult that
holds the raw payloads (saved verbatim for reprocessing) and a list of
normalized Observation records.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

SRP_URL = "https://myaccount.srpnet.com/myaccountapi/api/outages/getall"
APS_BASE = "https://aps-ags.esriemcs.com/arcgis/rest/services/APSOutageMap/MapServer"
# Layer 0 = Outages, 5 = SpecialEvents, 8 = PSPS_Events (same schema); 3 = feed timestamp table.
APS_OUTAGE_LAYERS = {0: "outage", 5: "special_event", 8: "psps"}
APS_PROPS_LAYER = 3

TIMEOUT = 30

# Arizona keeps MST (UTC-7) all year, no daylight saving.
PHOENIX_OFFSET = timedelta(hours=-7)
# APS 'off'/'etr' epochs look like Arizona wall-clock time stored as if UTC (one live record,
# 2026-11-13). "local" reinterprets them; "utc" takes them as-is. Raw values are kept in extra.
APS_TIME_MODE = os.environ.get("APS_TIME_MODE", "local")

# Coarse box around the Phoenix-Mesa-Scottsdale urban area, used only as a quick flag.
# The study-area clip itself uses the Census urban-area polygon at analysis time.
METRO_BBOX = (-112.65, 33.20, -111.50, 33.92)  # lon_min, lat_min, lon_max, lat_max


def in_metro_bbox(lat, lon) -> bool | None:
    if lat is None or lon is None:
        return None
    x0, y0, x1, y1 = METRO_BBOX
    return x0 <= lon <= x1 and y0 <= lat <= y1


def user_agent() -> str:
    contact = os.environ.get("OUTAGE_CONTACT", "")
    tail = f"; contact: {contact}" if contact else ""
    return f"PhoenixOutageResearch/0.1 (academic research, polite polling{tail})"


@dataclass
class Observation:
    """One outage as seen in one poll, in a utility-neutral shape."""
    utility: str
    source_id: str | None          # utility's own outage id (APS Ticket, SRP outageId)
    start_utc: str | None          # utility-reported start, ISO 8601 UTC
    etr_utc: str | None            # estimated restoration, ISO 8601 UTC
    customers: int | None
    lat: float | None
    lon: float | None
    cause: str | None = None
    status: str | None = None
    area: str | None = None        # APS City/APSArea, SRP cross-street text
    kind: str = "outage"           # outage | planned | special_event | psps
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    utility: str
    ok: bool
    observations: list[Observation]
    raw: dict[str, Any]            # name -> parsed JSON payload, saved verbatim
    feed_updated_utc: str | None = None
    error: str | None = None


def _iso_from_epoch_ms(v: Any, local_wall: bool = False) -> str | None:
    if v in (None, "", 0):
        return None
    dt = datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    if local_wall:  # value is Phoenix wall time mislabelled as UTC
        dt = dt - PHOENIX_OFFSET
    return dt.isoformat(timespec="seconds")


def _iso_from_text(v: Any) -> str | None:
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- SRP
def parse_srp(payload: list[dict[str, Any]]) -> list[Observation]:
    obs = []
    for r in payload:
        maint = bool(r.get("isMaintenanceOutage"))
        obs.append(Observation(
            utility="SRP",
            source_id=str(r["outageId"]) if r.get("outageId") not in (None, "") else None,
            start_utc=_iso_from_text(r.get("outageBegan")),
            etr_utc=_iso_from_text(r.get("estimatedRestorationTime")),
            customers=_int(r.get("numberCustomersAffected")),
            lat=r.get("latitude"),
            lon=r.get("longitude"),
            cause=r.get("outageProblem"),
            area=" ".join(str(r.get("crossRoadText") or "").split()) or None,
            kind="planned" if maint else "outage",
            extra={"estimatedUsersImpacted": r.get("estimatedUsersImpacted")},
        ))
    return obs


def fetch_srp(session: requests.Session) -> FetchResult:
    try:
        r = session.get(SRP_URL, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, list):
            raise ValueError(f"unexpected SRP payload type {type(payload).__name__}")
        return FetchResult("SRP", True, parse_srp(payload), {"getall": payload})
    except Exception as e:  # noqa: BLE001 - any failure means "unknown", never "no outages"
        return FetchResult("SRP", False, [], {}, error=repr(e))


# ---------------------------------------------------------------- APS
def parse_aps_layer(payload: dict[str, Any], kind: str) -> list[Observation]:
    if "error" in payload:
        raise ValueError(f"ArcGIS error: {payload['error']}")
    obs = []
    for f in payload.get("features", []):
        a = f.get("attributes", {})
        g = f.get("geometry") or {}
        ticket = a.get("Ticket")
        local = APS_TIME_MODE == "local"
        this_kind = "planned" if str(a.get("outagetype") or "").strip().lower().startswith("planned") else kind
        obs.append(Observation(
            utility="APS",
            source_id=str(ticket).strip() if ticket not in (None, "") else None,
            start_utc=_iso_from_epoch_ms(a.get("off"), local),
            etr_utc=_iso_from_epoch_ms(a.get("etr"), local),
            customers=_int(a.get("customers")),
            lat=g.get("y"),
            lon=g.get("x"),
            cause=a.get("Cause"),
            status=str(a.get("outagestatus")) if a.get("outagestatus") is not None else None,
            area=a.get("Boundary") or a.get("City") or a.get("APSArea"),
            kind=this_kind,
            extra={**{k: a.get(k) for k in ("City", "APSArea", "outagetype", "outageimpact",
                                            "datastatus", "Boundary", "Comments", "OBJECTID")},
                   "off_ms": a.get("off"), "etr_ms": a.get("etr"), "time_mode": APS_TIME_MODE},
        ))
    return obs


def fetch_aps(session: requests.Session) -> FetchResult:
    raw: dict[str, Any] = {}
    obs: list[Observation] = []
    try:
        for layer, kind in APS_OUTAGE_LAYERS.items():
            r = session.get(f"{APS_BASE}/{layer}/query", timeout=TIMEOUT, params={
                "where": "1=1", "outFields": "*", "returnGeometry": "true",
                "outSR": "4326", "f": "json"})
            r.raise_for_status()
            payload = r.json()
            raw[f"layer{layer}"] = payload
            obs.extend(parse_aps_layer(payload, kind))
            if payload.get("exceededTransferLimit"):
                raise ValueError(f"layer {layer} exceeded 2000-record limit; add paging")
        r = session.get(f"{APS_BASE}/{APS_PROPS_LAYER}/query", timeout=TIMEOUT,
                        params={"where": "1=1", "outFields": "*", "f": "json"})
        r.raise_for_status()
        props = r.json()
        raw["props"] = props
        feats = props.get("features") or [{}]
        ts = _iso_from_epoch_ms(feats[0].get("attributes", {}).get("TIMESTAMP"))
        return FetchResult("APS", True, obs, raw, feed_updated_utc=ts)
    except Exception as e:  # noqa: BLE001
        return FetchResult("APS", False, [], raw, error=repr(e))


FETCHERS = {"APS": fetch_aps, "SRP": fetch_srp}

_STATIC_KEYS = ("fields", "fieldAliases", "spatialReference", "geometryType",
                "displayFieldName", "globalIdFieldName", "objectIdFieldName")


def slim_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop the static schema blocks ArcGIS repeats in every response (keeps logs small)."""
    return {k: ({kk: vv for kk, vv in v.items() if kk not in _STATIC_KEYS}
                if isinstance(v, dict) else v) for k, v in raw.items()}


def parse_raw(utility: str, raw: dict[str, Any]) -> tuple[list[Observation], str | None]:
    """Re-create observations from a logged raw payload (used when replaying the log)."""
    if utility == "SRP":
        return parse_srp(raw.get("getall", [])), None
    obs: list[Observation] = []
    for layer, kind in APS_OUTAGE_LAYERS.items():
        if f"layer{layer}" in raw:
            obs.extend(parse_aps_layer(raw[f"layer{layer}"], kind))
    feats = (raw.get("props") or {}).get("features") or [{}]
    return obs, _iso_from_epoch_ms(feats[0].get("attributes", {}).get("TIMESTAMP"))


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent(), "Accept": "application/json"})
    return s


def dumps_raw(raw: dict[str, Any]) -> bytes:
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
