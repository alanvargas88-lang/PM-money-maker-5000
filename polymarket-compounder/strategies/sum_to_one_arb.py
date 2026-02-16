"""
Persistent strategy: Sum-to-One Binary Arbitrage.

Runs at every balance level — this is the safety-net strategy.

**How it works:**  In a binary market (YES / NO), the two outcomes
must resolve to exactly $1.00 combined.  If you can buy YES + NO for
less than $1.00 (after fees), you lock in a risk-free profit at
resolution regardless of the outcome.

The trick is that the "real" cost isn't the best ask — you may need
to eat through several price levels.  This strategy walks the full
order book via ``book_analyzer`` to compute the true fill price at
the intended trade size.
"""

from __future__ import annotations

from typing import Optional

import config
from core.client import PolymarketClient
from core.market_scanner import MarketScanner, MarketInfo
from core.book_analyzer import (
    walk_book_asks,
    combined_fill_cost,
    available_liquidity_at_price,
    best_ask_price,
)
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from utils.risk_manager import RiskManager, TradeRequest
from utils.pnl_tracker import PnLTracker
from utils.logger import get_logger

log = get_logger(__name__)

STRATEGY_NAME = "sum_to_one_arb"


class SumToOneArb:
    """Scan binary markets for YES + NO combined cost < $1 and execute.

    Lifecycle:
    1. Fetch all active binary markets with sufficient volume.
    2. For each, pull both order books.
    3. Walk the book to get real fill cost at intended size.
    4. If combined cost < 1.0 - fees - buffer, submit paired limit orders.
    5. Handle partial fills via OrderManager.
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

    async def scan_and_execute(self) -> None:
        """One full scan cycle: find arbs and execute if approved."""
        markets = await self._scanner.fetch_active_markets()
        candidates = self._scanner.filter_binary_tradable(
            markets, min_volume=config.MIN_DAILY_VOLUME_ARB
        )

        for market in candidates:
            await self._evaluate_market(market)

    async def _evaluate_market(self, market: MarketInfo) -> None:
        """Check a single market for arb opportunity and execute."""
        yes_id = market.yes_token_id
        no_id = market.no_token_id
        if not yes_id or not no_id:
            return

        # Fetch order books
        try:
            yes_book = self._client.get_order_book(yes_id)
            no_book = self._client.get_order_book(no_id)
        except Exception as exc:
            log.debug("Book fetch failed for %s: %s", market.slug, exc)
            return

        yes_asks = yes_book.get("asks", [])
        no_asks = no_book.get("asks", [])

        if not yes_asks or not no_asks:
            return

        # Determine trade size based on balance and available liquidity
        balance = await self._client.get_balance()
        base_size_usd = min(
            balance * config.MAX_POSITION_PCT,
            config.MAX_TRADE_USD,
        )
        # Apply risk manager sizing multiplier
        size_usd = base_size_usd * self._risk.get_position_multiplier()

        if size_usd < config.MIN_TRADE_USD:
            return

        # Walk the book to compute real fill cost
        # First, estimate how many shares that buys at roughly current price
        yes_best = best_ask_price(yes_asks)
        no_best = best_ask_price(no_asks)
        if yes_best is None or no_best is None:
            return

        # Quick sanity check: if best asks already sum > threshold, skip
        naive_sum = yes_best + no_best
        if naive_sum > config.ARB_THRESHOLD:
            return

        # Estimate share count from USD budget
        # Each share of YES + NO costs approximately naive_sum
        estimated_shares = size_usd / naive_sum if naive_sum > 0 else 0
        if estimated_shares <= 0:
            return

        # Walk both books at that share count
        cost = combined_fill_cost(yes_asks, no_asks, estimated_shares)
        if cost is None:
            return  # Not enough depth

        # Calculate profit
        # At resolution, YES + NO pays exactly $1.00 per share
        revenue_per_share = 1.0
        fees_per_share = cost * config.ESTIMATED_FEE_RATE
        profit_per_share = revenue_per_share - cost - fees_per_share

        if profit_per_share < config.MIN_ARB_PROFIT_PCT:
            return

        total_cost = cost * estimated_shares
        total_profit = profit_per_share * estimated_shares

        log.info(
            "Arb found: %s — cost=%.4f, profit/share=%.4f, total=$%.2f",
            market.question[:60], cost, profit_per_share, total_profit,
        )

        # Calculate fill prices for each side individually
        yes_fill = walk_book_asks(yes_asks, estimated_shares)
        no_fill = walk_book_asks(no_asks, estimated_shares)

        # Risk check
        trade_req = TradeRequest(
            strategy=STRATEGY_NAME,
            token_id=yes_id,
            side="BUY",
            price=cost,  # Combined effective price
            size=estimated_shares,
            max_loss_usd=total_cost * config.SLIPPAGE_BUFFER,
        )
        approved, reason = self._risk.check_trade(trade_req, balance)
        if not approved:
            log.info("Arb rejected by risk manager: %s", reason)
            return

        # Execute paired order
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

        # In dry-run or after both fill, simulate the resolution payout
        if pair.yes_leg.status == "filled" and pair.no_leg.status == "filled":
            new_balance = balance + total_profit  # Simulated
            # Close positions at $1.00 (resolution)
            yes_record = self._tracker.close_position(
                yes_id, exit_price=1.0, balance_after=new_balance, phase=0
            )
            no_record = self._tracker.close_position(
                no_id, exit_price=0.0, balance_after=new_balance, phase=0
            )
            # The NO side "exit_price" is 0 because the YES side pays out.
            # But since we bought both, the combined payout is $1.00 per pair.
            # For journal purposes, record the arb trade as a single entry.
            if yes_record:
                self._pnl.record_trade(yes_record)
                self._risk.record_trade_completed(is_win=True)

            log.info(
                "Arb executed: %s — profit $%.2f (%.1f%%)",
                market.question[:40],
                total_profit,
                (profit_per_share / cost) * 100,
            )
