"""Write small synthetic GRIB2 files with eccodes and push them through the real decode path."""
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eccodes as ec  # noqa

from zephyrus.sources import (decode_regular_file, decode_lambert_file, make_box,  # noqa
                                 _assemble, rotate_lambert_winds)

LEVELS = [850, 700, 500, 300, 200, 100, 50, 30, 10]


def _msg(f, base, kind, level, values, date=20261022, hour=12, step=3):
    g = ec.codes_clone(base)
    ec.codes_set(g, "dataDate", date)
    ec.codes_set(g, "dataTime", hour * 100)
    ec.codes_set(g, "stepUnits", 1)
    ec.codes_set(g, "forecastTime", step)
    ec.codes_set(g, "discipline", 0)
    cat, num = {"gh": (3, 5), "u": (2, 2), "v": (2, 3), "orog": (3, 5)}[kind]
    if kind == "orog":
        ec.codes_set(g, "typeOfFirstFixedSurface", 1)
    else:
        ec.codes_set(g, "typeOfFirstFixedSurface", 100)
        ec.codes_set(g, "scaleFactorOfFirstFixedSurface", 0)
        ec.codes_set(g, "scaledValueOfFirstFixedSurface", int(level * 100))
    ec.codes_set(g, "parameterCategory", cat)
    ec.codes_set(g, "parameterNumber", num)
    ec.codes_set_values(g, values.ravel().astype(float))
    ec.codes_write(g, f)
    ec.codes_release(g)


def test_regular_gfs_like():
    base = ec.codes_grib_new_from_samples("regular_ll_pl_grib2")
    # 0.5 deg global, lat 90 -> -90, lon 0 -> 359.5 (GFS ordering)
    for k, v in dict(Ni=720, Nj=361, latitudeOfFirstGridPointInDegrees=90,
                     longitudeOfFirstGridPointInDegrees=0, latitudeOfLastGridPointInDegrees=-90,
                     longitudeOfLastGridPointInDegrees=359.5, iDirectionIncrementInDegrees=0.5,
                     jDirectionIncrementInDegrees=0.5).items():
        ec.codes_set(base, k, v)
    lat = np.linspace(90, -90, 361)[:, None] * np.ones((1, 720))
    lon = np.arange(720)[None, :] * 0.5 * np.ones((361, 1))
    lon180 = ((lon + 180) % 360) - 180
    path = tempfile.mktemp(suffix=".grb2")
    with open(path, "wb") as f:
        for lev in LEVELS:
            _msg(f, base, "gh", lev, 1000.0 + 0 * lat + (1000 - lev) * 20)
            _msg(f, base, "u", lev, lon180 / 10.0)       # u encodes longitude
            _msg(f, base, "v", lev, lat / 10.0)          # v encodes latitude
        _msg(f, base, "orog", 0, 1000 + lat)
    box = make_box(39.5, -118.5, 200)
    valid, fields, ter, la, lo = decode_regular_file(path, box)
    assert la[0] < la[-1] and lo[0] < lo[-1]
    assert lo[0] < -118.5 < lo[-1] and la[0] < 39.5 < la[-1]
    U = fields[("u", 500.0)]
    V = fields[("v", 500.0)]
    assert np.allclose(U, lo[None, :] / 10, atol=0.02)
    assert np.allclose(V, la[:, None] / 10, atol=0.02)
    assert np.allclose(ter, 1000 + la[:, None], atol=1)
    import datetime as dt
    assert dt.datetime.fromtimestamp(valid, dt.timezone.utc).hour == 15
    times, levels, z, u, v = _assemble([(valid, fields)], LEVELS)
    assert list(levels) == sorted(LEVELS, reverse=True) and z.shape[1] == len(LEVELS)
    assert (np.diff(z[0, :, 0, 0]) > 0).all()


def test_lambert_hrrr_like():
    base = ec.codes_grib_new_from_samples("GRIB2")
    ec.codes_set(base, "gridDefinitionTemplateNumber", 30)
    nx, ny = 400, 300
    for k, v in dict(shapeOfTheEarth=6, Nx=nx, Ny=ny, scanningMode=64, latitudeOfFirstGridPointInDegrees=34.0,
                     longitudeOfFirstGridPointInDegrees=235.0, LaDInDegrees=38.5,
                     LoVInDegrees=262.5, Latin1InDegrees=38.5, Latin2InDegrees=38.5,
                     DxInMetres=3000, DyInMetres=3000, latitudeOfSouthernPoleInDegrees=-90,
                     longitudeOfSouthernPoleInDegrees=0).items():
        ec.codes_set(base, k, v)
    ec.codes_set_values(base, np.zeros(nx * ny))
    ec.codes_set(base, "resolutionAndComponentFlags", 8)  # winds grid-relative
    ec.codes_set(base, "productDefinitionTemplateNumber", 0)
    lats = ec.codes_get_array(base, "latitudes").reshape(ny, nx)
    lons = ((ec.codes_get_array(base, "longitudes").reshape(ny, nx) + 180) % 360) - 180
    # earth-relative truth: 10 m/s due east everywhere -> encode as grid-relative
    meta = {"Latin1": 38.5, "Latin2": 38.5, "LoV": 262.5}
    n = math.sin(math.radians(38.5))
    a = n * np.radians(lons + 97.5)
    ue, ve = 10.0 * np.ones_like(lats), np.zeros_like(lats)
    ug = np.cos(a) * ue - np.sin(a) * ve       # inverse rotation
    vg = np.sin(a) * ue + np.cos(a) * ve
    chk_u, chk_v = rotate_lambert_winds(ug, vg, lons, meta)
    assert np.allclose(chk_u, ue) and np.allclose(chk_v, ve)
    path = tempfile.mktemp(suffix=".grb2")
    with open(path, "wb") as f:
        for lev in LEVELS:
            _msg(f, base, "gh", lev, 1000.0 + 0 * lats + (1000 - lev) * 20, step=0)
            _msg(f, base, "u", lev, ug, step=0)
            _msg(f, base, "v", lev, vg, step=0)
        _msg(f, base, "orog", 0, lats * 100, step=0)
    box = make_box(39.0, -119.5, 150)
    valid, fields, ter, rg = decode_lambert_file(path, box)
    U = fields[("u", 500.0)]
    V = fields[("v", 500.0)]
    ok = np.isfinite(U)
    assert ok.mean() > 0.9, ok.mean()
    assert np.allclose(U[ok], 10, atol=0.05), np.nanmax(abs(U - 10))
    assert np.allclose(V[ok], 0, atol=0.05), np.nanmax(abs(V))
    assert np.allclose(ter[ok], (rg.lat[:, None] * 100 * np.ones_like(ter))[ok], atol=2)


if __name__ == "__main__":
    test_regular_gfs_like()
    print("ok regular")
    test_lambert_hrrr_like()
    print("ok lambert")
