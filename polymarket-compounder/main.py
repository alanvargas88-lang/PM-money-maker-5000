#!/usr/bin/env python3
"""
Polymarket Compounder — Main Entry Point

Orchestrates all phases of the compound bot:
  Phase 1 ($0–$250):   Resolution arb + sum-to-one arb
  Phase 2 ($250–$500):  + New market sniping
  Phase 3 ($500+):      + Directional engine

All phases keep sum-to-one arb running as a persistent background
strategy.  Phases stack — they don't replace.

Usage:
    1. Copy .env.example to .env and fill in your credentials.
    2. pip install -r requirements.txt
    3. python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import List, Optional

import config
from core.client import PolymarketClient
from core.market_scanner import MarketScanner
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from strategies.sum_to_one_arb import SumToOneArb
from strategies.resolution_arb import ResolutionArb
from strategies.new_market_sniper import NewMarketSniper
from strategies.directional_engine import DirectionalEngine
from utils.risk_manager import RiskManager
from utils.pnl_tracker import PnLTracker
from utils.telegram_alerts import send_telegram
from utils.logger import get_logger

log = get_logger("main")

# Flag for graceful shutdown
_running = True


# ------------------------------------------------------------------
# Phase management
# ------------------------------------------------------------------

def determine_phase(balance: float) -> int:
    """Determine which phase to activate based on current balance.

    If ACTIVE_PHASE is set to 1/2/3 in .env, that overrides the
    balance-based auto-phase logic.
    """
    manual = config.ACTIVE_PHASE
    if manual in (1, 2, 3):
        return manual

    # Auto-phase based on balance
    if balance >= config.PHASE_3_BALANCE:
        return 3
    elif balance >= config.PHASE_2_BALANCE:
        return 2
    else:
        return 1


def build_strategies(
    phase: int,
    client: PolymarketClient,
    scanner: MarketScanner,
    order_mgr: OrderManager,
    tracker: PositionTracker,
    risk: RiskManager,
    pnl: PnLTracker,
) -> List:
    """Instantiate the correct set of strategies for the given phase.

    Phase 1: sum-to-one arb + resolution arb
    Phase 2: + new market sniper
    Phase 3: + directional engine
    """
    strategies: List = []

    # Sum-to-one arb is ALWAYS active (persistent strategy)
    strategies.append(SumToOneArb(client, scanner, order_mgr, tracker, risk, pnl))

    if phase >= 1:
        strategies.append(ResolutionArb(client, scanner, order_mgr, tracker, risk, pnl))

    if phase >= 2:
        strategies.append(NewMarketSniper(client, scanner, order_mgr, tracker, risk, pnl))

    if phase >= 3:
        strategies.append(DirectionalEngine(client, scanner, order_mgr, tracker, risk, pnl))

    return strategies


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

async def setup() -> tuple:
    """Full startup sequence.

    1. Init client (auth + approvals)
    2. Run self-test
    3. Query balance
    4. Build scanners, risk manager, strategies
    5. Return (client, strategies, scanner, tracker, risk, pnl, balance)
    """
    log.info("=" * 60)
    log.info("Polymarket Compounder starting up")
    log.info("DRY_RUN = %s", config.DRY_RUN)
    log.info("=" * 60)

    if config.DRY_RUN:
        log.info("[DRY RUN] All trades will be simulated — no real orders")

    # Initialize client
    client = PolymarketClient()
    await client.setup()

    # Connectivity self-test
    ok = await client.self_test()
    if not ok:
        log.error("Self-test failed — check API credentials and network")
        sys.exit(1)

    # Balance
    balance = await client.get_balance()
    log.info("Starting balance: $%.2f", balance)

    if balance < config.MIN_TRADE_USD and not config.DRY_RUN:
        log.error(
            "Balance ($%.2f) below minimum trade size ($%.2f). "
            "Fund your wallet with USDC on Polygon.",
            balance, config.MIN_TRADE_USD,
        )
        sys.exit(1)

    # Core components
    scanner = MarketScanner()
    tracker = PositionTracker()
    risk = RiskManager(tracker)
    risk.set_day_start_balance(balance)
    order_mgr = OrderManager(client)
    pnl = PnLTracker(tracker)
    pnl.set_starting_balance(balance)

    # Determine phase and build strategies
    phase = determine_phase(balance)
    strategies = build_strategies(phase, client, scanner, order_mgr, tracker, risk, pnl)

    phase_names = {1: "Resolution Arb", 2: "+ New Market Sniper", 3: "+ Directional Engine"}
    active_strats = [phase_names.get(i, "") for i in range(1, phase + 1)]
    log.info(
        "Phase %d active — strategies: Sum-to-One Arb (always), %s",
        phase, ", ".join(active_strats),
    )

    await send_telegram(
        f"🤖 Polymarket Compounder started\n"
        f"Balance: ${balance:.2f}\n"
        f"Phase: {phase}\n"
        f"Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE'}"
    )

    return client, strategies, scanner, order_mgr, tracker, risk, pnl, phase


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

async def main_loop(
    client: PolymarketClient,
    strategies: List,
    scanner: MarketScanner,
    order_mgr: OrderManager,
    tracker: PositionTracker,
    risk: RiskManager,
    pnl: PnLTracker,
    phase: int,
) -> None:
    """Run all active strategies in a continuous loop.

    - Re-checks phase on every cycle (balance may have changed).
    - Runs all strategies concurrently via asyncio.gather.
    - Handles rate limits with exponential backoff.
    - Catches per-strategy errors without crashing others.
    """
    global _running
    cycle = 0

    while _running:
        cycle += 1
        try:
            # Re-check balance and phase
            current_balance = await client.get_balance()
            new_phase = determine_phase(current_balance)

            if new_phase != phase:
                log.info(
                    "Phase transition: %d → %d at $%.2f",
                    phase, new_phase, current_balance,
                )
                phase = new_phase
                strategies = build_strategies(
                    phase, client, scanner, order_mgr, tracker, risk, pnl,
                )
                await send_telegram(
                    f"📈 Phase {phase} activated! Balance: ${current_balance:.2f}"
                )

            # Check if trading is allowed (risk cooldown)
            if not risk.is_trading_allowed:
                log.debug("Risk cooldown active — skipping cycle %d", cycle)
                await asyncio.sleep(config.SCAN_INTERVAL)
                continue

            # Run all strategies concurrently
            tasks = [
                _run_strategy_safe(strategy)
                for strategy in strategies
            ]
            await asyncio.gather(*tasks)

            # Periodic PnL summary check
            await pnl.check_daily_summary()

            # Log cycle info every 30 cycles (~5 min at 10s interval)
            if cycle % 30 == 0:
                open_pos = len(tracker.open_positions())
                exposure = tracker.total_exposure()
                log.info(
                    "Cycle %d — balance=$%.2f, phase=%d, "
                    "open_positions=%d, exposure=$%.2f",
                    cycle, current_balance, phase, open_pos, exposure,
                )

            await asyncio.sleep(config.SCAN_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Main loop error: %s", exc, exc_info=True)
            await asyncio.sleep(30)


async def _run_strategy_safe(strategy: object) -> None:
    """Run a strategy's scan_and_execute, catching errors so one
    failing strategy doesn't take down the others."""
    name = type(strategy).__name__
    try:
        await strategy.scan_and_execute()  # type: ignore[attr-defined]
    except Exception as exc:
        log.error("Strategy %s error: %s", name, exc, exc_info=True)


