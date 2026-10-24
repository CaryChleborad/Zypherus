"""Live telemetry: SondeHub Amateur (no key) and aprs.fi (API key), plus manual fixes.

SondeHub asks that its REST API not be polled hard; the app polls at most once a minute.
aprs.fi returns only the latest position per request, so fixes are accumulated per callsign.
"""
from __future__ import annotations

import datetime as dt
import threading

import requests

from .tracks import parse_time

_STORE: dict[str, dict[float, tuple]] = {}
_LOCK = threading.Lock()


def _add(callsign, fixes):
    with _LOCK:
        d = _STORE.setdefault(callsign.upper(), {})
        for f in fixes:
            d[f[0]] = f
        return sorted(d.values())


def fixes(callsign):
    with _LOCK:
        return sorted(_STORE.get(callsign.upper(), {}).values())


def clear(callsign):
    with _LOCK:
        _STORE.pop(callsign.upper(), None)


def add_manual(callsign, t, lat, lon, alt):
    return _add(callsign, [(float(t), float(lat), float(lon), float(alt))])


def sondehub(callsign, last_s=6 * 3600):
    url = f"https://api.v2.sondehub.org/amateur/telemetry/{callsign}"
    r = requests.get(url, params={"last": int(last_s)}, timeout=30)
    r.raise_for_status()
    js = r.json()
    rows = []
    items = js if isinstance(js, list) else [v for vv in js.values() for v in
                                              (vv.values() if isinstance(vv, dict) else vv)]
    for it in items:
        try:
            rows.append((parse_time(it["datetime"]), float(it["lat"]), float(it["lon"]), float(it["alt"])))
        except (KeyError, TypeError, ValueError):
            continue
    return _add(callsign, rows)


def aprsfi(callsign, apikey):
    r = requests.get("https://api.aprs.fi/api/get",
                     params={"name": callsign, "what": "loc", "apikey": apikey, "format": "json"},
                     timeout=30)
    r.raise_for_status()
    js = r.json()
    if js.get("result") != "ok":
        raise RuntimeError(js.get("description", "aprs.fi error"))
    rows = []
    for e in js.get("entries", []):
        if "altitude" not in e:
            continue
        rows.append((float(e.get("lasttime") or e["time"]), float(e["lat"]), float(e["lng"]),
                     float(e["altitude"])))
    return _add(callsign, rows)
