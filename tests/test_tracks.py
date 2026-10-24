import datetime as dt
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from zephyrus.flight import FlightParams, simulate  # noqa
from zephyrus.windfield import WindField  # noqa
from zephyrus.tracks import load_track, analyze  # noqa
from zephyrus.hindcast import evaluate  # noqa
from synth import make_source  # noqa

T0 = 1.8e9


def _truth():
    uf = lambda z, la, lo, t: 4 + z / 1500.0  # noqa
    vf = lambda z, la, lo, t: 2 - z / 8000.0  # noqa
    data = {"gfs": make_source(ufunc=uf, vfunc=vf)}
    wf = WindField(data, "gfs")
    p = FlightParams(lat=39.5, lon=-118.5, launch_time=T0, ascent_rate=5.2, burst_alt=31000,
                     descent_rate=7.5, launch_alt=1200, dt=2)
    r = simulate(wf, p)
    tr = np.array(r.track)[::15]  # a fix every 30 s, like APRS
    return data, tr, r.summary()


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_all(tr):
    d = tempfile.mkdtemp()
    paths = {}
    p = os.path.join(d, "f.csv")
    with open(p, "w") as f:
        f.write("datetime,latitude,longitude,altitude (ft)\n")
        for t, la, lo, al, _ in tr:
            f.write(f"{_iso(t)},{la:.6f},{lo:.6f},{al/0.3048:.0f}\n")
    paths["csv"] = p
    p = os.path.join(d, "f.gpx")
    with open(p, "w") as f:
        f.write('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>')
        for t, la, lo, al, _ in tr:
            f.write(f'<trkpt lat="{la}" lon="{lo}"><ele>{al}</ele><time>{_iso(t)}</time></trkpt>')
        f.write("</trkseg></trk></gpx>")
    paths["gpx"] = p
    p = os.path.join(d, "f.kml")
    with open(p, "w") as f:
        f.write('<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">'
                '<Document><Placemark><gx:Track>')
        f.write("".join(f"<when>{_iso(t)}</when>" for t, *_ in tr))
        f.write("".join(f"<gx:coord>{lo} {la} {al}</gx:coord>" for t, la, lo, al, _ in tr))
        f.write("</gx:Track></Placemark></Document></kml>")
    paths["kml"] = p
    p = os.path.join(d, "f.txt")
    with open(p, "w") as f:
        for t, la, lo, al, _ in tr:
            lt = dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=-7)))
            ad, am = int(abs(la)), (abs(la) - int(abs(la))) * 60
            od, om = int(abs(lo)), (abs(lo) - int(abs(lo))) * 60
            f.write(f"{lt:%Y-%m-%d %H:%M:%S} PDT: KK6ABC-11>APRS,WIDE2-1,qAR,N6XYZ:"
                    f"/{lt:%H%M%S}h{ad:02d}{am:05.2f}N/{od:03d}{om:05.2f}WO090/012/A={al/0.3048:06.0f}\n")
    paths["aprs"] = p
    return paths


def test_readers_and_analysis():
    data, tr, truth = _truth()
    for kind, path in _write_all(tr).items():
        t = load_track(path)
        a = analyze(t)
        assert abs(a["launch"]["t"] - T0) < 61, kind
        assert abs(a["ascent_rate"] - 5.2) < 0.1, (kind, a["ascent_rate"])
        assert abs(a["burst"]["alt"] - 31000) < 200, kind
        assert abs(a["descent_rate_sl"] - 7.5) < 0.4, (kind, a["descent_rate_sl"])
        assert abs(a["landing"]["lat"] - truth["landing"]["lat"]) < 0.01, kind


def test_hindcast_self_consistent():
    data, tr, truth = _truth()
    t = load_track(_write_all(tr)["gpx"])
    r = evaluate(t, data, modes=("gfs",),
                 nominal=dict(ascent_rate=5.0, burst_alt=30480, descent_rate=7.0))
    runs = r["modes"]["gfs"]["runs"]
    assert runs["observed_params"]["landing_miss_km"] < 2.0, runs["observed_params"]["landing_miss_km"]
    assert runs["nominal_params"]["landing_miss_km"] > runs["observed_params"]["landing_miss_km"]
    band = r["modes"]["gfs"]["wind_error"]
    assert band and all(b["rms_vec_err"] < 1.0 for b in band), band


if __name__ == "__main__":
    test_readers_and_analysis()
    print("ok readers")
    test_hindcast_self_consistent()
    print("ok hindcast")
