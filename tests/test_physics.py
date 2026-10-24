import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zephyrus.atmosphere import density, temp_pressure, pressure_to_altitude  # noqa
from zephyrus.flight import FlightParams, Uncertainty, simulate, haversine, descent_speed  # noqa
from zephyrus.windfield import WindField  # noqa
from zephyrus.sources import parse_idx, select_messages, coalesce, rotate_lambert_winds  # noqa
from synth import make_source  # noqa

T0 = 1.8e9


def test_atmosphere():
    t, p = temp_pressure(0)
    assert abs(p - 101325) < 1
    _, p11 = temp_pressure(11000)
    assert abs(p11 - 22632) < 5
    _, p30 = temp_pressure(30480)
    assert 1000 < p30 < 1200          # ~11 hPa at 100k ft
    assert abs(pressure_to_altitude(1000.0) - 31055) < 100  # 10 hPa ~ 31 km
    assert abs(float(density(0)) - 1.225) < 1e-3


def test_constant_wind_drift():
    wf = WindField({"gfs": make_source()}, mode="gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, ascent_rate=5, burst_alt=30480,
                     descent_rate=6, dt=2)
    r = simulate(wf, p)
    s = r.summary()
    assert s["landing"]["landed"]
    total = s["landing"]["time"] - T0
    # analytic ascent time
    assert abs(s["ascent_min"] * 60 - (30480 - 1200) / 5) < 3
    # analytic descent time: integral dz / (v0 sqrt(rho0/rho))
    zz = np.linspace(1200, 30480, 20001)
    tdesc = np.trapezoid(1 / descent_speed(6, zz), zz)
    assert abs(s["descent_min"] * 60 - tdesc) < 5, (s["descent_min"] * 60, tdesc)
    # pure eastward drift = 10 m/s * total time
    assert abs(s["range_km"] * 1000 - 10 * total) / (10 * total) < 0.002
    assert abs(s["bearing_deg"] - 90) < 0.5
    assert abs(s["landing"]["lat"] - 39.5) < 0.01


def test_shear_and_time_interp():
    # wind varies with height and time; compare to brute-force small-step integration
    uf = lambda z, la, lo, t: 5 + z / 1000.0 + (t - T0) / 3600.0  # noqa
    vf = lambda z, la, lo, t: -3 + 0 * z  # noqa
    wf = WindField({"gfs": make_source(ufunc=uf, vfunc=vf)}, mode="gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, ascent_rate=5, burst_alt=25000,
                     descent_rate=7, dt=10)
    a = simulate(wf, p).summary()["landing"]
    p.dt = 1
    b = simulate(wf, p).summary()["landing"]
    assert haversine(a["lat"], a["lon"], b["lat"], b["lon"]) < 50  # step-size converged


def test_terrain_landing():
    # terrain slopes upward to the east: 1000 m at west edge -> 2000 m at east edge
    src = make_source()
    X = len(src["lon"])
    src["terrain"] = np.tile(np.linspace(1000, 2000, X, dtype=np.float32), (len(src["lat"]), 1))
    wf = WindField({"gfs": src}, mode="gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, descent_rate=6)
    s = simulate(wf, p).summary()
    g = float(wf.terrain(s["landing"]["lat"], s["landing"]["lon"])[0])
    assert abs(s["landing"]["alt"] - g) < 1
    assert s["landing"]["alt"] > 1500


def test_blend():
    g = make_source(ufunc=lambda z, *a: 20 + 0 * z)
    h = make_source(ufunc=lambda z, *a: 0 * z, levels=list(range(1000, 49, -25)), step=0.03,
                    half=2.0)
    wf = WindField({"gfs": g, "hrrr": h}, mode="blend")
    u, _ = wf.sample([39.5] * 4, [-118.5] * 4, [5000, wf.blend_lo, wf.blend_hi, 25000], [T0] * 4)
    assert abs(u[0]) < 1e-3 and abs(u[1]) < 1e-3
    assert abs(u[2] - 20) < 1e-3 and abs(u[3] - 20) < 1e-3
    # outside HRRR box -> pure GFS even at low level
    u2, _ = wf.sample([39.5], [-121.0], [3000], [T0])
    assert abs(u2[0] - 20) < 1e-3


def test_monte_carlo():
    wf = WindField({"gfs": make_source()}, mode="gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0)
    r = simulate(wf, p, Uncertainty(members=200))
    e = r.summary()["ensemble"]
    assert e["n"] == 201
    assert 2 < e["r95_km"] < 60
    assert e["ellipse95"]["a_m"] >= e["ellipse95"]["b_m"]


def test_idx_and_rotation():
    idx = ("1:0:d=2026102212:HGT:500 mb:3 hour fcst:\n2:100:d=2026102212:TMP:500 mb:3 hour fcst:\n"
           "3:200:d=2026102212:UGRD:500 mb:3 hour fcst:\n4:300:d=2026102212:VGRD:500 mb:3 hour fcst:\n"
           "5:400:d=2026102212:HGT:surface:3 hour fcst:\n6:500:d=2026102212:UGRD:450 mb:3 hour fcst:\n")
    rows = parse_idx(idx)
    sel = select_messages(rows, [500])
    assert sel == [(0, 99), (200, 299), (300, 399), (400, 499)]
    assert coalesce(sel) == [[0, 99], [200, 499]]
    meta = {"Latin1": 38.5, "Latin2": 38.5, "LoV": 262.5}
    # at the central meridian, no rotation
    u, v = rotate_lambert_winds(np.array([3.0]), np.array([4.0]), np.array([-97.5]), meta)
    assert abs(u[0] - 3) < 1e-9 and abs(v[0] - 4) < 1e-9
    # in Nevada (lon -116) grid-north wind -> true wind has an eastward component (~11.5 deg)
    u, v = rotate_lambert_winds(np.array([0.0]), np.array([10.0]), np.array([-116.0]), meta)
    ang = math.degrees(math.atan2(u[0], v[0]))
    assert abs(ang + 11.52) < 0.05  # west of LoV: grid north is west of true north


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
