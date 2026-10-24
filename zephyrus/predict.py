"""High-level operations used by the web app and CLI: multi-model prediction, launch window,
reverse prediction (where to launch), and in-flight re-prediction."""
from __future__ import annotations

import datetime as dt
import json
import math
import os

import numpy as np

from .flight import FlightParams, Uncertainty, simulate, haversine, bearing, spread_stats, EARTH_R
from .windfield import WindField, EnsembleWind, CHOICES, available_choices, gefs_members

MODEL_COLORS = {"blend": "#d9480f", "rap": "#9c36b5", "ecmwf": "#1971c2", "gfs": "#2f9e44",
                "gefs": "#c2255c", "raob": "#0c8599"}


def label(key):
    if key.startswith("raob-"):
        from .sources import RAOB_STATIONS
        st = key.split("-", 1)[1]
        return f"Sounding {st} ({RAOB_STATIONS.get(st, (st,))[0]}) + GFS"
    return CHOICES.get(key, key)


def color(key):
    return MODEL_COLORS.get(key.split("-")[0] if key.startswith("raob") else key, "#495057")


def _maybe_refine_terrain(wf, lat, lon, offline, log=None):
    try:
        from .dem import refine_patch
        patch = refine_patch(lat, lon, radius_km=25, zoom=12, offline=offline)
        wf.add_dem(patch)
        return True
    except Exception as e:  # noqa
        if log:
            log(f"Fine terrain not available here ({e})")
        return False


def run_models(wf_base: WindField, p: FlightParams, unc: Uncertainty, models, refine=True,
               offline=False, gefs_mc_per_member=5, log=None, start_alt=None, measured=None,
               tau_h=6.0):
    """measured: observed wind dict (tracks.observed_winds) -> each model is corrected against
    its OWN error at the measured points before flying."""
    avail = available_choices(wf_base.data)
    models = [m for m in models if m in avail] or avail[:1]
    out = {"models": {}, "warnings": []}
    # pass 1: fine terrain around the first model's nominal landing
    if refine:
        first = wf_base.member(models[0] if models[0] != "gefs" else gefs_members(wf_base.data)[0])
        r0 = simulate(first, p, Uncertainty(), start_alt=start_alt)
        la, lo = r0.landing
        if _maybe_refine_terrain(wf_base, la, lo, offline, log):
            out["terrain"] = "fine (~30 m)"
    for m in models:
        if m == "gefs":
            out["models"]["gefs"] = _run_gefs(wf_base, p, unc, gefs_mc_per_member, start_alt,
                                              measured, tau_h)
            continue
        wf = wf_base.member(m)
        if measured is not None:
            out["measured_bins"] = wf.apply_measured(measured, tau_h=tau_h)
        r = simulate(wf, p, unc, start_alt=start_alt)
        s = r.summary()
        if s["wind_time_clamped"]:
            out["warnings"].append(f"{label(m)}: flight ran past the end of the wind data")
        out["models"][m] = {"label": label(m), "color": color(m), "summary": s, "track": r.track}
    out["consensus"] = consensus(out["models"])
    return out


def _run_gefs(wf_base, p, unc, mc_per_member, start_alt, measured=None, tau_h=6.0):
    """All GEFS members and their Monte Carlo runs in ONE vectorized flight."""
    mem = gefs_members(wf_base.data)
    ens = EnsembleWind([wf_base.member(k) for k in mem])
    if measured is not None:
        ens.apply_measured(measured, tau_h=tau_h)
    per = 1 + max(0, int(mc_per_member))
    which = np.repeat(np.arange(len(mem)), per)          # trajectory -> member
    nominal_idx = np.arange(len(mem)) * per               # first trajectory of each member: unperturbed
    u = Uncertainty(**{**unc.__dict__, "members": len(which) - 1})
    r = simulate(ens, p, u, start_alt=start_alt, wind_index=which, nominal_idx=nominal_idx,
                 track_idx=nominal_idx)
    lats, lons = list(r.lat[r.landed]), list(r.lon[r.landed])
    nominal = [(float(r.lat[i]), float(r.lon[i])) for i in nominal_idx]
    tracks = [t[::6] for t in r.tracks]
    nl = np.array(nominal)
    s = spread_stats(nl[:, 0], nl[:, 1])
    mean = s["mean"] if s else list(nominal[0])
    allspread = spread_stats(lats, lons, *mean)
    summ = {"landing": {"lat": mean[0], "lon": mean[1]}, "members": len(mem),
            "member_landings": [[float(a), float(b)] for a, b in nominal],
            "range_km": float(haversine(p.lat, p.lon, mean[0], mean[1])) / 1000,
            "bearing_deg": bearing(p.lat, p.lon, mean[0], mean[1]),
            "member_spread": s, "ensemble": allspread}
    return {"label": f"GEFS ensemble ({len(mem)} members)", "color": color("gefs"), "summary": summ,
            "member_tracks": tracks}


def consensus(models):
    pts = [(v["summary"]["landing"]["lat"], v["summary"]["landing"]["lon"]) for v in models.values()]
    if not pts:
        return None
    a = np.array(pts)
    c = a.mean(axis=0)
    d = [float(haversine(c[0], c[1], x, y)) / 1000 for x, y in pts]
    pair = max((float(haversine(*pts[i], *pts[j])) / 1000 for i in range(len(pts))
                for j in range(i + 1, len(pts))), default=0.0)
    return {"lat": float(c[0]), "lon": float(c[1]), "max_from_mean_km": max(d),
            "max_pairwise_km": pair, "n": len(pts)}


