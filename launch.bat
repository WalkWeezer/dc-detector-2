@echo off
REM DC-Detector v0.2 — Windows launcher
REM Double-click or run from terminal: launch.bat

cd /d "%~dp0"

echo ============================================================
echo  DC-Detector v0.2 — Windows launcher
echo ============================================================

REM Create venv if missing
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: failed to create venv. Is Python 3.10+ installed?
        pause
        exit /b 1
    )
)

REM Activate & install deps
call venv\Scripts\activate.bat

echo Installing / updating dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

REM Copy example config if no config.yaml exists
if not exist "config.yaml" (
    echo No config.yaml found — copying from config.example.yaml
    copy config.example.yaml config.yaml 1>NUL 2>NUL
)

REM Set PYTHONPATH
set PYTHONPATH=%~dp0src

echo.
echo Starting DC-Detector...
echo.

python src\launcher.py %*

pause
