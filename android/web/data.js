/* Zephyrus phone data layer: Open-Meteo downloads, laptop wind-pack (.npz) import, on-phone
 * storage (IndexedDB), terrain tiles, and an HTTP helper that falls back to the Android bridge
 * when a server does not allow browser (CORS) requests. */
(function (root) {
  'use strict';
  const E = root.ZephyrusEngine;

  // ------------------------------------------------------------------ HTTP
  const pending = {};
  let reqId = 0;
  root.__zbridge = (id, status, b64) => {           // called by MainActivity
    const p = pending[id]; if (!p) return; delete pending[id];
    const bin = atob(b64 || ''), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    p({ status, bytes });
  };
  function bridgeGet(url) {
    return new Promise((resolve, reject) => {
      const id = 'r' + (++reqId); pending[id] = resolve;
      try { root.ZBridge.httpGet(id, url); } catch (e) { delete pending[id]; reject(e); }
      setTimeout(() => { if (pending[id]) { delete pending[id]; reject(new Error('timeout ' + url)); } }, 180000);
    });
  }
  async function httpGet(url, as = 'json') {
    let status, bytes;
    try {
      const r = await root.__origFetch(url);
      status = r.status; bytes = new Uint8Array(await r.arrayBuffer());
    } catch (e) {
      if (!root.ZBridge) throw e;
      ({ status, bytes } = await bridgeGet(url));   // CORS-blocked or cleartext: go native
    }
    if (as === 'bytes') return { status, bytes };
    const text = new TextDecoder().decode(bytes);
    if (as === 'text') return { status, text };
    let json = null; try { json = JSON.parse(text); } catch (e) { /* not JSON */ }
    return { status, json, text };
  }

  // ------------------------------------------------------------------ Open-Meteo
  const OM = 'https://api.open-meteo.com/v1', OM_ENS = 'https://ensemble-api.open-meteo.com/v1/ensemble';
  // Levels kept modest: Open-Meteo counts every 10 variables per location as one API call.
  const GFS_LEVELS = [1000, 950, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150, 100, 70, 50, 40, 30, 20, 15, 10];
  const ECMWF_LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50];
  const GEFS_LEVELS = [850, 700, 500, 300, 250, 200, 150, 100, 50];
  const GEFS_IDS = ['gfs025', 'ncep_gefs025', 'gfs05', 'ncep_gefs05', 'gfs_seamless', 'ncep_gefs_seamless'];
  const MODELS = {
    gfs: { label: 'GFS 0.25° (Open-Meteo)', url: OM + '/gfs', levels: GFS_LEVELS, grid: [7, 7], dlat: 0.75 },
    ecmwf: { label: 'ECMWF IFS (Open-Meteo)', url: OM + '/ecmwf', levels: ECMWF_LEVELS, grid: [7, 7], dlat: 0.75 },
    gefs: { label: 'GEFS ensemble (Open-Meteo)', url: OM_ENS, levels: GEFS_LEVELS, grid: [3, 3], dlat: 1.5, ids: GEFS_IDS },
  };
  const pad = n => String(n).padStart(2, '0');
  const isoHour = t => { const d = new Date(t * 1000); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:00`; };

  function omGrid(lat0, lon0, spec) {
    const [ny, nx] = spec.grid, dlat = spec.dlat, dlon = dlat / Math.cos(lat0 * Math.PI / 180);
    const lat = Array.from({ length: ny }, (_, j) => +(lat0 + (j - (ny - 1) / 2) * dlat).toFixed(4));
    const lon = Array.from({ length: nx }, (_, i) => +(lon0 + (i - (nx - 1) / 2) * dlon).toFixed(4));
    return { lat, lon };
  }

  /** Convert an Open-Meteo multi-location response into pack sources.
   *  Returns {key: source} — one key, or one per ensemble member. */
  function omToSources(resp, grid, levels, key, isEnsemble) {
    const locs = Array.isArray(resp) ? resp : [resp];
    const Y = grid.lat.length, X = grid.lon.length;
    if (locs.length !== Y * X) throw new Error(`Open-Meteo returned ${locs.length} locations, expected ${Y * X}`);
    const times = locs[0].hourly.time.map(Number), T = times.length;
    const rx = /^(wind_speed|wind_direction|geopotential_height)_(\d+)hPa(?:_member(\d+))?$/;
    const members = new Set();
    for (const k of Object.keys(locs[0].hourly)) { const m = k.match(rx); if (m) members.add(m[3] ? +m[3] : 0); }
    const memList = [...members].sort((a, b) => a - b);
    const out = {};
    for (const mem of memList) {
      const suf = mem ? `_member${pad(mem)}` : '';
      // levels with data at the first location, highest pressure (lowest altitude) first
      const lv = levels.filter(L => { const a = locs[0].hourly[`geopotential_height_${L}hPa${suf}`]; return a && a.some(v => v !== null); })
        .sort((a, b) => b - a);
      if (lv.length < 5) continue;
      const Lc = lv.length, N = T * Lc * Y * X;
      const z = new Float32Array(N), u = new Float32Array(N), v = new Float32Array(N), terrain = new Float32Array(Y * X);
      for (let j = 0; j < Y; j++) for (let i = 0; i < X; i++) {
        const loc = locs[j * X + i], h = loc.hourly;
        terrain[j * X + i] = Number.isFinite(loc.elevation) ? loc.elevation : NaN;
        lv.forEach((L, l) => {
          const zz = h[`geopotential_height_${L}hPa${suf}`], ss = h[`wind_speed_${L}hPa${suf}`], dd = h[`wind_direction_${L}hPa${suf}`];
          for (let t = 0; t < T; t++) {
            const idx = ((t * Lc + l) * Y + j) * X + i;
            const gz = zz ? zz[t] : null, sp = ss ? ss[t] : null, dr = dd ? dd[t] : null;
            z[idx] = gz == null ? NaN : 6371008.8 * gz / (6371008.8 - gz);
            u[idx] = sp == null || dr == null ? 0 : -sp * Math.sin(dr * Math.PI / 180);
            v[idx] = sp == null || dr == null ? 0 : -sp * Math.cos(dr * Math.PI / 180);
          }
        });
      }
      // fill missing heights in a column from the level below/above so the column stays monotonic
      for (let t = 0; t < T; t++) for (let c = 0; c < Y * X; c++) for (let l = 0; l < Lc; l++) {
        const idx = (t * Lc + l) * Y * X + c;
        if (!Number.isFinite(z[idx])) z[idx] = E.pressureToAltitude(lv[l] * 100);
      }
      const name = isEnsemble ? `${key}-${mem ? 'p' + pad(mem) : 'c00'}` : key;
      out[name] = { times: Float64Array.from(times), levels: Float32Array.from(lv), lat: Float64Array.from(grid.lat),
        lon: Float64Array.from(grid.lon), z, u, v, terrain };
    }
    return out;
  }

  async function fetchOpenMeteo(key, lat, lon, start, hours, log) {
    const spec = MODELS[key], grid = omGrid(lat, lon, spec);
    const lats = [], lons = [];
    for (const la of grid.lat) for (const lo of grid.lon) { lats.push(la); lons.push(lo); }
    const vars = [];
    for (const L of spec.levels) vars.push(`wind_speed_${L}hPa`, `wind_direction_${L}hPa`, `geopotential_height_${L}hPa`);
    const t0 = Math.floor(start / 3600) * 3600 - 3600, t1 = t0 + Math.ceil(hours + 2) * 3600;
    const q = `latitude=${lats.join(',')}&longitude=${lons.join(',')}&hourly=${vars.join(',')}` +
      `&wind_speed_unit=ms&timeformat=unixtime&timezone=GMT&start_hour=${isoHour(t0)}&end_hour=${isoHour(t1)}`;
    const ids = spec.ids || [null];
    let lastErr = null;
    for (const id of ids) {
      const url = `${spec.url}?${q}${id ? '&models=' + id : ''}`;
      log(`${spec.label}: requesting ${lats.length} points × ${spec.levels.length} levels${id ? ' (' + id + ')' : ''}`);
      const r = await httpGet(url, 'json');
      if (r.status === 200 && r.json && !r.json.error) {
        const src = omToSources(r.json, grid, spec.levels, key, !!spec.ids);
        const n = Object.keys(src).length;
        if (n) { log(`  ${spec.label}: ${n > 1 ? n + ' members, ' : ''}${src[Object.keys(src)[0]].levels.length} levels, ${(r.text || '').length ? '' : ''}OK`); return src; }
        lastErr = 'no pressure-level winds in the response';
      } else {
        lastErr = (r.json && r.json.reason) || `HTTP ${r.status}`;
        if (r.status === 429) { lastErr = 'Open-Meteo rate limit reached; wait a minute and try again'; break; }
      }
    }
    throw new Error(`${spec.label}: ${lastErr}`);
  }

  // ------------------------------------------------------------------ terrain tiles (Terrarium)
  const TILE = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';
  const tileXY = (lat, lon, z) => { const n = 2 ** z, lr = lat * Math.PI / 180;
    return [(lon + 180) / 360 * n, (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n]; };
  async function decodePng(bytes) {
    const bmp = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
    const cv = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(256, 256) : Object.assign(document.createElement('canvas'), { width: 256, height: 256 });
    const ctx = cv.getContext('2d'); ctx.drawImage(bmp, 0, 0);
    const px = ctx.getImageData(0, 0, 256, 256).data, h = new Int16Array(256 * 256);
    for (let i = 0; i < h.length; i++) h[i] = Math.round(px[i * 4] * 256 + px[i * 4 + 1] + px[i * 4 + 2] / 256 - 32768);
    return h;
  }
  async function getTile(z, x, y, offline) {
    const key = `${z}/${x}/${y}`;
    let bytes = await Store.get('tiles', key);
    if (!bytes) {
      if (offline) return null;
      const r = await httpGet(TILE.replace('{z}', z).replace('{x}', x).replace('{y}', y), 'bytes').catch(() => null);
      if (!r || r.status !== 200) return null;
      bytes = r.bytes; await Store.put('tiles', key, bytes);
    }
    return decodePng(bytes);
  }
  async function buildDem(lat, lon, radiusKm, zoom, log, offline = false) {
    const dlat = radiusKm / 111.2, dlon = radiusKm / (111.2 * Math.cos(lat * Math.PI / 180));
    const [fx0, fy0] = tileXY(lat + dlat, lon - dlon, zoom), [fx1, fy1] = tileXY(lat - dlat, lon + dlon, zoom);
    const x0 = Math.floor(fx0), y0 = Math.floor(fy0), x1 = Math.floor(fx1), y1 = Math.floor(fy1);
    const w = (x1 - x0 + 1) * 256, h = (y1 - y0 + 1) * 256, Z = new Int16Array(w * h).fill(-32768);
    if (log) log(`Terrain: ${(x1 - x0 + 1) * (y1 - y0 + 1)} tiles at zoom ${zoom}`);
    let got = 0;
    const jobs = [];
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) jobs.push([x, y]);
    await Promise.all(jobs.map(async ([x, y]) => {
      const t = await getTile(zoom, x, y, offline).catch(() => null); if (!t) return; got++;
      for (let r = 0; r < 256; r++) Z.set(t.subarray(r * 256, r * 256 + 256), ((y - y0) * 256 + r) * w + (x - x0) * 256);
    }));
    if (!got) throw new Error('no terrain tiles available');
    return { z: Z, w, h, zoom, x0, y0 };
  }

  // ------------------------------------------------------------------ .npz reader (laptop packs)
  async function inflateRaw(bytes) {
    if (typeof DecompressionStream === 'undefined') throw new Error('this phone cannot unzip packs');
    const ds = new DecompressionStream('deflate-raw');
    const out = new Response(new Blob([bytes]).stream().pipeThrough(ds));
    return new Uint8Array(await out.arrayBuffer());
  }
  function parseNpy(buf) {
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const major = buf[6], hlen = major === 1 ? dv.getUint16(8, true) : dv.getUint32(8, true), hoff = major === 1 ? 10 : 12;
    const header = new TextDecoder('latin1').decode(buf.subarray(hoff, hoff + hlen));
    const descr = header.match(/'descr':\s*'([^']+)'/)[1];
    const shape = (header.match(/'shape':\s*\(([^)]*)\)/)[1].match(/\d+/g) || []).map(Number);
    const count = shape.reduce((a, b) => a * b, 1), off = hoff + hlen, raw = buf.slice(off);
    const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
    let data;
    if (descr === '<f4') data = new Float32Array(ab, 0, count);
    else if (descr === '<f8') data = new Float64Array(ab, 0, count);
    else if (descr === '<i2') data = new Int16Array(ab, 0, count);
    else if (descr === '<i4') data = new Int32Array(ab, 0, count);
    else if (descr === '<i8') data = Float64Array.from(new BigInt64Array(ab, 0, count), Number);
    else if (/^<U\d+$/.test(descr)) {
      const n = +descr.slice(2), u32 = new Uint32Array(ab, 0, n);
      data = String.fromCodePoint(...Array.from(u32).filter(c => c));
    } else throw new Error('unsupported dtype ' + descr);
    return { shape, data };
  }
  async function readZip(bytes) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), files = {};
    let eocd = -1; for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 70000); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    if (eocd < 0) throw new Error('not a zip/npz file');
    let n = dv.getUint16(eocd + 10, true), cdOff = dv.getUint32(eocd + 16, true);
    if (cdOff === 0xffffffff || n === 0xffff) {              // zip64 end of central directory
      const loc = eocd - 20; const z64 = Number(dv.getBigUint64(loc + 8, true));
      n = Number(dv.getBigUint64(z64 + 32, true)); cdOff = Number(dv.getBigUint64(z64 + 48, true));
    }
    let p = cdOff;
    for (let k = 0; k < n; k++) {
      const method = dv.getUint16(p + 10, true); let csize = dv.getUint32(p + 20, true), usize = dv.getUint32(p + 24, true);
      const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
      let lho = dv.getUint32(p + 42, true);
      const name = new TextDecoder().decode(bytes.subarray(p + 46, p + 46 + nlen));
      let x = p + 46 + nlen; const xe = x + xlen;
      while (x + 4 <= xe) {                                   // zip64 extra field
        const id = dv.getUint16(x, true), sz = dv.getUint16(x + 2, true);
        if (id === 1) { let q = x + 4;
          if (usize === 0xffffffff) { usize = Number(dv.getBigUint64(q, true)); q += 8; }
          if (csize === 0xffffffff) { csize = Number(dv.getBigUint64(q, true)); q += 8; }
          if (lho === 0xffffffff) { lho = Number(dv.getBigUint64(q, true)); q += 8; } }
        x += 4 + sz;
      }
      const lnl = dv.getUint16(lho + 26, true), lxl = dv.getUint16(lho + 28, true), start = lho + 30 + lnl + lxl;
      const comp = bytes.subarray(start, start + csize);
      files[name] = method === 0 ? comp : method === 8 ? await inflateRaw(comp) : null;
      if (!files[name]) throw new Error('unsupported zip compression in ' + name);
      p += 46 + nlen + xlen + clen;
    }
    return files;
  }
  async function readPack(bytes) {
    const files = await readZip(bytes), data = {}; let meta = {};
    for (const [fname, buf] of Object.entries(files)) {
      const key = fname.replace(/\.npy$/, ''), arr = parseNpy(buf);
      if (key === 'meta') { meta = JSON.parse(arr.data); continue; }
      const us = key.indexOf('_'), src = key.slice(0, us), field = key.slice(us + 1);
      (data[src] = data[src] || {})[field] = arr.shape.length ? arr.data : arr.data[0];
      if (src === 'dem' && field === 'z') { data.dem.h = arr.shape[0]; data.dem.w = arr.shape[1]; }
    }
    if (data.dem) { for (const f of ['zoom', 'x0', 'y0']) data.dem[f] = Number(data.dem[f]); }
    return { data, meta };
  }

  // ------------------------------------------------------------------ IndexedDB store
  const Store = {
    _db: null,
    open() {
      if (this._db) return this._db;
      this._db = new Promise((res, rej) => {
        const r = indexedDB.open('zephyrus', 1);
        r.onupgradeneeded = () => { r.result.createObjectStore('packs'); r.result.createObjectStore('tiles'); };
        r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
      });
      return this._db;
    },
    async tx(store, mode, fn) {
      const db = await this.open();
      return new Promise((res, rej) => { const t = db.transaction(store, mode), s = t.objectStore(store), q = fn(s);
        t.oncomplete = () => res(q && q.result); t.onerror = () => rej(t.error); });
    },
    get(store, key) { return this.tx(store, 'readonly', s => s.get(key)); },
    put(store, key, val) { return this.tx(store, 'readwrite', s => s.put(val, key)); },
    del(store, key) { return this.tx(store, 'readwrite', s => s.delete(key)); },
    keys(store) { return this.tx(store, 'readonly', s => s.getAllKeys()); },
  };

  root.ZephyrusData = { httpGet, MODELS, fetchOpenMeteo, omToSources, omGrid, buildDem, getTile, readPack, readZip, parseNpy, Store, tileXY };
})(typeof window !== 'undefined' ? window : globalThis);
