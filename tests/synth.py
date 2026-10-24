"""Synthetic wind packs for testing (no network)."""
import numpy as np

from zephyrus.atmosphere import pressure_to_altitude


def make_source(lat0=39.5, lon0=-118.5, t0=1.8e9, hours=6, step=0.25, half=3.0,
                ufunc=None, vfunc=None, terrain=1200.0, levels=None):
    levels = np.array(levels or [1000, 900, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100,
                                 70, 50, 30, 20, 10, 7, 5, 3], np.float32)
    lat = np.arange(lat0 - half, lat0 + half + 1e-9, step)
    lon = np.arange(lon0 - half, lon0 + half + 1e-9, step)
    times = t0 + 3600.0 * np.arange(hours + 1)
    zl = pressure_to_altitude(levels * 100.0)
    T, L, Y, X = len(times), len(levels), len(lat), len(lon)
    z = np.broadcast_to(zl[None, :, None, None], (T, L, Y, X)).astype(np.float32).copy()
    ufunc = ufunc or (lambda z, la, lo, t: 10.0 + 0 * z)
    vfunc = vfunc or (lambda z, la, lo, t: 0 * z)
    TT, ZZ, LA, LO = np.meshgrid(times, zl, lat, lon, indexing="ij")
    u = ufunc(ZZ, LA, LO, TT).astype(np.float32)
    v = vfunc(ZZ, LA, LO, TT).astype(np.float32)
    ter = np.full((Y, X), terrain, np.float32) if np.isscalar(terrain) else terrain
    return dict(times=times, levels=levels, lat=lat, lon=lon, z=z, u=u, v=v, terrain=ter)
