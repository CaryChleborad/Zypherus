"""Latex balloon physics: ascent rate vs altitude and burst altitude from balloon + gas + lift.

Method (as in the CUSF burst calculator and Sobester et al. 2014):
  launch volume   V0 = (neck_lift + m_balloon) / (rho_air0 - rho_gas0)
  free lift       F  = (neck_lift - m_payload) * g                     (constant with altitude)
  volume aloft    V(h) = V0 * (P0 / P(h)) * (T(h) / T0)                  (ideal gas, no superpressure)
  ascent rate     w(h) = sqrt(2 F / (rho(h) * Cd * A(h))),  A = pi r^2
  burst           when 2 r(h) >= burst diameter

Ascent profile: with a constant Cd the formula makes the balloon speed up about 2x by 30 km
(w ~ rho^-1/6). Real latex balloons climb at a nearly steady rate because drag changes with
Reynolds number and shape (the reason ASTRA uses an empirical drag model). So the default
profile holds the computed launch rate constant (as the CUSF burst calculator does); "drag"
keeps the constant-Cd theory for comparison. Hindcasts of your own tracks settle which fits.
"""
from __future__ import annotations

import math

import numpy as np

from .atmosphere import temp_pressure, density

R_UNIV = 8.314462618
M_AIR = 0.0289644
M_GAS = {"he": 0.0040026, "h2": 0.002016}
G0 = 9.80665

# Nominal manufacturer figures as tabulated by the CUSF burst calculator:
# name: (mass g, burst diameter m, ascent drag coefficient). Check your balloon's spec sheet.
BALLOONS = {
    "Totex/Kaymont 200": (200, 3.00, 0.25), "Totex/Kaymont 300": (300, 3.78, 0.25),
    "Totex/Kaymont 350": (350, 4.12, 0.25), "Totex/Kaymont 450": (450, 4.72, 0.25),
    "Totex/Kaymont 500": (500, 4.99, 0.25), "Totex/Kaymont 600": (600, 6.02, 0.30),
    "Totex/Kaymont 700": (700, 6.53, 0.30), "Totex/Kaymont 800": (800, 7.00, 0.30),
    "Totex/Kaymont 1000": (1000, 7.86, 0.30), "Totex/Kaymont 1200": (1200, 8.63, 0.25),
    "Totex/Kaymont 1500": (1500, 9.44, 0.25), "Totex/Kaymont 2000": (2000, 10.54, 0.25),
    "Totex/Kaymont 3000": (3000, 13.00, 0.25),
    "Hwoyee 200": (200, 3.00, 0.25), "Hwoyee 300": (300, 3.80, 0.25),
    "Hwoyee 500": (500, 5.00, 0.25), "Hwoyee 600": (600, 5.80, 0.30),
    "Hwoyee 800": (800, 6.80, 0.30), "Hwoyee 1000": (1000, 7.50, 0.30),
    "Hwoyee 1200": (1200, 8.50, 0.25), "Hwoyee 1600": (1600, 10.50, 0.25),
    "Hwoyee 2000": (2000, 11.00, 0.25), "Hwoyee 3000": (3000, 12.50, 0.25),
}

ALT_GRID = np.arange(-500.0, 50000.0, 25.0)


def weibull_unit_mean(k, size, rng):
    """Weibull samples scaled to mean 1 (Renegar: burst diameter shape k = 14.4)."""
    lam = 1.0 / math.gamma(1 + 1 / k)
    return lam * rng.weibull(k, size)


def ascent_tables(balloon_g, burst_d, cd, payload_g, neck_lift_g, gas="he", launch_alt=0.0,
                  burst_scale=None, cd_scale=None, lift_scale=None, profile="constant"):
    """Vectorized over members (scales are arrays of length N, default 1).
    Returns (rate[N, G] m/s on ALT_GRID, burst_alt[N] m)."""
    burst_scale = np.atleast_1d(1.0 if burst_scale is None else burst_scale).astype(float)
    n = len(burst_scale)
    cd_scale = np.ones(n) if cd_scale is None else np.atleast_1d(cd_scale)
    lift_scale = np.ones(n) if lift_scale is None else np.atleast_1d(lift_scale)
    t0, p0 = temp_pressure(launch_alt)
    mg = M_GAS[gas]
    rho_a0 = p0 * M_AIR / (R_UNIV * t0)
    rho_g0 = p0 * mg / (R_UNIV * t0)
    neck = neck_lift_g / 1000 * lift_scale
    v0 = (neck + balloon_g / 1000) / (rho_a0 - rho_g0)                       # [N]
    free = np.maximum(neck - payload_g / 1000, 1e-4) * G0                    # [N]
    T, P = temp_pressure(ALT_GRID)
    rho = density(ALT_GRID)
    V = v0[:, None] * (p0 / P)[None, :] * (T / t0)[None, :]                  # [N, G]
    r = (3 * V / (4 * math.pi)) ** (1 / 3)
    A = math.pi * r ** 2
    rate = np.sqrt(2 * free[:, None] / (rho[None, :] * cd * cd_scale[:, None] * A))
    if profile == "constant":
        k0 = int(np.clip(np.searchsorted(ALT_GRID, launch_alt), 0, len(ALT_GRID) - 1))
        rate = np.repeat(rate[:, k0:k0 + 1], len(ALT_GRID), axis=1)
    d_burst = burst_d * burst_scale
    burst_alt = np.empty(n)
    for i in range(n):
        k = np.searchsorted(2 * r[i], d_burst[i])
        burst_alt[i] = ALT_GRID[min(k, len(ALT_GRID) - 1)]
    return rate, burst_alt


def summary(balloon_g, burst_d, cd, payload_g, neck_lift_g, gas="he", launch_alt=0.0, profile="constant"):
    rate, burst = ascent_tables(balloon_g, burst_d, cd, payload_g, neck_lift_g, gas, launch_alt,
                                profile=profile)
    k0 = np.searchsorted(ALT_GRID, launch_alt)
    kb = np.searchsorted(ALT_GRID, burst[0])
    seg = rate[0, k0:kb]
    t_up = float(np.sum(25.0 / seg)) if len(seg) else float("nan")
    return {"burst_alt": float(burst[0]), "launch_rate": float(rate[0, k0]),
            "mean_rate": (burst[0] - launch_alt) / t_up if t_up else float("nan"),
            "ascent_min": t_up / 60, "free_lift_g": neck_lift_g - payload_g}


def neck_lift_for_rate(target_rate, balloon_g, burst_d, cd, payload_g, gas="he", launch_alt=0.0):
    """Neck lift (g) giving `target_rate` m/s at launch (bisection)."""
    lo, hi = payload_g + 1, payload_g + 20000
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        rate, _ = ascent_tables(balloon_g, burst_d, cd, payload_g, mid, gas, launch_alt)
        if rate[0, np.searchsorted(ALT_GRID, launch_alt)] < target_rate:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
