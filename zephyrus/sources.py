"""Download winds aloft from several forecast models and build an offline "wind pack".

Models (see MODELS below):
  hrrr   NOAA HRRR 3 km, CONUS, 1000-50 hPa, hourly, 18 h (48 h at 00/06/12/18Z)    AWS byte-range
  rap    NOAA RAP 13 km, CONUS, 1000-100 hPa, hourly, 21 h (51 h at 03/09/15/21Z)  AWS byte-range
  gfs    NOAA GFS 0.25 deg, global, 1000-3 hPa, hourly to 120 h      NOMADS filter -> AWS byte-range
  ecmwf  ECMWF IFS 0.25 deg open data, 1000-50 hPa, 3-hourly          data.ecmwf.int -> AWS mirror
  gefs   NOAA GEFS 0.5 deg ensemble (control + 30 members), 3-hourly  NOMADS filter -> AWS byte-range
  raob   Radiosonde soundings (Iowa Environmental Mesonet archive), 00Z/12Z

A wind pack (.npz) holds one entry per source ("gfs", "hrrr", "gefs-p03", "raob-KREV", ...):
  times [T] unix s, levels [L] hPa, lat [Y], lon [X] regular grid (lon -180..180),
  z [T,L,Y,X] geometric height m MSL, u/v [T,L,Y,X] earth-relative m/s, terrain [Y,X] m MSL
plus an optional "dem" entry (high-resolution terrain, see dem.py) and JSON metadata.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import requests

EARTH_R = 6371008.8
G0 = 9.80665

_session = requests.Session()
_session.headers["User-Agent"] = "zephyrus/2.2 (amateur balloon landing prediction)"


def log(msg, cb=None):
    print(msg, flush=True)
    if cb:
        cb(msg)


# ============================================================================ HTTP
def _get(url, headers=None, params=None, timeout=60, tries=4):
    last = None
    for i in range(tries):
        try:
            r = _session.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code in (200, 206):
                return r
            if r.status_code in (404, 403):
                return None
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise IOError(f"GET failed {url}: {last}")


def _exists(url):
    try:
        r = _session.head(url, timeout=20, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ============================================================================ index files
def parse_idx(text):
    """NCEP .idx -> list of [start, end_or_None, var, level_str]."""
    rows = []
    for line in text.strip().splitlines():
        p = line.split(":")
        if len(p) < 5:
            continue
        rows.append([int(p[1]), None, p[3], p[4]])
    # A message's bytes end where the next message with a LARGER offset starts. NCEP packs some
    # vector winds (e.g. RAP UGRD+VGRD) as sub-fields of ONE message, listed as "12.1" and "12.2"
    # with the same offset; both then get the whole shared message.
    starts = sorted({r[0] for r in rows})
    nxt = {a: b for a, b in zip(starts, starts[1:])}
    for r in rows:
        r[1] = nxt[r[0]] - 1 if r[0] in nxt else None
    return rows


def _level_hpa(level_str):
    if level_str.endswith(" mb"):
        try:
            return float(level_str[:-3])
        except ValueError:
            return None
    return None


def select_messages(rows, levels, want_surface_hgt=True):
    levels = set(float(l) for l in levels)
    out = []
    for start, end, var, lev in rows:
        if var in ("UGRD", "VGRD", "HGT"):
            p = _level_hpa(lev)
            if p is not None and p in levels:
                out.append((start, end))
            elif want_surface_hgt and var == "HGT" and lev == "surface":
                out.append((start, end))
    return sorted(set(out), key=lambda r: r[0])  # shared multi-field messages fetched once


def select_ecmwf(text, levels):
    """ECMWF open-data .index (JSON lines) -> list of (start, end)."""
    levels = set(int(l) for l in levels)
    out = []
    for line in text.strip().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("levtype") == "pl" and d.get("param") in ("u", "v", "gh", "z") \
                and int(d.get("levelist", -1)) in levels:
            out.append((int(d["_offset"]), int(d["_offset"]) + int(d["_length"]) - 1))
    return out


def coalesce(ranges):
    ranges = sorted(ranges)
    merged = []
    for s, e in ranges:
        if merged and merged[-1][1] is not None and s == merged[-1][1] + 1:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def byte_range_download(url, levels, dest, idx_kind="grib", mode="wb"):
    if idx_kind == "ecmwf":
        idx = _get(url.rsplit(".", 1)[0] + ".index")
        if idx is None:
            raise FileNotFoundError(url + " (.index)")
        ranges = coalesce(select_ecmwf(idx.text, levels))
    else:
        idx = _get(url + ".idx")
        if idx is None:
            raise FileNotFoundError(url + ".idx")
        ranges = coalesce(select_messages(parse_idx(idx.text), levels))
    if not ranges:
        raise ValueError(f"no wanted messages in {url}")

    def fetch(rg):
        s, e = rg
        r = _get(url, headers={"Range": f"bytes={s}-{'' if e is None else e}"}, timeout=120)
        if r is None:
            raise IOError(f"range fetch failed {url}")
        return r.content

    with ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(fetch, ranges))
    with open(dest, mode) as f:
        for p in parts:
            f.write(p)
    return sum(len(p) for p in parts)


# ============================================================================ GRIB decode
def decode_grib(path):
    """Yield dicts: kind ('gh','u','v','orog'), level hPa, values 2D, lats 2D, lons 2D, meta."""
    import eccodes as ec

    ec.codes_grib_multi_support_on()   # read every field of multi-field messages (RAP winds)
    grid_cache = {}                    # lat/lon arrays are slow to compute; once per grid
    with open(path, "rb") as f:
        while True:
            gid = ec.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                disc = ec.codes_get(gid, "discipline")
                cat = ec.codes_get(gid, "parameterCategory")
                num = ec.codes_get(gid, "parameterNumber")
                tol = ec.codes_get(gid, "typeOfLevel")
                if disc != 0:
                    continue
                scale = 1.0
                if (cat, num) == (3, 5):
                    kind = "orog" if tol == "surface" else "gh"
                elif (cat, num) == (3, 4) and tol == "isobaricInhPa":  # geopotential m2/s2
                    kind, scale = "gh", 1 / G0
                elif (cat, num) == (2, 2):
                    kind = "u"
                elif (cat, num) == (2, 3):
                    kind = "v"
                else:
                    continue
                if kind != "orog" and tol != "isobaricInhPa":
                    continue
                level = float(ec.codes_get(gid, "level")) if kind != "orog" else 0.0
                ni = ec.codes_get(gid, "Nx" if ec.codes_is_defined(gid, "Nx") else "Ni")
                nj = ec.codes_get(gid, "Ny" if ec.codes_is_defined(gid, "Ny") else "Nj")
                vals = ec.codes_get_values(gid).reshape(nj, ni) * scale
                meta = {
                    "grid": ec.codes_get(gid, "gridType"),
                    "valid": int(ec.codes_get(gid, "validityDate")),
                    "validtime": int(ec.codes_get(gid, "validityTime")),
                    "uvRelativeToGrid": int(ec.codes_get(gid, "uvRelativeToGrid"))
                    if ec.codes_is_defined(gid, "uvRelativeToGrid") else 0,
                }
                if meta["grid"] == "lambert":
                    meta["LoV"] = ec.codes_get(gid, "LoVInDegrees")
                    meta["Latin1"] = ec.codes_get(gid, "Latin1InDegrees")
                    meta["Latin2"] = ec.codes_get(gid, "Latin2InDegrees")
                key = (meta["grid"], ni, nj, ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees"),
                       ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
                if key not in grid_cache:
                    lats = ec.codes_get_array(gid, "latitudes").reshape(nj, ni)
                    lons = ec.codes_get_array(gid, "longitudes").reshape(nj, ni)
                    grid_cache[key] = (lats, ((lons + 180) % 360) - 180)
                lats, lons = grid_cache[key]
                yield {"kind": kind, "level": level, "values": vals, "lats": lats,
                       "lons": lons, "meta": meta}
            finally:
                ec.codes_release(gid)


def rotate_lambert_winds(u, v, lons, meta):
    """HRRR/RAP U/V are grid-relative; rotate to earth-relative (east/north)."""
    l1, l2 = math.radians(meta["Latin1"]), math.radians(meta["Latin2"])
    if abs(l1 - l2) < 1e-6:
        n = math.sin(l1)
    else:
        n = (math.log(math.cos(l1) / math.cos(l2)) /
             math.log(math.tan(math.pi / 4 + l2 / 2) / math.tan(math.pi / 4 + l1 / 2)))
    lov = ((meta["LoV"] + 180) % 360) - 180
    dlon = ((lons - lov + 180) % 360) - 180
    a = n * np.radians(dlon)
    ca, sa = np.cos(a), np.sin(a)
    return ca * u + sa * v, -sa * u + ca * v


def gph_to_geometric(z):
    return EARTH_R * z / (EARTH_R - z)


# ============================================================================ box / regrid
def make_box(lat, lon, radius_km):
    dlat = radius_km / 111.2
    dlon = radius_km / (111.2 * math.cos(math.radians(lat)))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


class _Regridder:
    """Precomputed linear (Delaunay barycentric) resample from curvilinear to regular grid."""

    def __init__(self, lats, lons, box, step):
        from scipy.spatial import Delaunay

        s, n, w, e = box
        pad = 2 * step + 0.1
        m = (lats >= s - pad) & (lats <= n + pad) & (lons >= w - pad) & (lons <= e + pad)
        if not m.any():
            raise ValueError("launch area is outside this model's domain")
        jj, ii = np.where(m)
        sl = (slice(jj.min(), jj.max() + 1), slice(ii.min(), ii.max() + 1))
        pts = np.column_stack([lons[sl].ravel(), lats[sl].ravel()])
        self.lat = np.arange(s, n + step / 2, step)
        self.lon = np.arange(w, e + step / 2, step)
        LON, LAT = np.meshgrid(self.lon, self.lat)
        tgt = np.column_stack([LON.ravel(), LAT.ravel()])
        tri = Delaunay(pts)
        simp = tri.find_simplex(tgt)
        self.valid = simp >= 0
        simp = np.where(self.valid, simp, 0)
        X = tri.transform[simp, :2]
        Y = tgt - tri.transform[simp, 2]
        b = np.einsum("ijk,ik->ij", X, Y)
        self.w = np.column_stack([b, 1 - b.sum(axis=1)])
        self.v = tri.simplices[simp]
        self.shape = LAT.shape
        self.sl = sl

    def __call__(self, field):
        f = field[self.sl].ravel()
        out = (f[self.v] * self.w).sum(axis=1)
        out[~self.valid] = np.nan
        return out.reshape(self.shape)


def _crop_regular(lats, lons, box):
    """Crop a regular lat/lon grid; return (lat1d asc, lon1d asc, crop function)."""
    s, n, w, e = box
    lat1 = lats[:, 0]
    lon1 = lons[0, :]
    order = np.argsort(lon1)
    lon1s = lon1[order]
    lat_flip = lat1[0] > lat1[-1]
    lat1s = lat1[::-1] if lat_flip else lat1
    pad = 0.6
    jy = np.where((lat1s >= s - pad) & (lat1s <= n + pad))[0]
    ix = np.where((lon1s >= w - pad) & (lon1s <= e + pad))[0]
    if len(jy) < 2 or len(ix) < 2:
        raise ValueError("box not covered by grid")

    def crop(f):
        f = f[::-1] if lat_flip else f
        f = f[:, order]
        return f[jy[0]:jy[-1] + 1, ix[0]:ix[-1] + 1]

    return lat1s[jy[0]:jy[-1] + 1], lon1s[ix[0]:ix[-1] + 1], crop


def _valid_unix(meta):
    d, t = str(meta["valid"]), f"{meta['validtime']:04d}"
    return dt.datetime.strptime(d + t, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc).timestamp()


def decode_regular_file(path, box):
    fields, crop, valid, terrain, lat1, lon1 = {}, None, None, None, None, None
    for m in decode_grib(path):
        if crop is None:
            lat1, lon1, crop = _crop_regular(m["lats"], m["lons"], box)
        valid = _valid_unix(m["meta"])
        if m["kind"] == "orog":
            terrain = crop(m["values"])
        else:
            fields[(m["kind"], m["level"])] = crop(m["values"])
    return valid, fields, terrain, lat1, lon1


def decode_lambert_file(path, box, rg=None, step=0.03):
    """Decode an HRRR/RAP file; returns (valid, fields, terrain, regridder)."""
    raw, valid, lons_full, terrain = {}, None, None, None
    for m in decode_grib(path):
        if rg is None:
            rg = _Regridder(m["lats"], m["lons"], box, step)
        lons_full = m["lons"]
        valid = _valid_unix(m["meta"])
        raw[(m["kind"], m["level"])] = (m["values"], m["meta"])
    fields = {}
    for (k, lev), (vals, meta) in raw.items():
        if k == "orog":
            terrain = rg(vals)
        elif k == "gh":
            fields[(k, lev)] = rg(vals)
        elif k == "u" and ("v", lev) in raw:
            vv = raw[("v", lev)][0]
            uu = vals
            if meta["uvRelativeToGrid"] and meta["grid"] == "lambert":
                uu, vv = rotate_lambert_winds(vals, vv, lons_full, meta)
            fields[("u", lev)] = rg(uu)
            fields[("v", lev)] = rg(vv)
    return valid, fields, terrain, rg


def _assemble(fields_by_hour, levels=None):
    """list of (valid_unix, {(kind, lev): 2D}) -> times, levels, z, u, v  [T,L,Y,X]."""
    common = sorted(set.intersection(*[
        {lev for (k, lev) in f if k == "u"} & {lev for (k, lev) in f if k == "v"} &
        {lev for (k, lev) in f if k == "gh"} for _, f in fields_by_hour]), reverse=True)
    if len(common) < 5:
        raise ValueError("too few pressure levels decoded")
    T = len(fields_by_hour)
    shp = next(iter(fields_by_hour[0][1].values())).shape
    z = np.empty((T, len(common)) + shp, np.float32)
    u = np.empty_like(z)
    v = np.empty_like(z)
    for ti, (_, f) in enumerate(fields_by_hour):
        for li, lev in enumerate(common):
            z[ti, li] = gph_to_geometric(f[("gh", lev)])
            u[ti, li] = f[("u", lev)]
            v[ti, li] = f[("v", lev)]
    times = np.array([t for t, _ in fields_by_hour], np.float64)
    o = np.argsort(times)
    return times[o], np.array(common, np.float32), z[o], u[o], v[o]


# ============================================================================ model registry
AWS_GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
AWS_HRRR = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
AWS_RAP = "https://noaa-rap-pds.s3.amazonaws.com"
AWS_GEFS = "https://noaa-gefs-pds.s3.amazonaws.com"
ECMWF_BASES = ["https://data.ecmwf.int/forecasts",
               "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"]
NOMADS = "https://nomads.ncep.noaa.gov/cgi-bin"

GFS_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400,
              350, 300, 250, 200, 150, 100, 70, 50, 40, 30, 20, 15, 10, 7, 5, 3]
GEFS_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400,
               350, 300, 250, 200, 150, 100, 70, 50, 30, 20, 10, 7, 5, 3]
ECMWF_LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]
# HRRR full CONUS fields are ~0.7 MB each, so levels cost real download time (first live run:
# 39 levels = 78 MB per forecast hour). 25 hPa spacing in the lower atmosphere where terrain
# channels the flow, 50 hPa above 500 hPa (as GFS uses): 31 levels, about 21% less.
HRRR_LEVELS = list(range(1000, 499, -25)) + list(range(450, 99, -50)) + [75, 50]


@dataclass
class ModelSpec:
    name: str
    label: str
    grid: str                      # "regular" | "lambert"
    cadence_h: int                 # hours between cycles
    step_h: int                    # forecast output interval
    levels: list
    urls: Callable                 # (cycle, fh, member) -> list of GRIB URLs
    maxfh: Callable                # (cycle) -> max forecast hour
    idx_kind: str = "grib"
    regrid_deg: float = 0.03
    nomads: Callable | None = None  # (cycle, fh, box, member) -> list of (url, params)
    members: list = field(default_factory=lambda: [None])
    top_hpa: float = 50.0          # highest level in the data
    domain: tuple | None = None    # (s, n, w, e) rough coverage check


def _hrrr_urls(c, fh, m):
    return [f"{AWS_HRRR}/hrrr.{c:%Y%m%d}/conus/hrrr.t{c:%H}z.wrfprsf{fh:02d}.grib2"]


def _rap_urls(c, fh, m):
    return [f"{AWS_RAP}/rap.{c:%Y%m%d}/rap.t{c:%H}z.awp130pgrbf{fh:02d}.grib2"]


def _gfs_urls(c, fh, m):
    return [f"{AWS_GFS}/gfs.{c:%Y%m%d}/{c:%H}/atmos/gfs.t{c:%H}z.pgrb2.0p25.f{fh:03d}"]


def _ecmwf_urls(c, fh, m):
    return [f"{b}/{c:%Y%m%d}/{c:%H}z/ifs/0p25/oper/{c:%Y%m%d%H}0000-{fh}h-oper-fc.grib2"
            for b in ECMWF_BASES]


def _gefs_urls(c, fh, m):
    return [f"{AWS_GEFS}/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2ap5/ge{m}.t{c:%H}z.pgrb2a.0p50.f{fh:03d}",
            f"{AWS_GEFS}/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2bp5/ge{m}.t{c:%H}z.pgrb2b.0p50.f{fh:03d}"]


def _nomads_params(levels, box, pad=0.75):
    s, n, w, e = box
    p = {"var_HGT": "on", "var_UGRD": "on", "var_VGRD": "on", "lev_surface": "on",
         "subregion": "", "leftlon": f"{w - pad:.2f}", "rightlon": f"{e + pad:.2f}",
         "toplat": f"{n + pad:.2f}", "bottomlat": f"{s - pad:.2f}"}
    for lev in levels:
        p[f"lev_{lev}_mb"] = "on"
    return p


def _gfs_nomads(c, fh, box, m):
    p = _nomads_params(GFS_LEVELS, box)
    p.update(dir=f"/gfs.{c:%Y%m%d}/{c:%H}/atmos", file=f"gfs.t{c:%H}z.pgrb2.0p25.f{fh:03d}")
    return [(f"{NOMADS}/filter_gfs_0p25.pl", p)]


def _gefs_nomads(c, fh, box, m):
    out = []
    for ab in ("a", "b"):
        p = _nomads_params(GEFS_LEVELS, box)
        p.update(dir=f"/gefs.{c:%Y%m%d}/{c:%H}/atmos/pgrb2{ab}p5",
                 file=f"ge{m}.t{c:%H}z.pgrb2{ab}.0p50.f{fh:03d}")
        out.append((f"{NOMADS}/filter_gefs_atmos_0p50{ab}.pl", p))
    return out


CONUS = (21.0, 53.0, -135.0, -60.0)

MODELS: dict[str, ModelSpec] = {
    "hrrr": ModelSpec("hrrr", "HRRR 3 km", "lambert", 1, 1, HRRR_LEVELS, _hrrr_urls,
                      lambda c: 48 if c.hour % 6 == 0 else 18, regrid_deg=0.03, top_hpa=50,
                      domain=CONUS),
    "rap": ModelSpec("rap", "RAP 13 km", "lambert", 1, 1, list(range(1000, 99, -25)), _rap_urls,
                     lambda c: 51 if c.hour % 6 == 3 else 21, regrid_deg=0.1, top_hpa=100,
                     domain=CONUS),
    "gfs": ModelSpec("gfs", "GFS 0.25°", "regular", 6, 1, GFS_LEVELS, _gfs_urls,
                     lambda c: 120, nomads=_gfs_nomads, top_hpa=3),
    "ecmwf": ModelSpec("ecmwf", "ECMWF IFS 0.25°", "regular", 6, 3, ECMWF_LEVELS, _ecmwf_urls,
                       lambda c: 144 if c.hour % 12 == 0 else 90, idx_kind="ecmwf", top_hpa=50),
    "gefs": ModelSpec("gefs", "GEFS ensemble 0.5°", "regular", 6, 3, GEFS_LEVELS, _gefs_urls,
                      lambda c: 240, nomads=_gefs_nomads, top_hpa=3,
                      members=["c00"] + [f"p{i:02d}" for i in range(1, 31)]),
}


def _floor_hour(t, h):
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - dt.timedelta(hours=t.hour % h)


def _fh_range(spec, cyc, start, end):
    s = (start - cyc).total_seconds() / 3600
    e = (end - cyc).total_seconds() / 3600
    fh0 = max(0, int(math.floor(s / spec.step_h)) * spec.step_h)
    fh1 = int(math.ceil(e / spec.step_h)) * spec.step_h
    return list(range(fh0, fh1 + 1, spec.step_h))


def _first_existing(urls, idx_kind):
    for u in urls:
        idx = u.rsplit(".", 1)[0] + ".index" if idx_kind == "ecmwf" else u + ".idx"
        if _exists(idx):
            return u
    return None


def choose_cycle(spec, start, end, now=None, cb=None, member=None):
    """Latest cycle at or before `start` (and `now`) whose last needed file exists."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cyc = _floor_hour(min(start, now), spec.cadence_h)
    tries = 12 if spec.cadence_h == 1 else 6
    for _ in range(tries):
        fhs = _fh_range(spec, cyc, start, end)
        if fhs[-1] <= spec.maxfh(cyc):
            m = member or spec.members[0]
            if _first_existing(spec.urls(cyc, fhs[-1], m)[:1] if spec.name != "ecmwf"
                               else spec.urls(cyc, fhs[-1], m), spec.idx_kind):
                return cyc
            log(f"  {spec.name} {cyc:%Y-%m-%d %HZ} f{fhs[-1]:03d} not published yet", cb)
        cyc -= dt.timedelta(hours=spec.cadence_h)
    raise RuntimeError(f"No {spec.name.upper()} run found covering "
                       f"{start:%Y-%m-%d %H:%MZ} to {end:%H:%MZ}")


