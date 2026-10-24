@echo off
rem Zephyrus installer for Windows. Double-click to run.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
