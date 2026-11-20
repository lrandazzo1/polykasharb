"""Polymarket: market metadata from the Gamma API, live orderbooks from the CLOB API."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from config import Settings
from http_utils import request_json
from models import Book, Level, PolyMarket

log = logging.getLogger("polymarket")

GAMMA_PAGE = 100  # server-side cap
BOOKS_BATCH = 100


def _json_list(val: Any) -> List[Any]:
    """Gamma encodes outcomes / outcomePrices / clobTokenIds as JSON strings."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip():
        try:
            out = json.loads(val)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def _float(val: Any) -> Optional[float]:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_market(m: Dict[str, Any], fee_override: str = "auto") -> Optional[PolyMarket]:
    if not m.get("enableOrderBook") or m.get("closed") or m.get("archived"):
        return None
    if m.get("acceptingOrders") is False or m.get("active") is False:
        return None
    outcomes = [str(o) for o in _json_list(m.get("outcomes"))]
    tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2 or not m.get("conditionId"):
        return None

    sched = m.get("feeSchedule") or {}
    fees_enabled = bool(m.get("feesEnabled"))
    rate = float(sched.get("rate") or 0.0) if fees_enabled else 0.0
    exponent = float(sched.get("exponent") or 1.0)
    if fee_override.lower() != "auto":
        rate, fees_enabled = float(fee_override), float(fee_override) > 0

    return PolyMarket(
        condition_id=m["conditionId"],
        question=(m.get("question") or "").strip(),
        slug=m.get("slug") or "",
        outcomes=outcomes,
        token_ids=tokens,
        end_date=m.get("endDate") or m.get("endDateIso") or "",
        description=(m.get("description") or "").strip(),
        best_bid=_float(m.get("bestBid")),
        best_ask=_float(m.get("bestAsk")),
        fees_enabled=fees_enabled,
        fee_rate=rate,
        fee_exponent=exponent,
        min_order_size=_float(m.get("orderMinSize")) or 0.0,
        liquidity_usd=_float(m.get("liquidityNum")) or _float(m.get("liquidity")) or 0.0,
        event_slug=((m.get("events") or [{}])[0] or {}).get("slug") or "",
        tick_size=_float(m.get("orderPriceMinTickSize")) or 0.01,
        neg_risk=bool(m.get("negRisk")),
    )


def parse_book(payload: Dict[str, Any]) -> Book:
    asks = []
    for a in payload.get("asks") or []:
        p, s = _float(a.get("price")), _float(a.get("size"))
        if p and s and 0 < p < 1:
            asks.append(Level(p, s))
    asks.sort(key=lambda l: l.price)  # CLOB returns asks worst-first; normalise
    return Book(asks)


class PolymarketClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, mock_dir: Optional[Path] = None):
        self.s = settings
        self.http = client
        self.mock_dir = mock_dir
        self._mock_books: Dict[str, Any] = {}
        if mock_dir:
            for b in json.loads((mock_dir / "polymarket_books.json").read_text("utf-8")):
                self._mock_books[b["asset_id"]] = b

    async def fetch_active_markets(self) -> Dict[str, PolyMarket]:
        """Gamma caps pages at 100 rows and offsets at ~2000, so walk /markets/keyset cursors."""
        raw_markets: List[Dict[str, Any]] = []
        if self.mock_dir:
            raw_markets = json.loads((self.mock_dir / "polymarket_markets.json").read_text("utf-8"))
        else:
            cursor: Optional[str] = None
            for _ in range(self.s.poly_max_pages):
                # liquidity_num_min: markets thinner than the alert threshold can never qualify
                params: Dict[str, Any] = {"active": "true", "closed": "false", "limit": GAMMA_PAGE,
                                          "liquidity_num_min": self.s.min_liquidity_usd}
                if cursor:
                    params["after_cursor"] = cursor
                data = await request_json(self.http, "GET", f"{self.s.gamma_base_url}/markets/keyset",
                                          params=params, retries=self.s.http_retries)
                rows = data.get("markets") if isinstance(data, dict) else data
                raw_markets.extend(rows or [])
                cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not rows or not cursor:
                    break
            else:
                log.warning("Polymarket pagination stopped at POLY_MAX_PAGES=%d", self.s.poly_max_pages)
        markets: Dict[str, PolyMarket] = {}
        for raw in raw_markets:
            pm = parse_market(raw, self.s.poly_fee_rate_override)
            if pm:
                markets[pm.condition_id] = pm
        log.info("Polymarket: %d active binary CLOB markets", len(markets))
        return markets

    async def fetch_markets_by_condition(self, condition_ids: Iterable[str]) -> Dict[str, PolyMarket]:
        """Fast path used at startup: load only the already-matched markets."""
        ids = list(dict.fromkeys(condition_ids))
        if self.mock_dir:
            allm = await self.fetch_active_markets()
            return {c: allm[c] for c in ids if c in allm}
        out: Dict[str, PolyMarket] = {}
        for i in range(0, len(ids), 50):
            params = [("condition_ids", c) for c in ids[i : i + 50]] + [("limit", "100")]
            rows = await request_json(self.http, "GET", f"{self.s.gamma_base_url}/markets",
                                      params=params, retries=self.s.http_retries)
            for raw in rows or []:
                pm = parse_market(raw, self.s.poly_fee_rate_override)
                if pm:
                    out[pm.condition_id] = pm
        return out

    async def is_resolved(self, condition_id: str) -> bool:
        """True once the market is closed and its outcome prices have resolved to 0/1."""
        if self.mock_dir:
            return False
        rows = await request_json(self.http, "GET", f"{self.s.gamma_base_url}/markets",
                                  params=[("condition_ids", condition_id), ("closed", "true")],
                                  retries=self.s.http_retries)
        for m in rows or []:
            prices = [_float(x) for x in _json_list(m.get("outcomePrices"))]
            if m.get("closed") and prices and all(p in (0.0, 1.0) for p in prices):
                return True
        return False

    async def fetch_books(self, token_ids: Iterable[str], fast: bool = False) -> Dict[str, Book]:
        """Batch-fetch ask books via POST /books, falling back to GET /book per token."""
        ids = list(dict.fromkeys(token_ids))
        out: Dict[str, Book] = {}
        if self.mock_dir:
            for t in ids:
                if t in self._mock_books:
                    out[t] = _jitter(parse_book(self._mock_books[t]))
            return out
        for i in range(0, len(ids), BOOKS_BATCH):
            chunk = ids[i : i + BOOKS_BATCH]
            try:
                data = await request_json(
                    self.http, "POST", f"{self.s.clob_base_url}/books",
                    json_body=[{"token_id": t} for t in chunk],
                    retries=2 if fast else self.s.http_retries, max_delay=4.0 if fast else 30.0,
                    channel="price" if fast else "bulk",
                )
                for b in data or []:
                    if b.get("asset_id"):
                        out[str(b["asset_id"])] = parse_book(b)
            except (httpx.HTTPError, ValueError) as exc:
                if fast:  # price loop: never fan out into hundreds of single requests every 3 s
                    log.warning("POST /books failed for %d tokens (%r); skipped this pass", len(chunk), exc)
                    continue
                log.warning("POST /books failed (%r); falling back to single requests", exc)
                for t in chunk:
                    try:
                        b = await request_json(
                            self.http, "GET", f"{self.s.clob_base_url}/book",
                            params={"token_id": t}, retries=self.s.http_retries,
                        )
                        out[t] = parse_book(b)
                    except (httpx.HTTPError, ValueError) as exc2:
                        log.warning("GET /book %s… failed: %r", t[:12], exc2)
        return out


def _jitter(book: Book) -> Book:
    for lvl in book.asks:
        lvl.size = max(1.0, round(lvl.size * random.uniform(0.85, 1.15), 2))
    return book