def _download_fh(spec, cyc, fh, box, member, dest):
    """Fetch one forecast hour into `dest`. Returns (bytes, how)."""
    recent = (dt.datetime.now(dt.timezone.utc) - cyc).days <= 8
    if spec.nomads and recent:
        total, ok = 0, True
        with open(dest, "wb") as f:
            for url, params in spec.nomads(cyc, fh, box, member):
                try:
                    r = _session.get(url, params=params, timeout=120)
                except requests.RequestException:
                    ok = False
                    break
                if r.status_code != 200 or not r.content.startswith(b"GRIB"):
                    ok = False
                    break
                f.write(r.content)
                total += len(r.content)
        if ok and total:
            return total, "NOMADS subset"
    total = 0
    urls = spec.urls(cyc, fh, member)
    if spec.idx_kind == "ecmwf":  # mirrors: first one that answers
        for u in urls:
            try:
                return byte_range_download(u, spec.levels, dest, "ecmwf"), u.split("/")[2]
            except (FileNotFoundError, IOError):
                continue
        raise FileNotFoundError(urls[0])
    mode = "wb"
    for u in urls:
        total += byte_range_download(u, spec.levels, dest, spec.idx_kind, mode)
        mode = "ab"
    return total, "AWS"


def fetch_model(name, box, start, end, cb=None, now=None, member=None, cycle=None):
    spec = MODELS[name]
    if spec.domain:
        s, n, w, e = spec.domain
        if not (s < (box[0] + box[1]) / 2 < n and w < (box[2] + box[3]) / 2 < e):
            raise ValueError(f"{spec.label} does not cover this location")
    m = member or spec.members[0]
    cyc = cycle or choose_cycle(spec, start, end, now, cb, m)
    fhs = _fh_range(spec, cyc, start, end)
    tag = f"{name}{'-' + m if m else ''}"
    log(f"{spec.label}{' ' + m if m else ''}: run {cyc:%Y-%m-%d %HZ}, hours {fhs[0]}-{fhs[-1]}", cb)
    tmp = tempfile.mkdtemp(prefix=f"{tag}_")

    def one(fh):
        path = os.path.join(tmp, f"f{fh:03d}.grb2")
        n, how = _download_fh(spec, cyc, fh, box, m, path)
        log(f"  {tag} f{fh:03d}: {n/1e6:.2f} MB via {how}", cb)
        return fh, path

    with ThreadPoolExecutor(4) as ex:
        files = dict(ex.map(one, fhs))
    hours, terrain, rg, lat1, lon1 = [], None, None, None, None
    for fh in fhs:
        path = files[fh]
        if spec.grid == "lambert":
            valid, fields, ter, rg = decode_lambert_file(path, box, rg, spec.regrid_deg)
            lat1, lon1 = rg.lat, rg.lon
        else:
            valid, fields, ter, lat1, lon1 = decode_regular_file(path, box)
        if ter is not None:
            terrain = ter
        hours.append((valid, fields))
        os.remove(path)
    times, levels, z, u, v = _assemble(hours)
    if terrain is None:
        terrain = np.full(z.shape[2:], np.nan, np.float32)
    return dict(times=times, levels=levels, lat=np.asarray(lat1), lon=np.asarray(lon1), z=z, u=u,
                v=v, terrain=np.asarray(terrain, np.float32), cycle=cyc.isoformat())


