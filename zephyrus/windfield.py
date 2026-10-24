"""Wind field lookup from a wind pack. Vectorized over N points.

A WindField has a *primary* source (HRRR, RAP, ECMWF, a GEFS member, a sounding, ...) and a
*top-up* source (GFS) used above the primary's highest level and wherever the primary has no data
(outside its domain or time window). Between the two there is a linear blend zone.
"""
from __future__ import annotations

import numpy as np

from .sources import load_pack, MODELS


class GridSource:
    def __init__(self, d, name):
        self.name = name
        self.t = np.asarray(d["times"], float)
        self.lat = np.asarray(d["lat"], float)
        self.lon = np.asarray(d["lon"], float)
        self.z = np.asarray(d["z"], np.float32)
        self.u = np.asarray(d["u"], np.float32)
        self.v = np.asarray(d["v"], np.float32)
        self.terrain_grid = np.asarray(d["terrain"], np.float32)
        self.ok2d = np.isfinite(self.z[0, 0])
        if not self.ok2d.all():  # fill NaN edges so interpolation never returns NaN
            for arr in (self.z, self.u, self.v):
                m = ~np.isfinite(arr)
                arr[m] = 0.0
        top = self.z[:, -1][:, self.ok2d] if self.ok2d.any() else self.z[:, -1]
        self.top_z = float(np.min(top))           # lowest altitude of the top level
        self.levels = np.asarray(d.get("levels", []), float)
        # [T, Y, X, L, 3] with z/u/v together: one contiguous gather per grid corner
        self.zuv = np.ascontiguousarray(np.stack([self.z, self.u, self.v], -1).transpose(0, 2, 3, 1, 4))

    def covers(self, lat, lon, t):
        lat, lon, t = np.atleast_1d(lat), np.atleast_1d(lon), np.atleast_1d(t)
        inside = ((lat >= self.lat[0]) & (lat <= self.lat[-1]) &
                  (lon >= self.lon[0]) & (lon <= self.lon[-1]) &
                  (t >= self.t[0] - 1) & (t <= self.t[-1] + 1))
        if inside.any() and not self.ok2d.all():
            iy, ix, _, _ = self._hidx(lat, lon)
            inside &= (self.ok2d[iy, ix] & self.ok2d[iy + 1, ix + 1] &
                       self.ok2d[iy, ix + 1] & self.ok2d[iy + 1, ix])
        return inside

    def _hidx(self, lat, lon):
        fy = np.interp(lat, self.lat, np.arange(len(self.lat)))
        fx = np.interp(lon, self.lon, np.arange(len(self.lon)))
        iy = np.clip(fy.astype(int), 0, len(self.lat) - 2)
        ix = np.clip(fx.astype(int), 0, len(self.lon) - 2)
        return iy, ix, fy - iy, fx - ix

    def _tidx(self, t):
        if len(self.t) == 1:
            z = np.zeros_like(t, dtype=int)
            return z, z, np.zeros_like(t, dtype=float)
        ft = np.interp(t, self.t, np.arange(len(self.t)))
        it = np.clip(ft.astype(int), 0, len(self.t) - 2)
        return it, it + 1, ft - it

    def sample(self, lat, lon, alt, t, stack=None, which=None):
        """u, v at N points. With `stack` [M,T,Y,X,L,3] and `which` [N], point i uses member which[i]."""
        lat, lon, alt, t = (np.atleast_1d(np.asarray(x, float)) for x in (lat, lon, alt, t))
        iy, ix, wy, wx = self._hidx(lat, lon)
        t0, t1, wt = self._tidx(t)
        a = self.zuv if stack is None else stack
        if stack is not None:
            iy, ix = (which, iy), (which, ix)  # prepend member index to every gather below
        f4 = np.float32
        w00, w01 = ((1 - wy) * (1 - wx)).astype(f4)[:, None, None], ((1 - wy) * wx).astype(f4)[:, None, None]
        w10, w11 = (wy * (1 - wx)).astype(f4)[:, None, None], (wy * wx).astype(f4)[:, None, None]
        out = 0.0
        for ti, w in ((t0, 1 - wt), (t1, wt)):
            if stack is None:
                c = (a[ti, iy, ix] * w00 + a[ti, iy, ix + 1] * w01 +
                     a[ti, iy + 1, ix] * w10 + a[ti, iy + 1, ix + 1] * w11)    # [N, L, 3]
            else:
                m, y = iy
                x = ix[1]
                c = (a[m, ti, y, x] * w00 + a[m, ti, y, x + 1] * w01 +
                     a[m, ti, y + 1, x] * w10 + a[m, ti, y + 1, x + 1] * w11)
            u, v = _vinterp(c[..., 0].T, c[..., 1].T, c[..., 2].T, alt)
            out = out + w[:, None] * np.stack([u, v], 1)
        return out[:, 0], out[:, 1]

    def terrain(self, lat, lon):
        lat, lon = np.atleast_1d(lat), np.atleast_1d(lon)
        iy, ix, wy, wx = self._hidx(lat, lon)
        g = self.terrain_grid
        return (g[iy, ix] * (1 - wy) * (1 - wx) + g[iy, ix + 1] * (1 - wy) * wx +
                g[iy + 1, ix] * wy * (1 - wx) + g[iy + 1, ix + 1] * wy * wx)


