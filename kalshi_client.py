"""Kalshi public v2 REST API: open markets + orderbooks (no auth needed for reads)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from config import Settings
from http_utils import RateLimiter, request_json
from models import Book, KalshiMarket, Level

log = logging.getLogger("kalshi")


# ---------------------------------------------------------------------------
# parsing (shared by live + mock so the mock exercises the real code path)
# ---------------------------------------------------------------------------
def _price(m: Dict[str, Any], key: str) -> Optional[float]:
    """Kalshi now returns `<key>_dollars` strings; older payloads used integer cents."""
    raw = m.get(f"{key}_dollars")
    if raw not in (None, ""):
        try:
            val = float(raw)
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None
    raw = m.get(key)
    if isinstance(raw, (int, float)) and raw > 0:
        return raw / 100.0
    return None


def parse_market(m: Dict[str, Any]) -> Optional[KalshiMarket]:
    if m.get("market_type", "binary") != "binary":
        return None
    if m.get("status") not in (None, "open", "active"):
        return None
    return KalshiMarket(
        ticker=m["ticker"],
        event_ticker=m.get("event_ticker", ""),
        title=(m.get("title") or "").strip(),
        subtitle=(m.get("yes_sub_title") or m.get("subtitle") or "").strip(),
        close_time=m.get("close_time") or m.get("expiration_time") or "",
        rules="\n\n".join(x for x in ((m.get("rules_primary") or "").strip(),
                                       (m.get("rules_secondary") or "").strip()) if x),
        yes_ask=_price(m, "yes_ask"),
        no_ask=_price(m, "no_ask"),
        yes_bid=_price(m, "yes_bid"),
        no_bid=_price(m, "no_bid"),
        yes_ask_size=_size(m, "yes_ask_size"),
        no_ask_size=_size(m, "no_ask_size") or _size(m, "yes_bid_size"),
        expected_expiration=m.get("expected_expiration_time") or m.get("close_time") or "",
    )


def _size(m: Dict[str, Any], key: str) -> float:
    for k in (f"{key}_fp", key):
        try:
            if m.get(k) not in (None, ""):
                return float(m[k])
        except (TypeError, ValueError):
            pass
    return 0.0


def _levels(raw: Optional[Iterable], cents: bool) -> List[Tuple[float, float]]:
    out = []
    for item in raw or []:
        try:
            price, qty = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if cents:
            price /= 100.0
        if qty > 0 and 0 < price < 1:
            out.append((price, qty))
    return out


def parse_orderbook(payload: Dict[str, Any]) -> Tuple[Book, Book]:
    """Kalshi books list BIDS only. A NO bid at p is a YES ask at 1-p (and vice versa).

    Returns (yes_asks, no_asks), each sorted cheapest first.
    """
    yes_bids: List[Tuple[float, float]] = []
    no_bids: List[Tuple[float, float]] = []
    fp = payload.get("orderbook_fp")
    ob = payload.get("orderbook") or {}
    if fp:
        yes_bids, no_bids = _levels(fp.get("yes_dollars"), False), _levels(fp.get("no_dollars"), False)
    elif ob.get("yes_dollars") is not None or ob.get("no_dollars") is not None:
        yes_bids, no_bids = _levels(ob.get("yes_dollars"), False), _levels(ob.get("no_dollars"), False)
    else:
        yes_bids, no_bids = _levels(ob.get("yes"), True), _levels(ob.get("no"), True)

    yes_asks = sorted((Level(round(1 - p, 4), q) for p, q in no_bids), key=lambda l: l.price)
    no_asks = sorted((Level(round(1 - p, 4), q) for p, q in yes_bids), key=lambda l: l.price)
    return Book(yes_asks), Book(no_asks)


# ---------------------------------------------------------------------------
class KalshiClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, mock_dir: Optional[Path] = None):
        self.s = settings
        self.http = client
        self.mock_dir = mock_dir
        self.limiter = RateLimiter(settings.kalshi_rps)  # catalog paging
        self.price_limiter = RateLimiter(settings.kalshi_price_rps)  # fast price loop, never starved by paging
        self._batch_ok = True
        self.series_categories: Dict[str, str] = {}
        self._series_at = 0.0
        self._mock_books: Dict[str, Any] = {}
        if mock_dir:
            self._mock_books = json.loads((mock_dir / "kalshi_orderbooks.json").read_text("utf-8"))

    async def fetch_open_markets(self) -> Dict[str, KalshiMarket]:
        markets: Dict[str, KalshiMarket] = {}
        cursor: Optional[str] = None
        for page in range(self.s.kalshi_max_pages):
            if self.mock_dir:
                data = json.loads((self.mock_dir / "kalshi_markets.json").read_text("utf-8"))
            else:
                params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
                if cursor:
                    params["cursor"] = cursor
                data = await request_json(
                    self.http, "GET", f"{self.s.kalshi_base_url}/markets",
                    params=params, retries=self.s.http_retries, limiter=self.limiter,
                )
            for raw in data.get("markets") or []:
                mk = parse_market(raw)
                if mk:
                    markets[mk.ticker] = mk
            cursor = data.get("cursor")
            if not cursor:
                break
        else:
            log.warning("Kalshi pagination stopped at KALSHI_MAX_PAGES=%d", self.s.kalshi_max_pages)
        log.info("Kalshi: %d open binary markets", len(markets))
        return markets

    async def fetch_markets_by_ticker(self, tickers: Iterable[str]) -> Dict[str, KalshiMarket]:
        """Fast path used at startup: load only the already-matched markets."""
        tickers = list(dict.fromkeys(tickers))
        out: Dict[str, KalshiMarket] = {}
        if self.mock_dir:
            allm = await self.fetch_open_markets()
            return {t: allm[t] for t in tickers if t in allm}
        for i in range(0, len(tickers), 100):
            data = await request_json(
                self.http, "GET", f"{self.s.kalshi_base_url}/markets",
                params={"tickers": ",".join(tickers[i : i + 100]), "limit": 1000},
                retries=self.s.http_retries, limiter=self.limiter,
            )
            for raw in data.get("markets") or []:
                mk = parse_market(raw)
                if mk:
                    out[mk.ticker] = mk
        return out

    async def load_series_categories(self, max_age: float = 6 * 3600) -> Dict[str, str]:
        """{series_ticker: category} for every Kalshi series (one call, ~17k rows; cached for 6 h)."""
        import time as _time

        if self.series_categories and _time.time() - self._series_at < max_age:
            return self.series_categories
        try:
            if self.mock_dir:
                rows = json.loads((self.mock_dir / "kalshi_series.json").read_text("utf-8"))
            else:
                data = await request_json(self.http, "GET", f"{self.s.kalshi_base_url}/series",
                                          retries=self.s.http_retries, limiter=self.limiter)
                rows = data.get("series") or []
            self.series_categories = {r["ticker"]: (r.get("category") or "Other") for r in rows if r.get("ticker")}
            self._series_at = _time.time()
            log.info("Kalshi: %d series across %d categories", len(self.series_categories),
                     len(set(self.series_categories.values())))
        except (httpx.HTTPError, ValueError, OSError, KeyError) as exc:
            log.warning("Kalshi series/category list failed (%r); categories shown as 'Other'", exc)
        return self.series_categories

    def tag_categories(self, markets: Dict[str, KalshiMarket]) -> None:
        for m in markets.values():
            m.category = self.series_categories.get(m.series, "Other")

    async def market_status(self, ticker: str) -> str:
        """Public market status (open / closed / settled / finalized …), used to release cap room."""
        if self.mock_dir:
            return "open"
        data = await request_json(self.http, "GET", f"{self.s.kalshi_base_url}/markets/{ticker}",
                                  retries=self.s.http_retries, limiter=self.limiter)
        return str((data.get("market") or {}).get("status") or "")

    async def fetch_orderbook(self, ticker: str) -> Optional[Tuple[Book, Book]]:
        try:
            if self.mock_dir:
                payload = self._mock_books.get(ticker)
                if payload is None:
                    return None
                return _jitter(parse_orderbook(payload))
            payload = await request_json(
                self.http, "GET", f"{self.s.kalshi_base_url}/markets/{ticker}/orderbook",
                params={"depth": 10}, retries=self.s.http_retries, limiter=self.limiter,
            )
            return parse_orderbook(payload)
        except httpx.HTTPStatusError as exc:
            log.warning("Kalshi orderbook %s -> HTTP %s", ticker, exc.response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Kalshi orderbook %s failed: %r", ticker, exc)
        return None

    async def fetch_orderbooks_batch(self, tickers: Iterable[str]) -> Dict[str, Tuple[Book, Book]]:
        """Price loop: GET /markets/orderbooks, up to 100 tickers per call (repeated `tickers=` params).

        Fails soft: a chunk that errors is skipped (its pairs drop out of this pass only). Falls back to
        per-ticker requests if the batch endpoint is unavailable.
        """
        tickers = list(dict.fromkeys(tickers))
        if self.mock_dir:
            return {t: _jitter(parse_orderbook(self._mock_books[t])) for t in tickers if t in self._mock_books}
        if not self._batch_ok:
            return await self.fetch_orderbooks(tickers)
        out: Dict[str, Tuple[Book, Book]] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i : i + 100]
            try:
                data = await request_json(
                    self.http, "GET", f"{self.s.kalshi_base_url}/markets/orderbooks",
                    params=[("tickers", t) for t in chunk], retries=2, max_delay=4.0, limiter=self.price_limiter,
                    channel="price",
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 405):
                    log.warning("Kalshi batch orderbook endpoint unavailable (HTTP %s); using per-market requests",
                                exc.response.status_code)
                    self._batch_ok = False
                    return await self.fetch_orderbooks(tickers)
                log.warning("Kalshi batch orderbooks HTTP %s for %d tickers; skipped this pass",
                            exc.response.status_code, len(chunk))
                continue
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Kalshi batch orderbooks failed for %d tickers (%r); skipped this pass", len(chunk), exc)
                continue
            for ob in data.get("orderbooks") or []:
                if ob.get("ticker"):
                    out[ob["ticker"]] = parse_orderbook({"orderbook_fp": ob.get("orderbook_fp") or {}})
        return out

    async def fetch_orderbooks(self, tickers: Iterable[str]) -> Dict[str, Tuple[Book, Book]]:
        tickers = list(dict.fromkeys(tickers))
        results = await asyncio.gather(*(self.fetch_orderbook(t) for t in tickers))
        return {t: r for t, r in zip(tickers, results) if r is not None}


def _jitter(books: Tuple[Book, Book]) -> Tuple[Book, Book]:
    """Mock only: wiggle sizes a little so the dashboard visibly updates."""
    for book in books:
        for lvl in book.asks:
            lvl.size = max(1.0, round(lvl.size * random.uniform(0.85, 1.15)))
    return books