# ------------------------------------------------------------------ launch window
def launch_window(wf_base, p: FlightParams, model, t_start, t_end, step_h=1.0, target=None):
    """Nominal landing for each launch time in [t_start, t_end]."""
    wf = wf_base.member(model if model != "gefs" else gefs_members(wf_base.data)[0])
    tmin, tmax = wf.time_range
    rows = []
    t = t_start
    while t <= t_end + 1:
        if t < tmin - 1 or t > tmax:
            t += step_h * 3600
            continue
        q = FlightParams(**{**p.__dict__, "launch_time": t})
        r = simulate(wf, q)
        s = r.summary()
        row = {"time": t, "lat": s["landing"]["lat"], "lon": s["landing"]["lon"],
               "range_km": s["range_km"], "bearing_deg": s["bearing_deg"],
               "flight_min": s["ascent_min"] + s["descent_min"], "clamped": s["wind_time_clamped"]}
        if target:
            row["to_target_km"] = float(haversine(target[0], target[1], row["lat"], row["lon"])) / 1000
        rows.append(row)
        t += step_h * 3600
    best = min(rows, key=lambda r: r["to_target_km"]) if (target and rows) else None
    return {"rows": rows, "best": best, "model": model}


# ------------------------------------------------------------------ reverse prediction
def launch_site_for_target(wf_base, p: FlightParams, model, target, iters=8, tol_m=100):
    """Where to launch (at p.launch_time) so the nominal flight lands on `target` (lat, lon)."""
    wf = wf_base.member(model if model != "gefs" else gefs_members(wf_base.data)[0])
    la, lo = p.lat, p.lon
    hist = []
    for _ in range(iters):
        q = FlightParams(**{**p.__dict__, "lat": la, "lon": lo, "launch_alt": None})
        s = simulate(wf, q).summary()
        el, eo = s["landing"]["lat"], s["landing"]["lon"]
        miss = float(haversine(el, eo, *target))
        hist.append({"launch": [la, lo], "landing": [el, eo], "miss_m": miss})
        if miss < tol_m:
            break
        la += target[0] - el
        lo += target[1] - eo
    best = min(hist, key=lambda h: h["miss_m"])
    return {"launch": best["launch"], "landing": best["landing"], "miss_km": best["miss_m"] / 1000,
            "iterations": hist}


# ------------------------------------------------------------------ in-flight
def in_flight(wf_base, fixes, p: FlightParams, unc: Uncertainty, models, use_measured=True,
              tau_h=6.0, offline=False, refine=True, gefs_mc_per_member=5):
    """Re-predict from the latest telemetry fix.

    fixes: list of (t, lat, lon, alt). Ascent rate, descent rate and (while descending) burst are
    taken from the telemetry; measured ascent winds correct the model on the way down."""
    from .tracks import Track, observed_winds
    from .atmosphere import RHO0, density

    tr = Track(*zip(*fixes))
    t, la, lo, al = tr.t[-1], tr.lat[-1], tr.lon[-1], tr.alt[-1]
    imax = int(np.argmax(tr.alt))
    recent = tr.t >= t - 600
    rate = np.polyfit(tr.t[recent] - t, tr.alt[recent], 1)[0] if recent.sum() >= 3 else 0.0
    descending = (imax < len(tr) - 1 and al < tr.alt[imax] - 300) or rate < -2
    info = {"phase": "descent" if descending else "ascent", "alt": float(al), "time": float(t),
            "lat": float(la), "lon": float(lo), "max_alt": float(tr.alt[imax]),
            "vertical_rate": float(rate)}
    q = FlightParams(**{**p.__dict__, "lat": float(la), "lon": float(lo), "launch_time": float(t),
                        "start_phase": 1 if descending else 0})
    if descending:
        seg = (tr.t >= tr.t[imax]) & recent
        if seg.sum() >= 3:
            zm = tr.alt[seg].mean()
            v = -np.polyfit(tr.t[seg], tr.alt[seg], 1)[0]
            if v > 0.5:
                q.descent_rate = float(v / np.sqrt(RHO0 / density(zm)))
                info["descent_rate_sl"] = q.descent_rate
        info["burst_alt"] = float(tr.alt[imax])
    else:
        up = tr.alt[: imax + 1]
        if len(up) >= 5 and q.balloon is None:
            q.ascent_rate = float(max(np.polyfit(tr.t[: imax + 1], up, 1)[0], 0.5))
            info["ascent_rate"] = q.ascent_rate
        q.burst_alt = max(q.burst_alt, float(al) + 100)
    obs = observed_winds(tr, min_dt=20) if use_measured else None
    if obs is not None:
        asc = obs["t"] <= tr.t[imax]
        obs = {k: v[asc] for k, v in obs.items()}
        if len(obs["t"]) < 3:
            obs = None
    res = run_models(wf_base, q, unc, models, refine=refine, offline=offline, start_alt=float(al),
                     gefs_mc_per_member=gefs_mc_per_member, measured=obs, tau_h=tau_h)
    if "measured_bins" in res:
        info["measured_bins"] = res.pop("measured_bins")
    res["state"] = info
    return res


# ------------------------------------------------------------------ history
def log_prediction(params: dict, result: dict):
    from .paths import DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    rec = {"saved": dt.datetime.now(dt.timezone.utc).isoformat(), "params": params,
           "landings": {k: v["summary"]["landing"] for k, v in result.get("models", {}).items()},
           "consensus": result.get("consensus")}
    with open(os.path.join(DATA_DIR, "history.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