# ============================================================================ soundings
# Upper-air stations around the Great Basin (IEM uses K-prefixed ids for US sites).
RAOB_STATIONS = {
    "KREV": ("Reno, NV", 39.57, -119.80), "KLKN": ("Elko, NV", 40.86, -115.74),
    "KVEF": ("Las Vegas, NV", 36.05, -115.18), "KSLC": ("Salt Lake City, UT", 40.77, -111.95),
    "KOAK": ("Oakland, CA", 37.73, -122.22), "KNKX": ("San Diego, CA", 32.85, -117.12),
    "KBOI": ("Boise, ID", 43.57, -116.21), "KMFR": ("Medford, OR", 42.37, -122.88),
    "KFGZ": ("Flagstaff, AZ", 35.23, -111.82), "KGJT": ("Grand Junction, CO", 39.12, -108.53),
    "KEDW": ("Edwards AFB, CA", 34.90, -117.92), "KTUS": ("Tucson, AZ", 32.23, -110.96),
}


def nearest_stations(lat, lon, n=2, max_km=500):
    from .flight import haversine
    d = sorted((float(haversine(lat, lon, la, lo)) / 1000, k) for k, (_, la, lo) in RAOB_STATIONS.items())
    return [k for km, k in d[:n] if km <= max_km]


