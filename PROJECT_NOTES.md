# PROJECT_NOTES.md — Zephyrus (formerly balloonpred) (for AI assistants working on this code)

Read this first. Keep it current: update the Status, Decisions, and Open items sections whenever
you change behavior. Companion documents (human-facing, kept in Claude Docs, not in the repo):
- "Zephyrus: How It Works" — plain-language guide for JP (the friend this is built for)
- "High-Resolution Multi-Model Landing Prediction for Amateur Stratospheric Balloons (Draft)" — paper draft
When code changes alter anything those documents state (methods, defaults, models, results), update them too.

## Owner and context
- Owner: Cary Chleborad. Built for his friend JP. Launch area: Nevada desert, burst ~100,000 ft,
  small parachute, fast descent. Users prefer US units (ft, ft/min, ft/s, mi) and Pacific time.
- Possible future publication; hindcast results on JP's real flights are the core evidence.
- JP's flight logs: requested, NOT yet received (as of 2026-10-22). No real-data results exist yet.

## Environment limits seen during development
- The development sandbox could not reach NOAA, ECMWF, AWS S3 data buckets, IEM, SondeHub, aprs.fi
  or tile servers (egress policy). All network code is tested only against a fake HTTP server
  (tests/test_v2.py::FakeHTTP). The first live run on the user's machine is the first real test.
- URL patterns were taken from Herbie docs (RAP/GEFS on AWS), NCO product pages, the SondeHub API
  wiki, and IEM's RAOB page. Unverified specifics flagged below.

