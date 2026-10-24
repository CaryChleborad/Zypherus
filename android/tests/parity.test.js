// node android/tests/parity.test.js OUTDIR   — compares the JS engine with Python reference results
const fs = require('fs'), path = require('path');
const W = path.join(__dirname, '..', 'web');
require(path.join(W, 'engine.js')); globalThis.__origFetch = fetch; require(path.join(W, 'data.js'));
const E = globalThis.ZephyrusEngine, D = globalThis.ZephyrusData;
const out = process.argv[2];
let fails = 0; const ok = (c, m) => { if (!c) { fails++; console.log('FAIL', m); } };
(async () => {
  const ref = JSON.parse(fs.readFileSync(path.join(out, 'ref.json')));
  const { data, meta } = await D.readPack(new Uint8Array(fs.readFileSync(path.join(out, 'ref.npz'))));
  ok(meta.box.length === 4, 'meta box'); ok(data.dem && data.dem.zoom === 9 && data.dem.w === 768, 'dem parsed');
  const base = new E.WindField(data, 'gfs');
  let worst = 0;
  for (const c of ref.cases) {
    const r = E.summary(E.simulate(base.member(c.mode), c.p, {}, { startAlt: c.start_alt }));
    const d = E.haversine(r.landing.lat, r.landing.lon, c.landing[0], c.landing[1]);
    worst = Math.max(worst, d);
    ok(d < 5, `${c.mode} ${JSON.stringify(c.p).slice(0, 60)} landing differs by ${d.toFixed(2)} m`);
    ok(Math.abs(r.landing.time - c.landing[2]) < 1, `${c.mode} landing time ${r.landing.time - c.landing[2]} s`);
    ok(Math.abs(r.landing.alt - c.landing[3]) < 0.5, `${c.mode} landing alt ${r.landing.alt} vs ${c.landing[3]}`);
  }
  console.log(`flights: ${ref.cases.length} cases, worst landing difference ${worst.toFixed(3)} m`);
  for (const [la, lo, h] of ref.terrain) { const j = base.terrain(la, lo); ok(Math.abs(j - h) < 0.01, `terrain ${la},${lo}: ${j} vs ${h}`); }
  const b = { balloon_g: 1200, burst_d: 8.63, cd: 0.25, payload_g: 1500, neck_lift_g: 2900, gas: 'he' };
  const ph = E.summary(E.simulate(base.member('gfs'), { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000, balloon: b }));
  ok(Math.abs(ph.burst.alt - ref.physics.burst_alt) < 0.5, `physics burst ${ph.burst.alt} vs ${ref.physics.burst_alt}`);
  ok(E.haversine(ph.landing.lat, ph.landing.lon, ...ref.physics.landing) < 5, 'physics landing');
  const mc = E.summary(E.simulate(base.member('gfs'), { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000 }, { members: 400 }));
  const rr = mc.ensemble.r95_km / ref.mc.r95_km;
  console.log(`Monte Carlo r95: JS ${mc.ensemble.r95_km.toFixed(2)} km vs Python ${ref.mc.r95_km.toFixed(2)} km (different random numbers)`);
  ok(rr > 0.8 && rr < 1.25, 'MC spread within 25%');
  const w = E.launchWindow(base, { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000 }, 'gfs', 1.8e9, 1.8e9 + 3 * 3600, 1, [39.6, -117.5]);
  ok(w.rows.length === ref.window.length, 'window rows');
  w.rows.forEach((x, i) => ok(E.haversine(x.lat, x.lon, ...ref.window[i]) < 5, 'window row ' + i));
  const rv = E.launchSiteForTarget(base, { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000 }, 'gfs', [39.55, -117.6]);
  ok(rv.miss_km < 0.11, 'reverse ' + rv.miss_km);
  const g = E.runModels(base, { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000 }, { members: 0 }, ['gefs'], { gefsMcPerMember: 0 });
  g.models.gefs.summary.member_landings.forEach((x, i) => ok(E.haversine(...x, ...ref.gefs_members[i]) < 5, 'gefs member ' + i));
  const t0 = Date.now(); E.runModels(base, { lat: 39.5, lon: -118.5, launch_time: 1.8e9 + 900, descent_rate: 7, ascent_rate: 5.1, burst_alt: 30000 }, { members: 200 }, ['blend', 'ecmwf', 'gfs', 'gefs'], {});
  console.log(`4 models x 201 runs + GEFS: ${((Date.now() - t0) / 1000).toFixed(1)} s in Node`);
  // exports are well-formed enough to parse
  ok(E.toKml(g).startsWith('<?xml') && E.toGpx(g).includes('<wpt'), 'exports');
  console.log(fails ? `${fails} FAILURES` : 'ALL PARITY CHECKS PASSED'); process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
