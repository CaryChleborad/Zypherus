"""Unit parsing. Bare numbers: altitude = feet, ascent = ft/min, descent = ft/s."""
import re

FT = 0.3048
_RX = re.compile(r"^\s*([-+]?\d*\.?\d+)\s*([a-z/]*)\s*$", re.I)


def _split(s):
    m = _RX.match(str(s))
    if not m:
        raise ValueError(f"bad value {s!r}")
    return float(m.group(1)), m.group(2).lower()


def altitude_m(s):
    v, u = _split(s)
    if u in ("", "ft", "feet", "'"):
        return v * FT
    if u in ("m", "meters"):
        return v
    if u == "km":
        return v * 1000
    raise ValueError(f"altitude unit {u!r}")


def speed_ms(s, default="fpm"):
    v, u = _split(s)
    u = u or default
    table = {"m/s": 1.0, "mps": 1.0, "ms": 1.0, "ft/s": FT, "fps": FT, "ft/min": FT / 60,
             "fpm": FT / 60, "m/min": 1 / 60, "mph": 0.44704, "kt": 0.514444, "kts": 0.514444}
    if u not in table:
        raise ValueError(f"speed unit {u!r}")
    return v * table[u]
