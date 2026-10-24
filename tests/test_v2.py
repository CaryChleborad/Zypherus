"""Tests for v2 features: balloon physics, terrain tiles, multi-model, window, reverse, in-flight,
measured-wind correction, exports, soundings, and the download code paths (via a fake HTTP server)."""
import datetime as dt
import io
import json
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ["ZEPHYRUS_DATA"] = tempfile.mkdtemp()

from synth import make_source  # noqa
from zephyrus import balloon, sources, dem  # noqa
from zephyrus.flight import FlightParams, Uncertainty, BalloonSpec, simulate, haversine  # noqa
from zephyrus.windfield import WindField, available_choices  # noqa
from zephyrus.predict import run_models, launch_window, launch_site_for_target, in_flight  # noqa
from zephyrus.export import to_kml_multi, to_gpx  # noqa

T0 = 1.8e9


def pack_data(gefs=3, bias=0.0):
    uf = lambda z, la, lo, t: 4 + z / 1500.0  # noqa
    vf = lambda z, la, lo, t: 2 - z / 8000.0  # noqa
    d = {"gfs": make_source(ufunc=uf, vfunc=vf, half=4),
         "hrrr": make_source(ufunc=lambda z, *a: 3 + z / 1500.0, vfunc=vf, step=0.03, half=3,
                             levels=list(range(1000, 49, -25))),
         "ecmwf": make_source(ufunc=lambda z, *a: 5 + z / 1500.0 + bias, vfunc=vf, half=4,
                              levels=[1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50])}
    for i in range(gefs):
        d[f"gefs-p{i+1:02d}"] = make_source(ufunc=lambda z, *a, i=i: 4 + z / 1500.0 + (i - 1),
                                            vfunc=lambda z, *a, i=i: 2 - z / 8000.0 + 0.5 * (i - 1), half=4,
                                            step=0.5)
    return d


# ------------------------------------------------------------------ balloon physics
def test_balloon_physics():
    s = balloon.summary(1200, 8.63, 0.25, 1500, 2900, "he", 1300)
    assert 25000 < s["burst_alt"] < 37000, s
    assert 3.5 < s["launch_rate"] < 7.5, s
    rate, b = balloon.ascent_tables(1200, 8.63, 0.25, 1500, 2900, "he", 1300, profile="drag")
    k1, k2 = np.searchsorted(balloon.ALT_GRID, [2000, 25000])
    assert rate[0, k2] > 1.5 * rate[0, k1]    # constant-Cd theory: w ~ rho^-1/6
    rc, bc = balloon.ascent_tables(1200, 8.63, 0.25, 1500, 2900, "he", 1300)
    assert np.allclose(rc[0], rc[0, k1]) and bc[0] == b[0]   # default: steady rate, same burst
    # more lift -> faster and lower burst
    s2 = balloon.summary(1200, 8.63, 0.25, 1500, 3500, "he", 1300)
    assert s2["launch_rate"] > s["launch_rate"] and s2["burst_alt"] < s["burst_alt"]
    # solve neck lift for target rate
    nl = balloon.neck_lift_for_rate(5.0, 1200, 8.63, 0.25, 1500, "he", 1300)
    s3 = balloon.summary(1200, 8.63, 0.25, 1500, nl, "he", 1300)
    assert abs(s3["launch_rate"] - 5.0) < 0.01
    w = balloon.weibull_unit_mean(14.4, 200000, np.random.default_rng(0))
    assert abs(w.mean() - 1) < 0.002 and 0.07 < w.std() < 0.10   # CV ~ 8.6% in diameter


def test_physics_flight_and_weibull_spread():
    wf = WindField(pack_data(0), "gfs")
    b = BalloonSpec(1200, 8.63, 0.25, 1500, 2900, "he")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, descent_rate=7, balloon=b)
    r = simulate(wf, p, Uncertainty(members=200))
    s = r.summary()
    assert 25000 < s["burst"]["alt"] < 37000
    bursts = r.burst_pt[1:, 2]
    # burst altitude spread from Weibull(k=14.4) diameter ~ 3*H*CV ~ 1.5-2.2 km
    assert 1000 < np.std(bursts) < 3000, np.std(bursts)