## Architecture (zephyrus/)
| File | Role |
|---|---|
| sources.py | Model registry (MODELS: hrrr, rap, gfs, ecmwf, gefs), cycle selection, NOMADS subset / AWS byte-range download, GRIB decode (eccodes), Lambert->regular regrid (Delaunay barycentric), grid->earth wind rotation, soundings (IEM JSON), pack save/load (.npz) |
| dem.py | Terrarium terrain tiles (AWS elevation-tiles-prod), cache in data/tiles, DemTerrain bilinear lookup in Web-Mercator pixel space, refine_patch (z12 ~30 m around landing) |
| windfield.py | GridSource (arrays stored [T,Y,X,L,3] for contiguous gathers), WindField (primary model + GFS top-up with linear blend zone; measured-wind correction; terrain = DEM -> HRRR -> RAP -> GFS -> ECMWF) |
| flight.py | FlightParams, BalloonSpec, Uncertainty; vectorized RK2 integrator (dt=10 s) over Monte Carlo members; spread_stats (95% ellipse via covariance, chi2 2-dof 5.991) |
| balloon.py | Latex balloon physics (free lift, gas expansion, burst diameter), CUSF balloon table, Weibull burst spread, neck-lift solver |
| predict.py | run_models (multi-model + GEFS + consensus), launch_window, launch_site_for_target (fixed-point iteration), in_flight (telemetry -> re-predict), history log |
| tracks.py | Track readers (CSV, GPX, KML gx:Track/placemarks, raw APRS), analyze() (launch, ascent rate + per-5km profile, burst, sea-level descent coefficient, landing), observed_winds() |
| hindcast.py | evaluate() per model: observed_params, nominal_params, from_burst, from_burst_measured; 95% ellipse coverage (Mahalanobis^2 <= 5.991) for launch tests; wind RMS error by altitude band; results saved to data/hindcast_results/*.json; calibrate() incl. coverage95 |
| live.py | SondeHub amateur telemetry, aprs.fi (key), manual fixes; in-memory per-callsign store |
| export.py | Multi-model KML (folder per model), GPX landing waypoints, CSV |
| web/app.py + web/templates/index.html | Flask API + single-page Leaflet UI (tabs: Predict, Window, Live, Hindcast, Setup). Leaflet vendored in web/static for offline. Page served as static file (NOT a Jinja template: CSS `{#` breaks Jinja) |
| __main__.py | CLI: fetch, predict, window, reverse, hindcast, calibrate, web |
| paths.py | DATA_DIR / OUTPUT_DIR (env ZEPHYRUS_DATA / ZEPHYRUS_OUTPUT override) |
| doctor.py | Install check: Python version, packages, ecCodes native library, data dir, optional server reachability |
| ../install.sh, ../install.bat + install.ps1 | Interactive installers (ask before each step, --yes/-Yes for unattended). Create .venv; start_web.* run from .venv |

## Android app (android/)
- Architecture: Java `MainActivity` hosts a WebView; assets served from
  https://appassets.androidplatform.net/ by shouldInterceptRequest (secure origin -> GPS,
  IndexedDB). The page is the SAME zephyrus/web/templates/index.html; Gradle `copyWeb` injects
  engine.js, data.js, phone.js after leaflet.js. phone.js overrides window.fetch for "/api/..."
  and answers with the JS engine, so the UI code is shared with the laptop version.
- engine.js is a line-for-line port of flight.py / windfield.py / predict.py / balloon.py /
  export.py. android/tests/parity.test.js must report 0-5 m landing difference vs Python
  (currently 0.000 m on 12 cases + physics, window, reverse, GEFS, terrain). Any engine change in
  Python must be mirrored in engine.js; CI runs the parity test before building.
- Data on phone: Open-Meteo (/v1/gfs 26 levels to 10 hPa, 7x7 points 0.75 deg; /v1/ecmwf 13 levels
  to 50 hPa; ensemble API 3x3 points, 9 levels, model ids tried in order gfs025, ncep_gefs025, ...).
  Open-Meteo has NO HRRR pressure-level winds (checked docs 2026-10-23). Laptop packs via
  GET /api/pack/<name>?phone=1 (sources.slim_pack: 250 km crop, grids thinned to >= 0.06 deg),
  CORS allowed on /api/packs and /api/pack/. .npz read in JS (zip64 + deflate-raw + npy parser).
- Network fallback: data.js httpGet tries fetch, then ZBridge.httpGet (native, no CORS).
- Build: .github/workflows/android.yml -> parity test -> gradle 8.9 assembleRelease (AGP 8.5.2,
  compileSdk 34, minSdk 26) -> "latest" release asset Zephyrus.apk. versionCode =
  GITHUB_RUN_NUMBER. Signing key android/zephyrus.jks (password zephyrus-sideload) committed on
  purpose so updates install over earlier builds; sideload only, not for Play Store.
- Not verified: the APK has never been built (Google Maven/SDK blocked in the dev sandbox;
  MainActivity compiled only against stub classes). Open-Meteo response handling tested only
  against a mock built from their docs; GEFS model id and member pressure-level availability
  unknown until first live run. Open-Meteo free tier counts ~10 variables per location as one call.

## Wind pack format (.npz)
Keys `<source>_<field>`; split on the FIRST underscore, so source names use hyphens
(`gefs-p03`, `raob-KREV`). Per source: times[T] unix, levels[L] hPa (descending), lat[Y], lon[X]
ascending regular, z/u/v [T,L,Y,X] float32 (z geometric m MSL from geopotential height via
Re*Z/(Re-Z)), terrain[Y,X]. Optional `dem_z` (int16, -32768 = missing), `dem_zoom`, `dem_x0`,
`dem_y0`. `meta` = JSON (box, cycles per source, errors, created, models).

## Physics and defaults (sources in parentheses)
- Ascent: constant rate (Tawhiri convention) or balloon physics. Physics computes launch volume
  from neck lift, burst when diameter >= burst diameter. Default ascent PROFILE holds the launch
  rate constant; the constant-Cd "drag" profile speeds up ~2x by 30 km (w ~ rho^-1/6), which real
  flights do not show — chosen per Sobester 2014's finding that drag is the main ascent error.
  Hindcast reports ascent rate per 5 km band to settle this with real data.
- Descent: v(h) = v0 * sqrt(rho0/rho(h)), v0 = sea-level rate (Tawhiri make_drag_descent; Sirks
  et al. calibrated factor 1.019 +/- 0.006 on 30 descents -> exposed as `descent_factor`, default 1.0).
- Atmosphere: US Standard Atmosphere 1976 (table lookup in integrator).
- Integrator: RK2 midpoint, dt 10 s (converges within 50 m of dt 1 s in tests); terrain checked
  only below 5,000 m.
- Blend: HRRR primary below 15 km, GFS above 18 km, linear between. Other primaries: blend_hi =
  model top altitude - 500 m, blend_lo = blend_hi - 3 km. GEFS members have no top-up.
- Monte Carlo defaults (Renegar, UMD): wind direction 5 deg, wind speed 5%, ballistic/descent 5%,
  balloon Cd 7.5%, burst diameter Weibull k=14.4 (CV ~8.6% -> burst altitude sd ~1.7 km);
  constant-mode ascent 5%, burst altitude 5% (consistent with the Weibull figure); neck lift 3%
  (our choice); extra wind offset 0 m/s.
- Measured-wind correction (in-flight): residual (observed - model) per 500 m bin from ascent
  fixes, added to model winds at the same altitude with weight exp(-|dt|/tau), tau = 6 h. Tau is
  a guess; tune with hindcast `from_burst_measured`.
- Consensus: mean of each model's nominal landing (GEFS counts once, as its member mean).

## Performance
Five models x 201 Monte Carlo members ~ 9 s on the dev container. Hot path is
GridSource.sample (gathers); further speedups would need numba or per-member column caching.

## Tests (all must pass before shipping)
`python tests/test_physics.py`, `tests/test_grib.py`, `tests/test_tracks.py`, `tests/test_v2.py`.
Synthetic packs: tests/synth.py make_source(); multi-model: test_v2.pack_data().
Note: test_v2 sets ZEPHYRUS_DATA to a temp dir at import.

## Installer facts (checked on PyPI 2026-10-22)
- eccodes 2.50 ships win_amd64 wheels with the ecCodes DLLs bundled (cp310-cp314).
- macOS gets the native library from eccodeslib wheels only for macOS 13+ (arm64 and x86_64);
  older macOS needs `brew install eccodes`. Linux: eccodeslib manylinux_2_28 (glibc >= 2.28),
  else libeccodes0 / eccodes system package. Installers offer these.
- install.sh tested end to end (--yes) on Linux; install.ps1 parse-checked and its Python
  detection + package check run under PowerShell 7 on Linux. Not yet run on real Windows/macOS.

## First live run (2026-10-22, Windows PC, user's network)
- GFS via NOMADS grib filter: works, 0.16 MB per forecast hour.
- HRRR via AWS byte ranges: works, ~78 MB per forecast hour with 39 levels (~6 s/hour download
  there). Decode + regrid then took ~5 min for 5 hours -> fixed by computing grid lat/lon once
  per file (was per message). Levels trimmed to 31.
- RAP: downloaded (4.8 MB/hour) but failed "too few pressure levels": NCEP packs UGRD+VGRD as
  sub-fields of one message (idx "n.1"/"n.2", same offset). Fixed parse_idx (shared offsets) and
  turned on eccodes multi-field support. Not yet re-verified live.
- Pressing Predict while a download ran started a second identical download -> server now
  reuses the running job; buttons disabled during download.
- ECMWF, GEFS, soundings, terrain tiles: results not yet seen.

## Unverified / open items
- [ ] First live run: confirm all URL patterns (NOMADS filter names for GEFS a/b; RAP awp130
      idx availability; ECMWF .index path; IEM json/raob.py response shape; SondeHub response shape).
- [ ] HRRR wrfprs: confirm HGT:surface present (code falls back to wrfsfc file).
- [ ] Balloon table values are CUSF burst-calculator figures from memory; only Kaymont 1500 =
      9.44 m cross-checked (Near Space Sciences page). Verify against manufacturer sheets.
- [ ] Get JP's flight logs -> run hindcasts at lead 0, 6, 12, 24 h -> fill paper Results.
- [ ] Tune: blend zone heights, measured-wind tau, Monte Carlo sigmas (calibrate()).
- [ ] Possible: GEFS member count vs. download size; NAM 3 km nest; offline map tiles
      (blocked by OSM/OpenTopoMap tile policies — would need a permitted source).

## Change log
- 2026-10-22 v1: HRRR+GFS blend, constant ascent, drag descent, Monte Carlo, Flask UI, hindcast.
- 2026-10-22 v2: model registry (RAP, ECMWF, GEFS, soundings), DEM terrain, balloon physics,
  Renegar uncertainty defaults, multi-model UI with checkboxes + consensus, launch window,
  reverse prediction, live/in-flight mode with measured-wind correction, hindcast tests
  (from_burst, measured, ellipse coverage), calibration, GPX, CLI subcommands, 4x faster integrator.
  Docs written: JP guide + paper draft (Claude Docs), this file.
- 2026-10-22 v2.1: interactive installers (Windows/macOS/Linux), doctor check, .venv launchers.
- 2026-10-22 v2.3: renamed balloonpred -> Zephyrus (package `zephyrus`, env ZEPHYRUS_DATA /
  ZEPHYRUS_OUTPUT, project folder zephyrus/). Polling requests hidden from the console log.
  Live confirmation of the v2.2 fixes: HRRR 62 MB/hour with 31 levels, decode ~45 s for 5 hours
  (was ~5 min).
- 2026-10-23 v2.4: Android app (WebView + JS engine port, Open-Meteo, laptop pack transfer,
  GPS, KML/GPX save, GitHub Actions APK build); laptop /api/pack endpoint + slim_pack; shared
  index.html gained CFG.download_models, fmtCyc, phone hook.
- 2026-10-22 v2.2: tzdata dependency (Windows); GEFS members flown in one vectorized run
  (EnsembleWind); measured-wind correction computed per model; RAP multi-field fix; grid
  coordinate cache; HRRR 31 levels; duplicate-download guard.
