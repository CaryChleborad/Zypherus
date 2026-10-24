# Zephyrus installer for Windows (PowerShell 5.1 or newer).
# Run by double-clicking install.bat. Add -Yes to answer yes to everything.
# Everything is installed into a private folder (.venv) inside this project; nothing else on
# the computer is changed unless you agree to install Python.
param([switch]$Yes)

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$Here = (Get-Location).Path
$Venv = Join-Path $Here ".venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
$PyWant = "3.12"
$PyPkgVer = "3.12.7"

function Bold($t) { Write-Host $t -ForegroundColor White }
function Ok($t)   { Write-Host "  [ OK ] $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  [WARN] $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host "  [FAIL] $t" -ForegroundColor Red }
function Ask($q) {
    if ($Yes) { Write-Host "$q [Y/n] y (auto)"; return $true }
    $a = Read-Host "$q [Y/n]"
    return ($a -eq "" -or $a -match '^(y|yes)$')
}
function Stop-Setup($msg) {
    Write-Host ""; Fail $msg; Write-Host ""
    Write-Host "Nothing else was changed. You can run install.bat again any time."
    if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Test-Python($exe, $pre) {
    # Returns the full path of a working Python >= 3.10, or $null. Skips the Microsoft Store stub.
    try {
        $args2 = @()
        if ($pre) { $args2 += $pre }
        $args2 += @("-c", "import sys; print(sys.executable) if sys.version_info >= (3,10) else sys.exit(1)")
        $out = & $exe @args2 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out)) { return $out.Trim() }
    } catch { }
    return $null
}

function Find-Python {
    foreach ($v in @("-3.13", "-3.12", "-3.11", "-3.10")) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $p = Test-Python "py" $v; if ($p) { return $p }
        }
    }
    foreach ($c in @("python", "python3")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) {
            $p = Test-Python $c $null; if ($p) { return $p }
        }
    }
    foreach ($d in @("$env:LOCALAPPDATA\Programs\Python\Python313", "$env:LOCALAPPDATA\Programs\Python\Python312",
                     "$env:LOCALAPPDATA\Programs\Python\Python311", "$env:LOCALAPPDATA\Programs\Python\Python310",
                     "C:\Program Files\Python313", "C:\Program Files\Python312", "C:\Program Files\Python311")) {
        $exe = Join-Path $d "python.exe"
        if (Test-Path $exe) { $p = Test-Python $exe $null; if ($p) { return $p } }
    }
    return $null
}

Clear-Host
Bold "Zephyrus balloon landing predictor - setup"
Write-Host @"

This will:
  1. Check for Python $PyWant or newer (and offer to install it if missing)
  2. Create a private Python environment in:  $Venv
  3. Install the packages the predictor needs (about 150 MB download)
  4. Check that everything works, and optionally test the weather data servers

You will be asked before anything is installed.

"@
if (-not (Ask "Continue?")) { Write-Host "OK, nothing was changed."; exit 0 }

# ------------------------------------------------------------------ 1. Python
Write-Host ""; Bold "Step 1 of 4: Python"
$Py = Find-Python
if ($Py) {
    Ok ("Found " + (& $Py --version 2>&1) + " at $Py")
} else {
    Warn "Python 3.10 or newer is not installed."
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
        Warn "This is an ARM Windows PC. The GRIB reader may not have an ARM build; an x64 Python is the safer choice."
    }
    $done = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask "Install Python $PyWant for your user account with winget (Microsoft's installer)?") {
            winget install -e --id "Python.Python.$PyWant" --scope user --accept-package-agreements --accept-source-agreements
            $done = $true
        }
    }
    if (-not $done) {
        Write-Host "  I can download the official installer from python.org (python-$PyPkgVer-amd64.exe, about 25 MB)"
        Write-Host "  and install it for your user account only (no administrator rights needed)."
        if (-not (Ask "Download and install Python $PyPkgVer from python.org?")) {
            Stop-Setup "Python is required. Get it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
        }
        $tmp = Join-Path $env:TEMP "python-$PyPkgVer-amd64.exe"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PyPkgVer/python-$PyPkgVer-amd64.exe" -OutFile $tmp -UseBasicParsing
        } catch { Stop-Setup "Download failed: $($_.Exception.Message)" }
        Write-Host "  Installing (a progress window will appear)..."
        $p = Start-Process -FilePath $tmp -ArgumentList "/passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1" -Wait -PassThru
        if ($p.ExitCode -ne 0) { Stop-Setup "The Python installer exited with code $($p.ExitCode)." }
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $Py = Find-Python
    if (-not $Py) { Stop-Setup "Python still isn't found. Close this window and run install.bat again." }
    Ok ("Installed " + (& $Py --version 2>&1))
}