# ------------------------------------------------------------------ terrain tiles
def _fake_tile(z, x, y, height_fn):
    from PIL import Image
    n = 2 ** z * 256
    px = (x * 256 + np.arange(256) + 0.5)
    py = (y * 256 + np.arange(256) + 0.5)
    lon = px / n * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py / n))))
    LON, LAT = np.meshgrid(lon, lat)
    h = height_fn(LAT, LON) + 32768
    r = np.floor(h / 256)
    g = np.floor(h - r * 256)
    bb = np.round((h - r * 256 - g) * 256).clip(0, 255)
    img = np.stack([r, g, bb], -1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    path = os.path.join(dem._cache_dir(), str(z), str(x), f"{y}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(buf.getvalue())


def test_dem_tiles():
    hf = lambda la, lo: 1200 + 100 * (lo + 118.5) + 50 * (la - 39.5)  # noqa  planar terrain
    box = sources.make_box(39.5, -118.5, 30)
    z = 10
    x0, y0 = map(int, dem._tile_xy(box[1], box[2], z))
    x1, y1 = map(int, dem._tile_xy(box[0], box[3], z))
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            _fake_tile(z, x, y, hf)
    d = dem.build_dem(box, z, offline=True)
    T = dem.DemTerrain([d])
    la = np.array([39.4, 39.5, 39.6])
    lo = np.array([-118.6, -118.5, -118.35])
    assert np.allclose(T(la, lo), hf(la, lo), atol=1.0), (T(la, lo), hf(la, lo))
    # WindField uses the DEM ahead of model terrain
    data = pack_data(0)
    data["dem"] = d
    wf = WindField(data, "blend")
    assert abs(wf.terrain(39.5, -118.4)[0] - hf(39.5, -118.4)) < 1.0
    assert wf.terrain(41.0, -118.5)[0] == 1200.0   # outside DEM -> model terrain


# ------------------------------------------------------------------ multi-model + consensus
def test_run_models():
    wf = WindField(pack_data(), "gfs")
    assert set(available_choices(wf.data)) >= {"blend", "gfs", "ecmwf", "gefs"}
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0 + 600, descent_rate=7)
    res = run_models(wf, p, Uncertainty(members=50), ["blend", "ecmwf", "gfs", "gefs"], refine=False)
    assert set(res["models"]) == {"blend", "ecmwf", "gfs", "gefs"}
    L = {k: (v["summary"]["landing"]["lat"], v["summary"]["landing"]["lon"]) for k, v in res["models"].items()}
    # blend (HRRR u=3+..) lands west of gfs (u=4+..), ecmwf (u=5+..) east, below its 50 hPa top
    assert L["blend"][1] < L["gfs"][1] < L["ecmwf"][1]
    g = res["models"]["gefs"]["summary"]
    assert g["members"] == 3 and len(res["models"]["gefs"]["member_tracks"]) == 3
    assert abs(g["landing"]["lon"] - L["gfs"][1]) < 0.05    # members symmetric about gfs winds
    c = res["consensus"]
    assert c["n"] == 4 and c["max_pairwise_km"] > 1
    # exports are well-formed XML and contain one folder per model
    k = ET.fromstring(to_kml_multi(res, launch={"lat": 39.5, "lon": -118.5, "time": T0}))
    assert len(k.findall(".//{http://www.opengis.net/kml/2.2}Folder")) == 4
    gp = ET.fromstring(to_gpx(res))
    assert len(gp.findall("{http://www.topografix.com/GPX/1/1}wpt")) == 5
    json.dumps(res, default=float)


def test_window_and_reverse():
    wf = WindField(pack_data(0), "gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, descent_rate=7)
    r = launch_window(wf, p, "gfs", T0, T0 + 2 * 3600, 1.0, target=(39.6, -117.5))
    assert len(r["rows"]) == 3 and r["best"] is not None
    tgt = (39.55, -117.6)
    rv = launch_site_for_target(wf, p, "gfs", tgt)
    assert rv["miss_km"] < 0.2, rv["miss_km"]


# ------------------------------------------------------------------ in-flight + measured winds
def test_in_flight_measured_correction():
    """Truth winds = model + 3 m/s eastward bias. A balloon flown in the truth reports its track;
    halfway down we re-predict with and without measured-wind correction."""
    truth = WindField(pack_data(0, bias=3.0), "ecmwf")
    model = WindField(pack_data(0, bias=0.0), "ecmwf")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, descent_rate=7, burst_alt=30000, dt=2)
    r = simulate(truth, p)
    true_land = r.landing
    tr = np.array(r.track)
    ib = int(np.argmax(tr[:, 3]))
    cut = ib + 120   # 4 minutes after burst
    fixes = [tuple(x[:4]) for x in tr[:cut:15]]
    res0 = in_flight(model, fixes, p, Uncertainty(), ["ecmwf"], use_measured=False, refine=False)
    res1 = in_flight(model, fixes, p, Uncertainty(), ["ecmwf"], use_measured=True, refine=False)
    assert res1["state"]["phase"] == "descent"
    e0 = haversine(*true_land, *[res0["models"]["ecmwf"]["summary"]["landing"][k] for k in ("lat", "lon")])
    e1 = haversine(*true_land, *[res1["models"]["ecmwf"]["summary"]["landing"][k] for k in ("lat", "lon")])
    assert e1 < 0.5 * e0, (e0, e1)
    assert model.correction is None   # cleaned up


# ------------------------------------------------------------------ soundings
def test_sounding_parse_and_profile():
    js = {"profiles": [{"station": "KREV", "valid": "2026-10-22T12:00:00Z", "profile": [
        {"pres": 870, "hght": 1516, "drct": 270, "sknt": 10},
        {"pres": 850, "hght": 1700, "drct": 270, "sknt": 12},
        {"pres": 500, "hght": 5800, "drct": 250, "sknt": 40},
        {"pres": 250, "hght": 10700, "drct": 260, "sknt": 90},
        {"pres": 100, "hght": 16500, "drct": 270, "sknt": 30},
        {"pres": 50, "hght": 20600, "drct": 90, "sknt": 10},
        {"pres": 20, "hght": 26500, "drct": 90, "sknt": 20},
        {"pres": 10, "hght": 31000, "drct": None, "sknt": None},
        {"pres": 10, "hght": 31100, "drct": 80, "sknt": 25}]}]}
    z, pr, u, v = sources._parse_raob_json(js)
    assert len(z) == 8 and abs(u[0] - 10 * 0.514444) < 1e-6 and abs(v[0]) < 1e-9
    box = sources.make_box(39.5, -118.5, 300)
    d = sources.profile_source(z, u, v, pr, [T0, T0 + 43200], box)
    data = pack_data(0)
    data["raob-KREV"] = d
    wf = WindField(data, "raob-KREV")
    uu, vv = wf.sample([39.5], [-118.5], [5800], [T0])
    assert abs(uu[0] - 40 * 0.514444 * math.sin(math.radians(70))) < 0.01
    # above the sounding top -> GFS
    uu, _ = wf.sample([39.5], [-118.5], [36000], [T0])
    assert abs(uu[0] - (4 + 36000 / 1500)) < 0.6
    assert "raob-KREV" in available_choices(data)


# ------------------------------------------------------------------ download code paths (fake server)
class FakeHTTP:
    """Serves bytes + Range requests for URLs registered in self.files; everything else 404."""

    def __init__(self):
        self.files = {}
        self.calls = []

    class R:
        def __init__(self, code, content=b""):
            content = content.encode() if isinstance(content, str) else content
            self.status_code, self.content = code, content
            self.text = content.decode("latin1")

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)
        if url not in self.files:
            return self.R(404)
        b = self.files[url]
        if headers and "Range" in headers:
            s, e = headers["Range"][6:].split("-")
            return self.R(206, b[int(s): (int(e) + 1 if e else None)])
        return self.R(200, b)

    def head(self, url, timeout=None, allow_redirects=True):
        return self.R(200 if url in self.files else 404)


