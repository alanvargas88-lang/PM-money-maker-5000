"""
Polymarket Compounder — Web Dashboard

A user-friendly Streamlit app that wraps the entire bot.
No terminal knowledge required — everything is controlled from the browser.

Launch:  streamlit run app.py
"""

from __future__ import annotations

import csv
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_APP_DIR = Path(__file__).resolve().parent
_ENV_FILE = _APP_DIR / ".env"
_ENV_EXAMPLE = _APP_DIR / ".env.example"
_JOURNAL_FILE = _APP_DIR / "data" / "journal.csv"
_LOG_FILE = _APP_DIR / "data" / "trades.log"
_PID_FILE = _APP_DIR / "data" / ".bot.pid"

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Polymarket Compounder",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ===================================================================
# Helper functions
# ===================================================================

def _read_env() -> dict[str, str]:
    """Read the .env file into a dict."""
    values: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return values
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def _write_env(values: dict[str, str]) -> None:
    """Write settings back to the .env file, preserving comments."""
    lines: list[str] = []
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in values:
                    lines.append(f"{key}={values.pop(key)}")
                    continue
            lines.append(line)
    # Append any new keys that weren't in the file
    for key, val in values.items():
        lines.append(f"{key}={val}")
    _ENV_FILE.write_text("\n".join(lines) + "\n")


def _bot_is_running() -> bool:
    """Check if the bot process is alive."""
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return False


