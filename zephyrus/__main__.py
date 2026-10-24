"""Command line:  python -m zephyrus {fetch,predict,window,reverse,hindcast,calibrate,web} ...

Bare numbers use US units: altitude ft, ascent ft/min, descent ft/s (sea level).
Add a suffix to use others: 30km, 5m/s, 6m/s, 20fps, 1000fpm.
Times without a zone are local Pacific (America/Los_Angeles); add Z for UTC.
Models: blend (HRRR+GFS), rap, ecmwf, gfs, gefs, raob-KREV (etc.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

from .units import altitude_m, speed_ms
from .paths import DATA_DIR, OUTPUT_DIR

def _pacific():
    """Pacific time. Windows has no built-in time zone database; it comes from the tzdata package.
    If that is missing, fall back to this computer's own time zone rather than crash."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa
        print("Note: time zone data not installed (pip install tzdata); using this computer's time zone.")
        return dt.datetime.now().astimezone().tzinfo


LOCAL = _pacific()


def parse_launch(s):
    if s.lower() == "now":
        return dt.datetime.now(dt.timezone.utc)
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    d = dt.datetime.fromisoformat(s2)
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL)
    return d.astimezone(dt.timezone.utc)


def find_pack(lat, lon, start, end, pack_dir=DATA_DIR):
    """Newest saved pack that covers this launch point and time window."""
    from .sources import load_pack
    for p in sorted(glob.glob(os.path.join(pack_dir, "*.npz")), key=os.path.getmtime, reverse=True):
        try:
            data, meta = load_pack(p)
        except Exception:  # noqa
            continue
        s, n, w, e = meta["box"]
        src = [k for k in data if k != "dem" and not k.startswith("raob")]
        t0 = max(data[k]["times"][0] for k in src)
        t1 = min(data[k]["times"][-1] for k in src)
        if s < lat < n and w < lon < e and t0 <= start.timestamp() + 1 and t1 >= end.timestamp() - 1800:
            return p
    return None


def _models(s):
    return [m.strip() for m in s.split(",") if m.strip()]


def _params(a, start):
    from .flight import FlightParams, BalloonSpec
    from .balloon import BALLOONS
    b = None
    if a.balloon:
        mass, bd, cd = BALLOONS[a.balloon]
        b = BalloonSpec(mass, bd, a.cd or cd, a.payload_g, a.neck_lift_g, a.gas)
    return FlightParams(lat=a.lat, lon=a.lon, launch_time=start.timestamp(),
                        ascent_rate=speed_ms(a.ascent, "fpm"), burst_alt=altitude_m(a.burst),
                        descent_rate=speed_ms(a.descent, "fps"), descent_factor=a.descent_factor,
                        launch_alt=altitude_m(a.launch_alt) if a.launch_alt else None, balloon=b)


def _get_wf(a, start, hours):
    from .sources import build_pack
    from .windfield import WindField
    end = start + dt.timedelta(hours=hours)
    pack = a.pack or find_pack(a.lat, a.lon, start, end)
    if pack is None:
        if a.offline:
            sys.exit("No saved wind data covers this launch. Run 'fetch' while online first.")
        print("No saved wind data covers this launch; downloading...")
        pack = build_pack(a.lat, a.lon, start, hours=hours, models=_models(a.download),
                          radius_km=a.radius, gefs_members=a.gefs_members)
    return pack, WindField.open(pack)


def cmd_fetch(a):
    from .sources import build_pack
    build_pack(a.lat, a.lon, parse_launch(a.launch), hours=a.hours, models=_models(a.models),
               radius_km=a.radius, out_path=a.out, gefs_members=a.gefs_members,
               soundings=not a.no_soundings, dem_zoom=a.dem_zoom)