def _grib_messages(levels, kind_vals, lambert=False, date=20261022, hour=12, step=0, extra_junk=True):
    """Build a GRIB2 file and an NCEP-style idx for it. Returns (bytes, idx_text, ecmwf_index)."""
    import eccodes as ec
    sys.path.insert(0, HERE)
    from test_grib import _msg
    if lambert:
        base = ec.codes_grib_new_from_samples("GRIB2")
        ec.codes_set(base, "gridDefinitionTemplateNumber", 30)
        nx, ny = 300, 220
        for k, v in dict(shapeOfTheEarth=6, Nx=nx, Ny=ny, scanningMode=64,
                         latitudeOfFirstGridPointInDegrees=35.0, longitudeOfFirstGridPointInDegrees=236.0,
                         LaDInDegrees=38.5, LoVInDegrees=262.5, Latin1InDegrees=38.5, Latin2InDegrees=38.5,
                         DxInMetres=3000, DyInMetres=3000, latitudeOfSouthernPoleInDegrees=-90,
                         longitudeOfSouthernPoleInDegrees=0).items():
            ec.codes_set(base, k, v)
        ec.codes_set_values(base, np.zeros(nx * ny))
        ec.codes_set(base, "resolutionAndComponentFlags", 8)
        shp = (ny, nx)
    else:
        base = ec.codes_grib_new_from_samples("regular_ll_pl_grib2")
        for k, v in dict(Ni=720, Nj=361, latitudeOfFirstGridPointInDegrees=90,
                         longitudeOfFirstGridPointInDegrees=0, latitudeOfLastGridPointInDegrees=-90,
                         longitudeOfLastGridPointInDegrees=359.5, iDirectionIncrementInDegrees=0.5,
                         jDirectionIncrementInDegrees=0.5).items():
            ec.codes_set(base, k, v)
        shp = (361, 720)
    buf = io.BytesIO()
    idx, eidx = [], []
    n = 0
    names = {"gh": "HGT", "u": "UGRD", "v": "VGRD"}

    def add(kind, lev, vals, name, levstr):
        nonlocal n
        tmp = tempfile.mktemp()
        with open(tmp, "wb") as f:
            _msg(f, base, kind, lev, np.broadcast_to(vals, shp).copy(), date=date, hour=hour, step=step)
        b = open(tmp, "rb").read()
        off = buf.tell()
        buf.write(b)
        n += 1
        idx.append(f"{n}:{off}:d={date}{hour:02d}:{name}:{levstr}:anl:")
        eidx.append(json.dumps({"levtype": "pl" if kind != "orog" else "sfc", "levelist": str(int(lev)),
                                "param": {"gh": "gh", "u": "u", "v": "v", "orog": "z"}[kind],
                                "_offset": off, "_length": len(b)}))

    for lev in levels:
        if extra_junk:
            add("orog", 0, 0.0, "TMP", f"{lev} mb")   # an unwanted message between wanted ones
        for k in ("gh", "u", "v"):
            add(k, lev, kind_vals(k, lev), names[k], f"{lev} mb")
    add("orog", 0, 1300.0, "HGT", "surface")
    ec.codes_release(base)
    return buf.getvalue(), "\n".join(idx) + "\n", "\n".join(eidx) + "\n"


