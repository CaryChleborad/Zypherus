#!/bin/sh
# Start Zephyrus map interface (http://localhost:8080).
# Listens on the local network too, so a phone on the same Wi-Fi can open http://<this-computer>:8080
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "Not installed yet. Run ./install.sh first."
  exit 1
fi
echo "Zephyrus: open http://localhost:8080  (Ctrl+C to stop)"
( sleep 2; (command -v open >/dev/null && open http://localhost:8080) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8080) ) >/dev/null 2>&1 &
exec "$PY" -m zephyrus web --host 0.0.0.0 --port 8080
