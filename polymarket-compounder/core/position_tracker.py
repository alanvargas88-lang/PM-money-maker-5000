"""
Track all open positions, per-trade PnL, and win/loss streaks.

The PositionTracker is the single source of truth for "what do we
currently own?" and "how has each strategy been performing?".  Every
strategy registers trades here; the risk manager queries it before
approving new trades.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Position:
    """A single open position in a conditional token."""

    token_id: str
    market_id: str
    market_question: str
    side: str                   # 'YES' or 'NO'
    entry_price: float
    size: float                 # Number of shares held
    strategy: str               # Which strategy opened this position
    opened_at: float = field(default_factory=time.time)
    exit_price: Optional[float] = None
    closed_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def cost_basis(self) -> float:
        """Total USDC spent to open the position."""
        return self.entry_price * self.size

    @property
    def pnl(self) -> Optional[float]:
        """Realised PnL in USDC (None if still open)."""
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.size

    @property
    def pnl_pct(self) -> Optional[float]:
        """Realised PnL as a percentage of cost basis."""
        if self.exit_price is None or self.entry_price == 0:
            return None
        return (self.exit_price - self.entry_price) / self.entry_price


@dataclass
class TradeRecord:
    """Immutable record of a completed trade for the journal."""

    timestamp: float
    strategy: str
    market_name: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    pnl_pct: float
    balance_after: float
    phase: int


class PositionTracker:
    """Centralised position and trade tracking.

    Strategies call :meth:`open_position` when entering and
    :meth:`close_position` when exiting.  The risk manager
    calls :meth:`total_exposure` and :meth:`strategy_exposure`
    to enforce limits.
    """

    def __init__(self) -> None:
        self._positions: List[Position] = []
        self._trade_history: List[TradeRecord] = []
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._max_win_streak: int = 0
        self._max_loss_streak: int = 0

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_position(
        self,
        token_id: str,
        market_id: str,
        market_question: str,
        side: str,
        entry_price: float,
        size: float,
        strategy: str,
    ) -> Position:
        """Register a new open position."""
        pos = Position(
            token_id=token_id,
            market_id=market_id,
            market_question=market_question,
            side=side,
            entry_price=entry_price,
            size=size,
            strategy=strategy,
        )
        self._positions.append(pos)
        log.info(
            "Position opened: %s %s %.4f × %.2f [%s] — %s",
            side, token_id[:16], entry_price, size, strategy,
            market_question[:60],
        )
        return pos

    def close_position(
        self,
        token_id: str,
        exit_price: float,
        balance_after: float,
        phase: int,
    ) -> Optional[TradeRecord]:
        """Mark a position as closed and record the trade.

        Returns a TradeRecord for the PnL tracker, or None if the
        position was not found.
        """
        pos = self._find_open(token_id)
        if pos is None:
            log.warning("Cannot close unknown position for token %s", token_id[:16])
            return None

        pos.exit_price = exit_price
        pos.closed_at = time.time()

        pnl = pos.pnl or 0.0
        pnl_pct = pos.pnl_pct or 0.0

        # Update streaks
        if pnl >= 0:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
            self._max_win_streak = max(self._max_win_streak, self._consecutive_wins)
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            self._max_loss_streak = max(self._max_loss_streak, self._consecutive_losses)

        record = TradeRecord(
            timestamp=pos.closed_at,
            strategy=pos.strategy,
            market_name=pos.market_question,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usd=pos.cost_basis,
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            balance_after=balance_after,
            phase=phase,
        )
        self._trade_history.append(record)

        log.info(
            "Position closed: %s %s → PnL $%.2f (%.1f%%) [%s]",
            pos.side, token_id[:16], pnl, pnl_pct * 100, pos.strategy,
        )
        return record

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def open_positions(self) -> List[Position]:
        """Return all currently open positions."""
        return [p for p in self._positions if p.is_open]

    def total_exposure(self) -> float:
        """Total USDC cost basis across all open positions."""
        return sum(p.cost_basis for p in self._positions if p.is_open)

    def strategy_exposure(self, strategy: str) -> float:
        """Open exposure for a specific strategy."""
        return sum(
            p.cost_basis for p in self._positions
            if p.is_open and p.strategy == strategy
        )

    def strategy_position_count(self, strategy: str) -> int:
        """Number of open positions for a given strategy."""
        return sum(
            1 for p in self._positions
            if p.is_open and p.strategy == strategy
        )

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def consecutive_wins(self) -> int:
        return self._consecutive_wins

    @property
    def trade_history(self) -> List[TradeRecord]:
        return self._trade_history

    def strategy_trade_history(self, strategy: str) -> List[TradeRecord]:
        """Return trade records for a specific strategy."""
        return [t for t in self._trade_history if t.strategy == strategy]

    def strategy_win_rate(self, strategy: str) -> Optional[float]:
        """Win rate for a strategy, or None if no trades yet."""
        trades = self.strategy_trade_history(strategy)
        if not trades:
            return None
        wins = sum(1 for t in trades if t.pnl_usd >= 0)
        return wins / len(trades)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_open(self, token_id: str) -> Optional[Position]:
        """Find an open position by token ID (most recent first)."""
        for pos in reversed(self._positions):
            if pos.token_id == token_id and pos.is_open:
                return pos
        return None
