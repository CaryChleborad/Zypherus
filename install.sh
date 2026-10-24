#!/usr/bin/env bash
# Zephyrus installer for macOS and Linux.
#   ./install.sh          asks before each step
#   ./install.sh --yes    answers yes to everything (unattended)
# Everything is installed into a private folder (.venv) inside this project; nothing else on
# the computer is changed unless you agree to install Python or a system library.

set -u
cd "$(dirname "$0")"
HERE="$(pwd)"
VENV="$HERE/.venv"
PY_WANT="3.12"
PY_PKG_VER="3.12.7"
AUTO_YES=0
[ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] && AUTO_YES=1

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
ask() {  # ask "question" -> returns 0 for yes
  if [ "$AUTO_YES" = 1 ]; then echo "$1 [Y/n] y (auto)"; return 0; fi
  local a
  read -r -p "$1 [Y/n] " a </dev/tty || a="n"
  case "$a" in ""|y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}
stop() { echo; fail "$1"; echo; echo "Nothing else was changed. You can run ./install.sh again any time."; exit 1; }

OS="$(uname -s)"
clear 2>/dev/null || true
bold "Zephyrus balloon landing predictor - setup"
cat <<EOF

This will:
  1. Check for Python $PY_WANT or newer (and offer to install it if missing)
  2. Create a private Python environment in:  $VENV
  3. Install the packages the predictor needs (about 150 MB download)
  4. Check that everything works, and optionally test the weather data servers

You will be asked before anything is installed.

EOF
ask "Continue?" || { echo "OK, nothing was changed."; exit 0; }

# ------------------------------------------------------------------ 1. Python
echo; bold "Step 1 of 4: Python"
find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        command -v "$c"; return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -n "$PY" ]; then
  ok "Found $("$PY" --version 2>&1) at $PY"
else
  OLD="$(command -v python3 2>/dev/null || true)"
  [ -n "$OLD" ] && warn "Found $("$OLD" --version 2>&1), but the predictor needs 3.10 or newer."
  [ -z "$OLD" ] && warn "Python is not installed."
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      ask "Install Python $PY_WANT with Homebrew (brew install python@$PY_WANT)?" || stop "Python is required."
      brew install "python@$PY_WANT" || stop "Homebrew could not install Python."
    else
      echo "  Homebrew is not installed. I can download the official installer from python.org"
      echo "  (python-$PY_PKG_VER-macos11.pkg, about 45 MB). macOS will ask for your password."
      ask "Download and install Python $PY_PKG_VER from python.org?" || stop "Python is required. Get it from https://www.python.org/downloads/"
      TMPPKG="$(mktemp -d)/python.pkg"
      curl -fL --progress-bar -o "$TMPPKG" "https://www.python.org/ftp/python/$PY_PKG_VER/python-$PY_PKG_VER-macos11.pkg" \
        || stop "Download failed. Check the internet connection or install from https://www.python.org/downloads/"
      sudo installer -pkg "$TMPPKG" -target / || stop "The Python installer did not finish."
      export PATH="/Library/Frameworks/Python.framework/Versions/$PY_WANT/bin:$PATH"
    fi
  else
    if command -v apt-get >/dev/null 2>&1; then
      CMD="sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
    elif command -v dnf >/dev/null 2>&1; then
      CMD="sudo dnf install -y python3 python3-pip"
    elif command -v pacman >/dev/null 2>&1; then
      CMD="sudo pacman -S --noconfirm python python-pip"
    elif command -v zypper >/dev/null 2>&1; then
      CMD="sudo zypper install -y python312 python312-pip"
    else
      stop "I don't recognize this Linux's package manager. Install Python 3.10+ yourself, then re-run."
    fi
    echo "  Command: $CMD"
    ask "Install Python with your system package manager (needs your password)?" || stop "Python is required."
    sh -c "$CMD" || stop "The package manager could not install Python."
  fi
  hash -r 2>/dev/null || true
  PY="$(find_python || true)"
  [ -n "$PY" ] || stop "Python still isn't found. Open a new terminal window and run ./install.sh again."
  ok "Installed $("$PY" --version 2>&1)"
fi