def _start_bot() -> str:
    """Start the bot as a background process."""
    if _bot_is_running():
        return "Bot is already running."

    # Determine the Python executable inside the venv
    venv_python = _APP_DIR / "venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = _APP_DIR / "venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = Path(sys.executable)  # Fallback

    proc = subprocess.Popen(
        [str(venv_python), str(_APP_DIR / "main.py")],
        cwd=str(_APP_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _PID_FILE.parent.mkdir(exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    return f"Bot started (PID {proc.pid})."


def _stop_bot() -> str:
    """Stop the bot process gracefully."""
    if not _PID_FILE.exists():
        return "Bot is not running."
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        return f"Stop signal sent to PID {pid}."
    except (ValueError, ProcessLookupError, PermissionError, OSError) as exc:
        _PID_FILE.unlink(missing_ok=True)
        return f"Could not stop bot: {exc}"


def _read_journal() -> pd.DataFrame:
    """Load the trade journal CSV into a DataFrame."""
    if not _JOURNAL_FILE.exists() or _JOURNAL_FILE.stat().st_size < 10:
        return pd.DataFrame()
    try:
        df = pd.read_csv(_JOURNAL_FILE)
        return df
    except Exception:
        return pd.DataFrame()


def _read_log_tail(n: int = 40) -> str:
    """Return the last n lines of the trade log."""
    if not _LOG_FILE.exists():
        return "No log file yet. Start the bot to generate logs."
    try:
        lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"Error reading log: {exc}"


def _get_phase_for_balance(balance: float) -> int:
    """Determine phase from balance (mirrors config logic)."""
    if balance >= 500:
        return 3
    elif balance >= 250:
        return 2
    return 1


# ===================================================================
# Sidebar — Settings
# ===================================================================

st.sidebar.title("Settings")

# Ensure .env exists
if not _ENV_FILE.exists() and _ENV_EXAMPLE.exists():
    import shutil
    shutil.copy(_ENV_EXAMPLE, _ENV_FILE)

env = _read_env()

with st.sidebar.expander("Wallet & API", expanded=not env.get("PRIVATE_KEY", "").startswith("0x")):
    st.caption("Your private key never leaves this machine.")
    private_key = st.text_input(
        "Private Key",
        value=env.get("PRIVATE_KEY", ""),
        type="password",
        help="Polygon wallet private key (0x…). Must hold USDC.",
    )
    api_key = st.text_input(
        "API Key (optional)",
        value=env.get("POLYMARKET_API_KEY", ""),
        help="Leave blank to auto-derive from private key.",
    )
    api_secret = st.text_input(
        "API Secret (optional)",
        value=env.get("POLYMARKET_API_SECRET", ""),
        type="password",
    )
    passphrase = st.text_input(
        "Passphrase (optional)",
        value=env.get("POLYMARKET_PASSPHRASE", ""),
        type="password",
    )

with st.sidebar.expander("Telegram Alerts (optional)"):
    tg_token = st.text_input(
        "Bot Token",
        value=env.get("TELEGRAM_BOT_TOKEN", ""),
        help="Get this from @BotFather on Telegram.",
    )
    tg_chat = st.text_input(
        "Chat ID",
        value=env.get("TELEGRAM_CHAT_ID", ""),
        help="Send a message to @userinfobot to get your ID.",
    )

with st.sidebar.expander("Bot Mode"):
    dry_run = st.toggle(
        "Dry Run (simulation)",
        value=env.get("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        help="When ON, the bot simulates all trades. No real money moves.",
    )
    phase_options = {
        "Auto (based on balance)": "0",
        "Phase 1 only (safest)": "1",
        "Phase 1 + 2": "2",
        "Phase 1 + 2 + 3 (all strategies)": "3",
    }
    current_phase_val = env.get("ACTIVE_PHASE", "0")
    current_label = next(
        (k for k, v in phase_options.items() if v == current_phase_val),
        "Auto (based on balance)",
    )
    phase_choice = st.selectbox(
        "Strategy Phase",
        list(phase_options.keys()),
        index=list(phase_options.keys()).index(current_label),
        help="Which strategies to run. Auto adjusts as your balance grows.",
    )

if st.sidebar.button("Save Settings", use_container_width=True, type="primary"):
    updated = {
        "PRIVATE_KEY": private_key,
        "POLYMARKET_API_KEY": api_key,
        "POLYMARKET_API_SECRET": api_secret,
        "POLYMARKET_PASSPHRASE": passphrase,
        "TELEGRAM_BOT_TOKEN": tg_token,
        "TELEGRAM_CHAT_ID": tg_chat,
        "DRY_RUN": "true" if dry_run else "false",
        "ACTIVE_PHASE": phase_options[phase_choice],
    }
    _write_env(updated)
    st.sidebar.success("Settings saved!")


# ===================================================================
# Main content
# ===================================================================

st.title("Polymarket Compounder")

# ---------------------------------------------------------------------------
# Bot controls
# ---------------------------------------------------------------------------

running = _bot_is_running()

col_status, col_start, col_stop = st.columns([2, 1, 1])

with col_status:
    if running:
        st.success("Bot is RUNNING", icon="🟢")
    else:
        mode = "DRY RUN" if dry_run else "LIVE"
        st.info(f"Bot is STOPPED — mode: {mode}", icon="⚪")

with col_start:
    if st.button(
        "Start Bot",
        use_container_width=True,
        type="primary",
        disabled=running,
    ):
        # Validate settings before starting
        pk = _read_env().get("PRIVATE_KEY", "")
        if not pk or pk == "0x_your_private_key_here":
            st.error("Enter your private key in Settings first.")
        else:
            msg = _start_bot()
            st.success(msg)
            time.sleep(1)
            st.rerun()

with col_stop:
    if st.button(
        "Stop Bot",
        use_container_width=True,
        disabled=not running,
    ):
        msg = _stop_bot()
        st.warning(msg)
        time.sleep(1)
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_overview, tab_trades, tab_logs, tab_help = st.tabs(
    ["Overview", "Trade Journal", "Live Logs", "Help & Setup"]
)

# ---- Overview tab ----
with tab_overview:
    journal = _read_journal()

    if journal.empty:
        st.info(
            "No trades yet. Start the bot to begin scanning for opportunities. "
            "In dry-run mode it will simulate trades so you can see how it works."
        )
    else:
        # Summary metrics
        total_trades = len(journal)
        total_pnl = journal["pnl_usd"].astype(float).sum()
        wins = (journal["pnl_usd"].astype(float) >= 0).sum()
        win_rate = wins / total_trades if total_trades > 0 else 0
        latest_balance = float(journal["balance_after"].iloc[-1])
        current_phase = _get_phase_for_balance(latest_balance)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Balance", f"${latest_balance:,.2f}")
        m2.metric("Total PnL", f"${total_pnl:+,.2f}")
        m3.metric("Win Rate", f"{win_rate:.0%}")
        m4.metric("Trades", total_trades)
        m5.metric("Phase", current_phase)

        st.subheader("PnL Over Time")
        if "balance_after" in journal.columns and "datetime_utc" in journal.columns:
            chart_df = journal[["datetime_utc", "balance_after"]].copy()
            chart_df["balance_after"] = chart_df["balance_after"].astype(float)
            chart_df = chart_df.rename(columns={
                "datetime_utc": "Time",
                "balance_after": "Balance ($)",
            })
            st.line_chart(chart_df, x="Time", y="Balance ($)")

        st.subheader("PnL by Strategy")
        if "strategy" in journal.columns:
            strat_summary = (
                journal.groupby("strategy")["pnl_usd"]
                .agg(["count", "sum", "mean"])
                .rename(columns={
                    "count": "Trades",
                    "sum": "Total PnL ($)",
                    "mean": "Avg PnL ($)",
                })
            )
            strat_summary["Total PnL ($)"] = strat_summary["Total PnL ($)"].map("{:+.2f}".format)
            strat_summary["Avg PnL ($)"] = strat_summary["Avg PnL ($)"].map("{:+.4f}".format)
            st.dataframe(strat_summary, use_container_width=True)

# ---- Trade Journal tab ----
with tab_trades:
    journal = _read_journal()
    if journal.empty:
        st.info("No trades recorded yet.")
    else:
        # Filters
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            strategies = ["All"] + sorted(journal["strategy"].unique().tolist())
            strat_filter = st.selectbox("Strategy", strategies)
        with col_f2:
            sides = ["All"] + sorted(journal["side"].unique().tolist())
            side_filter = st.selectbox("Side", sides)

        filtered = journal.copy()
        if strat_filter != "All":
            filtered = filtered[filtered["strategy"] == strat_filter]
        if side_filter != "All":
            filtered = filtered[filtered["side"] == side_filter]

        st.dataframe(
            filtered.sort_values("timestamp", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

        # Download button
        csv_data = filtered.to_csv(index=False)
        st.download_button(
            "Download as CSV",
            csv_data,
            file_name="polymarket_trades.csv",
            mime="text/csv",
        )

# ---- Live Logs tab ----
with tab_logs:
    log_lines = st.slider("Lines to show", 10, 200, 50)
    log_text = _read_log_tail(log_lines)
    st.code(log_text, language="log")
    if st.button("Refresh Logs"):
        st.rerun()

# ---- Help tab ----
with tab_help:
    st.markdown("""
## Quick Start Guide

### Step 1: Get a Polygon Wallet
1. Install [MetaMask](https://metamask.io/) browser extension
2. Create a new wallet (or use existing)
3. **Switch to Polygon network** (MetaMask > Networks > Add Polygon)
4. Export your private key: MetaMask > Account > Export Private Key
5. Paste it in the **Settings** sidebar on the left

### Step 2: Fund with USDC
You need USDC on the Polygon network. Options:
- **Easiest:** Buy USDC on [Coinbase](https://www.coinbase.com) and withdraw
  directly to your Polygon address
- **Bridge:** If you have USDC on Ethereum, use [Polygon Bridge](https://portal.polygon.technology/bridge)
  to move it to Polygon
- Start with **$100** (the bot is designed for small bankrolls)

### Step 3: Configure & Run
1. Enter your private key in Settings (left sidebar)
2. Keep **Dry Run ON** for the first few days to see how it works
3. Click **Start Bot**
4. Watch the Overview tab and Trade Journal fill up
5. When confident, turn off Dry Run for live trading

---

## How the Bot Works

The bot runs **4 strategies** that activate in phases as your balance grows:

| Phase | Balance | Strategies | Risk |
|-------|---------|-----------|------|
| Always | Any | **Sum-to-One Arb**: buys YES+NO for < $1, collects $1 at resolution | Very Low |
| 1 | $0-250 | **Resolution Arb**: buys known winners before official resolution | Low |
| 2 | $250+ | **New Market Sniper**: exploits mispricing in freshly created markets | Moderate |
| 3 | $500+ | **Directional Engine**: volatility-based BTC price bets | Higher |

### Safety Features
- **Dry Run mode** (default) simulates everything — no real money at risk
- **Circuit breakers** pause trading after 3 consecutive losses
- **Drawdown limit** stops trading if you lose 5% in one day
- **Position limits** prevent any single bet from being too large
- Graceful shutdown cancels all open orders

---

## What the Phase Numbers Mean

- **Phase 1** ($0–$250): Only the safest strategies run. Target: $2–8/day.
- **Phase 2** ($250–$500): Adds new-market sniping. Target: +$3–10/day.
- **Phase 3** ($500+): Adds directional bets. Variable returns.
- Realistic timeline: **2–4 months** from $100 to $1,000 with discipline.

---

## FAQ

**Is this guaranteed to make money?**
No. This is not financial advice. Markets are unpredictable. The bot looks
for mathematical edges but can't guarantee profits.

**What can go wrong?**
- Polymarket could change fees or APIs
- Extended periods with no arb opportunities
- BTC volatility model can be wrong
- Network issues on Polygon

**Can I lose my entire balance?**
The risk manager limits total exposure to 40% of your balance, so a total
wipeout is very unlikely. But all trading carries risk.

**How do I stop the bot in an emergency?**
Click **Stop Bot** above. It cancels all open orders before shutting down.
    """)

# ---------------------------------------------------------------------------
# Auto-refresh when bot is running
# ---------------------------------------------------------------------------
if running:
    st.caption("Dashboard auto-refreshes every 30 seconds while bot is running.")
    time.sleep(30)
    st.rerun()