def cmd_predict(a):
    from .flight import Uncertainty
    from .predict import run_models, log_prediction
    from .export import to_kml_multi, to_gpx

    start = parse_launch(a.launch)
    pack, wf = _get_wf(a, start, a.hours)
    p = _params(a, start)
    res = run_models(wf, p, Uncertainty(members=a.members), _models(a.models), refine=not a.no_refine,
                     offline=a.offline, log=print)
    lt = lambda t: dt.datetime.fromtimestamp(t, LOCAL).strftime("%Y-%m-%d %H:%M %Z")  # noqa
    print(f"\nWind data: {os.path.basename(pack)}")
    for k, m in res["models"].items():
        s = m["summary"]
        D = s["landing"]
        line = f"{m['label']:32s} {D['lat']:.5f}, {D['lon']:.5f}  {s['range_km']*0.621371:6.1f} mi @ {s['bearing_deg']:3.0f}°"
        if D.get("time"):
            line += f"  lands {lt(D['time'])}"
        e = s.get("ensemble")
        if e:
            line += f"  95% within {e['r95_km']*0.621371:.1f} mi"
        print(line)
    c = res["consensus"]
    if c and c["n"] > 1:
        print(f"{'CONSENSUS':32s} {c['lat']:.5f}, {c['lon']:.5f}  models agree within "
              f"{c['max_pairwise_km']*0.621371:.1f} mi")
    for w in res["warnings"]:
        print("WARNING:", w)
    base = a.out or os.path.join(OUTPUT_DIR, f"prediction_{start:%Y%m%d_%H%M}Z")
    os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
    with open(base + ".kml", "w") as f:
        f.write(to_kml_multi(res, launch={"lat": p.lat, "lon": p.lon, "time": p.launch_time}))
    with open(base + ".gpx", "w") as f:
        f.write(to_gpx(res))
    with open(base + ".json", "w") as f:
        json.dump(res, f, default=float)
    log_prediction({"lat": a.lat, "lon": a.lon, "launch": start.isoformat(), "models": a.models}, res)
    print(f"Wrote {base}.kml / .gpx / .json")


def cmd_window(a):
    from .predict import launch_window
    t0, t1 = parse_launch(a.start), parse_launch(a.end)
    pack, wf = _get_wf(a, t0, (t1 - t0).total_seconds() / 3600 + 4)
    p = _params(a, t0)
    tgt = (a.target_lat, a.target_lon) if a.target_lat is not None else None
    r = launch_window(wf, p, a.model, t0.timestamp(), t1.timestamp(), a.step, tgt)
    for x in r["rows"]:
        s = (f"{dt.datetime.fromtimestamp(x['time'], LOCAL):%a %m-%d %H:%M}  -> {x['lat']:.4f}, {x['lon']:.4f}"
             f"  {x['range_km']*0.621371:5.1f} mi @ {x['bearing_deg']:3.0f}°")
        if tgt:
            s += f"  {x['to_target_km']*0.621371:5.1f} mi from target"
        print(s + ("   <-- best" if x is r["best"] else ""))


def cmd_reverse(a):
    from .predict import launch_site_for_target
    start = parse_launch(a.launch)
    pack, wf = _get_wf(a, start, a.hours)
    r = launch_site_for_target(wf, _params(a, start), a.model, (a.target_lat, a.target_lon))
    print(f"Launch at {r['launch'][0]:.5f}, {r['launch'][1]:.5f} -> lands "
          f"{r['miss_km']*0.621371:.2f} mi from target")


def cmd_hindcast(a):
    from .hindcast import hindcast_file, print_result, write_report
    nominal = None
    if a.ascent and a.burst and a.descent:
        nominal = dict(ascent_rate=speed_ms(a.ascent, "fpm"), burst_alt=altitude_m(a.burst),
                       descent_rate=speed_ms(a.descent, "fps"))
    files = []
    for pat in a.tracks:
        files += sorted(glob.glob(pat))
    results = []
    for f in files:
        try:
            r = hindcast_file(f, lead_hours=a.lead, tz_hours=a.tz, nominal=nominal,
                              models=_models(a.models), gefs_members_n=a.gefs_members)
            print_result(r)
            results.append(r)
        except Exception as e:  # noqa
            print(f"\n=== {f} === FAILED: {e}")
    if results:
        os.makedirs(os.path.dirname(a.report), exist_ok=True)
        write_report(results, a.report)
        print(f"\nReport: {a.report}")


def cmd_calibrate(a):
    from .hindcast import calibrate
    c = calibrate()
    print(json.dumps(c, indent=1) if c else "No hindcast results yet.")


def cmd_web(a):
    from .web.app import create_app
    app = create_app()
    print(f"Open http://localhost:{a.port}")
    app.run(host=a.host, port=a.port, debug=False, threaded=True)


