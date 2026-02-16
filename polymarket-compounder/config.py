"""
Polymarket Compounder - Central Configuration

All tunables live here so you never have to dig through strategy code
to adjust thresholds.  Values are loaded once at import time; the main
loop re-reads only the few that can change at runtime (DRY_RUN, phase
overrides) via environment.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ============ HELPERS ============

def _env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean from the environment ('true'/'1' → True)."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _env_int(key: str, default: int = 0) -> int:
    """Read an integer from the environment."""
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float = 0.0) -> float:
    """Read a float from the environment."""
    return float(os.getenv(key, str(default)))


# ============ GLOBAL SETTINGS ============

DRY_RUN: bool = _env_bool("DRY_RUN", default=True)
SCAN_INTERVAL: int = 10                        # Seconds between main-loop cycles
CHAIN_ID: int = 137                            # Polygon mainnet

# ============ PHASE THRESHOLDS ============
# Phase 1 (resolution arb + sum-to-one arb) is always active.
# Higher phases *stack* on top — they don't replace lower phases.

PHASE_2_BALANCE: float = 250.0                 # Activate new-market sniping
PHASE_3_BALANCE: float = 500.0                 # Activate directional engine
ACTIVE_PHASE: int = _env_int("ACTIVE_PHASE", 0)  # 0 = auto-phase

# ============ SUM-TO-ONE ARB ============

ARB_THRESHOLD: float = 0.985
"""Maximum combined fill price (YES + NO) to consider an arb opportunity.
After fees the effective cost must still be < 1.0."""

SLIPPAGE_BUFFER: float = 0.005
"""Extra margin deducted from arb profit to account for execution slippage."""

MIN_ARB_PROFIT_PCT: float = 0.005              # 0.5% minimum profit after fees

MIN_DAILY_VOLUME_ARB: float = 500.0
"""Skip markets with < $500 daily volume for sum-to-one arb (thin books)."""

# ============ RESOLUTION ARB ============

MIN_RESOLUTION_EDGE: float = 0.03              # 3 % discount on known winner
PRICE_BUFFER_PCT: float = 0.005
"""If the live price is within 0.5 % of the market's strike, skip —
the outcome is not unambiguous enough."""

MAX_RESOLUTION_POSITION_PCT: float = 0.20      # 20 % of balance

# ============ NEW MARKET SNIPER ============

NEW_MARKET_SCAN_INTERVAL: int = 30             # Seconds between new-market polls
NEW_MARKET_AGE_LIMIT: int = 900                # Only markets < 15 min old
HIGH_PRIORITY_THRESHOLD: float = 0.94          # Sum below this = high priority
MAX_NEW_MARKET_EXPOSURE_PCT: float = 0.25      # 25 % total balance cap

# ============ DIRECTIONAL ENGINE ============

MIN_EDGE_DIRECTIONAL: float = 0.10             # 10 pp edge required
MAX_DIRECTIONAL_POSITION_PCT: float = 0.10     # 10 % of balance per bet
MAX_CONCURRENT_DIRECTIONAL: int = 3
MAX_TOTAL_DIRECTIONAL_PCT: float = 0.25        # 25 % total balance cap
DIRECTIONAL_AUTO_DISABLE_WINRATE: float = 0.50 # Disable below 50 % over 20 bets
DIRECTIONAL_MIN_SAMPLE: int = 20               # Trades before evaluating winrate

# ============ RISK MANAGEMENT ============

MAX_TRADE_USD: float = 100.0
MIN_TRADE_USD: float = 2.0
MAX_POSITION_PCT: float = 0.20                 # Per-trade cap
MAX_TOTAL_EXPOSURE_PCT: float = 0.40           # All open positions combined
MAX_CONSECUTIVE_LOSSES: int = 3
MAX_DAILY_DRAWDOWN_PCT: float = 0.05           # 5 % daily drawdown limit
MAX_SINGLE_LOSS_PCT: float = 0.03              # 3 % of balance
COOLDOWN_MINUTES: int = 30
RECOVERY_POSITION_MULTIPLIER: float = 0.5      # Half size during recovery
RECOVERY_TRADE_COUNT: int = 5                  # Trades at reduced size
MAX_STRATEGY_EXPOSURE_PCT: float = 0.30        # Single strategy cap

# ============ EXTERNAL APIs ============

COINGECKO_BASE: str = "https://api.coingecko.com/api/v3"
BINANCE_TICKER: str = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES: str = "https://api.binance.com/api/v3/klines"
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"

# ============ ORDER EXECUTION ============

ORDER_TIMEOUT_SECONDS: int = 15
USE_LIMIT_ORDERS: bool = True                  # Always prefer maker orders
MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: int = 2                    # Exponential backoff base (seconds)

# ============ POLYMARKET FEE SCHEDULE ============
# Polymarket charges maker/taker fees on the CLOB.  As of 2025 the
# standard fee is ~2 % for takers, 0 % for makers (limit orders that
# rest on the book).  We use limit orders whenever possible.

MAKER_FEE_RATE: float = 0.0
TAKER_FEE_RATE: float = 0.02
ESTIMATED_FEE_RATE: float = 0.01               # Blended estimate for sizing

# ============ USDC CONTRACT (Polygon) ============

USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
