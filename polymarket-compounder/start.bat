@echo off
REM ============================================================
REM Polymarket Compounder — Start Dashboard (Windows)
REM ============================================================

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERROR: Run setup.bat first!
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo Starting dashboard...
echo Opening http://localhost:8501 in your browser...
echo.
echo Press Ctrl+C to stop.
echo.

streamlit run app.py --server.headless false --server.port 8501
