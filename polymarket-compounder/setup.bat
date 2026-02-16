@echo off
REM ============================================================
REM Polymarket Compounder — Setup (Windows)
REM ============================================================
REM Double-click this file or run:  setup.bat
REM ============================================================

echo ======================================
echo   Polymarket Compounder — Setup
echo ======================================
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed.
    echo.
    echo Download Python from: https://www.python.org/downloads/
    echo IMPORTANT: Check "Add Python to PATH" during install!
    echo.
    pause
    exit /b 1
)

echo Found Python:
python --version

REM Create virtual environment
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate and install
call venv\Scripts\activate.bat
echo Installing dependencies...
pip install --upgrade pip -q
pip install -r requirements.txt -q

REM Create .env
if not exist ".env" (
    copy .env.example .env
    echo Created .env file from template.
)

mkdir data 2>nul

echo.
echo ======================================
echo   Setup complete!
echo ======================================
echo.
echo Next: double-click start.bat
echo.
pause
