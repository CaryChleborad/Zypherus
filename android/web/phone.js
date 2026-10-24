/* Zephyrus phone glue. The map page (same index.html as the laptop version) talks to "/api/...".
 * On the phone there is no Python server, so this file answers those requests locally with the
 * JavaScript engine (engine.js) and data layer (data.js), and adds phone-only controls. */
(function () {
  'use strict';
  const E = window.ZephyrusEngine, D = window.ZephyrusData, S = D.Store;
  window.__origFetch = window.fetch.bind(window);

  // ------------------------------------------------------------------ packs
  const packCache = { name: null, data: null, meta: null, base: null };
  function packInfo(name, data, meta, bytes) {
    const keys = Object.keys(data).filter(k => k !== 'dem' && !k.startsWith('raob'));
    const t0 = Math.max(...keys.map(k => data[k].times[0])), t1 = Math.min(...keys.map(k => data[k].times[data[k].times.length - 1]));
    return { path: name, choices: E.availableChoices(data), box: meta.box, t0, t1, cycles: meta.cycles || {},
      created: meta.created, errors: meta.errors || {}, dem: !!data.dem, size_mb: +(bytes / 1e6).toFixed(1), source: meta.source || 'laptop' };
  }
  function packBytes(data) { let n = 0; for (const s of Object.values(data)) for (const v of Object.values(s)) if (v && v.byteLength) n += v.byteLength; return n; }
  async function savePack(name, data, meta) {
    const info = packInfo(name, data, meta, packBytes(data));
    await S.put('packs', 'data:' + name, { data, meta });
    await S.put('packs', 'info:' + name, info);
    return info;
  }
  async function listPacks() {
    const keys = (await S.keys('packs')).filter(k => String(k).startsWith('info:'));
    const infos = await Promise.all(keys.map(k => S.get('packs', k)));
    return infos.filter(Boolean).sort((a, b) => (b.created || '').localeCompare(a.created || ''));
  }
  async function loadWF(name) {
    if (packCache.name !== name) {
      const rec = await S.get('packs', 'data:' + name);
      if (!rec) throw new Error('wind data not found on this phone: ' + name);
      packCache.name = name; packCache.data = rec.data; packCache.meta = rec.meta;
      const ch = E.availableChoices(rec.data);
      packCache.base = new E.WindField(rec.data, rec.data.gfs ? 'gfs' : ch[0]);
      packCache.base.meta = rec.meta;
    }
    return packCache.base;
  }

  // ------------------------------------------------------------------ jobs (downloads)
  const jobs = {}; let nj = 0;
  function job(fn) {
    const id = 'j' + (++nj); jobs[id] = { status: 'running', log: [], result: null, error: null };
    const log = m => jobs[id].log.push(m);
    fn(log).then(r => { jobs[id].result = r; jobs[id].status = 'done'; })
      .catch(e => { console.error(e); jobs[id].error = e.message || String(e); jobs[id].status = 'error'; });
    return id;
  }
  async function downloadOM(j, log) {
    const lat = +j.lat, lon = +j.lon, start = +j.launch_time, hours = +j.hours || 4;
    const models = (j.models && j.models.length ? j.models : ['gfs']).filter(m => D.MODELS[m]);
    if (!models.includes('gfs')) models.unshift('gfs');            // GFS tops up every other model
    const data = {}, errors = {}, cycles = {};
    for (const m of models) {
      try { Object.assign(data, await D.fetchOpenMeteo(m, lat, lon, start, hours, log)); cycles[m] = 'Open-Meteo'; }
      catch (e) { errors[m] = e.message; log(`${m} failed: ${e.message}`); }
    }
    if (!Object.keys(data).length) throw new Error('No wind data downloaded: ' + JSON.stringify(errors));
    if (+j.dem_zoom !== 0) {
      try { data.dem = await D.buildDem(lat, lon, 250, Math.min(+j.dem_zoom || 9, 10), log); }
      catch (e) { errors.dem = e.message; log('Terrain download failed; using model terrain'); }
    }
    const lats = Object.values(data).filter(s => s.lat).map(s => [s.lat[0], s.lat[s.lat.length - 1], s.lon[0], s.lon[s.lon.length - 1]]);
    const box = [Math.max(...lats.map(b => b[0])), Math.min(...lats.map(b => b[1])), Math.max(...lats.map(b => b[2])), Math.min(...lats.map(b => b[3]))];
    const d = new Date(start * 1000), pad = n => String(n).padStart(2, '0');
    const name = `phone_${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}_${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}Z_${lat.toFixed(2)}_${lon.toFixed(2)}`;
    const meta = { launch_lat: lat, launch_lon: lon, box, cycles, errors, created: new Date().toISOString(), source: 'Open-Meteo' };
    const info = await savePack(name, data, meta);
    log(`Saved ${name} (${info.size_mb} MB): ${Object.keys(data).join(', ')}`);
    return name;
  }

  // ------------------------------------------------------------------ request params
  function params(j) {
    let balloon = null;
    if (j.ascent_mode === 'physics') {
      const b = E.BALLOONS[j.balloon]; const [mass, bd, cd] = b || [+j.balloon_g, +j.burst_d, 0.3];
      balloon = { balloon_g: mass, burst_d: bd, cd: j.cd ? +j.cd : cd, payload_g: +j.payload_g, neck_lift_g: +j.neck_lift_g,
        gas: j.gas || 'he', profile: j.ascent_profile || 'constant' };
    }
    const p = { lat: +j.lat, lon: +j.lon, launch_time: +j.launch_time, ascent_rate: +(j.ascent_rate ?? 5), burst_alt: +(j.burst_alt ?? 30480),
      descent_rate: +(j.descent_rate ?? 6), launch_alt: j.launch_alt == null || j.launch_alt === '' ? null : +j.launch_alt,
      balloon, descent_factor: +(j.descent_factor || 1) };
    const unc = { members: +(j.members || 0) };
    for (const [k, v] of Object.entries(j.unc || {})) if (k in E.UNC_DEFAULTS) unc[k] = +v;
    return [p, unc];
  }
  async function refineTerrain(wf, lat, lon) {
    try { wf.addDem(await D.buildDem(lat, lon, 20, 12, null)); return true; } catch (e) { return false; }
  }

  // ------------------------------------------------------------------ live telemetry
  const live = {};
  const addFixes = (cs, rows) => { const m = live[cs.toUpperCase()] = live[cs.toUpperCase()] || new Map();
    rows.forEach(r => m.set(r[0], r)); return [...m.values()].sort((a, b) => a[0] - b[0]); };
  async function sondehub(cs) {
    const r = await D.httpGet(`https://api.v2.sondehub.org/amateur/telemetry/${encodeURIComponent(cs)}?last=21600`, 'json');
    if (r.status !== 200) throw new Error('SondeHub HTTP ' + r.status);
    const js = r.json, items = Array.isArray(js) ? js : Object.values(js).flatMap(v => (v && typeof v === 'object') ? Object.values(v) : []);
    const rows = []; for (const it of items) { const t = Date.parse(it.datetime) / 1000;
      if (Number.isFinite(t) && it.lat != null) rows.push([t, +it.lat, +it.lon, +it.alt]); }
    return addFixes(cs, rows);
  }
  async function aprsfi(cs, key) {
    const r = await D.httpGet(`https://api.aprs.fi/api/get?name=${encodeURIComponent(cs)}&what=loc&apikey=${encodeURIComponent(key)}&format=json`, 'json');
    if (!r.json || r.json.result !== 'ok') throw new Error((r.json && r.json.description) || 'aprs.fi error');
    return addFixes(cs, (r.json.entries || []).filter(e => 'altitude' in e).map(e => [+(e.lasttime || e.time), +e.lat, +e.lng, +e.altitude]));
  }

  // ------------------------------------------------------------------ router
  const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json' } });
  async function route(path, init) {
    const body = init && typeof init.body === 'string' ? JSON.parse(init.body) : null;
    const wrap = async fn => { try { return reply(await fn()); } catch (e) { console.error(e); return reply({ error: e.message || String(e) }, 400); } };
    if (path === '/api/config') return reply({
      models: Object.fromEntries(Object.entries(D.MODELS).map(([k, m]) => [k, { label: m.label }])), download_models: ['gfs', 'ecmwf', 'gefs'],
      choices: E.CHOICES, colors: Object.fromEntries(Object.keys(E.CHOICES).map(k => [k, E.color(k)])),
      balloons: Object.fromEntries(Object.entries(E.BALLOONS).map(([k, v]) => [k, { mass_g: v[0], burst_d: v[1], cd: v[2] }])), stations: {}, phone: true });
    if (path === '/api/packs') return wrap(listPacks);
    if (path === '/api/fetch') return reply({ job: job(log => downloadOM(body, log)) });
    if (path.startsWith('/api/job/')) { const jb = jobs[path.slice(9)]; return jb ? reply({ ...jb, log: jb.log.slice(-60) }) : reply({ status: 'unknown' }, 404); }
    if (path === '/api/predict') return wrap(async () => {
      const wf = await loadWF(body.pack), [p, unc] = params(body);
      if (body.refine) { const first = body.models && body.models[0] || 'gfs';
        try { const w0 = wf.member(first === 'gefs' ? E.gefsMembers(wf.data)[0] : first); const s = E.summary(E.simulate(w0, p, {}));
          await refineTerrain(wf, s.landing.lat, s.landing.lon); } catch (e) { /* offline: keep coarse terrain */ } }
      const res = E.runModels(wf, p, unc, body.models || ['gfs'], { gefsMcPerMember: +(body.gefs_mc ?? 5) });
      res.kml = E.toKml(res, { lat: p.lat, lon: p.lon, time: p.launch_time }); res.gpx = E.toGpx(res);
      res.cycles = wf.meta.cycles || {};
      return res;
    });
    if (path === '/api/window') return wrap(async () => { const wf = await loadWF(body.pack), [p] = params(body);
      return E.launchWindow(wf, p, body.model || 'gfs', +body.t_start, +body.t_end, +(body.step_h || 1), body.target || null); });
    if (path === '/api/reverse') return wrap(async () => { const wf = await loadWF(body.pack), [p] = params(body);
      return E.launchSiteForTarget(wf, p, body.model || 'gfs', body.target); });
    if (path === '/api/balloon') return wrap(async () => {
      const bb = E.BALLOONS[body.balloon] || [+body.balloon_g, +body.burst_d, 0.3];
      const b = { balloon_g: bb[0], burst_d: bb[1], cd: body.cd ? +body.cd : bb[2], payload_g: +body.payload_g, neck_lift_g: +body.neck_lift_g,
        gas: body.gas || 'he', profile: body.ascent_profile || 'constant' };
      const la = +(body.launch_alt || 0), out = { mass_g: b.balloon_g, burst_d: b.burst_d, cd: b.cd };
      if (body.target_rate) out.neck_lift_for_target_g = E.neckLiftForRate(+body.target_rate, b, la);
      return Object.assign(out, E.balloonSummary(b, la));
    });
    if (path === '/api/live/poll') return wrap(async () => {
      const cs = body.callsign.trim();
      const fixes = body.source === 'aprsfi' ? await aprsfi(cs, body.apikey) : body.source === 'sondehub' ? await sondehub(cs) : addFixes(cs, []);
      return { fixes };
    });
    if (path === '/api/live/manual') return wrap(async () => ({ fixes: addFixes(body.callsign, [[+body.t, +body.lat, +body.lon, +body.alt]]) }));
    if (path === '/api/live/clear') { delete live[String(body.callsign).toUpperCase()]; return reply({ ok: true }); }
    if (path === '/api/live/predict') return wrap(async () => {
      const fixes = addFixes(body.callsign, []); if (fixes.length < 2) throw new Error('need at least 2 telemetry fixes');
      const wf = await loadWF(body.pack), last = fixes[fixes.length - 1];
      const [p, unc] = params({ ...body, lat: last[1], lon: last[2], launch_time: last[0] });
      const res = E.inFlight(wf, fixes, p, unc, body.models || ['gfs'], { useMeasured: body.use_measured !== false, gefsMcPerMember: +(body.gefs_mc ?? 5) });
      res.kml = E.toKml(res, null, fixes.map(f => [f[1], f[2], f[3]])); res.gpx = E.toGpx(res);
      return res;
    });
    if (path === '/api/hindcast') return reply({ error: 'Hindcasts run on the laptop version of Zephyrus.' }, 400);
    if (path === '/api/calibration') return reply({});
    return reply({ error: 'unknown request ' + path }, 404);
  }
  window.fetch = (url, init) => {
    const u = typeof url === 'string' ? url : url.url;
    if (u.startsWith('/api/')) return route(u, init);
    return window.__origFetch(url, init);
  };

  // ------------------------------------------------------------------ phone-only controls
  window.zephyrusPhoneInit = ({ $, map, setLaunch, loadPacks }) => {
    const btn = (label, onclick) => { const b = document.createElement('button'); b.textContent = label; b.onclick = onclick; return b; };
    // GPS launch point
    const gps = btn('Use my location', () => {
      if (!navigator.geolocation) { alert('Location not available'); return; }
      gps.disabled = true; gps.textContent = 'Locating…';
      navigator.geolocation.getCurrentPosition(pos => {
        setLaunch(L.latLng(pos.coords.latitude, pos.coords.longitude)); map.setView([pos.coords.latitude, pos.coords.longitude], 10);
        if (pos.coords.altitude != null) { const imp = $('units').value === 'imp'; $('lalt').value = Math.round(imp ? pos.coords.altitude / 0.3048 : pos.coords.altitude); }
        gps.disabled = false; gps.textContent = 'Use my location';
      }, err => { alert('Location failed: ' + err.message); gps.disabled = false; gps.textContent = 'Use my location'; },
      { enableHighAccuracy: true, timeout: 30000 });
    });
    const links = $('dl').parentElement; links.prepend(gps);
    $('dl').textContent = 'Download winds (Open-Meteo)';
    // Laptop wind packs (exact desktop data incl. HRRR/RAP)
    const fromLaptop = btn('Get winds from laptop', async () => {
      const last = localStorage.getItem('bp_laptop') || '192.168.0.25:8080';
      const addr = prompt('Laptop address shown in the Zephyrus window (Running on http://...):', last);
      if (!addr) return; localStorage.setItem('bp_laptop', addr);
      const base = 'http://' + addr.replace(/^https?:\/\//, '').replace(/\/$/, '');
      const log = m => { $('log').textContent += m + '\n'; };
      try {
        log('Contacting laptop ' + base + ' …');
        const r = await D.httpGet(base + '/api/packs', 'json');
        if (!r.json || !r.json.length) throw new Error('no wind data on the laptop');
        const choice = prompt('Which wind pack?\n' + r.json.map((p, i) => `${i + 1}. ${p.path} (${p.size_mb} MB)`).join('\n'), '1');
        const pk = r.json[(+choice || 1) - 1]; if (!pk) return;
        log(`Downloading ${pk.path} (${pk.size_mb} MB)…`);
        const f = await D.httpGet(base + '/api/pack/' + encodeURIComponent(pk.path) + '?phone=1', 'bytes');
        if (f.status !== 200) throw new Error('HTTP ' + f.status);
        const { data, meta } = await D.readPack(f.bytes); meta.source = 'laptop';
        await savePack(pk.path.replace(/\.npz$/, ''), data, meta);
        log('Saved on phone: ' + Object.keys(data).join(', ')); await loadPacks();
      } catch (e) { log('ERROR: ' + e.message); }
    });
    const importFile = btn('Import pack file', () => {
      const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.npz,application/octet-stream,application/zip';
      inp.onchange = async () => {
        const f = inp.files[0]; if (!f) return;
        try { const { data, meta } = await D.readPack(new Uint8Array(await f.arrayBuffer())); meta.source = 'file';
          await savePack(f.name.replace(/\.npz$/, ''), data, meta); $('log').textContent += `Imported ${f.name}\n`; await loadPacks(); }
        catch (e) { $('log').textContent += 'ERROR: ' + e.message + '\n'; }
      };
      inp.click();
    });
    links.append(fromLaptop, importFile);
    // keep the screen on while auto-updating a live flight
    $('lauto').addEventListener('change', () => { try { window.ZBridge && ZBridge.keepScreenOn($('lauto').checked); } catch (e) { /* not on Android */ } });
    // Hindcast runs on the laptop
    const h = $('p-hind'); if (h) h.innerHTML = '<p class="note">Hindcasts download large archived forecast files, so they run on the laptop version of Zephyrus. Results there carry over as calibration you can copy into Setup.</p>';
    // Saved packs: delete
    const pl = $('packList');
    if (pl) { const del = btn('Delete selected wind data', async () => { const n = $('pack').value; if (!n || !confirm('Delete ' + n + '?')) return;
      await S.del('packs', 'data:' + n); await S.del('packs', 'info:' + n); packCache.name = null; await loadPacks(); });
      pl.parentElement.insertBefore(del, pl.nextSibling); }
  };
})();
