"""
Gamma API market discovery and filtering.

The Gamma API (https://gamma-api.polymarket.com) is Polymarket's public
market metadata endpoint.  This module fetches, caches, and filters
markets based on criteria needed by each strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

import config
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class MarketInfo:
    """Parsed market metadata from the Gamma API."""

    condition_id: str
    question: str
    slug: str
    active: bool
    closed: bool
    enable_order_book: bool
    tokens: List[Dict[str, str]]
    """List of token dicts — each has 'token_id' and 'outcome' ('Yes'/'No')."""

    volume_24h: float
    created_at: str
    end_date: str
    category: str
    description: str

    @property
    def is_binary(self) -> bool:
        """True if the market has exactly two outcomes (Yes/No)."""
        return len(self.tokens) == 2

    @property
    def yes_token_id(self) -> Optional[str]:
        for t in self.tokens:
            if t.get("outcome", "").lower() == "yes":
                return t["token_id"]
        return None

    @property
    def no_token_id(self) -> Optional[str]:
        for t in self.tokens:
            if t.get("outcome", "").lower() == "no":
                return t["token_id"]
        return None


class MarketScanner:
    """Fetch and filter Polymarket markets via the Gamma API.

    Maintains an in-memory cache of known market IDs so that the
    new-market sniper can detect recently created markets quickly.
    """

    def __init__(self) -> None:
        self._known_market_ids: set[str] = set()
        self._last_full_fetch: float = 0.0
        self._cache: List[MarketInfo] = []
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create a reusable aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Gamma API fetching
    # ------------------------------------------------------------------

    async def fetch_active_markets(self) -> List[MarketInfo]:
        """Fetch all active markets from the Gamma API.

        Results are cached for 60 seconds to respect rate limits.
        """
        now = time.time()
        if self._cache and (now - self._last_full_fetch < 60):
            return self._cache

        session = await self._get_session()
        markets: List[MarketInfo] = []
        offset = 0
        limit = 100

        while True:
            url = (
                f"{config.GAMMA_API_BASE}/markets"
                f"?active=true&closed=false&limit={limit}&offset={offset}"
            )
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        log.warning("Gamma API returned %d at offset %d", resp.status, offset)
                        break
                    data = await resp.json()
            except Exception as exc:
                log.error("Gamma API fetch failed: %s", exc)
                break

            if not data:
                break

            for raw in data:
                market = self._parse_market(raw)
                if market:
                    markets.append(market)

            if len(data) < limit:
                break
            offset += limit

        self._cache = markets
        self._last_full_fetch = now

        # Update known IDs for new-market detection
        for m in markets:
            self._known_market_ids.add(m.condition_id)

        log.debug("Fetched %d active markets from Gamma API", len(markets))
        return markets

    async def detect_new_markets(self) -> List[MarketInfo]:
        """Return markets that appeared since the last full fetch.

        The first call always returns an empty list (everything is
        baseline). Subsequent calls return only genuinely new entries.
        """
        old_ids = set(self._known_market_ids)

        # Force a fresh fetch (bypass cache)
        self._last_full_fetch = 0.0
        all_markets = await self.fetch_active_markets()

        new_markets: List[MarketInfo] = []
        for m in all_markets:
            if m.condition_id not in old_ids:
                age = time.time() - _iso_to_ts(m.created_at)
                if age < config.NEW_MARKET_AGE_LIMIT:
                    new_markets.append(m)

        if new_markets:
            log.info("Detected %d new market(s)", len(new_markets))
        return new_markets

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------

    def filter_binary_tradable(
        self,
        markets: List[MarketInfo],
        min_volume: float = 0.0,
    ) -> List[MarketInfo]:
        """Keep only binary markets with order books and minimum volume."""
        return [
            m for m in markets
            if m.is_binary
            and m.active
            and not m.closed
            and m.enable_order_book
            and m.volume_24h >= min_volume
        ]

    def filter_btc_price_markets(
        self,
        markets: List[MarketInfo],
    ) -> List[MarketInfo]:
        """Identify BTC price threshold markets by keyword heuristics."""
        keywords = ["btc", "bitcoin"]
        price_keywords = ["above", "below", "price", "over", "under"]
        results = []
        for m in markets:
            q = m.question.lower()
            if any(k in q for k in keywords) and any(k in q for k in price_keywords):
                results.append(m)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_market(raw: Dict[str, Any]) -> Optional[MarketInfo]:
        """Parse a raw Gamma API market dict into a MarketInfo."""
        try:
            tokens = raw.get("tokens", [])
            parsed_tokens = []
            for t in tokens:
                parsed_tokens.append({
                    "token_id": t.get("token_id", ""),
                    "outcome": t.get("outcome", ""),
                })

            return MarketInfo(
                condition_id=raw.get("condition_id", ""),
                question=raw.get("question", ""),
                slug=raw.get("slug", ""),
                active=raw.get("active", False),
                closed=raw.get("closed", False),
                enable_order_book=raw.get("enable_order_book", False),
                tokens=parsed_tokens,
                volume_24h=float(raw.get("volume_num_24hr", 0) or 0),
                created_at=raw.get("created_at", ""),
                end_date=raw.get("end_date_iso", ""),
                category=raw.get("category", ""),
                description=raw.get("description", ""),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("Failed to parse market: %s", exc)
            return None


def _iso_to_ts(iso_str: str) -> float:
    """Best-effort ISO-8601 → Unix timestamp conversion."""
    from datetime import datetime, timezone

    try:
        # Handle common ISO formats from the Gamma API
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0
