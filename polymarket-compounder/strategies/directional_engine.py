"""
Phase 3 strategy: Directional Engine.

Activates at $500+ balance.

**The edge:** Take calculated directional positions on BTC price
markets where publicly available volatility data suggests the
market is mispriced.  This is NOT prediction or gambling — it is
systematic mispricing detection using a simple volatility model.

The model estimates the probability of BTC being above/below a
strike price in a given timeframe using recent realized volatility
and a normal-distribution approximation.  When the model's estimate
diverges from Polymarket's implied probability by >10 percentage
points, that's a potential entry.
"""

from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple

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
from utils.telegram_alerts import send_telegram

log = get_logger(__name__)

STRATEGY_NAME = "directional_engine"


class DirectionalEngine:
    """Volatility-informed directional bets on BTC price markets.

    Scan flow:
    1. Find BTC price markets with 1h–24h resolution windows.
    2. Fetch 24h of 1-minute BTC candles from Binance.
    3. Calculate rolling standard deviation (realized vol).
    4. For each market, estimate P(BTC > strike) using normal dist.
    5. Compare model probability vs Polymarket implied probability.
    6. If divergence > 10pp, enter a sized position (simplified Kelly).
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
        self._disabled: bool = False
        self._cached_vol: Optional[Tuple[float, float, float]] = None
        """(timestamp, btc_price, hourly_std_dev)"""

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    async def scan_and_execute(self) -> None:
        """One scan cycle: evaluate BTC markets for directional edge."""
        if self._disabled:
            return

        # Auto-disable check: if win rate < threshold over enough trades
        self._check_auto_disable()

        # Check concurrent position limit
        open_count = self._tracker.strategy_position_count(STRATEGY_NAME)
        if open_count >= config.MAX_CONCURRENT_DIRECTIONAL:
            return

        markets = await self._scanner.fetch_active_markets()
        btc_markets = self._scanner.filter_btc_price_markets(
            self._scanner.filter_binary_tradable(markets)
        )

        # Refresh volatility data (reuse cache for 5 minutes)
        vol_data = await self._get_volatility_data()
        if vol_data is None:
            return

        btc_price, hourly_vol = vol_data

        for market in btc_markets:
            if open_count >= config.MAX_CONCURRENT_DIRECTIONAL:
                break
            traded = await self._evaluate_market(market, btc_price, hourly_vol)
            if traded:
                open_count += 1

    async def _evaluate_market(
        self,
        market: MarketInfo,
        btc_price: float,
        hourly_vol: float,
    ) -> bool:
        """Check one market for directional edge. Returns True if traded."""
        from strategies.resolution_arb import ResolutionArb

        strike, is_above = ResolutionArb._parse_btc_question(market.question)
        if strike is None:
            return False

        # Estimate time to resolution from end_date
        hours_to_resolve = self._estimate_hours_to_resolve(market.end_date)
        if hours_to_resolve is None or hours_to_resolve <= 0 or hours_to_resolve > 24:
            return False

        # Scale volatility to the resolution window
        # Volatility scales with sqrt(time) under random walk
        scaled_vol = hourly_vol * math.sqrt(hours_to_resolve)

        # Estimate P(BTC > strike) using normal distribution
        if btc_price <= 0 or scaled_vol <= 0:
            return False

        # Log-normal approximation
        log_return_needed = math.log(strike / btc_price)
        z_score = log_return_needed / scaled_vol
        model_prob_above = 1.0 - _normal_cdf(z_score)

        # Get Polymarket implied probability
        yes_id = market.yes_token_id
        no_id = market.no_token_id
        if not yes_id or not no_id:
            return False

        try:
            yes_book = self._client.get_order_book(yes_id)
        except Exception:
            return False

        yes_asks = yes_book.get("asks", [])
        yes_best = best_ask_price(yes_asks)
        if yes_best is None:
            return False

        # Implied probability = ask price (for YES token)
        implied_prob = yes_best

        # Determine edge and direction
        if is_above:
            model_prob = model_prob_above
        else:
            model_prob = 1.0 - model_prob_above

        edge = model_prob - implied_prob

        # Only trade when edge exceeds threshold
        if abs(edge) < config.MIN_EDGE_DIRECTIONAL:
            return False

        # Determine which side to buy
        if edge > 0:
            # Model thinks YES is underpriced → buy YES
            buy_token = yes_id
            buy_side = "YES"
            buy_price = yes_best
            buy_asks = yes_asks
        else:
            # Model thinks NO is underpriced → buy NO
            try:
                no_book = self._client.get_order_book(no_id)
            except Exception:
                return False
            no_asks = no_book.get("asks", [])
            no_best = best_ask_price(no_asks)
            if no_best is None:
                return False
            buy_token = no_id
            buy_side = "NO"
            buy_price = no_best
            buy_asks = no_asks
            edge = -edge  # Make positive for sizing

        log.info(
            "Directional edge: %s — model=%.1f%%, market=%.1f%%, edge=%.1fpp, side=%s",
            market.question[:50],
            model_prob * 100,
            implied_prob * 100,
            edge * 100,
            buy_side,
        )

        # Simplified Kelly sizing: fraction = edge / odds
        # odds = (1/buy_price) - 1 for a binary bet
        odds = (1.0 / buy_price) - 1.0 if buy_price > 0 else 0
        kelly_fraction = edge / odds if odds > 0 else 0

        # Half-Kelly, capped at max position size
        balance = await self._client.get_balance()
        size_pct = min(
            kelly_fraction * 0.5,
            config.MAX_DIRECTIONAL_POSITION_PCT,
        )
        size_usd = balance * size_pct * self._risk.get_position_multiplier()

        # Check total directional exposure
        dir_exposure = self._tracker.strategy_exposure(STRATEGY_NAME)
        if balance > 0 and (dir_exposure + size_usd) / balance > config.MAX_TOTAL_DIRECTIONAL_PCT:
            log.debug("Directional exposure cap reached")
            return False

        if size_usd < config.MIN_TRADE_USD:
            return False
        size_usd = min(size_usd, config.MAX_TRADE_USD)

        shares = size_usd / buy_price
        fill = walk_book_asks(buy_asks, shares)
        if not fill.fully_fillable:
            shares = fill.total_filled
            if shares * buy_price < config.MIN_TRADE_USD:
                return False

        # Risk check
        trade_req = TradeRequest(
            strategy=STRATEGY_NAME,
            token_id=buy_token,
            side="BUY",
            price=fill.average_price,
            size=shares,
            max_loss_usd=fill.total_cost,  # Full loss if wrong
        )
        approved, reason = self._risk.check_trade(trade_req, balance)
        if not approved:
            log.info("Directional trade rejected: %s", reason)
            return False

        # Execute
        ticket = await self._orders.place_limit(
            token_id=buy_token,
            side="BUY",
            price=fill.average_price,
            size=shares,
        )

        if ticket.status in ("submitted", "filled"):
            self._tracker.open_position(
                token_id=buy_token,
                market_id=market.condition_id,
                market_question=market.question,
                side=buy_side,
                entry_price=fill.average_price,
                size=shares,
                strategy=STRATEGY_NAME,
            )

            # Dry-run simulation: coin-flip weighted by model probability
            if config.DRY_RUN:
                import random
                win = random.random() < model_prob
                exit_price = 1.0 if win else 0.0
                pnl_usd = (exit_price - fill.average_price) * shares
                new_balance = balance + pnl_usd
                record = self._tracker.close_position(
                    buy_token, exit_price=exit_price,
                    balance_after=new_balance, phase=3,
                )
                if record:
                    self._pnl.record_trade(record)
                    self._risk.record_trade_completed(is_win=win)
                result = "WIN" if win else "LOSS"
                log.info(
                    "[DRY RUN] Directional %s: %s $%.2f",
                    result, buy_side, pnl_usd,
                )

            return True

        return False

    # ------------------------------------------------------------------
    # Volatility model
    # ------------------------------------------------------------------

    async def _get_volatility_data(self) -> Optional[Tuple[float, float]]:
        """Fetch BTC price and compute realized hourly volatility.

        Returns (current_btc_price, hourly_std_dev_of_log_returns).
        Caches for 5 minutes to avoid excessive API calls.
        """
        now = time.time()
        if self._cached_vol and (now - self._cached_vol[0]) < 300:
            return self._cached_vol[1], self._cached_vol[2]

        candles = await self._fetch_binance_klines()
        if not candles or len(candles) < 60:
            return None

        # Extract close prices
        closes = [float(c[4]) for c in candles]  # Index 4 = close
        current_price = closes[-1]

        # Calculate 1-minute log returns
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                lr = math.log(closes[i] / closes[i - 1])
                log_returns.append(lr)

        if len(log_returns) < 30:
            return None

        # Standard deviation of 1-min returns
        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
        std_1min = math.sqrt(variance)

        # Scale to hourly: there are 60 one-minute periods in an hour
        hourly_vol = std_1min * math.sqrt(60)

        self._cached_vol = (now, current_price, hourly_vol)
        log.debug(
            "Volatility data: BTC=$%.0f, hourly_vol=%.4f (%.2f%%)",
            current_price, hourly_vol, hourly_vol * 100,
        )
        return current_price, hourly_vol

    async def _fetch_binance_klines(self) -> Optional[list]:
        """Fetch 24h of 1-minute BTC/USDT candles from Binance."""
        url = (
            f"{config.BINANCE_KLINES}"
            f"?symbol=BTCUSDT&interval=1m&limit=1440"
        )
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("Binance klines returned %d", resp.status)
                    return None
                return await resp.json()
        except Exception as exc:
            log.error("Binance klines fetch failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Auto-disable
    # ------------------------------------------------------------------

    def _check_auto_disable(self) -> None:
        """Disable Phase 3 if win rate drops below threshold."""
        history = self._tracker.strategy_trade_history(STRATEGY_NAME)
        if len(history) < config.DIRECTIONAL_MIN_SAMPLE:
            return

        win_rate = self._tracker.strategy_win_rate(STRATEGY_NAME)
        if win_rate is not None and win_rate < config.DIRECTIONAL_AUTO_DISABLE_WINRATE:
            self._disabled = True
            log.warning(
                "Directional engine AUTO-DISABLED: win rate %.1f%% < %.1f%% "
                "over %d trades. Set ACTIVE_PHASE=3 to re-enable after review.",
                win_rate * 100,
                config.DIRECTIONAL_AUTO_DISABLE_WINRATE * 100,
                len(history),
            )
            # Fire-and-forget Telegram alert
            import asyncio
            asyncio.create_task(send_telegram(
                f"⚠️ Directional engine auto-disabled!\n"
                f"Win rate: {win_rate:.0%} over {len(history)} trades.\n"
                f"Manual review required."
            ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_hours_to_resolve(end_date: str) -> Optional[float]:
        """Parse end_date ISO string and return hours until resolution."""
        from datetime import datetime, timezone
        try:
            cleaned = end_date.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            delta = dt - datetime.now(tz=timezone.utc)
            return delta.total_seconds() / 3600
        except (ValueError, AttributeError):
            return None


def _normal_cdf(x: float) -> float:
    """Approximate the standard normal CDF using the error function.

    Uses math.erf which is available in Python's standard library.
    Accurate to ~1e-7 which is more than sufficient for our purposes.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
