"""
Order placement, cancellation, and partial-fill handling.

Sits between strategies and the ClobClient to add:
  * Retry logic with exponential back-off
  * Timeout-based auto-cancel for unfilled legs
  * Partial-fill recovery (sell back filled leg at breakeven)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config
from core.client import PolymarketClient
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class OrderTicket:
    """Represents one leg of a trade before and after submission."""

    token_id: str
    side: str              # 'BUY' or 'SELL'
    price: float
    size: float            # Number of shares
    order_id: Optional[str] = None
    status: str = "pending"  # pending → submitted → filled / cancelled / failed
    submitted_at: float = 0.0


@dataclass
class PairedArbOrder:
    """Two legs of a sum-to-one arb that must both fill or be unwound."""

    yes_leg: OrderTicket
    no_leg: OrderTicket


class OrderManager:
    """Manages order lifecycle with retries, timeouts, and partial-fill recovery.

    Every strategy calls :meth:`place_limit` for single orders or
    :meth:`place_arb_pair` for paired arb legs.
    """

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client
        self._active_orders: List[OrderTicket] = []

    # ------------------------------------------------------------------
    # Single order placement
    # ------------------------------------------------------------------

    async def place_limit(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> OrderTicket:
        """Place a single limit order with retry logic.

        Retries up to ``config.MAX_RETRIES`` times on transient failures
        with exponential back-off.
        """
        ticket = OrderTicket(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
        )

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = self._client.create_limit_order(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                )
                ticket.order_id = resp.get("order_id") or resp.get("orderID", "")
                ticket.status = "submitted"
                ticket.submitted_at = time.time()
                self._active_orders.append(ticket)
                log.info(
                    "Order submitted: %s %s %.4f × %.2f → id=%s",
                    side, token_id[:16], price, size, ticket.order_id,
                )
                return ticket

            except Exception as exc:
                wait = config.RETRY_BACKOFF_BASE ** attempt
                log.warning(
                    "Order attempt %d/%d failed (%s). Retrying in %ds…",
                    attempt, config.MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)

        ticket.status = "failed"
        log.error("Order permanently failed after %d retries", config.MAX_RETRIES)
        return ticket

    # ------------------------------------------------------------------
    # Paired arb orders (sum-to-one)
    # ------------------------------------------------------------------

    async def place_arb_pair(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
        size: float,
    ) -> PairedArbOrder:
        """Place both legs of a sum-to-one arb and monitor for fills.

        If one leg fills but the other doesn't within ORDER_TIMEOUT,
        the unfilled leg is cancelled and the filled leg is sold back
        at breakeven to avoid unintended directional exposure.
        """
        pair = PairedArbOrder(
            yes_leg=OrderTicket(
                token_id=yes_token_id, side="BUY",
                price=yes_price, size=size,
            ),
            no_leg=OrderTicket(
                token_id=no_token_id, side="BUY",
                price=no_price, size=size,
            ),
        )

        # Submit both legs concurrently
        yes_task = self.place_limit(yes_token_id, "BUY", yes_price, size)
        no_task = self.place_limit(no_token_id, "BUY", no_price, size)

        pair.yes_leg, pair.no_leg = await asyncio.gather(yes_task, no_task)

        if pair.yes_leg.status == "failed" or pair.no_leg.status == "failed":
            # Cancel any submitted leg
            await self._cancel_if_submitted(pair.yes_leg)
            await self._cancel_if_submitted(pair.no_leg)
            log.warning("Arb pair aborted — one or both legs failed to submit")
            return pair

        # In dry-run mode, simulate both filling immediately
        if config.DRY_RUN:
            pair.yes_leg.status = "filled"
            pair.no_leg.status = "filled"
            return pair

        # Monitor fills with timeout
        await self._monitor_arb_fills(pair)
        return pair

    async def _monitor_arb_fills(self, pair: PairedArbOrder) -> None:
        """Wait for both legs to fill; unwind if only one leg fills."""
        deadline = time.time() + config.ORDER_TIMEOUT_SECONDS
        yes_filled = False
        no_filled = False

        while time.time() < deadline:
            if not yes_filled:
                yes_filled = await self._check_filled(pair.yes_leg)
            if not no_filled:
                no_filled = await self._check_filled(pair.no_leg)

            if yes_filled and no_filled:
                pair.yes_leg.status = "filled"
                pair.no_leg.status = "filled"
                log.info("Both arb legs filled successfully")
                return

            await asyncio.sleep(1)

        # Timeout: cancel unfilled leg, recover filled leg
        if yes_filled and not no_filled:
            await self._cancel_if_submitted(pair.no_leg)
            pair.no_leg.status = "cancelled"
            pair.yes_leg.status = "filled"
            log.warning("Arb timeout: YES filled, NO cancelled — recovering")
            await self._recover_filled_leg(pair.yes_leg)

        elif no_filled and not yes_filled:
            await self._cancel_if_submitted(pair.yes_leg)
            pair.yes_leg.status = "cancelled"
            pair.no_leg.status = "filled"
            log.warning("Arb timeout: NO filled, YES cancelled — recovering")
            await self._recover_filled_leg(pair.no_leg)

        else:
            # Neither filled — cancel both
            await self._cancel_if_submitted(pair.yes_leg)
            await self._cancel_if_submitted(pair.no_leg)
            pair.yes_leg.status = "cancelled"
            pair.no_leg.status = "cancelled"
            log.info("Arb timeout: neither leg filled, both cancelled")

    async def _check_filled(self, ticket: OrderTicket) -> bool:
        """Check if an order has been filled by querying open orders.

        If the order no longer appears in the open-orders list, we
        assume it was filled (the CLOB removes filled orders).
        """
        if not ticket.order_id or ticket.order_id == "dry-run-placeholder":
            return True

        try:
            open_orders = self._client.get_open_orders()
            for o in open_orders:
                if o.get("id") == ticket.order_id:
                    return False  # Still open
            return True  # No longer in open list → assumed filled
        except Exception as exc:
            log.debug("Fill check error: %s", exc)
            return False

    async def _recover_filled_leg(self, ticket: OrderTicket) -> None:
        """Sell back a filled leg at its entry price to neutralise exposure.

        This is a best-effort attempt.  If the sell order doesn't fill
        quickly, we log it and leave the position for the risk manager.
        """
        log.info(
            "Recovering filled leg: SELL %s @ %.4f × %.2f",
            ticket.token_id[:16], ticket.price, ticket.size,
        )
        sell_ticket = await self.place_limit(
            token_id=ticket.token_id,
            side="SELL",
            price=ticket.price,
            size=ticket.size,
        )
        if sell_ticket.status == "submitted":
            # Wait briefly for the sell to fill
            await asyncio.sleep(config.ORDER_TIMEOUT_SECONDS)
            filled = await self._check_filled(sell_ticket)
            if not filled:
                log.warning(
                    "Recovery sell did not fill — position remains open for %s",
                    ticket.token_id[:16],
                )

    async def _cancel_if_submitted(self, ticket: OrderTicket) -> None:
        """Cancel an order if it was successfully submitted."""
        if ticket.status == "submitted" and ticket.order_id:
            try:
                self._client.cancel_order(ticket.order_id)
                ticket.status = "cancelled"
            except Exception as exc:
                log.warning("Failed to cancel order %s: %s", ticket.order_id, exc)

    # ------------------------------------------------------------------
    # Bulk cancel
    # ------------------------------------------------------------------

    async def cancel_all(self) -> None:
        """Cancel all open orders.  Used during graceful shutdown."""
        try:
            self._client.cancel_all_orders()
            for ticket in self._active_orders:
                if ticket.status == "submitted":
                    ticket.status = "cancelled"
            log.info("All open orders cancelled")
        except Exception as exc:
            log.error("Failed to cancel all orders: %s", exc)
