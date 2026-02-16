"""
Central risk management — circuit breakers, drawdown tracking, position limits.

Every strategy calls ``risk_manager.check_trade(…)`` before executing.
The risk manager also manages cooldown / recovery states so that the
bot automatically reduces risk after a losing streak.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Tuple

import config
from core.position_tracker import PositionTracker
from utils.logger import get_logger

log = get_logger(__name__)


class RiskState(Enum):
    """Operating states for the risk manager."""
    NORMAL = auto()
    COOLDOWN = auto()
    RECOVERY = auto()


@dataclass
class TradeRequest:
    """Parameters of a proposed trade for risk review."""

    strategy: str
    token_id: str
    side: str
    price: float
    size: float               # Shares
    max_loss_usd: float       # Worst-case loss for this trade

    @property
    def cost_usd(self) -> float:
        """USDC outlay for this trade."""
        return self.price * self.size


class RiskManager:
    """Enforces position limits, drawdown caps, and cooldown logic.

    Usage::

        approved, reason = risk_manager.check_trade(trade_request, balance)
        if not approved:
            log.warning("Trade rejected: %s", reason)
            return
    """

    def __init__(self, tracker: PositionTracker) -> None:
        self._tracker = tracker
        self._state: RiskState = RiskState.NORMAL
        self._cooldown_until: float = 0.0
        self._recovery_trades_remaining: int = 0
        self._day_start_balance: float = 0.0
        self._day_start_time: float = time.time()
        self._last_known_balance: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_day_start_balance(self, balance: float) -> None:
        """Called at the start of each UTC day to reset drawdown tracking."""
        self._day_start_balance = balance
        self._day_start_time = time.time()
        log.info("Day-start balance set: $%.2f", balance)

    def check_trade(
        self,
        trade: TradeRequest,
        current_balance: float,
    ) -> Tuple[bool, str]:
        """Evaluate whether a proposed trade is allowed.

        Returns (approved, reason).  ``reason`` is empty on approval
        or a human-readable rejection explanation.
        """
        self._last_known_balance = current_balance

        # -- Cooldown check --
        if self._state == RiskState.COOLDOWN:
            if time.time() < self._cooldown_until:
                remaining = int(self._cooldown_until - time.time())
                return False, f"In cooldown ({remaining}s remaining)"
            # Cooldown expired — enter recovery
            self._enter_recovery()

        # -- Daily drawdown --
        if self._day_start_balance > 0:
            drawdown = (self._day_start_balance - current_balance) / self._day_start_balance
            if drawdown >= config.MAX_DAILY_DRAWDOWN_PCT:
                self._enter_cooldown(extended=False)
                return False, (
                    f"Daily drawdown limit hit ({drawdown:.1%} >= "
                    f"{config.MAX_DAILY_DRAWDOWN_PCT:.1%})"
                )

        # -- Consecutive losses --
        if self._tracker.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            if self._state != RiskState.RECOVERY:
                self._enter_cooldown(extended=False)
                return False, (
                    f"Consecutive loss limit ({self._tracker.consecutive_losses} losses)"
                )

        # -- Single-trade max loss --
        if current_balance > 0:
            loss_pct = trade.max_loss_usd / current_balance
            if loss_pct > config.MAX_SINGLE_LOSS_PCT:
                return False, (
                    f"Single-trade loss too large ({loss_pct:.1%} > "
                    f"{config.MAX_SINGLE_LOSS_PCT:.1%})"
                )

        # -- Per-trade position size --
        if current_balance > 0:
            position_pct = trade.cost_usd / current_balance
            if position_pct > config.MAX_POSITION_PCT:
                return False, (
                    f"Position too large ({position_pct:.1%} > "
                    f"{config.MAX_POSITION_PCT:.1%} of balance)"
                )

        # -- Total exposure --
        total_exposure = self._tracker.total_exposure()
        if current_balance > 0:
            new_total = (total_exposure + trade.cost_usd) / current_balance
            if new_total > config.MAX_TOTAL_EXPOSURE_PCT:
                return False, (
                    f"Total exposure limit ({new_total:.1%} > "
                    f"{config.MAX_TOTAL_EXPOSURE_PCT:.1%})"
                )

        # -- Per-strategy exposure --
        strat_exposure = self._tracker.strategy_exposure(trade.strategy)
        if current_balance > 0:
            new_strat = (strat_exposure + trade.cost_usd) / current_balance
            if new_strat > config.MAX_STRATEGY_EXPOSURE_PCT:
                return False, (
                    f"Strategy exposure limit for {trade.strategy} "
                    f"({new_strat:.1%} > {config.MAX_STRATEGY_EXPOSURE_PCT:.1%})"
                )

        # -- Minimum trade size --
        if trade.cost_usd < config.MIN_TRADE_USD:
            return False, f"Trade too small (${trade.cost_usd:.2f} < ${config.MIN_TRADE_USD})"

        # -- Maximum trade size --
        if trade.cost_usd > config.MAX_TRADE_USD:
            return False, f"Trade too large (${trade.cost_usd:.2f} > ${config.MAX_TRADE_USD})"

        return True, ""

    def get_position_multiplier(self) -> float:
        """Return the sizing multiplier based on current risk state.

        During recovery, positions are reduced to avoid compounding
        losses.
        """
        if self._state == RiskState.RECOVERY:
            return config.RECOVERY_POSITION_MULTIPLIER
        return 1.0

    def record_trade_completed(self, is_win: bool) -> None:
        """Notify the risk manager that a trade has resolved.

        During recovery, this counts down the recovery trades and
        transitions back to NORMAL or re-enters cooldown.
        """
        if self._state == RiskState.RECOVERY:
            self._recovery_trades_remaining -= 1

            if not is_win:
                # Another loss during recovery — extend cooldown
                log.warning("Loss during recovery — extending cooldown to 2 hours")
                self._enter_cooldown(extended=True)
                return

            if self._recovery_trades_remaining <= 0:
                log.info("Recovery complete — returning to normal operation")
                self._state = RiskState.NORMAL

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def is_trading_allowed(self) -> bool:
        """Quick check — is the risk manager in a state that allows any trading?"""
        if self._state == RiskState.COOLDOWN:
            return time.time() >= self._cooldown_until
        return True

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    def _enter_cooldown(self, extended: bool) -> None:
        """Pause all trading for the cooldown period."""
        minutes = config.COOLDOWN_MINUTES * (4 if extended else 1)
        self._state = RiskState.COOLDOWN
        self._cooldown_until = time.time() + minutes * 60
        log.warning(
            "Entering cooldown for %d minutes (extended=%s)",
            minutes, extended,
        )

    def _enter_recovery(self) -> None:
        """Transition from cooldown to recovery (reduced position sizes)."""
        self._state = RiskState.RECOVERY
        self._recovery_trades_remaining = config.RECOVERY_TRADE_COUNT
        log.info(
            "Entering recovery mode — %d trades at %.0f%% size",
            config.RECOVERY_TRADE_COUNT,
            config.RECOVERY_POSITION_MULTIPLIER * 100,
        )
