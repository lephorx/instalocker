@echo off
REM One-click launcher for people who already have Python installed --
REM see the GitHub Releases page for a standalone .exe that doesn't need
REM Python at all. Creates a venv on first run (skipped after that),
REM installs/updates deps, then starts the helper.
cd /d "%~dp0"

if not exist venv (
    echo Setting up (first run only)...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -q -r requirements.txt

echo.
echo Starting Instalocker helper -- leave this window open while you play.
echo.
python run.py

pause
