"""Build a synthetic multi-model wind pack (+terrain) and reference results from the Python engine.
Usage: python android/tests/make_reference.py OUTDIR"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
os.environ["ZEPHYRUS_DATA"] = out
import numpy as np
from test_v2 import pack_data
from zephyrus.sources import save_pack, make_box
from zephyrus.windfield import WindField
from zephyrus.flight import FlightParams, Uncertainty, BalloonSpec, simulate
from zephyrus.predict import launch_window, launch_site_for_target, run_models

T0 = 1.8e9
data = pack_data(gefs=4)
# time-varying, sheared GFS so time interpolation is exercised
data["gfs"]["u"] = data["gfs"]["u"] + (np.arange(data["gfs"]["u"].shape[0])[:, None, None, None] * 0.7).astype(np.float32)
# a terrain mosaic: planar ramp in Web-Mercator pixels
dem = {"z": (1200 + np.add.outer(np.arange(512) * 0.5, np.arange(768) * 0.8)).astype(np.int16),
       "zoom": np.array(9), "x0": np.array(86), "y0": np.array(195)}
data["dem"] = dem
save_pack(os.path.join(out, "ref.npz"), data, {"box": list(make_box(39.5, -118.5, 300)), "cycles": {"gfs": "x"}})
base = WindField(data, "gfs")
ref = {"cases": []}
P = dict(lat=39.5, lon=-118.5, launch_time=T0 + 900, descent_rate=7.0, ascent_rate=5.1, burst_alt=30000.0)
for mode in ["blend", "gfs", "ecmwf", "gefs-p02"]:
    for extra in [{}, {"launch_alt": 1500.0}, {"start_phase": 1, "lat": 39.6, "lon": -118.2}]:
        p = FlightParams(**{**P, **extra})
        sa = 20000.0 if extra.get("start_phase") == 1 else None
        s = simulate(base.member(mode), p, start_alt=sa).summary()
        ref["cases"].append({"mode": mode, "p": {**P, **extra}, "start_alt": sa,
                             "landing": [s["landing"]["lat"], s["landing"]["lon"], s["landing"]["time"], s["landing"]["alt"]],
                             "burst": [s["burst"]["lat"], s["burst"]["lon"], s["burst"]["alt"]]})
# balloon physics
b = BalloonSpec(1200, 8.63, 0.25, 1500, 2900, "he")
s = simulate(base.member("gfs"), FlightParams(**P, balloon=b)).summary()
ref["physics"] = {"landing": [s["landing"]["lat"], s["landing"]["lon"]], "burst_alt": s["burst"]["alt"]}
# terrain lookups
from zephyrus.dem import DemTerrain
pts = [[39.4, -118.6], [39.55, -118.45], [39.7, -118.1], [41.5, -118.5]]
ref["terrain"] = [[la, lo, float(base.terrain(la, lo)[0])] for la, lo in pts]
# Monte Carlo spread
r = simulate(base.member("gfs"), FlightParams(**P), Uncertainty(members=400)).summary()
ref["mc"] = {"r50_km": r["ensemble"]["r50_km"], "r95_km": r["ensemble"]["r95_km"], "a_m": r["ensemble"]["ellipse95"]["a_m"]}
w = launch_window(base, FlightParams(**P), "gfs", T0, T0 + 3 * 3600, 1.0, target=(39.6, -117.5))
ref["window"] = [[x["lat"], x["lon"]] for x in w["rows"]]
rv = launch_site_for_target(base, FlightParams(**P), "gfs", (39.55, -117.6))
ref["reverse_miss_km"] = rv["miss_km"]
g = run_models(base, FlightParams(**P), Uncertainty(members=0), ["gefs"], refine=False, gefs_mc_per_member=0)
ref["gefs_members"] = g["models"]["gefs"]["summary"]["member_landings"]
json.dump(ref, open(os.path.join(out, "ref.json"), "w"))
print("reference written", len(ref["cases"]), "cases")
