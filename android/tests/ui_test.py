"""Phone-UI test in Chromium at phone size, with Open-Meteo and terrain tiles mocked and a real
laptop server providing a wind pack. Usage: python android/tests/ui_test.py WORKDIR LAPTOP_PORT"""
import asyncio, io, json, math, os, subprocess, sys, time, urllib.parse
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from build_web import build  # noqa

work, laptop_port = sys.argv[1], int(sys.argv[2])
www = build(os.path.join(work, "www"))
srv = subprocess.Popen([sys.executable, "-m", "http.server", "8095", "-d", www], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)


def std_height(p_hpa):
    # US standard atmosphere, troposphere + lower stratosphere (good enough for a mock)
    p = p_hpa * 100
    if p > 22632.06:
        return 288.15 / 0.0065 * (1 - (p / 101325) ** (287.05287 * 0.0065 / 9.80665))
    return 11000 - 287.05287 * 216.65 / 9.80665 * math.log(p / 22632.06)


def om_response(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    lats = [float(x) for x in q["latitude"][0].split(",")]
    lons = [float(x) for x in q["longitude"][0].split(",")]
    hv = q["hourly"][0].split(",")
    t0 = int(time.mktime(time.strptime(q["start_hour"][0], "%Y-%m-%dT%H:%M"))) - time.timezone
    t1 = int(time.mktime(time.strptime(q["end_hour"][0], "%Y-%m-%dT%H:%M"))) - time.timezone
    times = list(range(t0, t1 + 1, 3600))
    members = [""] + ([f"_member{m:02d}" for m in range(1, 5)] if "ensemble" in url else [])
    ecm = "ecmwf" in url
    out = []
    for la, lo in zip(lats, lons):
        h = {"time": times}
        for v in hv:
            name, lev = v.rsplit("_", 1)
            p = int(lev.replace("hPa", ""))
            z = std_height(p)
            for k, suf in enumerate(members):
                if name == "geopotential_height":
                    val = z
                elif name == "wind_speed":
                    val = 6 + z / 1500 + (2 if ecm else 0) + 0.8 * k
                else:
                    val = 265 + 3 * k
                h[v + suf] = [round(val, 2)] * len(times)
        out.append({"latitude": la, "longitude": lo, "elevation": 1350.0, "hourly": h})
    return out


async def main():
    from PIL import Image
    flat = Image.new("RGB", (256, 256), (128 + 5, 64, 0))   # (133*256 + 64) - 32768 = 1344 m
    buf = io.BytesIO(); flat.save(buf, "PNG"); tile = buf.getvalue()
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context(viewport={"width": 400, "height": 860}, geolocation={"latitude": 39.52, "longitude": -118.48},
                                  permissions=["geolocation"])
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        calls = []

        async def om(route):
            calls.append(route.request.url)
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(om_response(route.request.url)),
                                headers={"Access-Control-Allow-Origin": "*"})
        await pg.route("**/api.open-meteo.com/**", om)
        await pg.route("**/ensemble-api.open-meteo.com/**", om)
        await pg.route("**/elevation-tiles-prod/**", lambda r: r.fulfill(status=200, content_type="image/png", body=tile,
                                                                            headers={"Access-Control-Allow-Origin": "*"}))
        await pg.route("**/tile.opentopomap.org/**", lambda r: r.abort())
        dialogs = iter([f"localhost:{laptop_port}", "1"])
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept(next(dialogs, "")) if d.type == "prompt" else d.accept()))
        await pg.goto("http://localhost:8095/index.html")
        await pg.wait_for_timeout(1200)
        # GPS
        await pg.click("text=Use my location")
        await pg.wait_for_timeout(800)
        lat = await pg.input_value("#lat")
        assert abs(float(lat) - 39.52) < 1e-3, lat
        # Open-Meteo download
        for k in ["gfs", "ecmwf", "gefs"]:
            await pg.evaluate(f"document.querySelector('#dlModels input[value={k}]').checked = true")
        t0 = int(time.time() // 3600 * 3600 + 1800)
        await pg.evaluate(f"""()=>{{const d=new Date({t0}*1000); document.getElementById('when').value=new Date(d-d.getTimezoneOffset()*60e3).toISOString().slice(0,16);}}""")
        await pg.click("#dl")
        await pg.wait_for_function("document.getElementById('log').textContent.includes('Saved')", timeout=60000)
        log1 = await pg.inner_text("#log")
        for k in ["gfs", "ecmwf", "gefs"]:
            await pg.evaluate(f"(()=>{{const c=document.querySelector('#modelChecks input[value={k}]'); if(c && !c.disabled) c.checked=true;}})()")
        await pg.click("#go")
        await pg.wait_for_selector("#out .res", timeout=60000)
        await pg.wait_for_timeout(500)
        out1 = await pg.inner_text("#out")
        await pg.screenshot(path=os.path.join(work, "phone_predict.png"), full_page=False)
        await pg.evaluate("document.getElementById('out').scrollIntoView()")
        await pg.screenshot(path=os.path.join(work, "phone_results.png"))
        # laptop pack import
        await pg.evaluate("document.getElementById('log').textContent=''")
        await pg.click("text=Get winds from laptop")
        await pg.wait_for_function("document.getElementById('log').textContent.includes('Saved on phone') || document.getElementById('log').textContent.includes('ERROR')", timeout=60000)
        log2 = await pg.inner_text("#log")
        opts = await pg.eval_on_selector_all("#pack option", "os => os.map(o => o.value)")
        print("OM calls:", len(calls), "| packs on phone:", opts)
        print("--- download log ---\n" + log1)
        print("--- prediction ---\n" + out1[:700])
        print("--- laptop import ---\n" + log2)
        laptop = [o for o in opts if o.startswith("demo")]
        if laptop:
            await pg.select_option("#pack", laptop[0])
            await pg.wait_for_timeout(300)
            await pg.evaluate("(()=>{for (const k of ['blend','gfs']) {const c=document.querySelector(`#modelChecks input[value=${k}]`); if(c && !c.disabled) c.checked=true;}})()")
            demo_t = await pg.evaluate("packs.find(p=>p.path.startsWith('demo')).t0")
            await pg.evaluate(f"""()=>{{const d=new Date(({demo_t}+600)*1000); document.getElementById('when').value=new Date(d-d.getTimezoneOffset()*60e3).toISOString().slice(0,16);}}""")
            await pg.click("#go")
            await pg.wait_for_selector("#out .res", timeout=60000)
            out2 = await pg.inner_text("#out")
            print("--- prediction from laptop pack ---\n" + out2[:500])
            await pg.screenshot(path=os.path.join(work, "phone_laptop_pack.png"))
        print("page errors:", errs)
        await b.close()
    srv.terminate()

asyncio.run(main())
