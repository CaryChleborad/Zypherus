"""US Standard Atmosphere 1976 (0-86 km): density, pressure, temperature vs geometric-ish altitude."""
import numpy as np

R_AIR = 287.05287
G0 = 9.80665
RHO0 = 1.225
# (base geopotential height m, base temp K, lapse K/m, base pressure Pa)
_LAYERS = [
    (0.0, 288.15, -0.0065, 101325.0),
    (11000.0, 216.65, 0.0, 22632.06),
    (20000.0, 216.65, 0.001, 5474.889),
    (32000.0, 228.65, 0.0028, 868.0187),
    (47000.0, 270.65, 0.0, 110.9063),
    (51000.0, 270.65, -0.0028, 66.93887),
    (71000.0, 214.65, -0.002, 3.956420),
]


def _layer(h):
    h = np.asarray(h, dtype=float)
    idx = np.searchsorted([l[0] for l in _LAYERS], h, side="right") - 1
    return np.clip(idx, 0, len(_LAYERS) - 1)


def temp_pressure(h):
    h = np.asarray(h, dtype=float)
    idx = _layer(h)
    hb = np.array([l[0] for l in _LAYERS])[idx]
    tb = np.array([l[1] for l in _LAYERS])[idx]
    lr = np.array([l[2] for l in _LAYERS])[idx]
    pb = np.array([l[3] for l in _LAYERS])[idx]
    t = tb + lr * (h - hb)
    with np.errstate(divide="ignore", invalid="ignore"):
        p_grad = pb * (tb / t) ** (G0 / (R_AIR * np.where(lr == 0, 1, lr)))
        p_iso = pb * np.exp(-G0 * (h - hb) / (R_AIR * tb))
    p = np.where(lr == 0, p_iso, p_grad)
    return t, p


def density(h):
    t, p = temp_pressure(h)
    return p / (R_AIR * t)


def pressure_to_altitude(p_pa):
    """Inverse of the standard atmosphere (numeric)."""
    grid = np.linspace(-500, 85000, 20000)
    _, pg = temp_pressure(grid)
    return np.interp(-np.asarray(p_pa, float), -pg, grid)


_TAB_H = np.arange(-1000.0, 90000.0, 10.0)
_TAB_RHO = None


def density_fast(h):
    """Table lookup version of density() for the integrator (10 m steps, <0.01% error)."""
    global _TAB_RHO
    if _TAB_RHO is None:
        _TAB_RHO = density(_TAB_H)
    return np.interp(h, _TAB_H, _TAB_RHO)
