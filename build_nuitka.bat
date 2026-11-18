@echo off
cd /d "%~dp0"
where py >/dev/null 2>&1
if errorlevel 1 (
  echo The "py" launcher was not found. Install Python 3.14.10 from python.org.
  pause
  exit /b 1
)
py -3.14 "%~dp0build_nuitka.py"
pause
