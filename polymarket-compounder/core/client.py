"""
ClobClient wrapper — authentication, approvals, and balance queries.

Encapsulates all direct interaction with the Polymarket CLOB client
and on-chain Polygon operations so the rest of the codebase never
touches raw Web3 or py-clob-client primitives.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from web3 import Web3

import config
from utils.logger import get_logger

log = get_logger(__name__)

# Minimal ERC-20 ABI — just balanceOf + approve
_ERC20_ABI: List[Dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Polygon RPC — public endpoint; swap for Alchemy/Infura for reliability
_POLYGON_RPC = "https://polygon-rpc.com"


class PolymarketClient:
    """High-level wrapper around the Polymarket CLOB and Polygon chain.

    Responsibilities
    ----------------
    * Initialise ClobClient with derived or explicit API credentials.
    * Execute one-time ERC-20 approvals for USDC and the CTF exchange.
    * Expose helpers: ``get_balance()``, ``get_order_book()``, etc.
    """

    def __init__(self) -> None:
        self._private_key: str = os.environ["PRIVATE_KEY"]
        self._w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC))
        self._account = self._w3.eth.account.from_key(self._private_key)
        self.address: str = self._account.address
        self._clob: Optional[ClobClient] = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Full initialisation sequence (call once at startup).

        1. Build ClobClient with API creds (derive if needed).
        2. Run USDC + CTF approvals so orders can execute.
        3. Log starting balance.
        """
        self._init_clob_client()
        await self._ensure_approvals()
        balance = await self.get_balance()
        log.info("Wallet %s — USDC balance: $%.2f", self.address, balance)

    def _init_clob_client(self) -> None:
        """Create the ClobClient, deriving API creds from the private key
        when they are not explicitly supplied in the environment."""

        api_key = os.getenv("POLYMARKET_API_KEY", "")
        api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")

        host = "https://clob.polymarket.com"

        if api_key and api_secret and passphrase:
            # Explicit credentials provided
            self._clob = ClobClient(
                host,
                key=self._private_key,
                chain_id=config.CHAIN_ID,
                creds={
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "api_passphrase": passphrase,
                },
            )
            log.info("ClobClient initialised with explicit API credentials")
        else:
            # Derive credentials from the private key.
            # The py-clob-client signs a CLOB auth message with your key
            # to obtain ephemeral API creds tied to your wallet.
            self._clob = ClobClient(
                host,
                key=self._private_key,
                chain_id=config.CHAIN_ID,
            )
            self._clob.set_api_creds(self._clob.derive_api_key())
            log.info("ClobClient initialised — API creds derived from private key")

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    async def _ensure_approvals(self) -> None:
        """Approve USDC spending and CTF conditional-token transfers.

        Why two approvals?
        - **USDC approval** lets the exchange contract pull USDC from your
          wallet when you place buy orders.
        - **CTF approval** lets the exchange transfer conditional tokens
          (YES / NO shares) out of your wallet when you sell.

        Both approvals are set to the maximum uint256 value so they only
        need to happen once per wallet.
        """
        max_uint = 2**256 - 1
        usdc = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.USDC_ADDRESS),
            abi=_ERC20_ABI,
        )
        ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.CTF_ADDRESS),
            abi=_ERC20_ABI,
        )
        exchange = Web3.to_checksum_address(config.EXCHANGE_ADDRESS)

        # Check existing allowances before sending txns
        usdc_allowance = usdc.functions.allowance(self.address, exchange).call()
        if usdc_allowance < 10**12:  # Less than 1M USDC approved
            log.info("Approving USDC for exchange contract…")
            self._send_approval(usdc, exchange, max_uint)
        else:
            log.debug("USDC approval already sufficient")

        ctf_allowance = ctf.functions.allowance(self.address, exchange).call()
        if ctf_allowance < 10**12:
            log.info("Approving CTF conditional tokens for exchange…")
            self._send_approval(ctf, exchange, max_uint)
        else:
            log.debug("CTF approval already sufficient")

    def _send_approval(
        self,
        contract: Any,
        spender: str,
        amount: int,
    ) -> None:
        """Build, sign, and send an ERC-20 approve() transaction."""
        nonce = self._w3.eth.get_transaction_count(self.address)
        tx = contract.functions.approve(spender, amount).build_transaction(
            {
                "from": self.address,
                "nonce": nonce,
                "gas": 60_000,
                "gasPrice": self._w3.eth.gas_price,
                "chainId": config.CHAIN_ID,
            }
        )
        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        log.info("Approval tx confirmed: %s (status=%s)", tx_hash.hex(), receipt["status"])

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        """Return on-chain USDC balance in human-readable dollars.

        USDC on Polygon has 6 decimals, so raw balance is divided by 1e6.
        """
        usdc = self._w3.eth.contract(
            address=Web3.to_checksum_address(config.USDC_ADDRESS),
            abi=_ERC20_ABI,
        )
        raw = usdc.functions.balanceOf(self.address).call()
        return raw / 1e6

    # ------------------------------------------------------------------
    # Market data pass-throughs
    # ------------------------------------------------------------------

    def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Fetch the full order book for a single token from the CLOB.

        Returns the raw dict with 'bids' and 'asks' lists, each entry
        containing 'price' and 'size'.
        """
        return self._clob.get_order_book(token_id)

    def get_order_books(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch order books for multiple tokens in one call (if supported)."""
        return [self.get_order_book(tid) for tid in token_ids]

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def create_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> Dict[str, Any]:
        """Place a limit (maker) order on the CLOB.

        Parameters
        ----------
        token_id : str
            The conditional token ID to trade.
        side : str
            'BUY' or 'SELL'.
        price : float
            Limit price in USDC (0.01–0.99 range for binary tokens).
        size : float
            Number of shares (not USD amount).

        Returns
        -------
        dict
            CLOB order response with ``order_id``, ``status``, etc.
        """
        if config.DRY_RUN:
            log.info(
                "[DRY RUN] Would place %s LIMIT %s @ $%.4f × %.2f shares (token=%s)",
                side, "order", price, size, token_id[:16],
            )
            return {
                "order_id": "dry-run-placeholder",
                "status": "simulated",
                "price": price,
                "size": size,
            }

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed = self._clob.create_and_sign_order(order_args)
        resp = self._clob.post_order(signed, OrderType.GTC)
        log.info(
            "Order placed: %s %s @ $%.4f × %.2f → %s",
            side, token_id[:16], price, size, resp,
        )
        return resp

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an open order by its ID."""
        if config.DRY_RUN:
            log.info("[DRY RUN] Would cancel order %s", order_id)
            return {"status": "simulated_cancel"}
        resp = self._clob.cancel(order_id)
        log.info("Cancelled order %s → %s", order_id, resp)
        return resp

    def cancel_all_orders(self) -> None:
        """Cancel every open order for this wallet."""
        if config.DRY_RUN:
            log.info("[DRY RUN] Would cancel all open orders")
            return
        resp = self._clob.cancel_all()
        log.info("Cancelled all orders → %s", resp)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return list of currently open orders."""
        return self._clob.get_orders()

    # ------------------------------------------------------------------
    # Connectivity self-test
    # ------------------------------------------------------------------

    async def self_test(self) -> bool:
        """Verify API connectivity by fetching server time.

        Returns True on success; logs and returns False on failure.
        """
        try:
            ts = self._clob.get_server_time()
            log.info("CLOB connectivity OK — server time: %s", ts)
            return True
        except Exception as exc:
            log.error("CLOB self-test failed: %s", exc)
            return False
