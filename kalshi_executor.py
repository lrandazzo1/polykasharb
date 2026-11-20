"""Authenticated Kalshi trading: signed requests, cash balance, V2 limit orders, cancels.

Read-only scanning (kalshi_client.py) never needs this. Credentials come only from .env:
    KALSHI_API_KEY_ID          key ID shown on kalshi.com → Settings → API keys
    KALSHI_PRIVATE_KEY_PATH    path to the downloaded private key (.key / .pem), or
    KALSHI_PRIVATE_KEY         the PEM text itself

Signing (docs.kalshi.com/getting_started/api_keys): sign  timestamp_ms + METHOD + path, where path
starts at /trade-api/v2 and excludes the query string. Ed25519 keys sign the bytes directly; RSA keys
use RSA-PSS / SHA-256 / MGF1(SHA-256) / salt = digest length. Signature is base64 in the
KALSHI-ACCESS-SIGNATURE header, next to KALSHI-ACCESS-KEY and KALSHI-ACCESS-TIMESTAMP.

Orders use the V2 endpoint POST /portfolio/events/orders (the legacy /portfolio/orders write
endpoints return 410 Gone). V2 quotes everything from the YES side: side "bid" buys YES, "ask" sells
YES; buying NO at q is an "ask" at YES price 1−q. Order placement is NEVER retried automatically:
a retry after an ambiguous network error could double the position, so ambiguity is resolved by
looking the order up by its client_order_id instead.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from config import Settings

log = logging.getLogger("kalshi.exec")


class KalshiExecError(Exception):
    """Order was definitively rejected (no fill)."""


class KalshiAmbiguousError(Exception):
    """The request may or may not have reached the exchange (timeout / connection drop)."""


# ---------------------------------------------------------------------------
class KalshiSigner:
    def __init__(self, key_id: str, pem: bytes):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

        self.key_id = key_id
        key = serialization.load_pem_private_key(pem, password=None)
        if isinstance(key, ed25519.Ed25519PrivateKey):
            self.kind = "Ed25519"
        elif isinstance(key, rsa.RSAPrivateKey):
            self.kind = "RSA"
        else:
            raise ValueError(f"unsupported Kalshi key type {type(key).__name__} (need Ed25519 or RSA)")
        self._key = key

    def sign(self, message: str) -> str:
        data = message.encode("utf-8")
        if self.kind == "Ed25519":
            sig = self._key.sign(data)
        else:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            sig = self._key.sign(
                data,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str, ts_ms: Optional[int] = None) -> Dict[str, str]:
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        path = path.split("?", 1)[0]
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method.upper() + path),
        }


def _fp(val: Any) -> float:
    try:
        return float(val) if val not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def price_str(p: float) -> str:
    """Kalshi V2 dollar string. Whole cents are valid on every market's price grid."""
    return f"{min(0.99, max(0.01, round(p, 2))):.4f}"


def v2_order_body(ticker: str, outcome: str, action: str, count: int, outcome_price: float,
                  tif: str = "fill_or_kill", reduce_only: bool = False,
                  client_order_id: Optional[str] = None) -> Dict[str, Any]:
    """Translate (buy|sell, YES|NO, price of that outcome) into the YES-quoted V2 order body."""
    outcome, action = outcome.upper(), action.lower()
    yes_price = outcome_price if outcome == "YES" else 1.0 - outcome_price
    if (outcome, action) in (("YES", "buy"), ("NO", "sell")):
        side = "bid"
    elif (outcome, action) in (("YES", "sell"), ("NO", "buy")):
        side = "ask"
    else:
        raise ValueError(f"bad outcome/action {outcome}/{action}")
    body: Dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "count": str(int(count)),
        "price": price_str(yes_price),
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id or f"arb-{uuid.uuid4()}",
    }
    if reduce_only:
        body["reduce_only"] = True  # Kalshi only accepts reduce_only with immediate_or_cancel
    return body


