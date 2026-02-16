"""
Order-book depth walking and real fill-price calculation.

Naively using the best bid/ask ignores the fact that your order size
may eat through multiple price levels.  This module walks the book to
compute the *volume-weighted average fill price* at a given trade size
so that arb profit calculations reflect reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Optional

from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class FillEstimate:
    """Result of walking the order book for a given size."""

    average_price: float
    """Volume-weighted average price across all filled levels."""

    total_filled: float
    """Total shares that can actually be filled (may be < requested)."""

    total_cost: float
    """USDC cost for the filled amount."""

    levels_consumed: int
    """How many price levels were (partially) consumed."""

    fully_fillable: bool
    """True if the book had enough depth to fill the entire request."""


def walk_book_asks(
    asks: List[Dict[str, Any]],
    target_size: float,
) -> FillEstimate:
    """Walk the ask side of the book to compute real buy cost.

    Parameters
    ----------
    asks : list
        Sorted list of ask levels, each with ``price`` (float-like)
        and ``size`` (float-like).  Lowest ask first.
    target_size : float
        Number of shares you want to buy.

    Returns
    -------
    FillEstimate
        Aggregated fill statistics.

    The function handles partial fills at the last consumed level.
    """
    remaining = target_size
    total_cost = 0.0
    levels = 0

    for level in asks:
        if remaining <= 0:
            break

        price = float(level["price"])
        available = float(level["size"])
        fill_qty = min(remaining, available)
        total_cost += fill_qty * price
        remaining -= fill_qty
        levels += 1

    filled = target_size - remaining
    avg_price = total_cost / filled if filled > 0 else 0.0

    return FillEstimate(
        average_price=avg_price,
        total_filled=filled,
        total_cost=total_cost,
        levels_consumed=levels,
        fully_fillable=(remaining <= 0),
    )


def walk_book_bids(
    bids: List[Dict[str, Any]],
    target_size: float,
) -> FillEstimate:
    """Walk the bid side of the book to compute real sell proceeds.

    Parameters
    ----------
    bids : list
        Sorted list of bid levels (highest bid first).
    target_size : float
        Number of shares you want to sell.

    Returns
    -------
    FillEstimate
    """
    remaining = target_size
    total_proceeds = 0.0
    levels = 0

    for level in bids:
        if remaining <= 0:
            break

        price = float(level["price"])
        available = float(level["size"])
        fill_qty = min(remaining, available)
        total_proceeds += fill_qty * price
        remaining -= fill_qty
        levels += 1

    filled = target_size - remaining
    avg_price = total_proceeds / filled if filled > 0 else 0.0

    return FillEstimate(
        average_price=avg_price,
        total_filled=filled,
        total_cost=total_proceeds,
        levels_consumed=levels,
        fully_fillable=(remaining <= 0),
    )


def combined_fill_cost(
    yes_asks: List[Dict[str, Any]],
    no_asks: List[Dict[str, Any]],
    size: float,
) -> Optional[float]:
    """Compute the combined cost of buying *size* shares of YES + NO.

    Returns the total USDC cost per share if both sides are fully
    fillable, otherwise ``None``.

    This is the key metric for sum-to-one arbitrage: if the combined
    cost is below $1.00 minus fees, there is a risk-free profit.
    """
    yes_fill = walk_book_asks(yes_asks, size)
    no_fill = walk_book_asks(no_asks, size)

    if not yes_fill.fully_fillable or not no_fill.fully_fillable:
        log.debug(
            "Insufficient depth for %.2f shares (YES filled=%.2f, NO filled=%.2f)",
            size, yes_fill.total_filled, no_fill.total_filled,
        )
        return None

    combined = yes_fill.average_price + no_fill.average_price
    return combined


def best_ask_price(asks: List[Dict[str, Any]]) -> Optional[float]:
    """Return the best (lowest) ask price, or None if empty."""
    if not asks:
        return None
    return float(asks[0]["price"])


def best_bid_price(bids: List[Dict[str, Any]]) -> Optional[float]:
    """Return the best (highest) bid price, or None if empty."""
    if not bids:
        return None
    return float(bids[0]["price"])


def available_liquidity_at_price(
    asks: List[Dict[str, Any]],
    max_price: float,
) -> float:
    """Total shares available on the ask side at or below *max_price*."""
    total = 0.0
    for level in asks:
        if float(level["price"]) > max_price:
            break
        total += float(level["size"])
    return total
