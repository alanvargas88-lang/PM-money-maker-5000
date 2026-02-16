#!/usr/bin/env bash
# ============================================================
# Polymarket Compounder — Start Dashboard
# ============================================================
# Launches the web dashboard in your default browser.
# Run setup.sh first if you haven't already.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
else
    echo "ERROR: Virtual environment not found. Run setup.sh first."
    exit 1
fi

echo "Starting Polymarket Compounder dashboard..."
echo "Opening http://localhost:8501 in your browser..."
echo ""
echo "Press Ctrl+C to stop."
echo ""

streamlit run app.py --server.headless false --server.port 8501
