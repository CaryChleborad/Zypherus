"""Assemble the phone web bundle (same steps as the Gradle copyWeb task) for local testing:
   python android/build_web.py OUTDIR"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INJECT = ('<script src="/static/leaflet.js"></script>',
          '<script src="/static/leaflet.js"></script><script src="/engine.js"></script>'
          '<script src="/data.js"></script><script src="/phone.js"></script>')


def build(out):
    if os.path.exists(out):
        shutil.rmtree(out)
    shutil.copytree(os.path.join(ROOT, "zephyrus", "web", "static"), os.path.join(out, "static"))
    html = open(os.path.join(ROOT, "zephyrus", "web", "templates", "index.html"), encoding="utf-8").read()
    assert INJECT[0] in html, "leaflet script tag not found in index.html"
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(html.replace(INJECT[0], INJECT[1]))
    for f in ("engine.js", "data.js", "phone.js"):
        shutil.copy(os.path.join(HERE, "web", f), os.path.join(out, f))
    return out


if __name__ == "__main__":
    print(build(sys.argv[1]))