# Debian/Ubuntu ship Python without venv support unless python3-venv is installed
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  warn "This Python can't create private environments (the venv module is missing)."
  if command -v apt-get >/dev/null 2>&1; then
    VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    ask "Install python$VER-venv with apt (needs your password)?" || stop "The venv module is required."
    sudo apt-get install -y "python$VER-venv" || sudo apt-get install -y python3-venv || stop "Could not install venv support."
  else
    stop "Install your distribution's Python venv package, then re-run."
  fi
fi

# ------------------------------------------------------------------ 2. private environment
echo; bold "Step 2 of 4: Private Python environment"
if [ -x "$VENV/bin/python" ]; then
  ok "Found an existing environment in .venv"
  if [ "$AUTO_YES" = 0 ] && ask "Rebuild it from scratch? (No = keep it and just update packages)"; then
    rm -rf "$VENV"
  fi
fi
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV" || stop "Could not create the environment in $VENV"
  ok "Created .venv"
fi
VPY="$VENV/bin/python"

# ------------------------------------------------------------------ 3. packages
echo; bold "Step 3 of 4: Packages"
echo "  Needed: $(tr '\n' ' ' < requirements.txt)"
MISSING="$("$VPY" - <<'EOF'
import importlib
need = {"numpy": "numpy", "scipy": "scipy", "requests": "requests", "flask": "flask", "PIL": "pillow", "eccodes": "eccodes", "tzdata": "tzdata"}
miss = []
for mod, pip in need.items():
    try:
        importlib.import_module(mod)
    except Exception:
        miss.append(pip)
print(" ".join(miss))
EOF
)"
if [ -z "$MISSING" ]; then
  ok "All packages are already installed"
  if ask "Check for package updates anyway?"; then DO_PIP=1; else DO_PIP=0; fi
else
  echo "  Missing: $MISSING"
  ask "Download and install them into .venv now?" || stop "The predictor can't run without these packages."
  DO_PIP=1
fi
if [ "$DO_PIP" = 1 ]; then
  "$VPY" -m pip install --upgrade pip --disable-pip-version-check -q || warn "Could not upgrade pip (continuing)"
  "$VPY" -m pip install --disable-pip-version-check -r requirements.txt || stop "Package installation failed (see the messages above)."
  ok "Packages installed"
fi

# ecCodes needs a native library; pip supplies it on Windows, macOS 13+ and most Linux.
if ! "$VPY" -c 'import eccodes; eccodes.codes_get_api_version()' >/dev/null 2>&1; then
  warn "The GRIB reader (ecCodes) package installed, but its native library didn't load."
  if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    ask "Install the ecCodes library with Homebrew (brew install eccodes)?" && brew install eccodes
  elif command -v apt-get >/dev/null 2>&1; then
    ask "Install the ecCodes library with apt (sudo apt-get install libeccodes0)?" && sudo apt-get install -y libeccodes0
  elif command -v dnf >/dev/null 2>&1; then
    ask "Install the ecCodes library with dnf (sudo dnf install eccodes)?" && sudo dnf install -y eccodes
  fi
fi

# ------------------------------------------------------------------ 4. check
echo; bold "Step 4 of 4: Checking the installation"
NET=""
ask "Also test whether the weather data servers can be reached? (about 1 minute)" && NET="--network"
"$VPY" -m zephyrus.doctor $NET
STATUS=$?

chmod +x start_web.sh 2>/dev/null || true
echo
if [ $STATUS -eq 0 ]; then
  bold "Setup complete."
  echo "  Start the predictor any time with:   ./start_web.sh"
  echo "  then open http://localhost:8080 in a browser."
  if ask "Run the built-in self-tests now? (about 2 minutes)"; then
    for t in tests/test_physics.py tests/test_grib.py tests/test_tracks.py tests/test_v2.py; do
      if "$VPY" "$t" >/dev/null 2>&1; then ok "$t"; else fail "$t (run '$VPY $t' to see why)"; fi
    done
  fi
  if ask "Start the predictor now?"; then exec ./start_web.sh; fi
else
  stop "Setup finished with problems. Fix the [FAIL] items above, then run ./install.sh again."
fi
