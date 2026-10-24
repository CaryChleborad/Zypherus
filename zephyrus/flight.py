"""Balloon flight integrator, vectorized over Monte Carlo members.

Ascent:  constant rate (Tawhiri style)  OR  latex-balloon physics (balloon.py, ASTRA style)
Burst:   fixed altitude (+ normal spread)  OR  from burst diameter (+ Weibull spread, Renegar)
Descent: drag law v(h) = v0 * sqrt(rho0 / rho(h)), v0 = sea-level rate (given, or from chute)
Landing: first crossing of the terrain surface (high-res DEM when the pack has one)
Winds:   horizontal velocity = wind (balloon is a tracer), RK2 midpoint, dt = 10 s
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .atmosphere import RHO0, density, density_fast

EARTH_R = 6371008.8


@dataclass
class BalloonSpec:
    balloon_g: float
    burst_d: float            # m
    cd: float = 0.3           # ascent drag coefficient
    payload_g: float = 1000   # everything below the balloon (payload + chute + line)
    neck_lift_g: float = 1800
    gas: str = "he"
    profile: str = "constant"  # "constant" (launch rate held) or "drag" (constant-Cd theory)


@dataclass
class FlightParams:
    lat: float
    lon: float
    launch_time: float              # unix s UTC (or "now" for an in-flight start)
    ascent_rate: float = 5.0        # m/s (constant-ascent mode)
    burst_alt: float = 30480.0      # m MSL (constant-ascent mode)
    descent_rate: float = 6.0       # m/s at sea level
    launch_alt: float | None = None  # m MSL; None -> terrain
    dt: float = 10.0               # s; RK2 converges to <50 m vs 1 s steps (tests)
    balloon: BalloonSpec | None = None   # set -> physics ascent + burst
    descent_factor: float = 1.0     # calibration multiplier (Sirks et al. found 1.019)
    start_phase: int = 0            # 0 ascending, 1 descending (in-flight re-prediction)


@dataclass
class Uncertainty:
    members: int = 0                # 0 = deterministic only
    ascent_pct: float = 5.0         # 1-sigma (constant mode); physics mode uses lift_pct + cd_pct
    burst_pct: float = 5.0          # constant mode; physics mode uses Weibull k
    burst_weibull_k: float = 14.4   # Renegar: burst diameter ~ Weibull(k=14.4)
    lift_pct: float = 3.0           # neck-lift measurement error (physics mode)
    cd_pct: float = 7.5             # ascent drag coefficient factor, Renegar SD 0.075
    descent_pct: float = 5.0        # ballistic coefficient factor, Renegar SD 0.05
    wind_dir_deg: float = 5.0       # Renegar: wind direction SD 5 deg
    wind_speed_pct: float = 5.0     # Renegar: wind magnitude SD 0.05
    wind_bias: float = 0.0          # extra constant u/v offset SD, m/s
    seed: int = 1


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(np.asarray(lon2) - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def descent_speed(v_sl, alt):
    return v_sl * np.sqrt(RHO0 / density_fast(np.maximum(alt, -400)))


def descent_rate_from_chute(mass_kg, chute_diam_m, cd=1.5):
    """Sea-level terminal velocity for a payload under a round chute."""
    area = math.pi * (chute_diam_m / 2) ** 2
    return math.sqrt(2 * mass_kg * 9.80665 / (RHO0 * cd * area))


def simulate(wind, p: FlightParams, unc: Uncertainty | None = None, max_hours=8.0,
             start_alt=None, keep_tracks=0, wind_index=None, nominal_idx=None, track_idx=None):
    """Run the nominal flight (member 0) plus `unc.members` perturbed flights.

    Ensemble use (all GEFS members in one vectorized run): `wind` is an EnsembleWind,
    `wind_index[i]` picks the weather member for trajectory i, `nominal_idx` lists trajectories
    flown without Monte Carlo perturbation, `track_idx` lists trajectories whose path is kept."""
    unc = unc or Uncertainty()
    n = 1 + max(0, unc.members)
    rng = np.random.default_rng(unc.seed)
    nom = np.zeros(n, bool)
    nom[0] = True
    if nominal_idx is not None:
        nom[np.asarray(nominal_idx, int)] = True

    def factor(pct):
        f = np.ones(n)
        if n > 1 and pct:
            f[1:] = 1 + rng.normal(0, pct / 100, n - 1)
        f[nom] = 1.0
        return np.maximum(f, 0.2)

    ground0 = float(wind.terrain(p.lat, p.lon)[0])
    alt0 = start_alt if start_alt is not None else (p.launch_alt if p.launch_alt is not None else ground0)

    # ---- ascent + burst
    rate_tab = None
    if p.balloon is not None:
        from .balloon import ascent_tables, weibull_unit_mean
        b = p.balloon
        bscale = np.ones(n)
        if n > 1:
            bscale[1:] = weibull_unit_mean(unc.burst_weibull_k, n - 1, rng)
        bscale[nom] = 1.0
        rate_tab, burst = ascent_tables(b.balloon_g, b.burst_d, b.cd, b.payload_g, b.neck_lift_g,
                                        b.gas, p.launch_alt if p.launch_alt is not None else ground0,
                                        burst_scale=bscale, cd_scale=factor(unc.cd_pct),
                                        lift_scale=factor(unc.lift_pct), profile=b.profile)
        asc = None
    else:
        asc = np.maximum(p.ascent_rate * factor(unc.ascent_pct), 0.5)
        burst = p.burst_alt * factor(unc.burst_pct)
    burst = np.maximum(burst, alt0 + (1 if p.start_phase == 0 else -1e9))
    dsc = np.maximum(p.descent_rate * p.descent_factor * factor(unc.descent_pct), 0.5)

    # ---- wind perturbation (per member, constant over the flight)
    th = np.zeros(n)
    sc = np.ones(n)
    ub = np.zeros(n)
    vb = np.zeros(n)
    if n > 1:
        th[1:] = np.radians(rng.normal(0, unc.wind_dir_deg, n - 1))
        sc[1:] = 1 + rng.normal(0, unc.wind_speed_pct / 100, n - 1)
        if unc.wind_bias:
            ub[1:] = rng.normal(0, unc.wind_bias, n - 1)
            vb[1:] = rng.normal(0, unc.wind_bias, n - 1)
    th[nom], sc[nom], ub[nom], vb[nom] = 0.0, 1.0, 0.0, 0.0
    cth, sth = np.cos(th), np.sin(th)
    widx = None if wind_index is None else np.asarray(wind_index, int)

    lat = np.full(n, p.lat, float)
    lon = np.full(n, p.lon, float)
    alt = np.full(n, alt0, float)
    t = np.full(n, p.launch_time, float)
    phase = np.full(n, p.start_phase, int)
    t_burst = np.full(n, np.nan)
    burst_pt = np.full((n, 3), np.nan)
    if p.start_phase == 1:
        t_burst[:] = p.launch_time
        burst_pt[:] = [p.lat, p.lon, alt0]
    landed_alt = np.full(n, np.nan)
    tmin, tmax = wind.time_range
    clamped = False
    ground_ceiling = 5000.0  # m; no terrain lookups above this (highest Nevada peaks ~4,000 m)
    if track_idx is None:
        track_idx = list(range(max(min(keep_tracks, n), 1)))
    track_idx = [int(k) for k in track_idx]
    tracks = [[(p.launch_time, p.lat, p.lon, alt0, p.start_phase)] for _ in track_idx]
    dt = p.dt

    def vel(i, la, lo, al, tt):
        if widx is None:
            u, v = wind.sample(la, lo, al, np.clip(tt, tmin, tmax))
        else:
            u, v = wind.sample(la, lo, al, np.clip(tt, tmin, tmax), which=widx[i])
        uu = sc[i] * (u * cth[i] + v * sth[i]) + ub[i]
        vv = sc[i] * (-u * sth[i] + v * cth[i]) + vb[i]
        return uu, vv

    def wvert(i, al, ph):
        if rate_tab is not None:
            k = np.clip(((al - (-500.0)) / 25.0).astype(int), 0, rate_tab.shape[1] - 1)
            up = rate_tab[i, k]
        else:
            up = asc[i]
        return np.where(ph == 0, up, -descent_speed(dsc[i], al))

    for _ in range(int(max_hours * 3600 / dt)):
        act = phase < 2
        if not act.any():
            break
        i = np.where(act)[0]
        la, lo, al, tt, ph = lat[i], lon[i], alt[i], t[i], phase[i]
        if (tt > tmax).any() or (tt < tmin).any():
            clamped = True
        u1, v1 = vel(i, la, lo, al, tt)
        w1 = wvert(i, al, ph)
        lam = la + np.degrees(v1 * dt / 2 / EARTH_R)
        lom = lo + np.degrees(u1 * dt / 2 / (EARTH_R * np.cos(np.radians(la))))
        alm = al + w1 * dt / 2
        u2, v2 = vel(i, lam, lom, alm, tt + dt / 2)
        w2 = wvert(i, alm, ph)
        nla = la + np.degrees(v2 * dt / EARTH_R)
        nlo = lo + np.degrees(u2 * dt / (EARTH_R * np.cos(np.radians(lam))))
        nal = al + w2 * dt
        ntt = tt + dt
        ph = ph.copy()

        b = (ph == 0) & (nal >= burst[i])
        if b.any():
            f = (burst[i][b] - al[b]) / np.maximum(nal[b] - al[b], 1e-6)
            nla[b] = la[b] + f * (nla[b] - la[b])
            nlo[b] = lo[b] + f * (nlo[b] - lo[b])
            ntt[b] = tt[b] + f * dt
            nal[b] = burst[i][b]
            ph[b] = 1
            t_burst[i[b]] = ntt[b]
            burst_pt[i[b]] = np.column_stack([nla[b], nlo[b], nal[b]])

        d = (ph == 1) & (nal < ground_ceiling)
        if d.any():
            g = wind.terrain(nla[d], nlo[d])
            hit = nal[d] <= g
            if hit.any():
                di = np.where(d)[0][hit]
                gg = g[hit]
                f = np.clip((al[di] - gg) / np.maximum(al[di] - nal[di], 1e-6), 0, 1)
                nla[di] = la[di] + f * (nla[di] - la[di])
                nlo[di] = lo[di] + f * (nlo[di] - lo[di])
                ntt[di] = tt[di] + f * dt
                nal[di] = gg
                ph[di] = 2
                landed_alt[i[di]] = gg

        lat[i], lon[i], alt[i], t[i], phase[i] = nla, nlo, nal, ntt, ph
        for j, k in enumerate(track_idx):
            if act[k]:
                tracks[j].append((float(t[k]), float(lat[k]), float(lon[k]), float(alt[k]), int(phase[k])))

    return FlightResult(p, unc, tracks, lat, lon, t, phase, t_burst, burst_pt, landed_alt, clamped)


class FlightResult:
    def __init__(self, p, unc, tracks, lat, lon, t, phase, t_burst, burst_pt, land_alt, clamped):
        self.p, self.unc = p, unc
        self.tracks = tracks
        self.track = tracks[0]
        self.lat, self.lon, self.t = lat, lon, t
        self.landed = phase == 2
        self.t_burst, self.burst_pt, self.land_alt = t_burst, burst_pt, land_alt
        self.clamped = clamped

    @property
    def landing(self):
        return float(self.lat[0]), float(self.lon[0])

    def summary(self):
        p = self.p
        la, lo = self.landing
        out = {
            "launch": {"lat": p.lat, "lon": p.lon, "time": p.launch_time, "alt": self.track[0][3]},
            "burst": {"lat": float(self.burst_pt[0, 0]), "lon": float(self.burst_pt[0, 1]),
                      "alt": float(self.burst_pt[0, 2]), "time": float(self.t_burst[0])},
            "landing": {"lat": la, "lon": lo, "alt": float(self.land_alt[0]),
                        "time": float(self.t[0]), "landed": bool(self.landed[0])},
            "ascent_min": (self.t_burst[0] - p.launch_time) / 60,
            "descent_min": (self.t[0] - self.t_burst[0]) / 60,
            "range_km": float(haversine(p.lat, p.lon, la, lo)) / 1000,
            "bearing_deg": bearing(p.lat, p.lon, la, lo),
            "wind_time_clamped": self.clamped,
        }
        if len(self.lat) > 1:
            out["ensemble"] = spread_stats(self.lat[self.landed], self.lon[self.landed], la, lo)
        return out


def spread_stats(la, lo, ref_lat=None, ref_lon=None):
    """Mean, 95% covariance ellipse, and 50/95% radii of a cloud of landing points."""
    la, lo = np.asarray(la, float), np.asarray(lo, float)
    if len(la) < 3:
        return None
    clat, clon = float(np.mean(la)), float(np.mean(lo))
    ref_lat = clat if ref_lat is None else ref_lat
    ref_lon = clon if ref_lon is None else ref_lon
    x = np.radians(lo - clon) * EARTH_R * math.cos(math.radians(clat))
    y = np.radians(la - clat) * EARTH_R
    cov = np.cov(np.vstack([x, y]))
    evals, evecs = np.linalg.eigh(cov)
    evals = np.maximum(evals, 0)
    major = evecs[:, 1]
    orient = (math.degrees(math.atan2(major[0], major[1])) + 360) % 180
    r = haversine(la, lo, ref_lat, ref_lon)
    k95 = math.sqrt(5.991)
    return {
        "n": int(len(la)),
        "mean": [clat, clon],
        "points": [[round(float(a), 5), round(float(b), 5)] for a, b in zip(la, lo)],
        "ellipse95": {"a_m": k95 * math.sqrt(evals[1]), "b_m": k95 * math.sqrt(evals[0]),
                      "orient_deg": orient, "center": [clat, clon]},
        "sigma_km": math.sqrt(evals.sum() / 2) / 1000,
        "r50_km": float(np.percentile(r, 50)) / 1000,
        "r95_km": float(np.percentile(r, 95)) / 1000,
    }