# ------------------------------------------------------------------ 2. private environment
Write-Host ""; Bold "Step 2 of 4: Private Python environment"
if (Test-Path $VPy) {
    Ok "Found an existing environment in .venv"
    if ((-not $Yes) -and (Ask "Rebuild it from scratch? (No = keep it and just update packages)")) {
        Remove-Item -Recurse -Force $Venv
    }
}
if (-not (Test-Path $VPy)) {
    & $Py -m venv $Venv
    if (-not (Test-Path $VPy)) { Stop-Setup "Could not create the environment in $Venv" }
    Ok "Created .venv"
}

# ------------------------------------------------------------------ 3. packages
Write-Host ""; Bold "Step 3 of 4: Packages"
Write-Host ("  Needed: " + ((Get-Content requirements.txt) -join " "))
# Python code uses single quotes only: Windows PowerShell 5.1 strips double quotes from native arguments
$check = "import importlib.util; need = {'numpy':'numpy','scipy':'scipy','requests':'requests','flask':'flask','PIL':'pillow','eccodes':'eccodes','tzdata':'tzdata'}; miss = [p for m, p in need.items() if importlib.util.find_spec(m) is None]; print(' '.join(miss))"
$missing = ("" + (& $VPy -c $check)).Trim()
$doPip = $true
if (-not $missing) {
    Ok "All packages are already installed"
    $doPip = Ask "Check for package updates anyway?"
} else {
    Write-Host "  Missing: $missing"
    if (-not (Ask "Download and install them into .venv now?")) { Stop-Setup "The predictor can't run without these packages." }
}
if ($doPip) {
    & $VPy -m pip install --upgrade pip --disable-pip-version-check -q
    & $VPy -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Stop-Setup "Package installation failed (see the messages above)." }
    Ok "Packages installed"
}

# ------------------------------------------------------------------ 4. check
Write-Host ""; Bold "Step 4 of 4: Checking the installation"
$net = @()
if (Ask "Also test whether the weather data servers can be reached? (about 1 minute)") { $net = @("--network") }
& $VPy -m zephyrus.doctor @net
$status = $LASTEXITCODE

Write-Host ""
if ($status -eq 0) {
    Bold "Setup complete."
    Write-Host "  Start the predictor any time by double-clicking start_web.bat,"
    Write-Host "  then open http://localhost:8080 in a browser."
    Write-Host "  (Windows may ask whether to allow Python on your network. Allow it on private"
    Write-Host "   networks if you want to open the map from a phone on the same Wi-Fi.)"
    if (Ask "Put a 'Zephyrus' shortcut on your desktop?") {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Zephyrus.lnk"))
        $lnk.TargetPath = Join-Path $Here "start_web.bat"
        $lnk.WorkingDirectory = $Here
        $lnk.Save()
        Ok "Shortcut created"
    }
    if (Ask "Run the built-in self-tests now? (about 2 minutes)") {
        foreach ($t in @("tests\test_physics.py", "tests\test_grib.py", "tests\test_tracks.py", "tests\test_v2.py")) {
            & $VPy $t *> $null
            if ($LASTEXITCODE -eq 0) { Ok $t } else { Fail "$t  (run '$VPy $t' to see why)" }
        }
    }
    if (Ask "Start the predictor now?") { Start-Process -FilePath (Join-Path $Here "start_web.bat") -WorkingDirectory $Here }
} else {
    Stop-Setup "Setup finished with problems. Fix the [FAIL] items above, then run install.bat again."
}
if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }
