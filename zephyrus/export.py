"""KML / GPX / CSV output for single- and multi-model predictions."""
from __future__ import annotations

import datetime as dt
import math
from xml.sax.saxutils import escape


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ellipse_coords(e, n=72):
    clat, clon = e["center"]
    th = math.radians(e["orient_deg"])
    out = []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        x, y = e["a_m"] * math.cos(a), e["b_m"] * math.sin(a)
        east = x * math.sin(th) + y * math.cos(th)
        north = x * math.cos(th) - y * math.sin(th)
        out.append((clat + math.degrees(north / 6371008.8),
                    clon + math.degrees(east / (6371008.8 * math.cos(math.radians(clat))))))
    return out


def _kml_color(hexrgb, alpha="ff"):
    h = hexrgb.lstrip("#")
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"


def _pm(nm, lat, lon, alt, desc="", color="ff0000ff"):
    return (f"<Placemark><name>{escape(nm)}</name><description>{escape(desc)}</description>"
            f"<Style><IconStyle><color>{color}</color></IconStyle></Style>"
            f"<Point><altitudeMode>absolute</altitudeMode><coordinates>{lon:.6f},{lat:.6f},{alt:.0f}"
            f"</coordinates></Point></Placemark>")


def _model_folder(key, m):
    s, col = m["summary"], m.get("color", "#ff8000")
    parts = [f"<Folder><name>{escape(m.get('label', key))}</name>"]
    if m.get("track"):
        coords = " ".join(f"{lo:.6f},{la:.6f},{al:.0f}" for _, la, lo, al, _ in m["track"])
        parts.append(f"<Placemark><name>Path</name><Style><LineStyle><color>{_kml_color(col)}</color>"
                     "<width>3</width></LineStyle></Style><LineString><altitudeMode>absolute</altitudeMode>"
                     f"<coordinates>{coords}</coordinates></LineString></Placemark>")
    for tr in m.get("member_tracks", []):
        coords = " ".join(f"{lo:.6f},{la:.6f},{al:.0f}" for _, la, lo, al, _ in tr)
        parts.append(f"<Placemark><Style><LineStyle><color>{_kml_color(col, '80')}</color><width>1</width>"
                     "</LineStyle></Style><LineString><altitudeMode>absolute</altitudeMode>"
                     f"<coordinates>{coords}</coordinates></LineString></Placemark>")
    B = s.get("burst")
    if B and B.get("lat") == B.get("lat") and B.get("lat") is not None:
        parts.append(_pm("Burst", B["lat"], B["lon"], B["alt"], f"{B['alt']/0.3048:,.0f} ft", "ff00ffff"))
    D = s["landing"]
    parts.append(_pm(f"Landing ({m.get('label', key)})", D["lat"], D["lon"], D.get("alt", 0) or 0,
                     f"{D['lat']:.5f}, {D['lon']:.5f}" + (f"  {_iso(D['time'])}" if D.get("time") else ""),
                     _kml_color(col)))
    ens = s.get("ensemble")
    if ens:
        ring = " ".join(f"{lo:.6f},{la:.6f},0" for la, lo in ellipse_coords(ens["ellipse95"]))
        parts.append(f"<Placemark><name>95% landing area</name><Style><LineStyle><color>{_kml_color(col)}"
                     f"</color><width>2</width></LineStyle><PolyStyle><color>{_kml_color(col, '30')}</color>"
                     "</PolyStyle></Style><Polygon><altitudeMode>clampToGround</altitudeMode><outerBoundaryIs>"
                     f"<LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                     "</Placemark>")
    parts.append("</Folder>")
    return "".join(parts)


def to_kml_multi(result, name="Balloon prediction", launch=None, observed=None):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
             f"<name>{escape(name)}</name>"]
    if launch:
        parts.append(_pm("Launch", launch["lat"], launch["lon"], launch.get("alt", 0) or 0,
                         _iso(launch["time"]) if launch.get("time") else "", "ff00ff00"))
    for key, m in result["models"].items():
        parts.append(_model_folder(key, m))
    c = result.get("consensus")
    if c and c["n"] > 1:
        parts.append(_pm("Model consensus", c["lat"], c["lon"], 0,
                         f"models agree within {c['max_pairwise_km']:.1f} km", "ffffffff"))
    if observed:
        oc = " ".join(f"{lo:.6f},{la:.6f},{al:.0f}" for la, lo, al in observed)
        parts.append("<Placemark><name>Actual flight</name><Style><LineStyle><color>ff00ff00</color>"
                     "<width>3</width></LineStyle></Style><LineString><altitudeMode>absolute</altitudeMode>"
                     f"<coordinates>{oc}</coordinates></LineString></Placemark>")
    parts.append("</Document></kml>")
    return "".join(parts)


def to_kml(summary, track, name="Balloon prediction", observed=None):
    """Single-model KML (CLI)."""
    res = {"models": {"pred": {"label": "Prediction", "color": "#ff8000", "summary": summary,
                               "track": track}}}
    return to_kml_multi(res, name, launch=summary["launch"], observed=observed)


def to_gpx(result):
    """Landing waypoints for a handheld GPS or phone navigation app."""
    pts = []
    for key, m in result["models"].items():
        D = m["summary"]["landing"]
        nm = escape(f"LAND {m.get('label', key)}"[:30])
        pts.append(f'<wpt lat="{D["lat"]:.6f}" lon="{D["lon"]:.6f}"><name>{nm}</name></wpt>')
    c = result.get("consensus")
    if c:
        pts.append(f'<wpt lat="{c["lat"]:.6f}" lon="{c["lon"]:.6f}"><name>LAND consensus</name></wpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="Zephyrus" '
            'xmlns="http://www.topografix.com/GPX/1/1">' + "".join(pts) + "</gpx>")


def track_csv(track):
    lines = ["time_utc,lat,lon,alt_m,alt_ft,phase"]
    for t, la, lo, al, ph in track:
        lines.append(f"{_iso(t)},{la:.6f},{lo:.6f},{al:.0f},{al/0.3048:.0f},"
                     f"{['ascent', 'descent', 'landed'][ph]}")
    return "\n".join(lines) + "\n"