# ------------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------------

async def graceful_shutdown(
    client: PolymarketClient,
    order_mgr: OrderManager,
    scanner: MarketScanner,
    tracker: PositionTracker,
    strategies: List,
) -> None:
    """Cancel all orders, close sessions, log final state."""
    log.info("Shutting down gracefully…")

    # Cancel all open orders
    await order_mgr.cancel_all()

    # Close HTTP sessions
    await scanner.close()
    for s in strategies:
        if hasattr(s, "close"):
            try:
                await s.close()
            except Exception:
                pass

    # Log final state
    final_balance = await client.get_balance()
    open_pos = tracker.open_positions()
    trades = tracker.trade_history

    log.info("=" * 60)
    log.info("SHUTDOWN SUMMARY")
    log.info("Final balance: $%.2f", final_balance)
    log.info("Open positions: %d", len(open_pos))
    log.info("Total trades executed: %d", len(trades))
    if trades:
        total_pnl = sum(t.pnl_usd for t in trades)
        wins = sum(1 for t in trades if t.pnl_usd >= 0)
        log.info("Total PnL: $%.2f", total_pnl)
        log.info("Win rate: %.0f%%", (wins / len(trades)) * 100)
    log.info("=" * 60)

    await send_telegram(
        f"🛑 Compounder shut down\n"
        f"Final balance: ${final_balance:.2f}\n"
        f"Trades: {len(trades)}\n"
        f"Open positions: {len(open_pos)}"
    )


def _handle_signal(signum: int, frame: object) -> None:
    """Signal handler for SIGINT/SIGTERM — sets the shutdown flag."""
    global _running
    log.info("Received signal %d — initiating shutdown…", signum)
    _running = False


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

async def main() -> None:
    """Top-level async entry point."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    client, strategies, scanner, order_mgr, tracker, risk, pnl, phase = await setup()

    try:
        await main_loop(
            client, strategies, scanner, order_mgr, tracker, risk, pnl, phase,
        )
    finally:
        await graceful_shutdown(client, order_mgr, scanner, tracker, strategies)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as exc:
        log.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