def _vinterp(Z, U, V, alt):
    """Z,U,V [L,N] with Z increasing along L; linear in height, clamped at ends."""
    L, N = Z.shape
    k = np.sum(Z < alt[None, :], axis=0)
    k = np.clip(k, 1, L - 1)
    n = np.arange(N)
    z0, z1 = Z[k - 1, n], Z[k, n]
    w = np.clip((alt - z0) / np.maximum(z1 - z0, 1e-3), 0.0, 1.0)
    return U[k - 1, n] * (1 - w) + U[k, n] * w, V[k - 1, n] * (1 - w) + V[k, n] * w


# Model choices offered to the user. key -> (label, primary source key or None for GFS alone)
CHOICES = {
    "blend": "HRRR 3 km + GFS above",
    "rap": "RAP 13 km + GFS above",
    "ecmwf": "ECMWF IFS + GFS above",
    "gfs": "GFS 0.25° only",
    "gefs": "GEFS ensemble",
    "raob": "Radiosonde + GFS above",
}


def available_choices(data):
    keys = set(data)
    out = []
    if "gfs" in keys:
        out.append("gfs")
    if "hrrr" in keys:
        out.insert(0, "blend")
    for k in ("rap", "ecmwf"):
        if k in keys:
            out.append(k)
    if any(k.startswith("gefs-") for k in keys):
        out.append("gefs")
    out += sorted(k for k in keys if k.startswith("raob-"))
    return out


def gefs_members(data):
    return sorted(k for k in data if k.startswith("gefs-"))


class WindField:
    def __init__(self, data, mode="blend", blend_lo=None, blend_hi=None, dem=True):
        self.data = data
        self.sources = {k: GridSource(v, k) for k, v in data.items() if k != "dem"}
        self.mode = mode
        prim = {"blend": "hrrr", "hrrr": "hrrr"}.get(mode, mode)
        if prim == "gefs":
            mem = gefs_members(data)
            if not mem:
                raise ValueError("pack has no GEFS members")
            prim = mem[0]
        if prim not in self.sources:
            if mode == "blend" and "gfs" in self.sources:
                prim = "gfs"  # outside HRRR coverage: fall back
            else:
                raise ValueError(f"pack has no {mode} data")
        self.primary = self.sources[prim]
        self.topup = None
        if prim != "gfs" and not prim.startswith("gefs-") and "gfs" in self.sources:
            self.topup = self.sources["gfs"]
        top = self.primary.top_z
        hi = blend_hi if blend_hi is not None else top - 500
        if prim == "hrrr" and blend_hi is None:
            hi = min(hi, 18000.0)
        self.blend_hi = hi
        self.blend_lo = blend_lo if blend_lo is not None else hi - 3000
        self.meta = {}
        self.correction = None
        self._dem = None
        if dem and "dem" in data:
            from .dem import DemTerrain
            self._dem = DemTerrain([data["dem"]])

    @classmethod
    def from_file(cls, path, mode="blend", **kw):
        data, meta = load_pack(path)
        wf = cls(data, mode, **kw)
        wf.meta = meta
        return wf

    @classmethod
    def open(cls, path):
        """Load a pack with a neutral base (GFS if present); use .member(key) per model."""
        data, meta = load_pack(path)
        wf = cls(data, "gfs" if "gfs" in data else available_choices(data)[0])
        wf.meta = meta
        return wf

    def member(self, key):
        """Same pack, different primary (e.g. a GEFS member)."""
        wf = WindField(self.data, key, dem=False)
        wf._dem, wf.meta, wf.correction = self._dem, self.meta, self.correction
        return wf

    @property
    def time_range(self):
        s = self.topup or self.primary
        return float(s.t[0]), float(s.t[-1])

    def sample(self, lat, lon, alt, t):
        lat, lon, alt, t = (np.atleast_1d(np.asarray(x, float)) for x in (lat, lon, alt, t))
        p, g = self.primary, self.topup
        if g is None:
            u, v = p.sample(lat, lon, alt, t)
        else:
            wp = np.clip((self.blend_hi - alt) / max(self.blend_hi - self.blend_lo, 1), 0, 1)
            wp = wp * p.covers(lat, lon, t)
            if not wp.any():
                u, v = g.sample(lat, lon, alt, t)
            elif (wp == 1).all():
                u, v = p.sample(lat, lon, alt, t)
            else:
                ug, vg = g.sample(lat, lon, alt, t)
                up, vp = p.sample(lat, lon, alt, t)
                u, v = wp * up + (1 - wp) * ug, wp * vp + (1 - wp) * vg
        if self.correction is not None:
            du, dv = self.correction(alt, t)
            u, v = u + du, v + dv
        return u, v

    # ------------------------------------------------------------------ terrain
    def add_dem(self, mosaic):
        from .dem import DemTerrain
        if self._dem is None:
            self._dem = DemTerrain([mosaic])
        else:
            self._dem.add(mosaic)

    def terrain(self, lat, lon):
        lat, lon = np.atleast_1d(np.asarray(lat, float)), np.atleast_1d(np.asarray(lon, float))
        out = self._dem(lat, lon) if self._dem is not None else np.full(lat.shape, np.nan)
        for key in ("hrrr", "rap", "gfs", "ecmwf"):
            if not np.isnan(out).any():
                break
            s = self.sources.get(key)
            if s is None or not np.isfinite(s.terrain_grid).any():
                continue
            inside = s.covers(lat, lon, s.t[0])
            val = np.where(inside, s.terrain(lat, lon), np.nan)
            out = np.where(np.isnan(out), val, out)
        return np.nan_to_num(out, nan=0.0)

    # ------------------------------------------------------------------ measured-wind correction
    def apply_measured(self, obs, tau_h=6.0, bin_m=500.0, min_pts=2):
        """Correct this field with winds measured by the balloon itself during ascent.

        obs: dict of arrays t, lat, lon, alt, u, v (see tracks.observed_winds). The model error
        in each altitude bin (observed minus model) is added back to the model wind at that
        altitude, fading with time since it was measured (e-folding tau_h hours)."""
        saved = self.correction
        self.correction = None
        um, vm = self.sample(obs["lat"], obs["lon"], obs["alt"], obs["t"])
        self.correction = saved
        du, dv = obs["u"] - um, obs["v"] - vm
        edges = np.arange(0, 45000 + bin_m, bin_m)
        idx = np.digitize(obs["alt"], edges) - 1
        zc, bu, bv, bt = [], [], [], []
        for b in np.unique(idx):
            m = idx == b
            if m.sum() < min_pts:
                continue
            zc.append(obs["alt"][m].mean())
            bu.append(np.median(du[m]))
            bv.append(np.median(dv[m]))
            bt.append(obs["t"][m].mean())
        if not zc:
            return 0
        zc, bu, bv, bt = map(np.array, (zc, bu, bv, bt))
        o = np.argsort(zc)
        zc, bu, bv, bt = zc[o], bu[o], bv[o], bt[o]
        zlo, zhi = zc[0], zc[-1]

        def corr(alt, t):
            w = np.exp(-np.abs(t - np.interp(alt, zc, bt)) / (tau_h * 3600))
            w = w * ((alt >= zlo - bin_m) & (alt <= zhi + bin_m))
            return w * np.interp(alt, zc, bu), w * np.interp(alt, zc, bv)

        self.correction = corr
        return len(zc)