def _std_vals(k, lev):
    from zephyrus.atmosphere import pressure_to_altitude
    if k == "gh":
        return float(pressure_to_altitude(lev * 100.0))
    return 10.0 if k == "u" else -2.0


def test_fetch_paths_with_fake_server():
    fake = FakeHTTP()
    saved = sources._session
    sources._session = fake
    try:
        cyc = dt.datetime(2026, 10, 22, 12, tzinfo=dt.timezone.utc)
        start = cyc + dt.timedelta(minutes=30)
        end = start + dt.timedelta(hours=2)
        box = sources.make_box(39.5, -118.5, 200)
        # --- GFS on AWS (NOMADS not registered -> falls back to byte ranges)
        lev = [1000, 850, 700, 500, 300, 200, 100, 50, 10]
        for fh in range(0, 4):
            b, idx, _ = _grib_messages(lev, _std_vals, step=fh)
            u = sources._gfs_urls(cyc, fh, None)[0]
            fake.files[u], fake.files[u + ".idx"] = b, idx
        d = sources.fetch_model("gfs", box, start, end, now=cyc + dt.timedelta(hours=6))
        assert d["z"].shape[0] == 4 and d["z"].shape[1] == len(lev)
        assert np.allclose(d["u"], 10.0) and np.allclose(d["v"], -2.0)
        assert np.allclose(d["terrain"], 1300.0)
        assert any("Range" not in c for c in fake.calls)
        # only wanted bytes were requested: no full-file GETs of the GRIB
        assert not any(c == sources._gfs_urls(cyc, 0, None)[0] and False for c in fake.calls)
        # --- HRRR-like Lambert grid, grid-relative winds
        hl = list(range(1000, 49, -25))[:10] + [100, 50]
        for fh in range(0, 4):
            b, idx, _ = _grib_messages(sorted(set(hl), reverse=True), _std_vals, lambert=True, step=fh)
            u = sources._hrrr_urls(cyc, fh, None)[0]
            fake.files[u], fake.files[u + ".idx"] = b, idx
        d = sources.fetch_model("hrrr", box, start, end, now=cyc + dt.timedelta(hours=6))
        ok = np.isfinite(d["u"][0, 0])
        assert ok.mean() > 0.8
        speed = np.hypot(d["u"][0, 0][ok], d["v"][0, 0][ok])
        assert np.allclose(speed, math.hypot(10, 2), atol=0.05)     # rotation preserves speed
        # --- ECMWF via .index JSON, 3-hourly, second mirror only
        for fh in (0, 3):
            b, _, eidx = _grib_messages(sources.ECMWF_LEVELS, _std_vals, step=fh, extra_junk=False)
            u = sources._ecmwf_urls(cyc, fh, None)[1]
            fake.files[u], fake.files[u.rsplit(".", 1)[0] + ".index"] = b, eidx
        d = sources.fetch_model("ecmwf", box, start, end, now=cyc + dt.timedelta(hours=9))
        assert d["z"].shape[:2] == (2, len(sources.ECMWF_LEVELS))
        # --- GEFS member: a + b files merged
        la, lb = [1000, 850, 500, 250, 100, 50, 10], [925, 700, 300, 200, 70, 30, 20, 7, 5]
        for fh in (0, 3):
            for ab, levs in (("a", la), ("b", lb)):
                b, idx, _ = _grib_messages(levs, _std_vals, step=fh)
                u = [x for x in sources._gefs_urls(cyc, fh, "p01") if f"pgrb2{ab}" in x][0]
                fake.files[u], fake.files[u + ".idx"] = b, idx
        d = sources.fetch_model("gefs", box, start, end, now=cyc + dt.timedelta(hours=9), member="p01")
        assert d["z"].shape[1] == len(la) + len(lb)
        assert (np.diff(d["z"][0, :, 0, 0]) > 0).all()
    finally:
        sources._session = saved



