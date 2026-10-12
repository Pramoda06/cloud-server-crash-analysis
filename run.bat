@echo off
REM One-click launcher for Windows: creates a virtual environment on first run,
REM installs Flask + psutil, then starts the simulator and opens the browser.
cd /d "%~dp0"
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv || (echo Python 3.8+ is required. Install it from python.org & pause & exit /b 1)
    call venv\Scripts\activate.bat
    python -m pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
python app.py
pause
