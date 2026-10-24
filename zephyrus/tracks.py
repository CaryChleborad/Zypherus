"""Read recorded balloon tracks (CSV, GPX, KML, raw APRS logs) and derive flight parameters."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import xml.etree.ElementTree as ET

import numpy as np

from .atmosphere import RHO0, density

FT = 0.3048
TZ_ABBR = {"UTC": 0, "GMT": 0, "Z": 0, "PST": -8, "PDT": -7, "MST": -7, "MDT": -6,
           "CST": -6, "CDT": -5, "EST": -5, "EDT": -4}


class Track:
    def __init__(self, t, lat, lon, alt, name=""):
        o = np.argsort(t)
        t, lat, lon, alt = (np.asarray(a, float)[o] for a in (t, lat, lon, alt))
        keep = np.concatenate([[True], np.diff(t) > 0])
        self.t, self.lat, self.lon, self.alt = t[keep], lat[keep], lon[keep], alt[keep]
        self.name = name

    def __len__(self):
        return len(self.t)


# ----------------------------------------------------------------------------- time parsing
def parse_time(s, default_tz_hours=0):
    s = str(s).strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", s):
        return float(s)
    if re.fullmatch(r"\d{12,14}", s):  # unix ms
        return float(s) / 1000
    tz = None
    m = re.search(r"\s([A-Z]{1,4})$", s)
    if m and m.group(1) in TZ_ABBR:
        tz = dt.timezone(dt.timedelta(hours=TZ_ABBR[m.group(1)]))
        s = s[: m.start()]
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            d = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognized time: {s!r}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz or dt.timezone(dt.timedelta(hours=default_tz_hours)))
    return d.timestamp()


# ----------------------------------------------------------------------------- readers
def load_track(path, default_tz_hours=0, alt_units=None):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    low = path.lower()
    head = text.lstrip()[:500].lower()
    if low.endswith(".gpx") or "<gpx" in head:
        tr = _read_gpx(text)
    elif low.endswith(".kml") or "<kml" in head:
        tr = _read_kml(text)
    elif _looks_aprs(text):
        tr = _read_aprs(text, default_tz_hours)
    else:
        tr = _read_csv(text, default_tz_hours, alt_units)
    tr.name = path.replace("\\", "/").split("/")[-1]
    if len(tr) < 5:
        raise ValueError(f"{path}: only {len(tr)} usable points")
    return tr


def _read_csv(text, tz, alt_units):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    hdr = [h.strip().lower() for h in rows[0]]

    def find(*names):
        for n in names:
            for i, h in enumerate(hdr):
                if h == n:
                    return i
        for n in names:
            for i, h in enumerate(hdr):
                if n in h:
                    return i
        return None

    it = find("datetime", "timestamp", "time_received", "time", "date")
    ilat = find("lat", "latitude")
    ilon = find("lon", "lng", "long", "longitude")
    ialt = find("alt", "altitude", "ele", "elevation", "height")
    if None in (it, ilat, ilon, ialt):
        raise ValueError(f"CSV header not understood: {rows[0]}")
    feet = alt_units == "ft" or (alt_units is None and ("ft" in hdr[ialt] or "feet" in hdr[ialt]))
    idate = find("date") if hdr[it] == "time" else None
    t, la, lo, al = [], [], [], []
    for r in rows[1:]:
        try:
            ts = r[it] if idate is None or idate == it else f"{r[idate]} {r[it]}"
            t.append(parse_time(ts, tz))
            la.append(float(r[ilat]))
            lo.append(float(r[ilon]))
            al.append(float(r[ialt]) * (FT if feet else 1.0))
        except (ValueError, IndexError):
            continue
    return Track(t, la, lo, al)


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _read_gpx(text):
    root = _strip_ns(ET.fromstring(text))
    t, la, lo, al = [], [], [], []
    for p in root.iter("trkpt"):
        ti, el = p.find("time"), p.find("ele")
        if ti is None or el is None:
            continue
        t.append(parse_time(ti.text))
        la.append(float(p.get("lat")))
        lo.append(float(p.get("lon")))
        al.append(float(el.text))
    return Track(t, la, lo, al)


def _read_kml(text):
    root = _strip_ns(ET.fromstring(text))
    t, la, lo, al = [], [], [], []
    for trk in root.iter("Track"):  # gx:Track
        whens = [w.text for w in trk.findall("when")]
        coords = [c.text for c in trk.findall("coord")]
        for w, c in zip(whens, coords):
            x, y, z = (float(v) for v in c.split())
            t.append(parse_time(w))
            lo.append(x)
            la.append(y)
            al.append(z)
    if not t:  # point placemarks with timestamps
        for pm in root.iter("Placemark"):
            w = pm.find(".//when")
            c = pm.find(".//Point/coordinates")
            if w is None or c is None:
                continue
            x, y, *z = (float(v) for v in c.text.strip().split(","))
            t.append(parse_time(w.text))
            lo.append(x)
            la.append(y)
            al.append(z[0] if z else np.nan)
    tr = Track(t, la, lo, al)
    ok = np.isfinite(tr.alt)
    return Track(tr.t[ok], tr.lat[ok], tr.lon[ok], tr.alt[ok])


_APRS_POS = re.compile(r"(\d{2})(\d{2}\.\d{2})([NS]).(\d{3})(\d{2}\.\d{2})([EW])")
_APRS_ALT = re.compile(r"/A=(-?\d{5,6})")
_APRS_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\s?[A-Z]{1,4})?)")


def _looks_aprs(text):
    lines = text.splitlines()[:50]
    return sum(bool(_APRS_ALT.search(l) and _APRS_POS.search(l)) for l in lines) >= 3


def _read_aprs(text, tz):
    t, la, lo, al = [], [], [], []
    for line in text.splitlines():
        m_ts, m_pos, m_alt = _APRS_TS.match(line.strip()), _APRS_POS.search(line), _APRS_ALT.search(line)
        if not (m_ts and m_pos and m_alt):
            continue
        d1, m1, ns, d2, m2, ew = m_pos.groups()
        lat = (int(d1) + float(m1) / 60) * (1 if ns == "N" else -1)
        lon = (int(d2) + float(m2) / 60) * (1 if ew == "E" else -1)
        try:
            t.append(parse_time(m_ts.group(1).rstrip(":"), tz))
        except ValueError:
            continue
        la.append(lat)
        lo.append(lon)
        al.append(int(m_alt.group(1)) * FT)
    return Track(t, la, lo, al)


# ----------------------------------------------------------------------------- analysis
def analyze(tr: Track):
    """Derive launch, ascent rate, burst, sea-level descent rate, landing from a track."""
    ib = int(np.argmax(tr.alt))
    asc = slice(0, ib + 1)
    ta, za = tr.t[asc], tr.alt[asc]
    # launch = last point before sustained climb (skip pre-launch packets on the ground)
    i0 = 0
    for i in range(ib):
        if za[min(i + 3, ib)] - za[i] > 30:
            i0 = i
            break
    ta, za = ta[i0:], za[i0:]
    rate = float(np.polyfit(ta - ta[0], za, 1)[0]) if len(ta) >= 3 else float(
        (za[-1] - za[0]) / max(ta[-1] - ta[0], 1))
    desc_t, desc_z = tr.t[ib:], tr.alt[ib:]
    v0s = []
    for k in range(len(desc_t) - 1):
        dtt = desc_t[k + 1] - desc_t[k]
        dz = desc_z[k] - desc_z[k + 1]
        zm = 0.5 * (desc_z[k] + desc_z[k + 1])
        if dtt <= 0 or dz <= 0 or zm > 25000:
            continue
        v0s.append(dz / dtt / np.sqrt(RHO0 / density(zm)))
    v0 = float(np.median(v0s)) if v0s else float("nan")
    # ascent rate by 5 km band: tells us whether these balloons climb steadily or speed up
    prof = []
    for lo in range(0, 45000, 5000):
        m = (za >= lo) & (za < lo + 5000)
        if m.sum() >= 3:
            prof.append({"band_m": [lo, lo + 5000],
                         "rate": float(np.polyfit(ta[m] - ta[m][0], za[m], 1)[0])})
    return {
        "ascent_profile": prof,
        "launch": {"t": float(tr.t[i0]), "lat": float(tr.lat[i0]), "lon": float(tr.lon[i0]),
                   "alt": float(tr.alt[i0])},
        "burst": {"t": float(tr.t[ib]), "lat": float(tr.lat[ib]), "lon": float(tr.lon[ib]),
                  "alt": float(tr.alt[ib])},
        "landing": {"t": float(tr.t[-1]), "lat": float(tr.lat[-1]), "lon": float(tr.lon[-1]),
                    "alt": float(tr.alt[-1])},
        "ascent_rate": rate,
        "descent_rate_sl": v0,
        "complete": bool(tr.alt[-1] < tr.alt[ib] - 0.8 * (tr.alt[ib] - tr.alt[i0])),
        "points": len(tr),
    }


def observed_winds(tr: Track, min_dt=30.0):
    """Balloon horizontal velocity between fixes ~= wind. Returns arrays t, lat, lon, alt, u, v."""
    from .flight import EARTH_R

    out = []
    j = 0
    for i in range(1, len(tr)):
        if tr.t[i] - tr.t[j] < min_dt:
            continue
        dtt = tr.t[i] - tr.t[j]
        lat_m = 0.5 * (tr.lat[i] + tr.lat[j])
        u = np.radians(tr.lon[i] - tr.lon[j]) * EARTH_R * np.cos(np.radians(lat_m)) / dtt
        v = np.radians(tr.lat[i] - tr.lat[j]) * EARTH_R / dtt
        out.append((0.5 * (tr.t[i] + tr.t[j]), lat_m, 0.5 * (tr.lon[i] + tr.lon[j]),
                    0.5 * (tr.alt[i] + tr.alt[j]), u, v))
        j = i
    a = np.array(out)
    return {k: a[:, n] for n, k in enumerate(("t", "lat", "lon", "alt", "u", "v"))} if len(a) else None
