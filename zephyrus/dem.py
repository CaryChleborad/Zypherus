"""High-resolution terrain from the AWS Terrain Tiles (Terrarium PNG encoding).

Sources behind the tiles: USGS 3DEP (10 m in the US), SRTM 30 m, GMTED, ETOPO1.
Height (m) = R*256 + G + B/256 - 32768.  Tiles are cached under data/tiles/ for offline use.

Zoom -> pixel size at 40 deg N: z9 ~235 m, z10 ~117 m, z11 ~59 m, z12 ~29 m.
"""
from __future__ import annotations

import io
import math
import os

import numpy as np

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
    return x, y


def _cache_dir():
    from .paths import DATA_DIR
    d = os.path.join(DATA_DIR, "tiles")
    os.makedirs(d, exist_ok=True)
    return d


def get_tile(z, x, y, offline=False):
    from PIL import Image

    path = os.path.join(_cache_dir(), str(z), str(x), f"{y}.png")
    if os.path.exists(path):
        raw = open(path, "rb").read()
    else:
        if offline:
            return None
        from .sources import _get
        r = _get(TILE_URL.format(z=z, x=x, y=y), timeout=15, tries=1)
        if r is None:
            return None
        raw = r.content
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
    a = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.float32)
    return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768


def build_dem(box, zoom=9, cb=None, offline=False, max_tiles=600):
    """Mosaic tiles covering box -> dict(z=int16 heights, zoom, x0, y0) (Web Mercator pixels)."""
    from concurrent.futures import ThreadPoolExecutor
    from .sources import log

    s, n, w, e = box
    x0f, y0f = _tile_xy(n, w, zoom)
    x1f, y1f = _tile_xy(s, e, zoom)
    x0, y0, x1, y1 = int(x0f), int(y0f), int(x1f), int(y1f)
    count = (x1 - x0 + 1) * (y1 - y0 + 1)
    while count > max_tiles and zoom > 5:
        zoom -= 1
        return build_dem(box, zoom, cb, offline, max_tiles)
    log(f"Terrain: {count} tiles at zoom {zoom}", cb)
    mosaic = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.float32)
    jobs = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def one(xy):
        x, y = xy
        return xy, get_tile(zoom, x, y, offline)

    if not offline:  # one quick probe so an offline field laptop doesn't wait on hundreds of tiles
        try:
            if get_tile(zoom, *jobs[0]) is None and get_tile(zoom, *jobs[-1]) is None:
                offline = True
        except IOError:
            offline = True
    missing = 0
    with ThreadPoolExecutor(8) as ex:
        for (x, y), t in ex.map(one, jobs):
            if t is None:
                missing += 1
                mosaic[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = np.nan
            else:
                mosaic[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = t
    if missing == len(jobs):
        raise IOError("no terrain tiles available")
    z = np.where(np.isfinite(mosaic), np.round(mosaic), -32768).astype(np.int16)
    return {"z": z, "zoom": np.array(zoom), "x0": np.array(x0), "y0": np.array(y0)}


class DemTerrain:
    """Bilinear lookups in one or more Terrarium mosaics; the finest that covers a point wins."""

    def __init__(self, mosaics):
        self.m = sorted(mosaics, key=lambda d: -int(d["zoom"]))

    def add(self, d):
        self.m.append(d)
        self.m.sort(key=lambda d: -int(d["zoom"]))

    def __call__(self, lat, lon):
        lat = np.atleast_1d(np.asarray(lat, float))
        lon = np.atleast_1d(np.asarray(lon, float))
        out = np.full(lat.shape, np.nan)
        for d in self.m:
            todo = ~np.isfinite(out)
            if not todo.any():
                break
            zoom, x0, y0, Z = int(d["zoom"]), int(d["x0"]), int(d["y0"]), d["z"]
            nn = 2 ** zoom * 256
            px = (lon[todo] + 180.0) / 360.0 * nn - x0 * 256 - 0.5
            lr = np.radians(lat[todo])
            py = (1 - np.log(np.tan(lr) + 1 / np.cos(lr)) / np.pi) / 2 * nn - y0 * 256 - 0.5
            H, W = Z.shape
            ok = (px >= 0) & (py >= 0) & (px < W - 1) & (py < H - 1)
            ix = np.clip(px.astype(int), 0, W - 2)
            iy = np.clip(py.astype(int), 0, H - 2)
            fx, fy = px - ix, py - iy
            c = [Z[iy, ix], Z[iy, ix + 1], Z[iy + 1, ix], Z[iy + 1, ix + 1]]
            bad = np.any([q == -32768 for q in c], axis=0)
            c = [q.astype(float) for q in c]
            v = (c[0] * (1 - fx) * (1 - fy) + c[1] * fx * (1 - fy) + c[2] * (1 - fx) * fy + c[3] * fx * fy)
            v[~ok | bad] = np.nan
            sub = out[todo]
            sub[np.isnan(sub)] = v[np.isnan(sub)]
            out[todo] = sub
        return out


def refine_patch(lat, lon, radius_km=20, zoom=12, cb=None, offline=False):
    """A fine terrain patch around a predicted landing point (tiles cached for offline reuse)."""
    from .sources import make_box
    return build_dem(make_box(lat, lon, radius_km), zoom, cb, offline, max_tiles=100)