def fetch_sounding(station, when, box, cb=None):
    """Most recent 00Z/12Z sounding at or before `when` -> pack source (uniform over the box)."""
    t = _floor_hour(when, 12)
    for _ in range(4):
        url = "https://mesonet.agron.iastate.edu/json/raob.py"
        try:
            r = _get(url, params={"ts": t.strftime("%Y%m%d%H%M"), "station": station}, timeout=30)
        except IOError:
            r = None
        prof = _parse_raob_json(r.json()) if r is not None else None
        if prof is not None and len(prof[0]) >= 8:
            break
        t -= dt.timedelta(hours=12)
    else:
        raise RuntimeError(f"no sounding for {station} near {when:%Y-%m-%d %HZ}")
    z, p, u, v = prof
    log(f"Sounding {station} {t:%Y-%m-%d %HZ}: {len(z)} levels to {z[-1]/0.3048:,.0f} ft", cb)
    return profile_source(z, u, v, p, [t.timestamp(), t.timestamp() + 12 * 3600], box,
                          name=f"raob-{station}", cycle=t.isoformat())


def _parse_raob_json(js):
    profs = js.get("profiles") or []
    if not profs:
        return None
    rows = profs[0].get("profile") or []
    pts = []
    for r in rows:
        try:
            h, p = float(r["hght"]), float(r["pres"])
            d, s = float(r["drct"]), float(r["sknt"]) * 0.514444
        except (TypeError, KeyError, ValueError):
            continue
        pts.append((h, p, -s * math.sin(math.radians(d)), -s * math.cos(math.radians(d))))
    if not pts:
        return None
    pts.sort()
    a = np.array([q for i, q in enumerate(pts) if i == 0 or q[0] > pts[i - 1][0] + 1])
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def profile_source(z, u, v, p, times, box, name="profile", cycle=None):
    """A single wind column, applied uniformly across the box (soundings, measured winds)."""
    s, n, w, e = box
    lat = np.array([s - 1, n + 1])
    lon = np.array([w - 1, e + 1])
    T, L = len(times), len(z)
    sh = (T, L, 2, 2)
    Z = np.broadcast_to(np.asarray(z, np.float32)[None, :, None, None], sh).copy()
    U = np.broadcast_to(np.asarray(u, np.float32)[None, :, None, None], sh).copy()
    V = np.broadcast_to(np.asarray(v, np.float32)[None, :, None, None], sh).copy()
    d = dict(times=np.asarray(times, float), levels=np.asarray(p, np.float32), lat=lat, lon=lon,
             z=Z, u=U, v=V, terrain=np.full((2, 2), np.nan, np.float32))
    if cycle:
        d["cycle"] = cycle
    return d


