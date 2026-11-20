"""--mock only: simulated Kalshi / Polymarket exchanges for exercising every execution path offline.

They subclass the real executors and replace only the network calls, so ExecutionManager runs the
exact live code: re-check, leg 1, leg 2, flatten, halt. Orders fill against the in-memory mock books.

MOCK_KALSHI_BALANCE / MOCK_POLY_BALANCE set the starting cash.
MOCK_EXEC_FAIL=leg2_reject   Polymarket leg is rejected, so the Kalshi leg gets flattened
MOCK_EXEC_FAIL=unhedged      Polymarket leg rejected and the flatten finds no bids (UNHEDGED + halt)
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, Optional

import httpx

from arb_engine import kalshi_taker_fee, poly_taker_fee
from config import Settings
from kalshi_executor import KalshiExecutor
from models import KalshiMarket, PolyMarket
from polymarket_executor import PolyExecError, PolymarketExecutor


class MockKalshiExecutor(KalshiExecutor):
    def __init__(self, s: Settings, http: httpx.AsyncClient, market: Callable[[str], Optional[KalshiMarket]]):
        super().__init__(s, http)
        self.market = market
        self.balance = s.mock_kalshi_balance
        self.status, self.detail = "ready", "Mock exchange (simulated balance)"

    async def init(self) -> None:
        self.balance_at = time.time()

    @property
    def configured(self) -> bool:
        return True

    async def refresh_balance(self) -> Optional[float]:
        self.balance_at = time.time()
        return self.balance

    async def place(self, body: Dict[str, Any], outcome: str) -> Dict[str, Any]:
        km = self.market(body["ticker"])
        count, yes_px = int(body["count"]), float(body["price"])
        px = yes_px if outcome.upper() == "YES" else 1.0 - yes_px
        oid = f"mock-{body['client_order_id'][-8:]}"
        empty = {"order_id": oid, "client_order_id": body["client_order_id"], "fill_count": 0.0,
                 "remaining_count": float(count), "avg_price": None, "fees": 0.0, "status": "canceled",
                 "reconciled": False}
        if km is None:
            return empty
        if body.get("reduce_only"):  # flatten: sell back at the best bid, not below the floor
            if self.s.mock_exec_fail == "unhedged":
                return empty
            opp = km.no_book if outcome.upper() == "YES" else km.yes_book  # bids for X = 1 - asks for not-X
            best_bid = 1.0 - opp.best.price if opp.best else 0.0
            if best_bid < px - 1e-9:
                return empty
            fee = kalshi_taker_fee(count, best_bid, self.s.kalshi_fee_rate)
            self.balance += count * best_bid - fee
            return {**empty, "fill_count": float(count), "remaining_count": 0.0, "avg_price": best_bid,
                    "fees": fee, "status": "executed"}
        book = km.yes_book if outcome.upper() == "YES" else km.no_book
        left, cost, fees = count, 0.0, 0.0
        for lvl in book.asks:
            if lvl.price > px + 1e-9 or left <= 0:
                break
            take = min(left, int(lvl.size))
            cost += take * lvl.price
            fees += kalshi_taker_fee(take, lvl.price, self.s.kalshi_fee_rate)
            left -= take
        if left > 0 or cost + fees > (self.balance or 0) + 1e-9:  # fill-or-kill, and no overdraft
            return empty
        self.balance -= cost + fees
        return {**empty, "fill_count": float(count), "remaining_count": 0.0, "avg_price": cost / count,
                "fees": fees, "status": "executed"}


class MockPolymarketExecutor(PolymarketExecutor):
    def __init__(self, s: Settings, http: httpx.AsyncClient, market_by_token: Callable[[str], Optional[PolyMarket]]):
        super().__init__(s, http)
        self.market_by_token = market_by_token
        self.balance = s.mock_poly_balance
        self.status, self.detail = "ready", "Mock exchange (simulated balance)"
        self.geo_blocked, self.geo_country = False, "mock"

    async def init(self) -> None:
        self.balance_at = time.time()

    @property
    def configured(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def check_geoblock(self, force: bool = False) -> bool:
        return True

    async def refresh_balance(self) -> Optional[float]:
        self.balance_at = time.time()
        return self.balance

    async def sign_market_buy(self, token_id: str, amount: float, max_price: float) -> str:
        return "signed OK (mock)"

    async def market_buy_fok(self, token_id: str, amount: float, max_price: float) -> Dict[str, Any]:
        if self.s.mock_exec_fail in ("leg2_reject", "unhedged"):
            raise PolyExecError("rejected (fok_not_filled): order couldn't be fully filled. "
                                "FOK orders are fully filled or killed. [MOCK_EXEC_FAIL]")
        pm = self.market_by_token(token_id)
        if pm is None:
            raise PolyExecError("unknown token")
        book = pm.books[pm.token_ids.index(token_id)]
        left, shares, fees = amount, 0.0, 0.0
        for lvl in book.asks:
            if lvl.price > max_price + 1e-9 or left <= 1e-9:
                break
            usd = min(left, lvl.price * lvl.size)
            got = usd / lvl.price
            shares += got
            fees += poly_taker_fee(got, lvl.price, pm.fee_rate, pm.fee_exponent)
            left -= usd
        if left > 1e-6:
            raise PolyExecError("rejected (fok_not_filled): order couldn't be fully filled. FOK orders are fully filled or killed.")
        if amount + fees > (self.balance or 0) + 1e-9:
            raise PolyExecError("rejected (not_enough_balance): not enough balance / allowance")
        self.balance -= amount + fees
        return {"order_id": f"mock-{int(time.time() * 1000)}", "status": "matched",
                "shares": math.floor(shares * 100) / 100, "spent": amount}