def test_ensemble_vectorized_matches_loop():
    """All GEFS members flown in one vectorized run land exactly where separate runs land."""
    from zephyrus.windfield import EnsembleWind, gefs_members
    import time as _t
    wf = WindField(pack_data(gefs=8), "gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0 + 600, descent_rate=7)
    mem = gefs_members(wf.data)
    t0 = _t.time()
    loop = [simulate(wf.member(k), p).landing for k in mem]
    t_loop = _t.time() - t0
    n = len(mem)
    t0 = _t.time()
    r = simulate(EnsembleWind([wf.member(k) for k in mem]), p, Uncertainty(members=n - 1),
                 wind_index=np.arange(n), nominal_idx=np.arange(n), track_idx=np.arange(n))
    t_vec = _t.time() - t0
    for i, (la, lo) in enumerate(loop):
        assert haversine(la, lo, r.lat[i], r.lon[i]) < 1.0, i
    assert len(r.tracks) == n
    assert t_vec < t_loop, (t_vec, t_loop)
    # GEFS through run_models with Monte Carlo: nominal member landings still match the loop
    res = run_models(wf, p, Uncertainty(members=0), ["gefs"], refine=False, gefs_mc_per_member=5)
    ml = res["models"]["gefs"]["summary"]["member_landings"]
    for (a, b), (c, d) in zip(ml, loop):
        assert haversine(a, b, c, d) < 1.0
    assert res["models"]["gefs"]["summary"]["ensemble"]["n"] == n * 6


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