class EnsembleWind:
    """Several WindFields (e.g. GEFS members) flown in ONE vectorized run.
    sample(..., which=k) gives each point the winds of member k."""

    def __init__(self, wfs):
        self.wfs = list(wfs)
        self.base = self.wfs[0]
        srcs = [w.primary for w in self.wfs]
        g0 = srcs[0]
        same = all(w.topup is None for w in self.wfs) and all(
            s.zuv.shape == g0.zuv.shape and np.array_equal(s.t, g0.t) and
            np.array_equal(s.lat, g0.lat) and np.array_equal(s.lon, g0.lon) for s in srcs)
        self.g0 = g0
        self.stack = np.stack([s.zuv for s in srcs]) if same else None

    @property
    def time_range(self):
        return self.base.time_range

    def terrain(self, lat, lon):
        return self.base.terrain(lat, lon)

    def sample(self, lat, lon, alt, t, which=None):
        lat, lon, alt, t = (np.atleast_1d(np.asarray(x, float)) for x in (lat, lon, alt, t))
        which = np.zeros(len(lat), int) if which is None else np.asarray(which, int)
        if self.stack is not None:
            u, v = self.g0.sample(lat, lon, alt, t, stack=self.stack, which=which)
        else:  # members on different grids: fall back to one call per member
            u, v = np.zeros(len(lat)), np.zeros(len(lat))
            for k in np.unique(which):
                m = which == k
                u[m], v[m] = self.wfs[k].sample(lat[m], lon[m], alt[m], t[m])
            return u, v
        for k, w in enumerate(self.wfs):
            if w.correction is not None:
                m = which == k
                if m.any():
                    du, dv = w.correction(alt[m], t[m])
                    u[m] += du
                    v[m] += dv
        return u, v

    def apply_measured(self, obs, **kw):
        return max(w.apply_measured(obs, **kw) for w in self.wfs)

    def clear_measured(self):
        for w in self.wfs:
            w.correction = None
