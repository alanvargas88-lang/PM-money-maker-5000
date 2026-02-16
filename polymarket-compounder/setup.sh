#!/usr/bin/env bash
# ============================================================
# Polymarket Compounder — One-Click Setup
# ============================================================
# Run this once:   bash setup.sh
# Then launch:     bash start.sh
# ============================================================

set -e

echo "======================================"
echo "  Polymarket Compounder — Setup"
echo "======================================"
echo ""

# ------------------------------------------------------------------
# 1. Check Python is installed
# ------------------------------------------------------------------
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.9+ is required but not found."
    echo ""
    echo "Install Python:"
    echo "  macOS:   brew install python3"
    echo "  Ubuntu:  sudo apt install python3 python3-pip python3-venv"
    echo "  Windows: https://www.python.org/downloads/"
    exit 1
fi

echo "Found $($PYTHON --version)"

# ------------------------------------------------------------------
# 2. Create virtual environment
# ------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv venv
    echo "Virtual environment created."
else
    echo "Virtual environment already exists."
fi

# Activate
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
fi

# ------------------------------------------------------------------
# 3. Install dependencies
# ------------------------------------------------------------------
echo "Installing dependencies (this may take a minute)..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "Dependencies installed."

# ------------------------------------------------------------------
# 4. Create .env from template if it doesn't exist
# ------------------------------------------------------------------
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env file from template."
    echo "You can edit it manually OR use the web dashboard to configure."
else
    echo ".env already exists — keeping your current settings."
fi

# ------------------------------------------------------------------
# 5. Create data directory
# ------------------------------------------------------------------
mkdir -p data

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo ""
echo "======================================"
echo "  Setup complete!"
echo "======================================"
echo ""
echo "Next step — launch the dashboard:"
echo ""
echo "  bash start.sh"
echo ""
echo "This opens a web page in your browser where you can:"
echo "  - Enter your wallet key and settings"
echo "  - Start/stop the bot with one click"
echo "  - Monitor trades, balance, and PnL live"
echo ""
