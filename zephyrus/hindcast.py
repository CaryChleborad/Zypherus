"""Re-fly recorded flights against archived forecast data and measure prediction error.

For each flight and each available model:
  * observed_params : launch -> landing using the flight's own ascent, burst and descent
                      (isolates wind/terrain error)
  * nominal_params  : the numbers you planned to use (if given) -> total pre-flight error
  * from_burst      : descent only, starting at the observed burst point (descent-phase error)
  * from_burst_measured : same, with the model corrected by the winds the balloon measured on
                      the way up (what the in-flight mode does)
plus model wind error vs. the balloon's own drift, by altitude band.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import os

import numpy as np

from .flight import FlightParams, Uncertainty, simulate, haversine
from .sources import build_pack, load_pack, MODELS
from .tracks import load_track, analyze, observed_winds
from .windfield import WindField, available_choices, gefs_members

BANDS = [(0, 5000), (5000, 10000), (10000, 15000), (15000, 20000), (20000, 25000), (25000, 40000)]


def _miss(s, a):
    return {
        "pred_landing": [s["landing"]["lat"], s["landing"]["lon"]],
        "landing_miss_km": float(haversine(s["landing"]["lat"], s["landing"]["lon"],
                                           a["landing"]["lat"], a["landing"]["lon"])) / 1000,
        "burst_miss_km": (float(haversine(s["burst"]["lat"], s["burst"]["lon"],
                                          a["burst"]["lat"], a["burst"]["lon"])) / 1000
                          if np.isfinite(s["burst"]["lat"]) else None),
        "flight_time_err_min": (s["landing"]["time"] - a["landing"]["t"]) / 60,
    }


def _sim_mean(wf_list, p, start_alt=None):
    """Nominal flight for one model, or mean landing of an ensemble (one vectorized run)."""
    if len(wf_list) == 1:
        s = simulate(wf_list[0], p, Uncertainty(), start_alt=start_alt).summary()
        return s, s
    from .windfield import EnsembleWind
    n = len(wf_list)
    r = simulate(EnsembleWind(wf_list), p, Uncertainty(members=n - 1), start_alt=start_alt,
                 wind_index=np.arange(n), nominal_idx=np.arange(n))
    s = r.summary()
    s.pop("ensemble", None)
    ok = r.landed
    s["landing"].update(lat=float(r.lat[ok].mean()), lon=float(r.lon[ok].mean()), time=float(r.t[ok].mean()))
    s["burst"].update(lat=float(np.nanmean(r.burst_pt[:, 0])), lon=float(np.nanmean(r.burst_pt[:, 1])),
                      time=float(np.nanmean(r.t_burst)))
    return s, s


def _coverage(wfs, p, a, members=100):
    """Is the actual landing inside the predicted 95% ellipse? (Mahalanobis^2 <= 5.991)"""
    import math
    from .flight import EARTH_R
    if len(wfs) == 1:
        r = simulate(wfs[0], p, Uncertainty(members=members, seed=7))
    else:
        from .windfield import EnsembleWind
        per = max(10, members // len(wfs))
        which = np.repeat(np.arange(len(wfs)), per)
        r = simulate(EnsembleWind(wfs), p, Uncertainty(members=len(which) - 1, seed=7), wind_index=which)
    la, lo = r.lat[r.landed], r.lon[r.landed]
    if len(la) < 5:
        return None
    c0, c1 = la.mean(), lo.mean()
    k = EARTH_R * math.pi / 180
    X = np.column_stack([(lo - c1) * k * math.cos(math.radians(c0)), (la - c0) * k])
    cov = np.cov(X.T)
    d = np.array([(a["landing"]["lon"] - c1) * k * math.cos(math.radians(c0)), (a["landing"]["lat"] - c0) * k])
    m2 = float(d @ np.linalg.solve(cov, d))
    return {"mahalanobis2": m2, "inside95": m2 <= 5.991}


def evaluate(tr, data, modes=None, use_observed_params=True, nominal=None, tau_h=6.0,
             coverage_members=100):
    a = analyze(tr)
    L, B = a["launch"], a["burst"]
    res = {"flight": tr.name, "analysis": a, "modes": {}}
    obs = observed_winds(tr)
    modes = modes or available_choices(data)
    base = WindField(data, "gfs" if "gfs" in data else modes[0])
    for mode in modes:
        try:
            if mode == "gefs":
                wfs = [base.member(k) for k in gefs_members(data)]
            else:
                wfs = [base.member(mode)]
        except ValueError:
            continue
        runs = {}
        sets = {}
        if use_observed_params and np.isfinite(a["descent_rate_sl"]):
            sets["observed_params"] = dict(ascent_rate=a["ascent_rate"], burst_alt=B["alt"],
                                           descent_rate=a["descent_rate_sl"])
        if nominal:
            sets["nominal_params"] = nominal
        for lab, ps in sets.items():
            p = FlightParams(lat=L["lat"], lon=L["lon"], launch_time=L["t"], launch_alt=L["alt"], **ps)
            s, _ = _sim_mean(wfs, p)
            runs[lab] = {**_miss(s, a), "summary": s}
            if coverage_members:
                cv = _coverage(wfs, p, a, coverage_members)
                if cv:
                    runs[lab].update(cv)
        # descent-only from the observed burst point, with and without measured-wind correction
        if a["complete"] and np.isfinite(a["descent_rate_sl"]):
            p = FlightParams(lat=B["lat"], lon=B["lon"], launch_time=B["t"],
                             descent_rate=a["descent_rate_sl"], start_phase=1)
            s, _ = _sim_mean(wfs, p, start_alt=B["alt"])
            runs["from_burst"] = _miss(s, a)
            if obs is not None:
                up = obs["t"] <= B["t"]
                o = {k: v[up] for k, v in obs.items()}
                if len(o["t"]) >= 3:
                    for w in wfs:
                        w.apply_measured(o, tau_h=tau_h)
                    s, _ = _sim_mean(wfs, p, start_alt=B["alt"])
                    runs["from_burst_measured"] = _miss(s, a)
                    for w in wfs:
                        w.correction = None
        bands = []
        if obs is not None:
            tmin, tmax = wfs[0].time_range
            ok = (obs["t"] >= tmin) & (obs["t"] <= tmax)
            if ok.any():
                us, vs = zip(*[w.sample(obs["lat"][ok], obs["lon"][ok], obs["alt"][ok], obs["t"][ok])
                               for w in wfs])
                du = np.mean(us, axis=0) - obs["u"][ok]
                dv = np.mean(vs, axis=0) - obs["v"][ok]
                z = obs["alt"][ok]
                for lo, hi in BANDS:
                    m = (z >= lo) & (z < hi)
                    if m.sum() >= 3:
                        bands.append({"band_m": [lo, hi], "n": int(m.sum()),
                                      "rms_vec_err": float(np.sqrt(np.mean(du[m] ** 2 + dv[m] ** 2))),
                                      "bias_u": float(du[m].mean()), "bias_v": float(dv[m].mean())})
        res["modes"][mode] = {"runs": runs, "wind_error": bands}
    return res


def hindcast_file(path, pack_dir=None, lead_hours=0.0, tz_hours=0, nominal=None,
                  radius_km=400, cb=None, models=("hrrr", "rap", "gfs", "ecmwf", "gefs"),
                  gefs_members_n=10):
    from .paths import DATA_DIR
    pack_dir = pack_dir or os.path.join(DATA_DIR, "hindcast")
    tr = load_track(path, default_tz_hours=tz_hours)
    a = analyze(tr)
    L = a["launch"]
    start = dt.datetime.fromtimestamp(L["t"], dt.timezone.utc)
    hours = (a["landing"]["t"] - L["t"]) / 3600 + 0.5
    pack = os.path.join(pack_dir, os.path.splitext(tr.name)[0] + f"_lead{lead_hours:g}.npz")
    if not os.path.exists(pack):
        now = start - dt.timedelta(hours=lead_hours) if lead_hours else start + dt.timedelta(hours=hours)
        build_pack(L["lat"], L["lon"], start, hours=max(hours, 2), models=models,
                   radius_km=radius_km, out_path=pack, cb=cb, now=now, gefs_members=gefs_members_n)
    data, meta = load_pack(pack)
    r = evaluate(tr, data, nominal=nominal)
    r["pack"] = pack
    r["cycles"] = meta.get("cycles")
    r["lead_hours"] = lead_hours
    save_result(r)
    return r


def save_result(r):
    from .paths import DATA_DIR
    d = os.path.join(DATA_DIR, "hindcast_results")
    os.makedirs(d, exist_ok=True)
    slim = json.loads(json.dumps(r, default=float))
    for m in slim["modes"].values():
        for run in m["runs"].values():
            run.pop("summary", None)
    name = os.path.splitext(r["flight"])[0] + f"_lead{r.get('lead_hours', 0):g}.json"
    with open(os.path.join(d, name), "w") as f:
        json.dump(slim, f, indent=1)


def load_results():
    from .paths import DATA_DIR
    out = []
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "hindcast_results", "*.json"))):
        with open(p) as f:
            out.append(json.load(f))
    return out


def calibrate(results=None):
    """Summarize all hindcasts: your balloons' real ascent/burst/descent and each model's error.
    The result feeds the 'My flights' profile and suggested Monte Carlo settings."""
    results = results if results is not None else load_results()
    if not results:
        return None
    A = [r["analysis"] for r in results]
    asc = np.array([a["ascent_rate"] for a in A])
    burst = np.array([a["burst"]["alt"] for a in A])
    desc = np.array([a["descent_rate_sl"] for a in A if np.isfinite(a["descent_rate_sl"])])
    prof = {"flights": len(A),
            "ascent_rate": {"mean": float(asc.mean()), "sd": float(asc.std(ddof=1)) if len(asc) > 1 else None},
            "burst_alt": {"mean": float(burst.mean()), "sd": float(burst.std(ddof=1)) if len(burst) > 1 else None},
            "descent_rate_sl": {"mean": float(desc.mean()) if len(desc) else None,
                                "sd": float(desc.std(ddof=1)) if len(desc) > 1 else None}}
    models, cover = {}, {}
    for r in results:
        for mode, m in r["modes"].items():
            for lab, run in m["runs"].items():
                models.setdefault(mode, {}).setdefault(lab, []).append(run["landing_miss_km"])
                if "inside95" in run:
                    cover.setdefault(mode, {}).setdefault(lab, []).append(bool(run["inside95"]))
    prof["miss_km"] = {mode: {lab: {"n": len(v), "median": float(np.median(v)), "mean": float(np.mean(v)),
                                    "p90": float(np.percentile(v, 90))} for lab, v in d.items()}
                       for mode, d in models.items()}
    # fraction of real landings inside the predicted 95% area: ~0.95 means the spread is honest,
    # much lower means the Monte Carlo sigmas are too small
    prof["coverage95"] = {mode: {lab: float(np.mean(v)) for lab, v in d.items()} for mode, d in cover.items()}
    sug = {}
    if prof["ascent_rate"]["sd"]:
        sug["ascent_pct"] = round(100 * prof["ascent_rate"]["sd"] / prof["ascent_rate"]["mean"], 1)
    if prof["burst_alt"]["sd"]:
        sug["burst_pct"] = round(100 * prof["burst_alt"]["sd"] / prof["burst_alt"]["mean"], 1)
    if prof["descent_rate_sl"]["sd"]:
        sug["descent_pct"] = round(100 * prof["descent_rate_sl"]["sd"] / prof["descent_rate_sl"]["mean"], 1)
    prof["suggested_uncertainty"] = sug
    return prof


def write_report(results, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["flight", "launch_utc", "lead_h", "ascent_mps", "burst_m", "descent_sl_mps", "model",
                    "test", "landing_miss_km", "burst_miss_km", "time_err_min", "inside95"])
        for r in results:
            a = r["analysis"]
            lt = dt.datetime.fromtimestamp(a["launch"]["t"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
            for mode, m in r["modes"].items():
                for lab, run in m["runs"].items():
                    bm = run.get("burst_miss_km")
                    w.writerow([r["flight"], lt, r.get("lead_hours", 0), f"{a['ascent_rate']:.2f}",
                                f"{a['burst']['alt']:.0f}", f"{a['descent_rate_sl']:.2f}", mode, lab,
                                f"{run['landing_miss_km']:.2f}", "" if bm is None else f"{bm:.2f}",
                                f"{run['flight_time_err_min']:.1f}", run.get("inside95", "")])


def print_result(r):
    a = r["analysis"]
    print(f"\n=== {r['flight']} ===")
    print(f"  launch {dt.datetime.fromtimestamp(a['launch']['t'], dt.timezone.utc):%Y-%m-%d %H:%MZ} "
          f"at {a['launch']['lat']:.4f}, {a['launch']['lon']:.4f}  ({a['points']} fixes)")
    print(f"  observed: ascent {a['ascent_rate']:.2f} m/s, burst {a['burst']['alt']:.0f} m "
          f"({a['burst']['alt']/0.3048:,.0f} ft), descent {a['descent_rate_sl']:.2f} m/s at sea level")
    if not a["complete"]:
        print("  WARNING: track looks incomplete (no low-altitude fixes after burst)")
    for mode, m in r["modes"].items():
        for lab, run in m["runs"].items():
            print(f"  {mode:12s} {lab:20s} landing miss {run['landing_miss_km']:6.2f} km | "
                  f"time err {run['flight_time_err_min']:+.1f} min")
        if m["wind_error"]:
            s = ", ".join(f"{b['band_m'][0]/1000:.0f}-{b['band_m'][1]/1000:.0f}km {b['rms_vec_err']:.1f}"
                          for b in m["wind_error"])
            print(f"        wind RMS err (m/s): {s}")
