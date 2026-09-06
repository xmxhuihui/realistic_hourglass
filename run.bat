@echo off
rem Launch the Hourglass Timer without a console window.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "Hourglass Timer" ".venv\Scripts\pythonw.exe" "hourglass_timer.py"
    goto :eof
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "Hourglass Timer" pythonw "hourglass_timer.py"
    goto :eof
)

echo Python was not found on PATH.
echo Install Python 3.9+ and run:  pip install numpy pillow
pause
