@echo off
cd /d "%~dp0"
python -m pip install pyinstaller
python -m PyInstaller --onefile --noconsole --uac-admin --icon rbx_limiter.ico --name RBXLimiter rbx_limiter.py
echo.
echo Done: dist\RBXLimiter.exe
pause
