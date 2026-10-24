/* Zephyrus flight engine — JavaScript port of the Python package (zephyrus/*.py).
 * Same physics, same interpolation, same blend rules; checked against the Python engine by
 * android/tests/parity.test.js. Pure computation: no DOM, no network.
 *
 * Wind pack in memory (same layout as the .npz pack):
 *   data[source] = {times: Float64Array[T], levels: Float32Array[L], lat: Float64Array[Y],
 *                   lon: Float64Array[X], z,u,v: Float32Array[T*L*Y*X] (C order), terrain: Float32Array[Y*X]}
 *   data.dem = {z: Int16Array[H*W], h, w, zoom, x0, y0}   (optional Terrarium mosaic)
 */
(function (root) {
  'use strict';
  const EARTH_R = 6371008.8, G0 = 9.80665, R_AIR = 287.05287, RHO0 = 1.225;
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;

  // ------------------------------------------------------------------ atmosphere (US Std 1976)
  const LAYERS = [[0, 288.15, -0.0065, 101325.0], [11000, 216.65, 0, 22632.06], [20000, 216.65, 0.001, 5474.889],
    [32000, 228.65, 0.0028, 868.0187], [47000, 270.65, 0, 110.9063], [51000, 270.65, -0.0028, 66.93887],
    [71000, 214.65, -0.002, 3.956420]];
  function tempPressure(h) {
    let i = 0;
    for (let k = 0; k < LAYERS.length; k++) if (h >= LAYERS[k][0]) i = k;
    const [hb, tb, lr, pb] = LAYERS[i];
    const t = tb + lr * (h - hb);
    const p = lr === 0 ? pb * Math.exp(-G0 * (h - hb) / (R_AIR * tb)) : pb * Math.pow(tb / t, G0 / (R_AIR * lr));
    return [t, p];
  }
  function density(h) { const [t, p] = tempPressure(h); return p / (R_AIR * t); }
  const RHO_TAB = new Float64Array(9100);              // -1000 .. 90000 m, 10 m steps
  for (let i = 0; i < RHO_TAB.length; i++) RHO_TAB[i] = density(-1000 + 10 * i);
  function densityFast(h) {
    let f = (h + 1000) / 10;
    if (f <= 0) return RHO_TAB[0];
    if (f >= RHO_TAB.length - 1) return RHO_TAB[RHO_TAB.length - 1];
    const i = Math.floor(f); f -= i;
    return RHO_TAB[i] * (1 - f) + RHO_TAB[i + 1] * f;
  }
  function pressureToAltitude(pPa) {     // inverse std atmosphere by bisection
    let lo = -500, hi = 85000;
    for (let k = 0; k < 60; k++) { const m = (lo + hi) / 2; if (tempPressure(m)[1] > pPa) lo = m; else hi = m; }
    return (lo + hi) / 2;
  }
  const descentSpeed = (v0, alt) => v0 * Math.sqrt(RHO0 / densityFast(Math.max(alt, -400)));

  // ------------------------------------------------------------------ helpers
  function interpIndex(x, arr) {          // np.interp(x, arr, 0..n-1) for ascending arr
    const n = arr.length;
    if (x <= arr[0]) return 0;
    if (x >= arr[n - 1]) return n - 1;
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m; else hi = m; }
    return lo + (x - arr[lo]) / (arr[hi] - arr[lo]);
  }
  function interp(x, xs, ys) {
    const f = interpIndex(x, xs), i = Math.min(Math.floor(f), xs.length - 2);
    if (xs.length === 1) return ys[0];
    const w = f - i; return ys[i] * (1 - w) + ys[i + 1] * w;
  }
  function haversine(la1, lo1, la2, lo2) {
    const p1 = la1 * D2R, p2 = la2 * D2R, dp = p2 - p1, dl = (lo2 - lo1) * D2R;
    const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * EARTH_R * Math.asin(Math.sqrt(Math.min(1, Math.max(0, a))));
  }
  function bearing(la1, lo1, la2, lo2) {
    const p1 = la1 * D2R, p2 = la2 * D2R, dl = (lo2 - lo1) * D2R;
    const y = Math.sin(dl) * Math.cos(p2), x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return (Math.atan2(y, x) * R2D + 360) % 360;
  }
  // seeded RNG (mulberry32) + normal (Box-Muller)
  function rng(seed) {
    let s = (seed >>> 0) || 1, spare = null;
    const u = () => { s |= 0; s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
    return {
      uniform: u,
      normal(mu, sd) {
        if (spare !== null) { const z = spare; spare = null; return mu + sd * z; }
        let a, b, r; do { a = 2 * u() - 1; b = 2 * u() - 1; r = a * a + b * b; } while (r >= 1 || r === 0);
        const m = Math.sqrt(-2 * Math.log(r) / r); spare = b * m; return mu + sd * a * m;
      },
    };
  }
  function gamma(z) {                    // Lanczos
    const g = 7, c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
      -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
    if (z < 0.5) return Math.PI / (Math.sin(Math.PI * z) * gamma(1 - z));
    z -= 1; let x = c[0]; for (let i = 1; i < g + 2; i++) x += c[i] / (z + i);
    const t = z + g + 0.5; return Math.sqrt(2 * Math.PI) * Math.pow(t, z + 0.5) * Math.exp(-t) * x;
  }

  // ------------------------------------------------------------------ grid source
  class GridSource {
    constructor(d, name) {
      this.name = name;
      this.t = Float64Array.from(d.times); this.lat = Float64Array.from(d.lat); this.lon = Float64Array.from(d.lon);
      this.levels = d.levels ? Float64Array.from(d.levels) : new Float64Array(0);
      const T = this.t.length, Y = this.lat.length, X = this.lon.length, L = d.z.length / (T * Y * X);
      this.T = T; this.Y = Y; this.X = X; this.L = L;
      this.ok2d = new Uint8Array(Y * X);
      for (let j = 0; j < Y * X; j++) this.ok2d[j] = Number.isFinite(d.z[j]) ? 1 : 0;   // z[0,0,:,:]
      this.allOk = this.ok2d.every(v => v === 1);
      // interleave to [T][Y][X][L][3] so a column is contiguous
      this.zuv = new Float32Array(T * Y * X * L * 3);
      let top = Infinity;
      for (let t = 0; t < T; t++) for (let l = 0; l < L; l++) for (let y = 0; y < Y; y++) for (let x = 0; x < X; x++) {
        const src = ((t * L + l) * Y + y) * X + x, dst = (((t * Y + y) * X + x) * L + l) * 3;
        const fin = Number.isFinite(d.z[src]);
        this.zuv[dst] = fin ? d.z[src] : 0; this.zuv[dst + 1] = Number.isFinite(d.u[src]) ? d.u[src] : 0;
        this.zuv[dst + 2] = Number.isFinite(d.v[src]) ? d.v[src] : 0;
        if (l === L - 1 && this.ok2d[y * X + x] && fin && d.z[src] < top) top = d.z[src];
      }
      this.topZ = top;
      this.terrainGrid = d.terrain ? Float32Array.from(d.terrain) : new Float32Array(Y * X).fill(NaN);
      this.colZ = new Float64Array(L); this.colU = new Float64Array(L); this.colV = new Float64Array(L);
    }
    _hidx(lat, lon) {
      const fy = interpIndex(lat, this.lat), fx = interpIndex(lon, this.lon);
      const iy = Math.min(Math.max(Math.floor(fy), 0), this.Y - 2), ix = Math.min(Math.max(Math.floor(fx), 0), this.X - 2);
      return [iy, ix, fy - iy, fx - ix];
    }
    _tidx(t) {
      if (this.T === 1) return [0, 0, 0];
      const ft = interpIndex(t, this.t), it = Math.min(Math.max(Math.floor(ft), 0), this.T - 2);
      return [it, it + 1, ft - it];
    }
    covers(lat, lon, t) {
      if (!(lat >= this.lat[0] && lat <= this.lat[this.Y - 1] && lon >= this.lon[0] && lon <= this.lon[this.X - 1]
        && t >= this.t[0] - 1 && t <= this.t[this.T - 1] + 1)) return false;
      if (this.allOk) return true;
      const [iy, ix] = this._hidx(lat, lon), X = this.X, ok = this.ok2d;
      return !!(ok[iy * X + ix] && ok[(iy + 1) * X + ix + 1] && ok[iy * X + ix + 1] && ok[(iy + 1) * X + ix]);
    }
    sample(lat, lon, alt, t) {
      const [iy, ix, wy, wx] = this._hidx(lat, lon), [t0, t1, wt] = this._tidx(t);
      const w00 = (1 - wy) * (1 - wx), w01 = (1 - wy) * wx, w10 = wy * (1 - wx), w11 = wy * wx;
      const L = this.L, X = this.X, Y = this.Y, a = this.zuv, Z = this.colZ, U = this.colU, V = this.colV;
      let ou = 0, ov = 0;
      for (const [ti, w] of [[t0, 1 - wt], [t1, wt]]) {
        if (w === 0 && ti === t1) continue;
        const b00 = (((ti * Y + iy) * X + ix) * L) * 3, b01 = b00 + L * 3, b10 = b00 + X * L * 3, b11 = b10 + L * 3;
        for (let l = 0; l < L; l++) {
          const o = l * 3;
          Z[l] = a[b00 + o] * w00 + a[b01 + o] * w01 + a[b10 + o] * w10 + a[b11 + o] * w11;
          U[l] = a[b00 + o + 1] * w00 + a[b01 + o + 1] * w01 + a[b10 + o + 1] * w10 + a[b11 + o + 1] * w11;
          V[l] = a[b00 + o + 2] * w00 + a[b01 + o + 2] * w01 + a[b10 + o + 2] * w10 + a[b11 + o + 2] * w11;
        }
        // vertical: k = #levels below alt, clamped [1, L-1]
        let k = 0; while (k < L && Z[k] < alt) k++;
        k = Math.min(Math.max(k, 1), L - 1);
        const z0 = Z[k - 1], z1 = Z[k];
        const f = Math.min(Math.max((alt - z0) / Math.max(z1 - z0, 1e-3), 0), 1);
        ou += w * (U[k - 1] * (1 - f) + U[k] * f); ov += w * (V[k - 1] * (1 - f) + V[k] * f);
      }
      return [ou, ov];
    }
    terrain(lat, lon) {
      const [iy, ix, wy, wx] = this._hidx(lat, lon), g = this.terrainGrid, X = this.X;
      return g[iy * X + ix] * (1 - wy) * (1 - wx) + g[iy * X + ix + 1] * (1 - wy) * wx +
        g[(iy + 1) * X + ix] * wy * (1 - wx) + g[(iy + 1) * X + ix + 1] * wy * wx;
    }
  }

  // ------------------------------------------------------------------ terrain mosaic (Terrarium)
  class DemTerrain {
    constructor(mosaics) { this.m = mosaics.slice().sort((a, b) => b.zoom - a.zoom); }
    add(d) { this.m.push(d); this.m.sort((a, b) => b.zoom - a.zoom); }
    at(lat, lon) {
      for (const d of this.m) {
        const nn = Math.pow(2, d.zoom) * 256;
        const px = (lon + 180) / 360 * nn - d.x0 * 256 - 0.5;
        const lr = lat * D2R;
        const py = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * nn - d.y0 * 256 - 0.5;
        if (px < 0 || py < 0 || px >= d.w - 1 || py >= d.h - 1) continue;
        const ix = Math.floor(px), iy = Math.floor(py), fx = px - ix, fy = py - iy, Z = d.z, W = d.w;
        const c0 = Z[iy * W + ix], c1 = Z[iy * W + ix + 1], c2 = Z[(iy + 1) * W + ix], c3 = Z[(iy + 1) * W + ix + 1];
        if (c0 === -32768 || c1 === -32768 || c2 === -32768 || c3 === -32768) continue;
        return c0 * (1 - fx) * (1 - fy) + c1 * fx * (1 - fy) + c2 * (1 - fx) * fy + c3 * fx * fy;
      }
      return NaN;
    }
  }

  // ------------------------------------------------------------------ wind field
  const CHOICES = { blend: 'HRRR 3 km + GFS above', rap: 'RAP 13 km + GFS above', ecmwf: 'ECMWF IFS + GFS above',
    gfs: 'GFS 0.25° only', gefs: 'GEFS ensemble', raob: 'Radiosonde + GFS above' };
  function sourceKeys(data) { return Object.keys(data).filter(k => k !== 'dem'); }
  function gefsMembers(data) { return sourceKeys(data).filter(k => k.startsWith('gefs-')).sort(); }
  function availableChoices(data) {
    const keys = new Set(sourceKeys(data)), out = [];
    if (keys.has('gfs')) out.push('gfs');
    if (keys.has('hrrr')) out.unshift('blend');
    for (const k of ['rap', 'ecmwf']) if (keys.has(k)) out.push(k);
    if (gefsMembers(data).length) out.push('gefs');
    return out.concat([...keys].filter(k => k.startsWith('raob-')).sort());
  }

  class WindField {
    constructor(data, mode = 'blend', opts = {}) {
      this.data = data; this.mode = mode;
      this.sources = opts.sources || {};
      if (!opts.sources) for (const k of sourceKeys(data)) this.sources[k] = new GridSource(data[k], k);
      let prim = (mode === 'blend' || mode === 'hrrr') ? 'hrrr' : mode;
      if (prim === 'gefs') { const m = gefsMembers(data); if (!m.length) throw new Error('no GEFS members'); prim = m[0]; }
      if (!this.sources[prim]) {
        if (mode === 'blend' && this.sources.gfs) prim = 'gfs'; else throw new Error(`wind data has no ${mode}`);
      }
      this.primaryKey = prim; this.primary = this.sources[prim];
      this.topup = (prim !== 'gfs' && !prim.startsWith('gefs-') && this.sources.gfs) ? this.sources.gfs : null;
      let hi = this.primary.topZ - 500; if (prim === 'hrrr') hi = Math.min(hi, 18000);
      this.blendHi = hi; this.blendLo = hi - 3000;
      this.correction = null; this.meta = opts.meta || {};
      this.dem = opts.dem !== undefined ? opts.dem : (data.dem ? new DemTerrain([data.dem]) : null);
    }
    member(key) { const w = new WindField(this.data, key, { sources: this.sources, dem: this.dem, meta: this.meta }); return w; }
    get timeRange() { const s = this.topup || this.primary; return [s.t[0], s.t[s.T - 1]]; }
    sample(lat, lon, alt, t) {
      const p = this.primary, g = this.topup; let u, v;
      if (!g) [u, v] = p.sample(lat, lon, alt, t);
      else {
        let wp = Math.min(Math.max((this.blendHi - alt) / Math.max(this.blendHi - this.blendLo, 1), 0), 1);
        if (wp > 0 && !p.covers(lat, lon, t)) wp = 0;
        if (wp === 0) [u, v] = g.sample(lat, lon, alt, t);
        else if (wp === 1) [u, v] = p.sample(lat, lon, alt, t);
        else { const a = p.sample(lat, lon, alt, t), b = g.sample(lat, lon, alt, t); u = wp * a[0] + (1 - wp) * b[0]; v = wp * a[1] + (1 - wp) * b[1]; }
      }
      if (this.correction) { const [du, dv] = this.correction(alt, t); u += du; v += dv; }
      return [u, v];
    }
    addDem(m) { if (!this.dem) this.dem = new DemTerrain([m]); else this.dem.add(m); }
    terrain(lat, lon) {
      let h = this.dem ? this.dem.at(lat, lon) : NaN;
      for (const key of ['hrrr', 'rap', 'gfs', 'ecmwf']) {
        if (Number.isFinite(h)) break;
        const s = this.sources[key];
        if (!s || !s.terrainGrid.some(Number.isFinite)) continue;
        if (s.covers(lat, lon, s.t[0])) h = s.terrain(lat, lon);
      }
      return Number.isFinite(h) ? h : 0;
    }
    applyMeasured(obs, tauH = 6.0, binM = 500, minPts = 2) {
      const saved = this.correction; this.correction = null;
      const bins = new Map();
      for (let i = 0; i < obs.t.length; i++) {
        const [um, vm] = this.sample(obs.lat[i], obs.lon[i], obs.alt[i], obs.t[i]);
        const b = Math.floor(obs.alt[i] / binM);
        if (!bins.has(b)) bins.set(b, { z: [], du: [], dv: [], t: [] });
        const e = bins.get(b); e.z.push(obs.alt[i]); e.du.push(obs.u[i] - um); e.dv.push(obs.v[i] - vm); e.t.push(obs.t[i]);
      }
      this.correction = saved;
      const median = a => { const s = a.slice().sort((x, y) => x - y), n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
      const mean = a => a.reduce((x, y) => x + y, 0) / a.length;
      const rows = [...bins.values()].filter(e => e.z.length >= minPts)
        .map(e => [mean(e.z), median(e.du), median(e.dv), mean(e.t)]).sort((a, b) => a[0] - b[0]);
      if (!rows.length) return 0;
      const zc = rows.map(r => r[0]), bu = rows.map(r => r[1]), bv = rows.map(r => r[2]), bt = rows.map(r => r[3]);
      const zlo = zc[0], zhi = zc[zc.length - 1];
      this.correction = (alt, t) => {
        if (alt < zlo - binM || alt > zhi + binM) return [0, 0];
        const w = Math.exp(-Math.abs(t - interp(alt, zc, bt)) / (tauH * 3600));
        return [w * interp(alt, zc, bu), w * interp(alt, zc, bv)];
      };
      return zc.length;
    }
  }

  // ------------------------------------------------------------------ balloon physics
  const M_AIR = 0.0289644, M_GAS = { he: 0.0040026, h2: 0.002016 }, R_UNIV = 8.314462618;
  const BALLOONS = {
    'Totex/Kaymont 200': [200, 3.00, 0.25], 'Totex/Kaymont 300': [300, 3.78, 0.25], 'Totex/Kaymont 350': [350, 4.12, 0.25],
    'Totex/Kaymont 450': [450, 4.72, 0.25], 'Totex/Kaymont 500': [500, 4.99, 0.25], 'Totex/Kaymont 600': [600, 6.02, 0.30],
    'Totex/Kaymont 700': [700, 6.53, 0.30], 'Totex/Kaymont 800': [800, 7.00, 0.30], 'Totex/Kaymont 1000': [1000, 7.86, 0.30],
    'Totex/Kaymont 1200': [1200, 8.63, 0.25], 'Totex/Kaymont 1500': [1500, 9.44, 0.25], 'Totex/Kaymont 2000': [2000, 10.54, 0.25],
    'Totex/Kaymont 3000': [3000, 13.00, 0.25], 'Hwoyee 200': [200, 3.00, 0.25], 'Hwoyee 300': [300, 3.80, 0.25],
    'Hwoyee 500': [500, 5.00, 0.25], 'Hwoyee 600': [600, 5.80, 0.30], 'Hwoyee 800': [800, 6.80, 0.30],
    'Hwoyee 1000': [1000, 7.50, 0.30], 'Hwoyee 1200': [1200, 8.50, 0.25], 'Hwoyee 1600': [1600, 10.50, 0.25],
    'Hwoyee 2000': [2000, 11.00, 0.25], 'Hwoyee 3000': [3000, 12.50, 0.25],
  };
  const ALT_N = 2020;                       // -500 .. 50000 m in 25 m steps
  const ALT_GRID = Float64Array.from({ length: ALT_N }, (_, i) => -500 + 25 * i);
  const TP_GRID = Array.from(ALT_GRID, h => tempPressure(h));
  const RHO_GRID = TP_GRID.map(([t, p]) => p / (R_AIR * t));
  function ascentTable(b, launchAlt, burstScale = 1, cdScale = 1, liftScale = 1) {
    const [t0, p0] = tempPressure(launchAlt), mg = M_GAS[b.gas || 'he'];
    const rhoA0 = p0 * M_AIR / (R_UNIV * t0), rhoG0 = p0 * mg / (R_UNIV * t0);
    const neck = b.neck_lift_g / 1000 * liftScale;
    const v0 = (neck + b.balloon_g / 1000) / (rhoA0 - rhoG0);
    const free = Math.max(neck - b.payload_g / 1000, 1e-4) * G0;
    const rate = new Float64Array(ALT_N), dBurst = b.burst_d * burstScale;
    let burstAlt = ALT_GRID[ALT_N - 1], found = false;
    for (let i = 0; i < ALT_N; i++) {
      const [T, P] = TP_GRID[i];
      const V = v0 * (p0 / P) * (T / t0), r = Math.cbrt(3 * V / (4 * Math.PI)), A = Math.PI * r * r;
      rate[i] = Math.sqrt(2 * free / (RHO_GRID[i] * b.cd * cdScale * A));
      if (!found && 2 * r >= dBurst) { burstAlt = ALT_GRID[i]; found = true; }
    }
    if ((b.profile || 'constant') === 'constant') {
      let k0 = 0; while (k0 < ALT_N - 1 && ALT_GRID[k0] < launchAlt) k0++;
      rate.fill(rate[k0]);
    }
    return { rate, burstAlt };
  }
  function balloonSummary(b, launchAlt) {
    const { rate, burstAlt } = ascentTable(b, launchAlt);
    let k0 = 0; while (k0 < ALT_N - 1 && ALT_GRID[k0] < launchAlt) k0++;
    let kb = 0; while (kb < ALT_N - 1 && ALT_GRID[kb] < burstAlt) kb++;
    let tUp = 0; for (let i = k0; i < kb; i++) tUp += 25 / rate[i];
    return { burst_alt: burstAlt, launch_rate: rate[k0], mean_rate: tUp ? (burstAlt - launchAlt) / tUp : NaN,
      ascent_min: tUp / 60, free_lift_g: b.neck_lift_g - b.payload_g };
  }
  function neckLiftForRate(target, b, launchAlt) {
    let lo = b.payload_g + 1, hi = b.payload_g + 20000;
    for (let i = 0; i < 60; i++) {
      const mid = (lo + hi) / 2, s = balloonSummary({ ...b, neck_lift_g: mid, profile: 'constant' }, launchAlt);
      if (s.launch_rate < target) lo = mid; else hi = mid;
    }
    return (lo + hi) / 2;
  }

  // ------------------------------------------------------------------ simulation
  const UNC_DEFAULTS = { members: 0, ascent_pct: 5, burst_pct: 5, burst_weibull_k: 14.4, lift_pct: 3, cd_pct: 7.5,
    descent_pct: 5, wind_dir_deg: 5, wind_speed_pct: 5, wind_bias: 0, seed: 1 };
  const PARAM_DEFAULTS = { ascent_rate: 5, burst_alt: 30480, descent_rate: 6, launch_alt: null, dt: 10,
    balloon: null, descent_factor: 1, start_phase: 0 };

  /** simulate(wind, p, unc, opts)
   *  wind: WindField, or {fields:[WindField...]} with opts.windIndex[i] picking the field per trajectory
   *  opts: startAlt, maxHours, nominalIdx (array), trackIdx (array), keepTracks */
  function simulate(wind, p, unc = {}, opts = {}) {
    p = { ...PARAM_DEFAULTS, ...p }; unc = { ...UNC_DEFAULTS, ...unc };
    const n = 1 + Math.max(0, unc.members | 0), r = rng(unc.seed);
    const fields = wind.fields || [wind], base = fields[0];
    const widx = opts.windIndex || null;
    const nom = new Uint8Array(n); nom[0] = 1; (opts.nominalIdx || []).forEach(i => { nom[i] = 1; });
    const factor = pct => { const f = new Float64Array(n).fill(1); if (n > 1 && pct) for (let i = 1; i < n; i++) f[i] = 1 + r.normal(0, pct / 100);
      for (let i = 0; i < n; i++) { if (nom[i]) f[i] = 1; f[i] = Math.max(f[i], 0.2); } return f; };
    const ground0 = base.terrain(p.lat, p.lon);
    const alt0 = opts.startAlt != null ? opts.startAlt : (p.launch_alt != null ? p.launch_alt : ground0);
    let asc = null, burst, rateTabs = null;
    if (p.balloon) {
      const lam = 1 / gamma(1 + 1 / unc.burst_weibull_k);
      const bscale = new Float64Array(n).fill(1);
      for (let i = 1; i < n; i++) bscale[i] = lam * Math.pow(-Math.log(1 - r.uniform()), 1 / unc.burst_weibull_k);
      for (let i = 0; i < n; i++) if (nom[i]) bscale[i] = 1;
      const cdS = factor(unc.cd_pct), liftS = factor(unc.lift_pct);
      const la = p.launch_alt != null ? p.launch_alt : ground0;
      rateTabs = []; burst = new Float64Array(n);
      for (let i = 0; i < n; i++) { const t = ascentTable(p.balloon, la, bscale[i], cdS[i], liftS[i]); rateTabs.push(t.rate); burst[i] = t.burstAlt; }
    } else {
      const fa = factor(unc.ascent_pct); asc = Float64Array.from(fa, f => Math.max(p.ascent_rate * f, 0.5));
      const fb = factor(unc.burst_pct); burst = Float64Array.from(fb, f => p.burst_alt * f);
    }
    for (let i = 0; i < n; i++) burst[i] = Math.max(burst[i], alt0 + (p.start_phase === 0 ? 1 : -1e9));
    const fd = factor(unc.descent_pct), dsc = Float64Array.from(fd, f => Math.max(p.descent_rate * p.descent_factor * f, 0.5));
    const th = new Float64Array(n), sc = new Float64Array(n).fill(1), ub = new Float64Array(n), vb = new Float64Array(n);
    if (n > 1) {
      for (let i = 1; i < n; i++) th[i] = r.normal(0, unc.wind_dir_deg) * D2R;
      for (let i = 1; i < n; i++) sc[i] = 1 + r.normal(0, unc.wind_speed_pct / 100);
      if (unc.wind_bias) { for (let i = 1; i < n; i++) ub[i] = r.normal(0, unc.wind_bias); for (let i = 1; i < n; i++) vb[i] = r.normal(0, unc.wind_bias); }
    }
    for (let i = 0; i < n; i++) if (nom[i]) { th[i] = 0; sc[i] = 1; ub[i] = 0; vb[i] = 0; }
    const [tmin, tmax] = base.timeRange, dt = p.dt, maxSteps = Math.floor((opts.maxHours || 8) * 3600 / dt);
    const trackIdx = opts.trackIdx || Array.from({ length: Math.max(Math.min(opts.keepTracks || 0, n), 1) }, (_, i) => i);
    const keep = new Map(trackIdx.map((k, j) => [k, j]));
    const tracks = trackIdx.map(() => [[p.launch_time, p.lat, p.lon, alt0, p.start_phase]]);
    const out = { lat: new Float64Array(n), lon: new Float64Array(n), t: new Float64Array(n), landed: new Uint8Array(n),
      tBurst: new Float64Array(n).fill(NaN), burstPt: Array.from({ length: n }, () => [NaN, NaN, NaN]),
      landAlt: new Float64Array(n).fill(NaN), clamped: false };
    const GROUND_CEIL = 5000;
    for (let i = 0; i < n; i++) {
      const w = widx ? fields[widx[i]] : base;
      const c = Math.cos(th[i]), s = Math.sin(th[i]);
      const vel = (la, lo, al, tt) => {
        const [u, v] = w.sample(la, lo, al, Math.min(Math.max(tt, tmin), tmax));
        return [sc[i] * (u * c + v * s) + ub[i], sc[i] * (-u * s + v * c) + vb[i]];
      };
      const wv = (al, ph) => {
        if (ph !== 0) return -descentSpeed(dsc[i], al);
        if (rateTabs) { const k = Math.min(Math.max(Math.floor((al + 500) / 25), 0), ALT_N - 1); return rateTabs[i][k]; }
        return asc[i];
      };
      let la = p.lat, lo = p.lon, al = alt0, tt = p.launch_time, ph = p.start_phase;
      if (ph === 1) { out.tBurst[i] = p.launch_time; out.burstPt[i] = [p.lat, p.lon, alt0]; }
      const tr = keep.has(i) ? tracks[keep.get(i)] : null;
      for (let step = 0; step < maxSteps && ph < 2; step++) {
        if (tt > tmax || tt < tmin) out.clamped = true;
        const [u1, v1] = vel(la, lo, al, tt), w1 = wv(al, ph);
        const lam = la + (v1 * dt / 2 / EARTH_R) * R2D;
        const lom = lo + (u1 * dt / 2 / (EARTH_R * Math.cos(la * D2R))) * R2D;
        const alm = al + w1 * dt / 2;
        const [u2, v2] = vel(lam, lom, alm, tt + dt / 2), w2 = wv(alm, ph);
        let nla = la + (v2 * dt / EARTH_R) * R2D;
        let nlo = lo + (u2 * dt / (EARTH_R * Math.cos(lam * D2R))) * R2D;
        let nal = al + w2 * dt, ntt = tt + dt, nph = ph;
        if (nph === 0 && nal >= burst[i]) {
          const f = (burst[i] - al) / Math.max(nal - al, 1e-6);
          nla = la + f * (nla - la); nlo = lo + f * (nlo - lo); ntt = tt + f * dt; nal = burst[i]; nph = 1;
          out.tBurst[i] = ntt; out.burstPt[i] = [nla, nlo, nal];
        }
        if (nph === 1 && nal < GROUND_CEIL) {
          const g = w.terrain(nla, nlo);
          if (nal <= g) {
            const f = Math.min(Math.max((al - g) / Math.max(al - nal, 1e-6), 0), 1);
            nla = la + f * (nla - la); nlo = lo + f * (nlo - lo); ntt = tt + f * dt; nal = g; nph = 2; out.landAlt[i] = g;
          }
        }
        la = nla; lo = nlo; al = nal; tt = ntt; ph = nph;
        if (tr) tr.push([tt, la, lo, al, ph]);
      }
      out.lat[i] = la; out.lon[i] = lo; out.t[i] = tt; out.landed[i] = ph === 2 ? 1 : 0;
    }
    out.tracks = tracks; out.track = tracks[0]; out.p = p; out.n = n;
    return out;
  }

  function spreadStats(la, lo, refLat, refLon) {
    const n = la.length; if (n < 3) return null;
    let clat = 0, clon = 0; for (let i = 0; i < n; i++) { clat += la[i]; clon += lo[i]; } clat /= n; clon /= n;
    if (refLat == null) { refLat = clat; refLon = clon; }
    const kx = D2R * EARTH_R * Math.cos(clat * D2R), ky = D2R * EARTH_R;
    let sxx = 0, syy = 0, sxy = 0;
    for (let i = 0; i < n; i++) { const x = (lo[i] - clon) * kx, y = (la[i] - clat) * ky; sxx += x * x; syy += y * y; sxy += x * y; }
    sxx /= n - 1; syy /= n - 1; sxy /= n - 1;
    const tr = sxx + syy, det = sxx * syy - sxy * sxy, disc = Math.sqrt(Math.max(tr * tr / 4 - det, 0));
    const l1 = Math.max(tr / 2 + disc, 0), l2 = Math.max(tr / 2 - disc, 0);
    let ex, ey;                                 // major eigenvector
    if (Math.abs(sxy) > 1e-12) { ex = l1 - syy; ey = sxy; } else if (sxx >= syy) { ex = 1; ey = 0; } else { ex = 0; ey = 1; }
    const orient = ((Math.atan2(ex, ey) * R2D) + 360) % 180;
    const d = Array.from(la, (v, i) => haversine(v, lo[i], refLat, refLon)).sort((a, b) => a - b);
    const pct = q => { const pos = (d.length - 1) * q, i = Math.floor(pos), f = pos - i; return i + 1 < d.length ? d[i] * (1 - f) + d[i + 1] * f : d[i]; };
    const k95 = Math.sqrt(5.991);
    return { n, mean: [clat, clon], points: Array.from(la, (v, i) => [+v.toFixed(5), +lo[i].toFixed(5)]),
      ellipse95: { a_m: k95 * Math.sqrt(l1), b_m: k95 * Math.sqrt(l2), orient_deg: orient, center: [clat, clon] },
      sigma_km: Math.sqrt((l1 + l2) / 2) / 1000, r50_km: pct(0.5) / 1000, r95_km: pct(0.95) / 1000 };
  }

  function summary(res) {
    const p = res.p, la = res.lat[0], lo = res.lon[0];
    const out = {
      launch: { lat: p.lat, lon: p.lon, time: p.launch_time, alt: res.track[0][3] },
      burst: { lat: res.burstPt[0][0], lon: res.burstPt[0][1], alt: res.burstPt[0][2], time: res.tBurst[0] },
      landing: { lat: la, lon: lo, alt: res.landAlt[0], time: res.t[0], landed: !!res.landed[0] },
      ascent_min: (res.tBurst[0] - p.launch_time) / 60, descent_min: (res.t[0] - res.tBurst[0]) / 60,
      range_km: haversine(p.lat, p.lon, la, lo) / 1000, bearing_deg: bearing(p.lat, p.lon, la, lo),
      wind_time_clamped: res.clamped,
    };
    if (res.n > 1) {
      const L = [], O = []; for (let i = 0; i < res.n; i++) if (res.landed[i]) { L.push(res.lat[i]); O.push(res.lon[i]); }
      out.ensemble = spreadStats(L, O, la, lo);
    }
    return out;
  }

  // ------------------------------------------------------------------ high-level operations
  const COLORS = { blend: '#d9480f', rap: '#9c36b5', ecmwf: '#1971c2', gfs: '#2f9e44', gefs: '#c2255c', raob: '#0c8599' };
  const color = k => COLORS[k.startsWith('raob') ? 'raob' : k] || '#495057';
  const label = k => k.startsWith('raob-') ? `Sounding ${k.slice(5)} + GFS` : (CHOICES[k] || k);
  const thin = (tr, k) => tr.filter((_, i) => i % k === 0 || i === tr.length - 1);

  function runGefs(wfBase, p, unc, mcPer, startAlt, measured, tauH) {
    const mem = gefsMembers(wfBase.data), fields = mem.map(k => wfBase.member(k));
    if (measured) fields.forEach(f => f.applyMeasured(measured, tauH));
    const per = 1 + Math.max(0, mcPer | 0), windIndex = [], nominalIdx = [];
    mem.forEach((_, m) => { nominalIdx.push(m * per); for (let j = 0; j < per; j++) windIndex.push(m); });
    const r = simulate({ fields }, p, { ...unc, members: windIndex.length - 1 }, { startAlt, windIndex, nominalIdx, trackIdx: nominalIdx });
    const L = [], O = []; for (let i = 0; i < r.n; i++) if (r.landed[i]) { L.push(r.lat[i]); O.push(r.lon[i]); }
    const nominal = nominalIdx.map(i => [r.lat[i], r.lon[i]]);
    const s = spreadStats(nominal.map(x => x[0]), nominal.map(x => x[1]));
    const mean = s ? s.mean : nominal[0];
    return { label: `GEFS ensemble (${mem.length} members)`, color: color('gefs'),
      summary: { landing: { lat: mean[0], lon: mean[1] }, members: mem.length, member_landings: nominal,
        range_km: haversine(p.lat, p.lon, mean[0], mean[1]) / 1000, bearing_deg: bearing(p.lat, p.lon, mean[0], mean[1]),
        member_spread: s, ensemble: spreadStats(L, O, mean[0], mean[1]) },
      member_tracks: r.tracks.map(t => thin(t, 6)) };
  }

  function consensus(models) {
    const pts = Object.values(models).map(m => [m.summary.landing.lat, m.summary.landing.lon]);
    if (!pts.length) return null;
    const c = [pts.reduce((a, x) => a + x[0], 0) / pts.length, pts.reduce((a, x) => a + x[1], 0) / pts.length];
    let pair = 0; for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) pair = Math.max(pair, haversine(...pts[i], ...pts[j]) / 1000);
    return { lat: c[0], lon: c[1], max_from_mean_km: Math.max(...pts.map(x => haversine(c[0], c[1], x[0], x[1]) / 1000)),
      max_pairwise_km: pair, n: pts.length };
  }

  function runModels(wfBase, p, unc, models, opts = {}) {
    const avail = availableChoices(wfBase.data);
    models = models.filter(m => avail.includes(m)); if (!models.length) models = avail.slice(0, 1);
    const out = { models: {}, warnings: [] };
    for (const m of models) {
      if (m === 'gefs') { out.models.gefs = runGefs(wfBase, p, unc, opts.gefsMcPerMember ?? 5, opts.startAlt, opts.measured, opts.tauH || 6); continue; }
      const wf = wfBase.member(m);
      if (opts.measured) out.measured_bins = wf.applyMeasured(opts.measured, opts.tauH || 6);
      const r = simulate(wf, p, unc, { startAlt: opts.startAlt });
      const s = summary(r);
      if (s.wind_time_clamped) out.warnings.push(`${label(m)}: flight ran past the end of the wind data`);
      out.models[m] = { label: label(m), color: color(m), summary: s, track: thin(r.track, 2) };
    }
    out.consensus = consensus(out.models);
    return out;
  }

  function launchWindow(wfBase, p, model, tStart, tEnd, stepH = 1, target = null) {
    const wf = wfBase.member(model === 'gefs' ? gefsMembers(wfBase.data)[0] : model);
    const [tmin, tmax] = wf.timeRange, rows = [];
    for (let t = tStart; t <= tEnd + 1; t += stepH * 3600) {
      if (t < tmin - 1 || t > tmax) continue;
      const s = summary(simulate(wf, { ...p, launch_time: t }, {}));
      const row = { time: t, lat: s.landing.lat, lon: s.landing.lon, range_km: s.range_km, bearing_deg: s.bearing_deg,
        flight_min: s.ascent_min + s.descent_min, clamped: s.wind_time_clamped };
      if (target) row.to_target_km = haversine(target[0], target[1], row.lat, row.lon) / 1000;
      rows.push(row);
    }
    const best = target && rows.length ? rows.reduce((a, b) => (b.to_target_km < a.to_target_km ? b : a)) : null;
    return { rows, best, model };
  }

  function launchSiteForTarget(wfBase, p, model, target, iters = 8, tolM = 100) {
    const wf = wfBase.member(model === 'gefs' ? gefsMembers(wfBase.data)[0] : model);
    let la = p.lat, lo = p.lon; const hist = [];
    for (let k = 0; k < iters; k++) {
      const s = summary(simulate(wf, { ...p, lat: la, lon: lo, launch_alt: null }, {}));
      const el = s.landing.lat, eo = s.landing.lon, miss = haversine(el, eo, target[0], target[1]);
      hist.push({ launch: [la, lo], landing: [el, eo], miss_m: miss });
      if (miss < tolM) break;
      la += target[0] - el; lo += target[1] - eo;
    }
    const best = hist.reduce((a, b) => (b.miss_m < a.miss_m ? b : a));
    return { launch: best.launch, landing: best.landing, miss_km: best.miss_m / 1000, iterations: hist };
  }

  function observedWinds(fixes, minDt = 20) {
    const o = { t: [], lat: [], lon: [], alt: [], u: [], v: [] }; let j = 0;
    for (let i = 1; i < fixes.length; i++) {
      const dt = fixes[i][0] - fixes[j][0]; if (dt < minDt) continue;
      const latm = (fixes[i][1] + fixes[j][1]) / 2;
      o.u.push((fixes[i][2] - fixes[j][2]) * D2R * EARTH_R * Math.cos(latm * D2R) / dt);
      o.v.push((fixes[i][1] - fixes[j][1]) * D2R * EARTH_R / dt);
      o.t.push((fixes[i][0] + fixes[j][0]) / 2); o.lat.push(latm); o.lon.push((fixes[i][2] + fixes[j][2]) / 2);
      o.alt.push((fixes[i][3] + fixes[j][3]) / 2); j = i;
    }
    return o.t.length ? o : null;
  }

  function linfit(xs, ys) {
    const n = xs.length, mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n;
    let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; }
    return sxx ? sxy / sxx : 0;
  }

  function inFlight(wfBase, fixes, p, unc, models, opts = {}) {
    fixes = fixes.slice().sort((a, b) => a[0] - b[0]);
    const last = fixes[fixes.length - 1], [t, la, lo, al] = last;
    let imax = 0; fixes.forEach((f, i) => { if (f[3] > fixes[imax][3]) imax = i; });
    const recent = fixes.filter(f => f[0] >= t - 600);
    const rate = recent.length >= 3 ? linfit(recent.map(f => f[0] - t), recent.map(f => f[3])) : 0;
    const descending = (imax < fixes.length - 1 && al < fixes[imax][3] - 300) || rate < -2;
    const info = { phase: descending ? 'descent' : 'ascent', alt: al, time: t, lat: la, lon: lo, max_alt: fixes[imax][3], vertical_rate: rate };
    const q = { ...p, lat: la, lon: lo, launch_time: t, start_phase: descending ? 1 : 0 };
    if (descending) {
      const seg = recent.filter(f => f[0] >= fixes[imax][0]);
      if (seg.length >= 3) {
        const zm = seg.reduce((a, f) => a + f[3], 0) / seg.length, v = -linfit(seg.map(f => f[0]), seg.map(f => f[3]));
        if (v > 0.5) { q.descent_rate = v / Math.sqrt(RHO0 / density(zm)); info.descent_rate_sl = q.descent_rate; }
      }
      info.burst_alt = fixes[imax][3];
    } else {
      const up = fixes.slice(0, imax + 1);
      if (up.length >= 5 && !q.balloon) { q.ascent_rate = Math.max(linfit(up.map(f => f[0]), up.map(f => f[3])), 0.5); info.ascent_rate = q.ascent_rate; }
      q.burst_alt = Math.max(q.burst_alt, al + 100);
    }
    let obs = opts.useMeasured !== false ? observedWinds(fixes) : null;
    if (obs) {
      const keepIdx = obs.t.map((tt, i) => tt <= fixes[imax][0] ? i : -1).filter(i => i >= 0);
      obs = keepIdx.length >= 3 ? Object.fromEntries(Object.entries(obs).map(([k, v]) => [k, keepIdx.map(i => v[i])])) : null;
    }
    const res = runModels(wfBase, q, unc, models, { startAlt: al, measured: obs, tauH: opts.tauH || 6, gefsMcPerMember: opts.gefsMcPerMember ?? 5 });
    if ('measured_bins' in res) { info.measured_bins = res.measured_bins; delete res.measured_bins; }
    res.state = info;
    return res;
  }

  // ------------------------------------------------------------------ exports
  const esc = s => String(s).replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[c]));
  const iso = t => new Date(t * 1000).toISOString().slice(0, 19) + 'Z';
  const kcol = (hex, a = 'ff') => { const h = hex.replace('#', ''); return a + h.slice(4, 6) + h.slice(2, 4) + h.slice(0, 2); };
  function ellipseCoords(e, n = 72) {
    const [clat, clon] = e.center, th = e.orient_deg * D2R, out = [];
    for (let k = 0; k <= n; k++) {
      const a = 2 * Math.PI * k / n, x = e.a_m * Math.cos(a), y = e.b_m * Math.sin(a);
      const east = x * Math.sin(th) + y * Math.cos(th), north = x * Math.cos(th) - y * Math.sin(th);
      out.push([clat + north / EARTH_R * R2D, clon + east / (EARTH_R * Math.cos(clat * D2R)) * R2D]);
    }
    return out;
  }
  const pm = (nm, la, lo, al, desc, col) => `<Placemark><name>${esc(nm)}</name><description>${esc(desc || '')}</description><Style><IconStyle><color>${col}</color></IconStyle></Style><Point><altitudeMode>absolute</altitudeMode><coordinates>${lo.toFixed(6)},${la.toFixed(6)},${Math.round(al || 0)}</coordinates></Point></Placemark>`;
  function toKml(res, launch, observed) {
    const parts = ['<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Zephyrus prediction</name>'];
    if (launch) parts.push(pm('Launch', launch.lat, launch.lon, launch.alt, launch.time ? iso(launch.time) : '', 'ff00ff00'));
    for (const [k, m] of Object.entries(res.models)) {
      const s = m.summary, col = m.color || '#ff8000';
      parts.push(`<Folder><name>${esc(m.label || k)}</name>`);
      const line = (tr, a, w) => `<Placemark><Style><LineStyle><color>${kcol(col, a)}</color><width>${w}</width></LineStyle></Style><LineString><altitudeMode>absolute</altitudeMode><coordinates>${tr.map(x => `${x[2].toFixed(6)},${x[1].toFixed(6)},${Math.round(x[3])}`).join(' ')}</coordinates></LineString></Placemark>`;
      if (m.track) parts.push(line(m.track, 'ff', 3));
      (m.member_tracks || []).forEach(tr => parts.push(line(tr, '80', 1)));
      if (s.burst && Number.isFinite(s.burst.lat)) parts.push(pm('Burst', s.burst.lat, s.burst.lon, s.burst.alt, `${Math.round(s.burst.alt / 0.3048).toLocaleString()} ft`, 'ff00ffff'));
      const D = s.landing; parts.push(pm(`Landing (${m.label || k})`, D.lat, D.lon, D.alt, `${D.lat.toFixed(5)}, ${D.lon.toFixed(5)}${D.time ? '  ' + iso(D.time) : ''}`, kcol(col)));
      if (s.ensemble) parts.push(`<Placemark><name>95% landing area</name><Style><LineStyle><color>${kcol(col)}</color><width>2</width></LineStyle><PolyStyle><color>${kcol(col, '30')}</color></PolyStyle></Style><Polygon><altitudeMode>clampToGround</altitudeMode><outerBoundaryIs><LinearRing><coordinates>${ellipseCoords(s.ensemble.ellipse95).map(x => `${x[1].toFixed(6)},${x[0].toFixed(6)},0`).join(' ')}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>`);
      parts.push('</Folder>');
    }
    const c = res.consensus; if (c && c.n > 1) parts.push(pm('Model consensus', c.lat, c.lon, 0, `models agree within ${c.max_pairwise_km.toFixed(1)} km`, 'ffffffff'));
    if (observed) parts.push(`<Placemark><name>Actual flight</name><Style><LineStyle><color>ff00ff00</color><width>3</width></LineStyle></Style><LineString><altitudeMode>absolute</altitudeMode><coordinates>${observed.map(x => `${x[1].toFixed(6)},${x[0].toFixed(6)},${Math.round(x[2])}`).join(' ')}</coordinates></LineString></Placemark>`);
    parts.push('</Document></kml>');
    return parts.join('');
  }
  function toGpx(res) {
    const w = Object.entries(res.models).map(([k, m]) => `<wpt lat="${m.summary.landing.lat.toFixed(6)}" lon="${m.summary.landing.lon.toFixed(6)}"><name>${esc(('LAND ' + (m.label || k)).slice(0, 30))}</name></wpt>`);
    const c = res.consensus; if (c) w.push(`<wpt lat="${c.lat.toFixed(6)}" lon="${c.lon.toFixed(6)}"><name>LAND consensus</name></wpt>`);
    return '<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="Zephyrus" xmlns="http://www.topografix.com/GPX/1/1">' + w.join('') + '</gpx>';
  }

  root.ZephyrusEngine = { EARTH_R, density, densityFast, tempPressure, pressureToAltitude, descentSpeed, haversine, bearing,
    GridSource, DemTerrain, WindField, CHOICES, availableChoices, gefsMembers, BALLOONS, ascentTable, balloonSummary,
    neckLiftForRate, simulate, summary, spreadStats, runModels, consensus, launchWindow, launchSiteForTarget, inFlight,
    observedWinds, toKml, toGpx, label, color, rng, UNC_DEFAULTS };
})(typeof window !== 'undefined' ? window : globalThis);
