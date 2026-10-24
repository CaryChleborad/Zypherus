"""Flask server for the map interface."""
from __future__ import annotations

import datetime as dt
import glob
import os
import tempfile
import threading
import traceback
import uuid

from flask import Flask, jsonify, request, send_from_directory, Response

from .. import live
from ..balloon import BALLOONS, summary as balloon_summary, neck_lift_for_rate
from ..export import to_kml_multi, to_gpx
from ..flight import FlightParams, Uncertainty, BalloonSpec
from ..paths import DATA_DIR
from ..predict import (run_models, launch_window, launch_site_for_target, in_flight, log_prediction,
                       label, color)
from ..sources import build_pack, load_pack, MODELS, RAOB_STATIONS
from ..windfield import WindField, CHOICES, available_choices

JOBS: dict[str, dict] = {}
_WF_CACHE: dict[str, tuple] = {}
_FETCHES: dict[tuple, str] = {}


def _job(fn, *args, **kw):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "log": [], "result": None, "error": None}

    def run():
        try:
            JOBS[jid]["result"] = fn(*args, cb=JOBS[jid]["log"].append, **kw)
            JOBS[jid]["status"] = "done"
        except Exception as e:  # noqa
            traceback.print_exc()
            JOBS[jid]["error"] = str(e)
            JOBS[jid]["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return jid


def _pack_path(name):
    return os.path.join(DATA_DIR, os.path.basename(name))


def _wf(name):
    """Load a pack once; hand out fresh WindField objects sharing its arrays."""
    path = _pack_path(name)
    mt = os.path.getmtime(path)
    hit = _WF_CACHE.get(path)
    if not hit or hit[0] != mt:
        data, meta = load_pack(path)
        _WF_CACHE.clear()
        _WF_CACHE[path] = (mt, data, meta)
    _, data, meta = _WF_CACHE[path]
    base = "gfs" if "gfs" in data else available_choices(data)[0]
    wf = WindField(data, base)
    wf.meta = meta
    return wf


def _pack_info(path):
    data, meta = load_pack(path)
    srcs = [k for k in data if k != "dem"]
    t0 = max(float(data[k]["times"][0]) for k in srcs if not k.startswith("raob"))
    t1 = min(float(data[k]["times"][-1]) for k in srcs if not k.startswith("raob"))
    return {"path": os.path.basename(path), "choices": available_choices(data), "box": meta["box"],
            "t0": t0, "t1": t1, "cycles": meta.get("cycles"), "created": meta.get("created"),
            "errors": meta.get("errors"), "dem": "dem" in data,
            "size_mb": round(os.path.getsize(path) / 1e6, 1)}


def _params(j):
    b = None
    if j.get("ascent_mode") == "physics":
        bal = BALLOONS.get(j.get("balloon"), None)
        mass, bd, cd = bal if bal else (float(j["balloon_g"]), float(j["burst_d"]), 0.3)
        b = BalloonSpec(balloon_g=mass, burst_d=bd, cd=float(j.get("cd") or cd),
                        payload_g=float(j["payload_g"]), neck_lift_g=float(j["neck_lift_g"]),
                        gas=j.get("gas", "he"), profile=j.get("ascent_profile", "constant"))
    p = FlightParams(lat=float(j["lat"]), lon=float(j["lon"]), launch_time=float(j["launch_time"]),
                     ascent_rate=float(j.get("ascent_rate", 5)), burst_alt=float(j.get("burst_alt", 30480)),
                     descent_rate=float(j.get("descent_rate", 6)),
                     launch_alt=float(j["launch_alt"]) if j.get("launch_alt") not in (None, "") else None,
                     balloon=b, descent_factor=float(j.get("descent_factor", 1.0)))
    u = j.get("unc", {})
    unc = Uncertainty(members=int(j.get("members", 0)),
                      **{k: float(v) for k, v in u.items() if k in Uncertainty.__dataclass_fields__})
    return p, unc


class _QuietPolling:
    """Hide the once-a-second progress checks (/api/job/...) from the console log."""

    def filter(self, record):
        return "/api/job/" not in record.getMessage()


def create_app():
    import logging
    logging.getLogger("werkzeug").addFilter(_QuietPolling())
    app = Flask(__name__, static_folder="static")
    os.makedirs(DATA_DIR, exist_ok=True)
    tdir = os.path.join(os.path.dirname(__file__), "templates")

    @app.get("/")
    def index():
        return send_from_directory(tdir, "index.html")

    @app.get("/api/config")
    def config():
        return jsonify({
            "models": {k: {"label": v.label, "top_hpa": v.top_hpa, "step_h": v.step_h,
                           "members": len(v.members) if v.members != [None] else 0}
                       for k, v in MODELS.items()},
            "choices": CHOICES, "colors": {k: color(k) for k in CHOICES},
            "balloons": {k: {"mass_g": v[0], "burst_d": v[1], "cd": v[2]} for k, v in BALLOONS.items()},
            "stations": RAOB_STATIONS})

    @app.after_request
    def _cors(resp):
        # the phone app (another origin) may list and download wind packs from this laptop
        if request.path.startswith(("/api/packs", "/api/pack/")):
            resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.get("/api/pack/<name>")
    def pack_file(name):
        path = _pack_path(name)
        if not os.path.exists(path):
            return jsonify({"error": "not found"}), 404
        if request.args.get("phone"):
            import io
            from ..sources import slim_pack
            buf = io.BytesIO()
            slim_pack(path, buf, radius_km=float(request.args.get("radius_km", 250)))
            return Response(buf.getvalue(), mimetype="application/octet-stream")
        return send_from_directory(DATA_DIR, os.path.basename(path), mimetype="application/octet-stream",
                                   as_attachment=True)

    @app.get("/api/packs")
    def packs():
        out = []
        for p in sorted(glob.glob(os.path.join(DATA_DIR, "*.npz")), key=os.path.getmtime, reverse=True):
            try:
                out.append(_pack_info(p))
            except Exception:  # noqa
                pass
        return jsonify(out)

    @app.post("/api/fetch")
    def fetch():
        j = request.get_json()
        start = dt.datetime.fromtimestamp(float(j["launch_time"]), dt.timezone.utc)
        key = (round(float(j["lat"]), 2), round(float(j["lon"]), 2), int(float(j["launch_time"]) // 3600),
               tuple(sorted(j.get("models", []))), float(j.get("hours", 4)))
        running = _FETCHES.get(key)
        if running and JOBS.get(running, {}).get("status") == "running":
            return jsonify({"job": running})  # same download already in progress: follow it
        jid = _job(build_pack, float(j["lat"]), float(j["lon"]), start,
                   hours=float(j.get("hours", 4)), models=j.get("models", ["hrrr", "gfs"]),
                   radius_km=float(j.get("radius_km", 350)),
                   gefs_members=int(j.get("gefs_members", 10)), soundings=bool(j.get("soundings", True)),
                   dem_zoom=int(j.get("dem_zoom", 9)))
        _FETCHES[key] = jid
        return jsonify({"job": jid})

    @app.get("/api/job/<jid>")
    def job(jid):
        jb = JOBS.get(jid)
        if not jb:
            return jsonify({"status": "unknown"}), 404
        res = jb["result"]
        if isinstance(res, str) and res.endswith(".npz"):
            res = os.path.basename(res)
        return jsonify({"status": jb["status"], "log": jb["log"][-60:], "result": res, "error": jb["error"]})

    def _wrap(fn):
        try:
            return jsonify(fn())
        except Exception as e:  # noqa
            traceback.print_exc()
            return jsonify({"error": str(e)}), 400

    @app.post("/api/predict")
    def predict():
        j = request.get_json()

        def go():
            wf = _wf(j["pack"])
            p, unc = _params(j)
            res = run_models(wf, p, unc, j.get("models", ["blend"]), refine=j.get("refine", True),
                             offline=j.get("offline", False), gefs_mc_per_member=int(j.get("gefs_mc", 5)))
            launch = {"lat": p.lat, "lon": p.lon, "time": p.launch_time,
                      "alt": next(iter(res["models"].values()))["summary"].get("launch", {}).get("alt", 0)}
            res["kml"] = to_kml_multi(res, launch=launch)
            res["gpx"] = to_gpx(res)
            res["cycles"] = wf.meta.get("cycles")
            log_prediction({k: v for k, v in j.items() if k != "pack"} | {"pack": j["pack"]}, res)
            return res
        return _wrap(go)

    @app.post("/api/window")
    def window():
        j = request.get_json()

        def go():
            wf = _wf(j["pack"])
            p, _ = _params(j)
            tgt = j.get("target")
            return launch_window(wf, p, j.get("model", "blend"), float(j["t_start"]), float(j["t_end"]),
                                 float(j.get("step_h", 1)), tuple(tgt) if tgt else None)
        return _wrap(go)

    @app.post("/api/reverse")
    def reverse():
        j = request.get_json()

        def go():
            wf = _wf(j["pack"])
            p, _ = _params(j)
            return launch_site_for_target(wf, p, j.get("model", "blend"), tuple(j["target"]))
        return _wrap(go)

    @app.post("/api/balloon")
    def balloon():
        j = request.get_json()

        def go():
            bal = BALLOONS.get(j.get("balloon"))
            mass, bd, cd = bal if bal else (float(j["balloon_g"]), float(j["burst_d"]), 0.3)
            cd = float(j.get("cd") or cd)
            la = float(j.get("launch_alt") or 0)
            out = {"mass_g": mass, "burst_d": bd, "cd": cd}
            if j.get("target_rate"):
                out["neck_lift_for_target_g"] = neck_lift_for_rate(float(j["target_rate"]), mass, bd, cd,
                                                                   float(j["payload_g"]), j.get("gas", "he"), la)
            out.update(balloon_summary(mass, bd, cd, float(j["payload_g"]), float(j["neck_lift_g"]),
                                       j.get("gas", "he"), la, j.get("ascent_profile", "constant")))
            return out
        return _wrap(go)

    # ---------------------------------------------------------------- live
    @app.post("/api/live/poll")
    def live_poll():
        j = request.get_json()

        def go():
            cs = j["callsign"].strip()
            if j.get("source") == "aprsfi":
                fx = live.aprsfi(cs, j["apikey"])
            elif j.get("source") == "sondehub":
                fx = live.sondehub(cs)
            else:
                fx = live.fixes(cs)
            return {"fixes": fx}
        return _wrap(go)

    @app.post("/api/live/manual")
    def live_manual():
        j = request.get_json()
        return _wrap(lambda: {"fixes": live.add_manual(j["callsign"], j["t"], j["lat"], j["lon"], j["alt"])})

    @app.post("/api/live/clear")
    def live_clear():
        live.clear(request.get_json()["callsign"])
        return jsonify({"ok": True})

    @app.post("/api/live/predict")
    def live_predict():
        j = request.get_json()

        def go():
            fx = live.fixes(j["callsign"])
            if len(fx) < 2:
                raise ValueError("need at least 2 telemetry fixes")
            wf = _wf(j["pack"])
            last = fx[-1]
            j2 = dict(j, lat=last[1], lon=last[2], launch_time=last[0])
            p, unc = _params(j2)
            res = in_flight(wf, fx, p, unc, j.get("models", ["blend"]),
                                              use_measured=j.get("use_measured", True),
                                              offline=j.get("offline", False))
            res["kml"] = to_kml_multi(res, observed=[(f[1], f[2], f[3]) for f in fx])
            res["gpx"] = to_gpx(res)
            return res
        return _wrap(go)

    # ---------------------------------------------------------------- hindcast
    @app.post("/api/hindcast")
    def hindcast():
        from ..hindcast import hindcast_file
        from ..tracks import load_track
        f = request.files["track"]
        d = tempfile.mkdtemp()
        path = os.path.join(d, os.path.basename(f.filename) or "track.csv")
        f.save(path)
        lead = float(request.form.get("lead", 0))
        tz = float(request.form.get("tz", 0))
        models = request.form.get("models", "hrrr,rap,gfs,ecmwf,gefs").split(",")

        def run(cb=None):
            r = hindcast_file(path, lead_hours=lead, tz_hours=tz, cb=cb, models=models)
            tr = load_track(path, default_tz_hours=tz)
            r["observed"] = [[float(a), float(b), float(c)] for a, b, c in zip(tr.lat, tr.lon, tr.alt)]
            for key, m in r["modes"].items():
                m["label"] = label(key)
                m["color"] = color(key)
                for run_ in m["runs"].values():
                    s = run_.pop("summary", None)
                    if s is not None:
                        run_["burst"] = s.get("burst")
            r["pack"] = os.path.basename(r["pack"])
            return r

        return jsonify({"job": _job(run)})

    @app.get("/api/calibration")
    def calibration():
        from ..hindcast import calibrate
        return _wrap(lambda: calibrate() or {})

    return app
