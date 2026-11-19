@echo off
cd /d "%~dp0"
echo Installing project requirements...
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
echo Starting Football AI Analyzer...
py app.py
pause
