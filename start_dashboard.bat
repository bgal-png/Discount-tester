@echo off
REM Double-click this file to launch the local Discount Tester dashboard.
REM Keeps a console window open while the dashboard runs — close that
REM window (or press Ctrl+C in it) to stop the dashboard.

cd /d "%~dp0"

echo Starting Discount Tester dashboard...
echo A browser tab will open at http://localhost:8501
echo Close this window to stop the dashboard.
echo.

python -m streamlit run dashboard.py

REM If python isn't found or streamlit fails, keep the window open so the
REM error message stays readable.
if errorlevel 1 (
    echo.
    echo --- Dashboard exited with an error. ---
    pause
)