def _flight_args(p):
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--ascent", default="1000fpm")
    p.add_argument("--burst", default="100000ft")
    p.add_argument("--descent", default="20fps", help="sea-level descent rate")
    p.add_argument("--descent-factor", type=float, default=1.0)
    p.add_argument("--launch-alt", help="default: terrain height")
    p.add_argument("--balloon", help="use balloon physics, e.g. 'Totex/Kaymont 1200'")
    p.add_argument("--payload-g", type=float, default=1500)
    p.add_argument("--neck-lift-g", type=float, default=2900)
    p.add_argument("--gas", default="he", choices=["he", "h2"])
    p.add_argument("--cd", type=float)
    p.add_argument("--pack", help="wind data .npz (default: newest saved one that fits)")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--download", default="hrrr,rap,gfs,ecmwf,gefs", help="models to download if needed")
    p.add_argument("--gefs-members", type=int, default=10)
    p.add_argument("--radius", type=float, default=350)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zephyrus", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download winds into an offline pack")
    f.add_argument("--lat", type=float, required=True)
    f.add_argument("--lon", type=float, required=True)
    f.add_argument("--launch", required=True)
    f.add_argument("--hours", type=float, default=4.0)
    f.add_argument("--models", default="hrrr,rap,gfs,ecmwf,gefs")
    f.add_argument("--gefs-members", type=int, default=10)
    f.add_argument("--radius", type=float, default=350, help="km around launch")
    f.add_argument("--dem-zoom", type=int, default=9, help="terrain tiles zoom (0 = none)")
    f.add_argument("--no-soundings", action="store_true")
    f.add_argument("--out")
    f.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("predict", help="predict a flight with one or more models")
    _flight_args(p)
    p.add_argument("--launch", required=True)
    p.add_argument("--models", default="blend,rap,ecmwf,gfs,gefs")
    p.add_argument("--members", type=int, default=200, help="Monte Carlo runs (0 = off)")
    p.add_argument("--no-refine", action="store_true", help="skip fine terrain at landing")
    p.add_argument("--hours", type=float, default=4.0)
    p.add_argument("--out", help="output path prefix")
    p.set_defaults(fn=cmd_predict)

    w = sub.add_parser("window", help="landing vs launch time")
    _flight_args(w)
    w.add_argument("--start", required=True)
    w.add_argument("--end", required=True)
    w.add_argument("--step", type=float, default=1.0, help="hours")
    w.add_argument("--model", default="blend")
    w.add_argument("--target-lat", type=float)
    w.add_argument("--target-lon", type=float)
    w.set_defaults(fn=cmd_window)

    r = sub.add_parser("reverse", help="where to launch to land on a target")
    _flight_args(r)
    r.add_argument("--launch", required=True)
    r.add_argument("--target-lat", type=float, required=True)
    r.add_argument("--target-lon", type=float, required=True)
    r.add_argument("--model", default="blend")
    r.add_argument("--hours", type=float, default=4.0)
    r.set_defaults(fn=cmd_reverse)

    h = sub.add_parser("hindcast", help="test predictions against recorded flights")
    h.add_argument("tracks", nargs="+", help="track files (CSV/GPX/KML/APRS log); globs ok")
    h.add_argument("--lead", type=float, default=0.0, help="hours before launch the forecast was made")
    h.add_argument("--tz", type=float, default=0.0, help="UTC offset for times without a zone")
    h.add_argument("--models", default="hrrr,rap,gfs,ecmwf,gefs")
    h.add_argument("--gefs-members", type=int, default=10)
    h.add_argument("--ascent", help="also test with these planned values")
    h.add_argument("--burst")
    h.add_argument("--descent")
    h.add_argument("--report", default=os.path.join(OUTPUT_DIR, "hindcast_report.csv"))
    h.set_defaults(fn=cmd_hindcast)

    c = sub.add_parser("calibrate", help="summarize all hindcasts (your balloons + model accuracy)")
    c.set_defaults(fn=cmd_calibrate)

    wb = sub.add_parser("web", help="start the map interface")
    wb.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from a phone on your network")
    wb.add_argument("--port", type=int, default=8080)
    wb.set_defaults(fn=cmd_web)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
