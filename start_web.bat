@echo off
rem Start Zephyrus map interface (http://localhost:8080)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not installed yet. Double-click install.bat first.
  pause
  exit /b 1
)
echo Zephyrus: open http://localhost:8080  (close this window to stop)
start "" http://localhost:8080
".venv\Scripts\python.exe" -m zephyrus web --host 0.0.0.0 --port 8080
pause
