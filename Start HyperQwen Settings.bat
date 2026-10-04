@echo off
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  HyperQwen Settings UI launcher
rem  Starts the local settings server and opens it in your browser.
rem  Stop: press Ctrl-C in this window (or just close it).
rem ---------------------------------------------------------------
where py >nul 2>nul
if not errorlevel 1 (
    py -3 ui\serve.py
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    python ui\serve.py
    exit /b
)

echo Python not found.
echo Install Python 3 and ensure "Add python.exe to PATH" is checked.
pause