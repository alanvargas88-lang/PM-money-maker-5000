"""
Phase 2 strategy: New Market Liquidity Sniping.

Activates at $250+ balance.

**The edge:** Newly launched markets have inefficient pricing — wide
spreads, thin books, and YES + NO often summing well below $1.00.
If you arrive before market makers optimize the book, you capture the
mispricing as risk-free arb (buy both sides cheaply, collect $1.00
at resolution).
"""

from __future__ import annotations

import time
from typing import Optional

import config
from core.client import PolymarketClient
from core.market_scanner import MarketScanner, MarketInfo
from core.book_analyzer import combined_fill_cost, walk_book_asks, best_ask_price
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from utils.risk_manager import RiskManager, TradeRequest
from utils.pnl_tracker import PnLTracker
from utils.logger import get_logger

log = get_logger(__name__)

STRATEGY_NAME = "new_market_sniper"


class NewMarketSniper:
    """Detect and exploit mispricing in newly created markets.

    Scan flow:
    1. Poll Gamma API for markets created in the last 15 minutes.
    2. Score each by combined YES + NO ask cost.
    3. If sum < 0.94, flag as high priority; 0.94–0.97 = standard.
    4. Execute immediately — speed matters before MMs arrive.
    """

    def __init__(
        self,
        client: PolymarketClient,
        scanner: MarketScanner,
        order_mgr: OrderManager,
        tracker: PositionTracker,
        risk: RiskManager,
        pnl: PnLTracker,
    ) -> None:
        self._client = client
        self._scanner = scanner
        self._orders = order_mgr
        self._tracker = tracker
        self._risk = risk
        self._pnl = pnl
        self._last_scan: float = 0.0

    async def scan_and_execute(self) -> None:
        """One scan cycle: detect new markets and evaluate for sniping."""
        now = time.time()
        if now - self._last_scan < config.NEW_MARKET_SCAN_INTERVAL:
            return
        self._last_scan = now

        new_markets = await self._scanner.detect_new_markets()
        if not new_markets:
            return

        binary = self._scanner.filter_binary_tradable(new_markets)
        for market in binary:
            await self._evaluate_market(market)

    async def _evaluate_market(self, market: MarketInfo) -> None:
        """Score and potentially execute on a new market."""
        yes_id = market.yes_token_id
        no_id = market.no_token_id
        if not yes_id or not no_id:
            return

        # Check that total new-market exposure is within limits
        balance = await self._client.get_balance()
        current_exposure = self._tracker.strategy_exposure(STRATEGY_NAME)
        if balance > 0 and current_exposure / balance >= config.MAX_NEW_MARKET_EXPOSURE_PCT:
            log.debug("New-market exposure limit reached — skipping %s", market.slug)
            return

        # Fetch order books
        try:
            yes_book = self._client.get_order_book(yes_id)
            no_book = self._client.get_order_book(no_id)
        except Exception as exc:
            log.debug("Book fetch failed for new market %s: %s", market.slug, exc)
            return

        yes_asks = yes_book.get("asks", [])
        no_asks = no_book.get("asks", [])

        if not yes_asks or not no_asks:
            return

        # Calculate combined cost at best ask (quick screen)
        yes_best = best_ask_price(yes_asks)
        no_best = best_ask_price(no_asks)
        if yes_best is None or no_best is None:
            return

        naive_sum = yes_best + no_best

        # Classify opportunity
        if naive_sum > 0.97:
            # Market makers already arrived
            log.debug("New market %s already efficient (sum=%.3f)", market.slug, naive_sum)
            return
        elif naive_sum <= config.HIGH_PRIORITY_THRESHOLD:
            priority = "HIGH"
        else:
            priority = "STANDARD"

        log.info(
            "New market opportunity [%s]: %s — sum=%.4f",
            priority, market.question[:60], naive_sum,
        )

        # Size: up to 15% of balance for new markets, constrained by liquidity
        max_usd = min(balance * 0.15, config.MAX_TRADE_USD)
        size_usd = max_usd * self._risk.get_position_multiplier()

        if size_usd < config.MIN_TRADE_USD:
            return

        # Estimate shares
        estimated_shares = size_usd / naive_sum if naive_sum > 0 else 0
        if estimated_shares <= 0:
            return

        # Walk the book for real fill prices
        cost = combined_fill_cost(yes_asks, no_asks, estimated_shares)
        if cost is None:
            # Not enough liquidity — try smaller size
            estimated_shares *= 0.5
            cost = combined_fill_cost(yes_asks, no_asks, estimated_shares)
            if cost is None:
                log.debug("Insufficient liquidity in new market %s", market.slug)
                return

        # Fee-adjusted profitability check
        fees = cost * config.ESTIMATED_FEE_RATE
        profit_per_share = 1.0 - cost - fees
        if profit_per_share < config.MIN_ARB_PROFIT_PCT:
            return

        total_cost = cost * estimated_shares
        total_profit = profit_per_share * estimated_shares

        # Risk check
        trade_req = TradeRequest(
            strategy=STRATEGY_NAME,
            token_id=yes_id,
            side="BUY",
            price=cost,
            size=estimated_shares,
            max_loss_usd=total_cost * 0.05,
        )
        approved, reason = self._risk.check_trade(trade_req, balance)
        if not approved:
            log.info("New market snipe rejected: %s", reason)
            return

        # Execute paired arb
        yes_fill = walk_book_asks(yes_asks, estimated_shares)
        no_fill = walk_book_asks(no_asks, estimated_shares)

        pair = await self._orders.place_arb_pair(
            yes_token_id=yes_id,
            no_token_id=no_id,
            yes_price=yes_fill.average_price,
            no_price=no_fill.average_price,
            size=estimated_shares,
        )

        # Track positions
        if pair.yes_leg.status == "filled":
            self._tracker.open_position(
                token_id=yes_id,
                market_id=market.condition_id,
                market_question=market.question,
                side="YES",
                entry_price=yes_fill.average_price,
                size=estimated_shares,
                strategy=STRATEGY_NAME,
            )
        if pair.no_leg.status == "filled":
            self._tracker.open_position(
                token_id=no_id,
                market_id=market.condition_id,
                market_question=market.question,
                side="NO",
                entry_price=no_fill.average_price,
                size=estimated_shares,
                strategy=STRATEGY_NAME,
            )

        if pair.yes_leg.status == "filled" and pair.no_leg.status == "filled":
            # Simulate resolution payout in dry-run
            new_balance = balance + total_profit
            record = self._tracker.close_position(
                yes_id, exit_price=1.0,
                balance_after=new_balance, phase=2,
            )
            self._tracker.close_position(
                no_id, exit_price=0.0,
                balance_after=new_balance, phase=2,
            )
            if record:
                self._pnl.record_trade(record)
                self._risk.record_trade_completed(is_win=True)

            log.info(
                "New market snipe executed [%s]: %s — profit $%.2f",
                priority, market.question[:40], total_profit,
            )
