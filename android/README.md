# Zephyrus for Android

The phone app is the same map and the same flight engine as the laptop version, running entirely
on the phone. Predictions work with no signal once wind data is saved on the phone.

## Where the phone gets winds

| Source | Models | Needs |
|---|---|---|
| **Download winds (Open-Meteo)** | GFS 0.25° (26 levels to 10 hPa), ECMWF IFS (to 50 hPa), GEFS ensemble | Phone internet |
| **Get winds from laptop** | Everything the laptop downloaded, including HRRR and RAP | Laptop running Zephyrus on the same Wi-Fi |
| **Import pack file** | Any `.npz` wind pack copied to the phone | The file |

Open-Meteo does not offer HRRR winds on pressure levels, so HRRR on the phone comes from the
laptop. Laptop packs are sent to the phone cropped to 250 km and with HRRR thinned to about 6 km,
which cuts their size in memory by about 4×.

Hindcasts stay on the laptop (they download large archive files).

## Getting the APK (one-time setup, about 15 minutes)

The APK is built on GitHub's servers, free.

1. Make a free account at https://github.com and sign in.
2. Click **+** (top right) → **New repository**. Name it `zephyrus`. Private is fine. Click **Create repository**.
3. On the new page click **uploading an existing file**. Drag in **everything inside** the
   `zephyrus` folder (including the `.github` folder; on Windows turn on *View → Hidden items* if
   you don't see it). Click **Commit changes**.
4. Click the **Actions** tab. The *Build Android app* job starts by itself and takes about
   6–8 minutes. A green check means the APK is ready.
5. On the phone, open `https://github.com/<your-name>/zephyrus/releases/latest`
   (for a private repository, sign in to GitHub in the phone's browser first), tap
   **Zephyrus.apk**, and allow the browser to install apps when Android asks.

Every later change: upload the changed files the same way; GitHub rebuilds and the new APK
installs over the old one, keeping saved wind data.

## Building on a PC instead

Open the `android` folder in Android Studio and press **Run**, or with Gradle 8.7+ and JDK 17:
`gradle assembleRelease` in this folder. The APK lands in `app/build/outputs/apk/release/`.

## Files

| Path | What |
|---|---|
| `web/engine.js` | JavaScript port of the flight engine (checked against Python by `tests/parity.test.js`) |
| `web/data.js` | Open-Meteo client, `.npz` reader, phone storage, terrain tiles |
| `web/phone.js` | Answers the map page's `/api/...` requests on the phone; phone-only buttons |
| `app/src/main/java/.../MainActivity.java` | WebView host: GPS, file picker, saving KML/GPX, network bridge |
| `zephyrus.jks` | Signing key so each build updates the installed app (sideload use only) |
| `tests/` | Parity test (Python vs JS) and phone UI test |