# ============================================================================ pack building
def build_pack(lat, lon, start, hours=4.0, models=("hrrr", "gfs"), radius_km=350, out_path=None,
               cb=None, now=None, gefs_members=10, soundings=True, dem_zoom=9):
    """Download everything for a launch at (lat, lon, start) into one .npz wind pack.

    `models` may include: hrrr, rap, gfs, ecmwf, gefs. GFS is always fetched (it tops up every
    other model above its highest level). Failures of individual models are logged, not fatal."""
    if isinstance(models, str):
        models = {"blend": ("hrrr", "gfs")}.get(models, (models,))
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=hours)
    box = make_box(lat, lon, radius_km)
    models = list(dict.fromkeys(["gfs"] + [m for m in models if m in MODELS]))
    data, errors, cycles = {}, {}, {}
    for name in models:
        spec = MODELS[name]
        members = spec.members[: gefs_members + 1] if name == "gefs" else [None]
        cyc = None
        for m in members:
            key = name if m is None else f"{name}-{m}"
            try:
                d = fetch_model(name, box, start, end, cb, now, m, cycle=cyc)
                cyc = dt.datetime.fromisoformat(d["cycle"])
                cycles[key] = d.pop("cycle")
                data[key] = d
            except Exception as e:  # noqa
                errors[key] = str(e)
                log(f"{key} failed: {e}", cb)
                if m is None or m == members[0]:
                    break
    if soundings:
        for st in nearest_stations(lat, lon):
            try:
                d = fetch_sounding(st, start, box, cb)
                cycles[f"raob-{st}"] = d.pop("cycle")
                data[f"raob-{st}"] = d
            except Exception as e:  # noqa
                errors[f"raob-{st}"] = str(e)
                log(f"Sounding {st} failed: {e}", cb)
    if not any(k == "gfs" or k.startswith(("hrrr", "rap", "ecmwf", "gefs")) for k in data):
        raise RuntimeError(f"No wind data downloaded: {errors}")
    if dem_zoom:
        try:
            from .dem import build_dem
            data["dem"] = build_dem(box, dem_zoom, cb=cb)
        except Exception as e:  # noqa
            errors["dem"] = str(e)
            log(f"Terrain download failed ({e}); using model terrain", cb)
    meta = {"launch_lat": lat, "launch_lon": lon, "start": start.isoformat(), "end": end.isoformat(),
            "box": box, "models": models, "errors": errors, "cycles": cycles,
            "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    if out_path is None:
        from .paths import DATA_DIR
        out_path = os.path.join(DATA_DIR, f"winds_{start:%Y%m%d_%H%M}Z_{lat:.2f}_{lon:.2f}.npz")
    save_pack(out_path, data, meta)
    log(f"Saved wind pack {os.path.basename(out_path)} ({os.path.getsize(out_path)/1e6:.1f} MB): "
        f"{', '.join(sorted(data))}", cb)
    return out_path


def save_pack(path, data, meta):
    if isinstance(path, (str, os.PathLike)):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    arrays = {"meta": np.array(json.dumps(meta))}
    for src, d in data.items():
        for k, a in d.items():
            arrays[f"{src}_{k}"] = np.asarray(a)
    np.savez_compressed(path, **arrays)


def slim_pack(path, out, radius_km=250.0, max_step_deg=0.06):
    """Phone-sized copy of a pack: crop to `radius_km` around the launch point and thin any grid
    finer than `max_step_deg` (HRRR 0.03 deg -> 0.06 deg, about 6 km). Terrain mosaic cropped too."""
    data, meta = load_pack(path)
    lat0, lon0 = meta.get("launch_lat"), meta.get("launch_lon")
    if lat0 is None:
        s, n, w, e = meta["box"]
        lat0, lon0 = (s + n) / 2, (w + e) / 2
    s, n, w, e = make_box(lat0, lon0, radius_km)
    for key, d in data.items():
        if key == "dem":
            continue
        la, lo = d["lat"], d["lon"]
        step = max(1, int(round(max_step_deg / max(abs(la[1] - la[0]), 1e-6)))) if len(la) > 1 else 1
        jy = np.where((la >= s - 0.5) & (la <= n + 0.5))[0]
        ix = np.where((lo >= w - 0.5) & (lo <= e + 0.5))[0]
        if len(jy) < 2 or len(ix) < 2:          # sounding columns etc.: keep as is
            continue
        ys = slice(jy[0], jy[-1] + 1, step)
        xs = slice(ix[0], ix[-1] + 1, step)
        d["lat"], d["lon"] = la[ys], lo[xs]
        for f in ("z", "u", "v"):
            d[f] = d[f][:, :, ys, xs]
        d["terrain"] = d["terrain"][ys, xs]
    if "dem" in data:
        dm = data["dem"]
        zoom, x0, y0 = int(dm["zoom"]), int(dm["x0"]), int(dm["y0"])
        n2 = 2 ** zoom * 256

        def px(la, lo):
            lr = math.radians(la)
            return ((lo + 180) / 360 * n2 - x0 * 256,
                    (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n2 - y0 * 256)
        xa, ya = px(n, w)
        xb, yb = px(s, e)
        c0, r0 = max(0, int(xa) // 256 * 256), max(0, int(ya) // 256 * 256)
        c1, r1 = min(dm["z"].shape[1], (int(xb) // 256 + 1) * 256), min(dm["z"].shape[0], (int(yb) // 256 + 1) * 256)
        dm["z"] = dm["z"][r0:r1, c0:c1]
        dm["x0"], dm["y0"] = np.array(x0 + c0 // 256), np.array(y0 + r0 // 256)
    meta = dict(meta, box=[s, n, w, e], slimmed_for_phone=True)
    save_pack(out, data, meta)


def load_pack(path):
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    data = {}
    for key in z.files:
        if key == "meta":
            continue
        src, k = key.split("_", 1)
        data.setdefault(src, {})[k] = z[key]
    return data, meta
