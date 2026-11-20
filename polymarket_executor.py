"""Authenticated Polymarket trading via the official `polymarket-client` SDK (Python 3.11+).

The older py-clob-client was archived in May 2026 and no longer works, so it is not used. The SDK is
imported lazily: on Python 3.9/3.10, or if it isn't installed, this module still imports and reports
a clear "unsupported" status instead of failing. Read-only scanning never needs any of this.

Credentials, from .env only:
    POLY_PRIVATE_KEY      EVM key that signs orders for your Polymarket account
    POLY_WALLET_ADDRESS   the existing Polymarket wallet (deposit / proxy / safe) holding your pUSD.
                          Required: without it the SDK would deploy a brand-new Deposit Wallet.

Region rules: Polymarket's geoblock endpoint is checked before connecting and again before every
order. If it reports `blocked` (the US, among others, is close-only), order placement is disabled.
This code respects that restriction and never tries to route around it.

Orders: Leg 2 is a FOK market BUY of a whole-cent USD amount with `max_price` = the worst price level
the sizing walked, so it fills completely at or below that price or not at all.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Dict, Optional

import httpx

from config import Settings

log = logging.getLogger("polymarket.exec")
SDK_MIN_PY = (3, 11)
GEOBLOCK_TTL = 300.0


class PolyExecError(Exception):
    """Order definitively not placed."""


class PolyAmbiguousError(Exception):
    """Order may or may not have been placed."""


class PolymarketExecutor:
    platform = "Polymarket"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.s = s
        self.http = http
        self.client: Any = None
        self.status = "unconfigured"  # unconfigured | unsupported | blocked | ready | error
        self.detail = "Read-only: set POLY_PRIVATE_KEY + POLY_WALLET_ADDRESS in .env"
        self.balance: Optional[float] = None
        self.balance_at = 0.0
        self.geo_blocked: Optional[bool] = None
        self.geo_country = ""
        self.geo_at = 0.0

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def can_trade(self) -> bool:
        return self.status == "ready" and self.geo_blocked is False

    # ------------------------------------------------------------- setup
    async def init(self) -> None:
        await self.check_geoblock(force=True)  # shown on the dashboard even in read-only mode
        if not self.s.poly_private_key:
            return
        if sys.version_info < SDK_MIN_PY:
            self.status = "unsupported"
            self.detail = (f"Polymarket's SDK (polymarket-client) needs Python 3.11+; this venv runs "
                           f"{sys.version_info.major}.{sys.version_info.minor}. See README → Polymarket setup.")
            log.warning("[Polymarket] %s", self.detail)
            return
        try:
            import polymarket  # noqa: F401
        except ImportError:
            self.status, self.detail = "unsupported", "polymarket-client is not installed: pip install polymarket-client"
            log.warning("[Polymarket] %s", self.detail)
            return
        if not self.s.poly_wallet_address:
            self.status = "error"
            self.detail = "Set POLY_WALLET_ADDRESS to your existing Polymarket wallet (prevents a new wallet deploy)"
            log.error("[Polymarket] %s", self.detail)
            return
        try:
            from polymarket import AsyncSecureClient

            self.client = await AsyncSecureClient.create(private_key=self.s.poly_private_key,
                                                         wallet=self.s.poly_wallet_address)
        except Exception as exc:  # SDK raises UserInputError / RequestRejectedError / TransportError
            self.status, self.detail = "error", f"Could not authenticate with Polymarket: {exc}"[:300]
            log.error("[Polymarket] authentication failed: %r", exc)
            return
        await self.refresh_balance()

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as exc:
                log.warning("[Polymarket] client close failed: %r", exc)

    async def check_geoblock(self, force: bool = False) -> bool:
        """True if order placement is allowed from this IP. Fails safe: an unknown result blocks."""
        if not force and self.geo_blocked is not None and time.time() - self.geo_at < GEOBLOCK_TTL:
            return not self.geo_blocked
        try:
            r = await self.http.get(self.s.poly_geoblock_url, timeout=10)
            r.raise_for_status()
            data = r.json()
            self.geo_blocked, self.geo_country = bool(data.get("blocked")), str(data.get("country") or "")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("[Polymarket] geoblock check failed (%r); treating trading as blocked", exc)
            self.geo_blocked, self.geo_country = True, "unknown"
        self.geo_at = time.time()
        if self.geo_blocked and self.status == "ready":
            self.status = "blocked"
            self.detail = (f"Polymarket does not accept new orders from your region ({self.geo_country}). "
                           "Execution on Polymarket is disabled.")
        elif not self.geo_blocked and self.status == "blocked":
            self.status = "ready"
        return not self.geo_blocked

    async def refresh_balance(self) -> Optional[float]:
        if self.client is None:
            return None
        try:
            ba = await self.client.get_balance_allowance(asset_type="COLLATERAL")
            self.balance = int(ba.balance) / 1e6  # pUSD, 6 decimals
            self.balance_at = time.time()
            if not any(int(v) > 0 for v in (ba.allowances or {}).values()):
                self.status = "error"
                self.detail = "pUSD allowance not set for the exchange contracts (approve trading on polymarket.com)"
            elif self.geo_blocked:
                self.status = "blocked"
                self.detail = (f"Polymarket does not accept new orders from your region ({self.geo_country}). "
                               "Execution on Polymarket is disabled.")
            else:
                self.status, self.detail = "ready", f"Wallet {self.s.poly_wallet_address[:6]}…{self.s.poly_wallet_address[-4:]}"
        except Exception as exc:
            self.status, self.detail = "error", f"Balance check failed: {exc}"[:300]
            log.error("[Polymarket] balance check failed: %r", exc)
        return self.balance

    # ------------------------------------------------------------- orders
    async def sign_market_buy(self, token_id: str, amount: float, max_price: float) -> str:
        """Dry run: build and sign the exact order without posting it. Returns a short description."""
        if self.client is None:
            return "unsigned (Polymarket credentials not configured)"
        signed = await self.client.create_market_order(token_id=token_id, side="BUY", amount=f"{amount:.2f}",
                                                       max_price=f"{max_price}", order_type="FOK")
        return f"signed OK ({type(signed).__name__})"

    async def market_buy_fok(self, token_id: str, amount: float, max_price: float) -> Dict[str, Any]:
        """Place leg 2. Returns {shares, spent, order_id, status}. Raises PolyExecError / PolyAmbiguousError."""
        if self.client is None:
            raise PolyExecError("Polymarket credentials not configured")
        if not await self.check_geoblock():
            raise PolyExecError(f"geoblocked ({self.geo_country}): Polymarket rejects orders from this region")
        from polymarket import errors as perr

        try:
            resp = await self.client.place_market_order(token_id=token_id, side="BUY", amount=f"{amount:.2f}",
                                                        max_price=f"{max_price}", order_type="FOK")
        except (perr.TransportError, perr.TimeoutError) as exc:
            raise PolyAmbiguousError(repr(exc)) from exc
        except perr.RequestRejectedError as exc:
            if exc.status >= 500:
                raise PolyAmbiguousError(f"HTTP {exc.status}: {exc}") from exc
            raise PolyExecError(f"rejected ({exc.code or exc.status}): {exc}") from exc
        except perr.PolymarketError as exc:
            raise PolyExecError(repr(exc)) from exc
        if not resp.ok:
            raise PolyExecError(f"rejected ({resp.code}): {resp.message}")
        if resp.status == "matched":
            return {"order_id": resp.order_id, "status": "matched", "shares": float(resp.taking_amount),
                    "spent": float(resp.making_amount)}
        # "delayed" (taker delay on some markets) or anything else: wait for the match, then cancel the rest
        return await self._settle_pending(resp.order_id, amount, max_price)

    async def _settle_pending(self, order_id: str, amount: float, max_price: float) -> Dict[str, Any]:
        matched = 0.0
        for _ in range(15):
            await asyncio.sleep(1.0)
            try:
                o = await self.client.get_order(order_id=order_id)
                matched = float(o.size_matched)
                if o.status.lower() not in ("live", "delayed"):
                    break
            except Exception as exc:  # order may have left the open-orders view once complete
                log.warning("[Polymarket] pending order %s lookup failed: %r", order_id, exc)
                raise PolyAmbiguousError(f"order {order_id} status unknown after delay") from exc
        try:
            await self.client.cancel_order(order_id=order_id)
        except Exception as exc:
            log.warning("[Polymarket] cancel of pending order %s failed: %r", order_id, exc)
        return {"order_id": order_id, "status": "delayed", "shares": matched,
                "spent": min(amount, matched * max_price)}

    def describe(self) -> Dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "balance": self.balance,
                "balance_at": self.balance_at, "geo_blocked": self.geo_blocked, "geo_country": self.geo_country}
