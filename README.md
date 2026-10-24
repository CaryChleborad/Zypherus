# Zephyrus

*Balloon landing prediction, named for the Greek god of the west wind: the westerlies aloft carry our balloons east.*

Predicts where a high-altitude latex balloon lands, using several forecast models at once:
HRRR (3 km), RAP (13 km), ECMWF IFS (0.25°), GFS (0.25°), the GEFS ensemble, and nearby
radiosonde soundings. Built for amateur launches in the Nevada desert.

## Install (once)

- **Windows:** double-click `install.bat`
- **Mac / Linux:** open a terminal in this folder and run `./install.sh`

The installer asks before each step. It finds Python 3.10+ (or offers to install 3.12 from
winget, Homebrew, python.org, or your Linux package manager), creates a private environment in
`.venv`, installs the packages, and checks that everything works, including whether the weather
servers can be reached. Add `--yes` (Mac/Linux) or `-Yes` (Windows PowerShell) for no questions.

Check an installation any time:  `.venv/bin/python -m zephyrus.doctor --network`
(Windows: `.venv\Scripts\python.exe -m zephyrus.doctor --network`)

## Map interface

    start_web.bat  (Windows)   or   ./start_web.sh  (Mac/Linux)

Open http://localhost:8080. `start_web` listens on the whole network, so a phone on the same
Wi-Fi can open http://<laptop-ip>:8080.

| Tab | What it does |
|---|---|
| Predict | Launch point/time, ascent (constant rate or balloon physics), descent (rate or parachute), tick the models to compare, Monte Carlo spread, KML/GPX export |
| Window | Landing point for every launch time in a range; best time to land near a target; where to launch to hit a target |
| Live | Pulls telemetry from SondeHub or aprs.fi (or manual entry) and re-predicts from the latest fix, correcting the model with the winds the balloon measured on the way up |
| Hindcast | Re-flies a recorded flight against archived forecasts, in every model, and reports the miss; "calibration" summarizes all your flights |
| Setup | Units, which models to download, area, terrain detail, uncertainty settings |

## Offline use

Downloads are saved as "wind packs" in `data/` (all models + terrain for the area). The night
before, press **Download winds** for the launch time. At the site, choose that pack under
*Wind data*. Terrain tiles are cached in `data/tiles/`. Only the map background needs internet.

## Android phone app

`android/` holds the phone version: the same map and flight engine, running entirely on the
phone. It downloads GFS, ECMWF and GEFS winds from Open-Meteo, or pulls the laptop's wind packs
(including HRRR) over Wi-Fi. GitHub builds the APK for free; see `android/README.md`.

## Command line

    python -m zephyrus fetch    --lat 40.0 --lon -118.9 --launch "2026-10-25 08:00"
    python -m zephyrus predict  --lat 40.0 --lon -118.9 --launch "2026-10-25 08:00" \
                                   --ascent 1000fpm --burst 100000ft --descent 20fps \
                                   --models blend,rap,ecmwf,gfs,gefs
    python -m zephyrus predict  ... --balloon "Totex/Kaymont 1200" --payload-g 1500 --neck-lift-g 2900
    python -m zephyrus window   --lat .. --lon .. --start "2026-10-25 06:00" --end "2026-10-26 18:00" \
                                   --target-lat 40.2 --target-lon -118.1
    python -m zephyrus reverse  --lat .. --lon .. --launch .. --target-lat .. --target-lon ..
    python -m zephyrus hindcast flights/*.csv flights/*.gpx --lead 12
    python -m zephyrus calibrate

Bare numbers: altitude ft, ascent ft/min, descent ft/s. Suffixes like `5m/s`, `30km` work.
Times are Pacific unless they end in `Z`. Outputs go to `output/`.

## Data sources

| Model | Grid | Top | Output | Reach | Fetched from |
|---|---|---|---|---|---|
| HRRR | 3 km | 50 hPa (~67k ft) | hourly | 18 h (48 h at 00/06/12/18Z) | AWS, byte ranges via .idx |
| RAP | 13 km | 100 hPa (~53k ft) | hourly | 21 h (51 h at 03/09/15/21Z) | AWS, byte ranges |
| GFS | 0.25° | 3 hPa (~130k ft) | hourly | 120 h | NOMADS subset, AWS fallback |
| ECMWF IFS | 0.25° | 50 hPa | 3-hourly | 144 h (00/12Z) | data.ecmwf.int, AWS mirror |
| GEFS | 0.5° × 31 members | 3 hPa | 3-hourly | 240 h | NOMADS subset, AWS fallback |
| Soundings | station | balloon top | 00Z/12Z | n/a | Iowa Environmental Mesonet |
| Terrain | 30–240 m | n/a | n/a | n/a | AWS Terrain Tiles (USGS 3DEP/SRTM) |

Every model except GFS and GEFS stops below burst altitude; above its top the program blends
into GFS. ECMWF open data is © ECMWF, CC BY 4.0.

## Tests

    python tests/test_physics.py && python tests/test_grib.py && python tests/test_tracks.py && python tests/test_v2.py

`test_v2.py` includes a fake NOAA/ECMWF server that exercises the download code (byte ranges,
index files, Lambert-grid wind rotation, GEFS a+b files) without network access.

See `PROJECT_NOTES.md` for design details and status.