# ---------------------------------------------------------------------------
class KalshiExecutor:
    platform = "Kalshi"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.s = s
        self.http = http
        self.base = s.kalshi_base_url.rstrip("/")
        self.base_path = urlparse(self.base).path.rstrip("/")  # "/trade-api/v2"
        self.signer: Optional[KalshiSigner] = None
        self.status = "unconfigured"  # unconfigured | ready | error
        self.detail = "Read-only: set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env"
        self.balance: Optional[float] = None
        self.balance_at = 0.0

    @property
    def configured(self) -> bool:
        return self.signer is not None

    @property
    def can_trade(self) -> bool:
        return self.status == "ready"

    async def init(self) -> None:
        if not self.s.kalshi_key_id:
            return
        try:
            if self.s.kalshi_private_key_pem:
                pem = self.s.kalshi_private_key_pem.replace("\\n", "\n").encode("utf-8")
            elif self.s.kalshi_private_key_path:
                pem = Path(self.s.kalshi_private_key_path).expanduser().read_bytes()
            else:
                raise ValueError("KALSHI_API_KEY_ID is set but no KALSHI_PRIVATE_KEY_PATH / KALSHI_PRIVATE_KEY")
            self.signer = KalshiSigner(self.s.kalshi_key_id, pem)
        except Exception as exc:
            self.status, self.detail = "error", f"Could not load Kalshi key: {exc}"
            log.error("[Kalshi] %s", self.detail)
            return
        await self.refresh_balance()

    # ------------------------------------------------------------- transport
    async def _send(self, method: str, endpoint: str, *, params: Optional[dict] = None,
                    body: Optional[dict] = None, retries: int = 0) -> Any:
        if not self.signer:
            raise KalshiExecError("Kalshi credentials not configured")
        attempt = 0
        while True:
            headers = self.signer.headers(method, self.base_path + endpoint)  # fresh timestamp each try
            try:
                resp = await self.http.request(method, self.base + endpoint, params=params, json=body,
                                               headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise KalshiAmbiguousError(f"{method} {endpoint}: {exc!r}") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.75 * attempt)
                continue
            if resp.status_code >= 500 and method != "GET":
                raise KalshiAmbiguousError(f"{method} {endpoint}: HTTP {resp.status_code} {resp.text[:200]}")
            if resp.status_code >= 400:
                raise KalshiExecError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}

    # ------------------------------------------------------------- account
    async def refresh_balance(self) -> Optional[float]:
        if not self.signer:
            return None
        try:
            data = await self._send("GET", "/portfolio/balance", retries=2)
            if data.get("balance_dollars") not in (None, ""):
                self.balance = float(data["balance_dollars"])
            else:
                self.balance = float(data.get("balance", 0)) / 100.0
            self.balance_at = time.time()
            self.status, self.detail = "ready", f"Key {self.s.kalshi_key_id[:8]}… ({self.signer.kind})"
        except (KalshiExecError, KalshiAmbiguousError, ValueError) as exc:
            self.status, self.detail = "error", f"Balance check failed: {exc}"[:300]
            log.error("[Kalshi] balance check failed: %s", exc)
        return self.balance

    # ------------------------------------------------------------- orders
    async def place(self, body: Dict[str, Any], outcome: str) -> Dict[str, Any]:
        """POST one V2 order, no automatic retry. `outcome` (YES/NO) is the side whose price avg_price
        reports. Returns {order_id, fill_count, avg_price, fees, ...}."""
        try:
            data = await self._send("POST", "/portfolio/events/orders", body=body, retries=0)
        except KalshiAmbiguousError:
            found = await self.find_order(body["ticker"], body["client_order_id"])
            if found is None:
                raise
            return found
        return self._normalise(data, body, outcome)

    async def find_order(self, ticker: str, client_order_id: str, attempts: int = 4) -> Optional[Dict[str, Any]]:
        """Resolve an ambiguous submit by looking the order up (reads are eventually consistent)."""
        for i in range(attempts):
            try:
                data = await self._send("GET", "/portfolio/orders", params={"ticker": ticker, "limit": 100},
                                        retries=2)
                for o in data.get("orders") or []:
                    if o.get("client_order_id") == client_order_id:
                        fill = _fp(o.get("fill_count_fp") or o.get("fill_count"))
                        cost = _fp(o.get("taker_fill_cost_dollars")) + _fp(o.get("maker_fill_cost_dollars"))
                        fees = _fp(o.get("taker_fees_dollars")) + _fp(o.get("maker_fees_dollars"))
                        return {"order_id": o.get("order_id", ""), "client_order_id": client_order_id,
                                "fill_count": fill, "remaining_count": _fp(o.get("remaining_count_fp")),
                                "avg_price": cost / fill if fill else None, "fees": fees,
                                "status": o.get("status", ""), "reconciled": True}
            except (KalshiExecError, KalshiAmbiguousError) as exc:
                log.warning("[Kalshi] order lookup %s failed (attempt %d): %s", client_order_id, i + 1, exc)
            await asyncio.sleep(1.0 + i)
        return None

    async def cancel(self, order_id: str, ticker: str) -> None:
        await self._send("DELETE", f"/portfolio/events/orders/{order_id}", params={"market_ticker": ticker},
                         retries=2)

    @staticmethod
    def _normalise(data: Dict[str, Any], body: Dict[str, Any], outcome: str) -> Dict[str, Any]:
        fill = _fp(data.get("fill_count"))
        avg_yes = _fp(data.get("average_fill_price")) if fill else None
        # V2 reports YES-side prices; convert back to the price of the outcome we traded
        avg = None if avg_yes is None else (avg_yes if outcome.upper() == "YES" else 1.0 - avg_yes)
        return {"order_id": data.get("order_id", ""), "client_order_id": data.get("client_order_id")
                or body["client_order_id"], "fill_count": fill, "remaining_count": _fp(data.get("remaining_count")),
                "avg_price": avg, "fees": _fp(data.get("average_fee_paid")) * fill, "status": "", "reconciled": False}

    def describe(self) -> Dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "balance": self.balance,
                "balance_at": self.balance_at}
