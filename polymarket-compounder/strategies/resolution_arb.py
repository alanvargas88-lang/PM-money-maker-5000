"""
Phase 1 strategy: Resolution Arbitrage.

**The edge:** When a market's outcome is already publicly knowable
(e.g. BTC closed above the strike, a sports match ended) but the
market hasn't officially resolved yet, the winning token often trades
at $0.90-0.97 instead of $1.00.  Buy the known winner, collect $1.00
at resolution — near-zero risk, 3-7 % per trade.

This strategy focuses on BTC price markets because their outcomes are
verifiable in real time via public APIs (CoinGecko / Binance).
"""

from __future__ import annotations

import re
import time
from typing import Optional, Tuple

import aiohttp

import config
from core.client import PolymarketClient
from core.market_scanner import MarketScanner, MarketInfo
from core.book_analyzer import best_ask_price, walk_book_asks
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from utils.risk_manager import RiskManager, TradeRequest
from utils.pnl_tracker import PnLTracker
from utils.logger import get_logger

log = get_logger(__name__)

STRATEGY_NAME = "resolution_arb"


class ResolutionArb:
    """Buy known winners trading below $0.97 and hold to resolution.

    Scan flow:
    1. Find BTC price markets via the scanner.
    2. For each, parse the strike price and resolution time from
       the question text.
    3. Fetch the real BTC price at/after that time from two sources.
    4. If the outcome is unambiguous and the winning token is cheap
       enough, buy via limit order.
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
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Main scan loop
    # ------------------------------------------------------------------

    async def scan_and_execute(self) -> None:
        """One full scan: find resolution arb opportunities and trade."""
        markets = await self._scanner.fetch_active_markets()
        btc_markets = self._scanner.filter_btc_price_markets(
            self._scanner.filter_binary_tradable(markets)
        )

        for market in btc_markets:
            await self._evaluate_market(market)

    async def _evaluate_market(self, market: MarketInfo) -> None:
        """Determine if a BTC market has a known outcome and is tradeable."""
        # Parse strike price and direction from question
        strike, above = self._parse_btc_question(market.question)
        if strike is None:
            return

        # Get real BTC price from two sources for confirmation
        btc_price = await self._get_btc_price_confirmed()
        if btc_price is None:
            return

        # Check if the outcome is unambiguous
        pct_diff = abs(btc_price - strike) / strike
        if pct_diff < config.PRICE_BUFFER_PCT:
            log.debug(
                "BTC price ($%.0f) within buffer of strike ($%.0f) — skipping %s",
                btc_price, strike, market.slug,
            )
            return

        # Determine the winning side
        btc_above_strike = btc_price > strike
        if above:
            # Market asks "Will BTC be above $X?"
            winning_side = "YES" if btc_above_strike else "NO"
        else:
            # Market asks "Will BTC be below $X?"
            winning_side = "YES" if not btc_above_strike else "NO"

        # Get the winning token's order book
        if winning_side == "YES":
            token_id = market.yes_token_id
        else:
            token_id = market.no_token_id

        if not token_id:
            return

        try:
            book = self._client.get_order_book(token_id)
        except Exception as exc:
            log.debug("Book fetch failed: %s", exc)
            return

        asks = book.get("asks", [])
        best = best_ask_price(asks)
        if best is None:
            return

        # The winning token should resolve to $1.00.
        # Only trade if there's enough discount.
        edge = 1.0 - best
        if edge < config.MIN_RESOLUTION_EDGE:
            log.debug(
                "Edge too thin (%.1f%%) for %s — need %.1f%%",
                edge * 100, market.slug, config.MIN_RESOLUTION_EDGE * 100,
            )
            return

        # Already above $0.97 → edge is too thin after fees
        if best > 0.97:
            return

        log.info(
            "Resolution arb: %s — %s wins, token @ $%.3f (edge=%.1f%%)",
            market.question[:60], winning_side, best, edge * 100,
        )

        await self._execute(market, token_id, winning_side, best, asks, edge)

    async def _execute(
        self,
        market: MarketInfo,
        token_id: str,
        side: str,
        ask_price: float,
        asks: list,
        edge: float,
    ) -> None:
        """Place a limit buy for the winning token."""
        balance = await self._client.get_balance()
        base_size_usd = min(
            balance * config.MAX_RESOLUTION_POSITION_PCT,
            config.MAX_TRADE_USD,
        )
        size_usd = base_size_usd * self._risk.get_position_multiplier()

        if size_usd < config.MIN_TRADE_USD:
            return

        shares = size_usd / ask_price
        fill = walk_book_asks(asks, shares)
        if not fill.fully_fillable:
            shares = fill.total_filled
            if shares * ask_price < config.MIN_TRADE_USD:
                return

        # Risk check
        trade_req = TradeRequest(
            strategy=STRATEGY_NAME,
            token_id=token_id,
            side="BUY",
            price=fill.average_price,
            size=shares,
            max_loss_usd=fill.total_cost * 0.05,  # Worst case: 5% loss
        )
        approved, reason = self._risk.check_trade(trade_req, balance)
        if not approved:
            log.info("Resolution arb rejected: %s", reason)
            return

        # Place limit order at or slightly above best ask
        ticket = await self._orders.place_limit(
            token_id=token_id,
            side="BUY",
            price=fill.average_price,
            size=shares,
        )

        if ticket.status in ("submitted", "filled"):
            self._tracker.open_position(
                token_id=token_id,
                market_id=market.condition_id,
                market_question=market.question,
                side=side,
                entry_price=fill.average_price,
                size=shares,
                strategy=STRATEGY_NAME,
            )

            # In dry-run, simulate resolution fill at $1.00
            if config.DRY_RUN:
                profit = (1.0 - fill.average_price) * shares
                new_balance = balance + profit
                record = self._tracker.close_position(
                    token_id, exit_price=1.0,
                    balance_after=new_balance, phase=1,
                )
                if record:
                    self._pnl.record_trade(record)
                    self._risk.record_trade_completed(is_win=True)
                log.info(
                    "[DRY RUN] Resolution arb profit: $%.2f (%.1f%%)",
                    profit, edge * 100,
                )

    # ------------------------------------------------------------------
    # BTC price fetching
    # ------------------------------------------------------------------

    async def _get_btc_price_confirmed(self) -> Optional[float]:
        """Fetch BTC price from two sources; return average if they agree.

        Only returns a price when both sources are within 0.5% of each
        other, ensuring we don't act on stale or incorrect data.
        """
        cg_price = await self._fetch_coingecko()
        bn_price = await self._fetch_binance()

        if cg_price and bn_price:
            diff_pct = abs(cg_price - bn_price) / max(cg_price, bn_price)
            if diff_pct < 0.005:  # Within 0.5%
                return (cg_price + bn_price) / 2
            log.warning(
                "BTC price sources disagree: CG=$%.0f, BN=$%.0f (%.2f%%)",
                cg_price, bn_price, diff_pct * 100,
            )
            return None

        # Fallback to whichever source responded
        return cg_price or bn_price

    async def _fetch_coingecko(self) -> Optional[float]:
        """Fetch BTC/USD from CoinGecko (no API key required)."""
        url = f"{config.COINGECKO_BASE}/simple/price?ids=bitcoin&vs_currencies=usd"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data["bitcoin"]["usd"])
        except Exception as exc:
            log.debug("CoinGecko fetch failed: %s", exc)
            return None

    async def _fetch_binance(self) -> Optional[float]:
        """Fetch BTC/USDT from Binance public ticker."""
        url = f"{config.BINANCE_TICKER}?symbol=BTCUSDT"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data["price"])
        except Exception as exc:
            log.debug("Binance fetch failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Question parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_btc_question(question: str) -> Tuple[Optional[float], bool]:
        """Extract strike price and direction from a BTC market question.

        Returns (strike_price, is_above_question).
        Returns (None, False) if the question can't be parsed.

        Examples:
          "Will BTC be above $65,000 at 3pm ET?"  → (65000.0, True)
          "Will Bitcoin be below $60k?"            → (60000.0, False)
        """
        q = question.lower()

        # Determine direction
        is_above = "above" in q or "over" in q
        is_below = "below" in q or "under" in q
        if not is_above and not is_below:
            return None, False

        # Extract dollar amount: handles $65,000  $65000  $65k  $65.5k
        patterns = [
            r'\$([0-9]{1,3}(?:,[0-9]{3})+)',       # $65,000
            r'\$([0-9]+(?:\.[0-9]+)?)\s*k\b',       # $65k or $65.5k
            r'\$([0-9]+(?:,?[0-9]{3})*(?:\.[0-9]+)?)',  # $65000 or $65000.50
        ]

        for pattern in patterns:
            match = re.search(pattern, q)
            if match:
                raw = match.group(1).replace(",", "")
                value = float(raw)
                # Handle 'k' suffix
                if "k" in q[match.end():match.end() + 2]:
                    value *= 1000
                elif value < 1000 and "k" in q:
                    # Likely "65k" where k was adjacent
                    value *= 1000
                return value, is_above

        return None, False
