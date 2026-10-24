"""Installation check:  python -m zephyrus.doctor [--network]

Checks the Python version, every required package, the GRIB decoder library, the data folder,
and (with --network) whether each weather data server can be reached from this computer.
Exit code 0 = ready to run.
"""
from __future__ import annotations

import importlib
import os
import platform
import sys
import tempfile

OK, BAD, WARN = "  [ OK ]", "  [FAIL]", "  [WARN]"
MIN_PY = (3, 10)

PACKAGES = [  # (import name, pip name, why)
    ("numpy", "numpy", "math on wind grids"),
    ("scipy", "scipy", "regridding HRRR/RAP"),
    ("requests", "requests", "downloading forecasts"),
    ("flask", "flask", "the map interface"),
    ("PIL", "pillow", "reading terrain tiles"),
    ("eccodes", "eccodes", "reading GRIB weather files"),
    ("tzdata", "tzdata", "time zones (Windows has none built in)"),
]

HOSTS = [
    ("NOAA NOMADS (GFS, GEFS subsets)", "https://nomads.ncep.noaa.gov/"),
    ("AWS GFS archive", "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"),
    ("AWS HRRR archive", "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/"),
    ("AWS RAP archive", "https://noaa-rap-pds.s3.amazonaws.com/"),
    ("AWS GEFS archive", "https://noaa-gefs-pds.s3.amazonaws.com/"),
    ("ECMWF open data", "https://data.ecmwf.int/forecasts/"),
    ("Terrain tiles", "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/0/0/0.png"),
    ("Soundings (Iowa Mesonet)", "https://mesonet.agron.iastate.edu/"),
    ("SondeHub tracking", "https://api.v2.sondehub.org/"),
]


def eccodes_hint():
    sysname = platform.system()
    if sysname == "Darwin":
        return ("macOS 13 or newer gets ecCodes automatically from pip. On older macOS install it with "
                "Homebrew:  brew install eccodes   then re-run the installer.")
    if sysname == "Linux":
        return ("Install the system library, then re-run the installer:\n"
                "         Debian/Ubuntu: sudo apt install libeccodes0   Fedora: sudo dnf install eccodes")
    return "Re-run the installer; pip should fetch a Windows build of eccodes with the library included."


def check_packages():
    ok = True
    for mod, pipname, why in PACKAGES:
        try:
            importlib.import_module(mod)
            try:
                from importlib.metadata import version
                ver = version(pipname)
            except Exception:  # noqa
                ver = ""
            print(f"{OK} {pipname} {ver}  ({why})")
        except Exception as e:  # noqa
            ok = False
            print(f"{BAD} {pipname} missing or broken ({why}): {str(e).splitlines()[0]}")
            if mod == "eccodes":
                print("         " + eccodes_hint())
    return ok


def check_eccodes_works():
    try:
        import eccodes as ec
        g = ec.codes_grib_new_from_samples("regular_ll_pl_grib2")
        ec.codes_release(g)
        print(f"{OK} ecCodes library {ec.codes_get_api_version()} can build and read GRIB2")
        return True
    except Exception as e:  # noqa
        print(f"{BAD} ecCodes library does not work: {str(e).splitlines()[0]}")
        print("         " + eccodes_hint())
        return False


def check_timezone():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Los_Angeles")
        print(f"{OK} Pacific time zone available")
        return True
    except Exception as e:  # noqa
        print(f"{BAD} time zone data missing: {e}. Fix: pip install tzdata")
        return False


def check_zephyrus():
    try:
        from zephyrus.flight import FlightParams, simulate  # noqa
        from zephyrus.web.app import create_app  # noqa
        print(f"{OK} zephyrus modules load")
        return True
    except Exception as e:  # noqa
        print(f"{BAD} zephyrus does not load: {e}")
        return False


def check_data_dir():
    from zephyrus.paths import DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DATA_DIR):
            pass
        print(f"{OK} data folder is writable: {DATA_DIR}")
        return True
    except OSError as e:
        print(f"{BAD} cannot write to data folder {DATA_DIR}: {e}")
        return False


def check_network():
    import requests
    print("\nChecking the weather data servers (10 s each)...")
    n_ok = 0
    for name, url in HOSTS:
        try:
            r = requests.get(url, timeout=10, stream=True)
            code = r.status_code
            r.close()
            good = code < 500
        except Exception as e:  # noqa
            good, code = False, type(e).__name__
        n_ok += good
        print(f"{OK if good else WARN} {name}  ({code})")
    if n_ok < len(HOSTS):
        print("         Unreachable servers mean those models will be skipped. Saved wind packs still work offline.")
    return n_ok


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print(f"Zephyrus - installation check\n{platform.platform()}\n")
    v = sys.version_info
    py_ok = (v.major, v.minor) >= MIN_PY
    print(f"{OK if py_ok else BAD} Python {v.major}.{v.minor}.{v.micro} at {sys.executable}"
          + ("" if py_ok else f"  (need {MIN_PY[0]}.{MIN_PY[1]} or newer)"))
    ok = py_ok and check_packages()
    if ok:
        ok = check_eccodes_works() and check_timezone() and check_zephyrus() and check_data_dir()
    if "--network" in argv and ok:
        check_network()
    print("\nREADY." if ok else "\nNOT READY - see the [FAIL] lines above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
