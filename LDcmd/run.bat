@echo off
REM Launch LDcmd from source.
REM Needs Python + dependencies installed first:
REM     pip install -r requirements.txt
cd /d "%~dp0"
python ldcmd.py
if errorlevel 1 pause
