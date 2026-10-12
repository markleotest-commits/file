@echo off
REM Build a standalone LDcmd.exe with PyInstaller.
REM     pip install pyinstaller
REM     build.bat
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name LDcmd ldcmd.py
echo.
echo Done. Executable is in: dist\LDcmd.exe
pause
