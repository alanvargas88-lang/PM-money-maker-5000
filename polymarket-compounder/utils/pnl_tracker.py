"""
PnL tracking — trade journal CSV, daily/weekly summaries.

Every resolved trade is appended to ``data/journal.csv``.  At midnight
UTC the tracker generates a daily summary (console + Telegram).
Weekly summaries aggregate the same metrics with additional stats
like best/worst trade and longest streaks.
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from core.position_tracker import TradeRecord, PositionTracker
from utils.logger import get_logger
from utils.telegram_alerts import send_telegram

log = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_JOURNAL_PATH = _PROJECT_ROOT / "data" / "journal.csv"

_CSV_COLUMNS = [
    "timestamp",
    "datetime_utc",
    "strategy",
    "market_name",
    "side",
    "entry_price",
    "exit_price",
    "size_usd",
    "pnl_usd",
    "pnl_pct",
    "balance_after",
    "phase",
]


class PnLTracker:
    """Trade journal and periodic PnL summaries.

    Call :meth:`record_trade` after every position close.
    Call :meth:`check_daily_summary` once per main-loop cycle;
    it only fires at midnight UTC.
    """

    def __init__(self, tracker: PositionTracker) -> None:
        self._tracker = tracker
        self._last_daily: Optional[float] = None
        self._last_weekly: Optional[float] = None
        self._starting_balance: float = 0.0
        self._ensure_journal_header()

    def set_starting_balance(self, balance: float) -> None:
        """Record the balance at bot startup for total-return calcs."""
        self._starting_balance = balance

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_trade(self, record: TradeRecord) -> None:
        """Append a completed trade to the CSV journal."""
        dt_str = datetime.fromtimestamp(record.timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        row = {
            "timestamp": record.timestamp,
            "datetime_utc": dt_str,
            "strategy": record.strategy,
            "market_name": record.market_name[:100],
            "side": record.side,
            "entry_price": f"{record.entry_price:.6f}",
            "exit_price": f"{record.exit_price:.6f}",
            "size_usd": f"{record.size_usd:.2f}",
            "pnl_usd": f"{record.pnl_usd:.4f}",
            "pnl_pct": f"{record.pnl_pct:.4f}",
            "balance_after": f"{record.balance_after:.2f}",
            "phase": record.phase,
        }
        try:
            with open(_JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
                writer.writerow(row)
        except OSError as exc:
            log.error("Failed to write journal: %s", exc)

    # ------------------------------------------------------------------
    # Periodic summaries
    # ------------------------------------------------------------------

    async def check_daily_summary(self) -> None:
        """Emit a daily summary if a UTC day boundary has been crossed."""
        now = time.time()
        current_day = datetime.fromtimestamp(now, tz=timezone.utc).date()

        if self._last_daily is not None:
            last_day = datetime.fromtimestamp(self._last_daily, tz=timezone.utc).date()
            if current_day == last_day:
                return

        self._last_daily = now
        await self._emit_daily_summary()

        # Weekly summary (every 7 days)
        if self._last_weekly is None or (now - self._last_weekly) >= 7 * 86400:
            self._last_weekly = now
            await self._emit_weekly_summary()

    async def _emit_daily_summary(self) -> None:
        """Generate and log/send the daily PnL summary."""
        history = self._tracker.trade_history
        today = datetime.now(tz=timezone.utc).date()

        day_trades = [
            t for t in history
            if datetime.fromtimestamp(t.timestamp, tz=timezone.utc).date() == today
        ]

        if not day_trades:
            log.info("Daily summary: No trades today")
            return

        total_pnl = sum(t.pnl_usd for t in day_trades)
        wins = sum(1 for t in day_trades if t.pnl_usd >= 0)
        win_rate = wins / len(day_trades) if day_trades else 0
        balance = day_trades[-1].balance_after if day_trades else 0

        summary = (
            f"📊 Daily Summary ({today})\n"
            f"Trades: {len(day_trades)}\n"
            f"Win rate: {win_rate:.0%}\n"
            f"Net PnL: ${total_pnl:+.2f}\n"
            f"Balance: ${balance:.2f}\n"
            f"Strategies: {', '.join(set(t.strategy for t in day_trades))}"
        )

        log.info(summary)
        await send_telegram(summary)

    async def _emit_weekly_summary(self) -> None:
        """Generate and log/send the weekly aggregate summary."""
        history = self._tracker.trade_history
        now = time.time()
        week_start = now - 7 * 86400

        week_trades = [t for t in history if t.timestamp >= week_start]

        if not week_trades:
            return

        total_pnl = sum(t.pnl_usd for t in week_trades)
        wins = sum(1 for t in week_trades if t.pnl_usd >= 0)
        win_rate = wins / len(week_trades) if week_trades else 0

        best = max(week_trades, key=lambda t: t.pnl_usd)
        worst = min(week_trades, key=lambda t: t.pnl_usd)
        balance = week_trades[-1].balance_after if week_trades else 0

        total_return = 0.0
        if self._starting_balance > 0:
            total_return = (balance - self._starting_balance) / self._starting_balance

        summary = (
            f"📈 Weekly Summary\n"
            f"Trades: {len(week_trades)}\n"
            f"Win rate: {win_rate:.0%}\n"
            f"Net PnL: ${total_pnl:+.2f}\n"
            f"Best trade: ${best.pnl_usd:+.2f} ({best.strategy})\n"
            f"Worst trade: ${worst.pnl_usd:+.2f} ({worst.strategy})\n"
            f"Balance: ${balance:.2f}\n"
            f"Total return since start: {total_return:+.1%}"
        )

        log.info(summary)
        await send_telegram(summary)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_journal_header(self) -> None:
        """Write the CSV header if the journal file doesn't exist yet."""
        if _JOURNAL_PATH.exists() and _JOURNAL_PATH.stat().st_size > 0:
            return
        _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(_JOURNAL_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
                writer.writeheader()
        except OSError as exc:
            log.error("Failed to create journal header: %s", exc)
