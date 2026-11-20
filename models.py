"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Level:
    price: float  # USD per contract/share (0-1)
    size: float  # contracts/shares available at this price


@dataclass
class Book:
    """Ask side of one outcome: what it costs to BUY that outcome. Sorted best (lowest) first."""

    asks: List[Level] = field(default_factory=list)

    @property
    def best(self) -> Optional[Level]:
        return self.asks[0] if self.asks else None

    @property
    def top_liquidity_usd(self) -> float:
        lvl = self.best
        return lvl.price * lvl.size if lvl else 0.0

    @property
    def depth_contracts(self) -> float:
        return sum(l.size for l in self.asks)


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    close_time: str
    rules: str
    yes_ask: Optional[float]
    no_ask: Optional[float]
    yes_bid: Optional[float]
    no_bid: Optional[float]
    yes_ask_size: float = 0.0  # contracts at best YES ask
    no_ask_size: float = 0.0  # contracts at best NO ask (= size at best YES bid)
    expected_expiration: str = ""  # when the underlying event is expected to settle
    yes_book: Book = field(default_factory=Book)  # asks for YES
    no_book: Book = field(default_factory=Book)  # asks for NO
    category: str = ""  # Kalshi series category (Sports, Politics, Entertainment, ...)

    @property
    def series(self) -> str:
        return (self.event_ticker or self.ticker).split("-")[0]

    @property
    def display_title(self) -> str:
        if self.subtitle and self.subtitle.lower() not in self.title.lower():
            return f"{self.title} — {self.subtitle}"
        return self.title

    @property
    def has_quotes(self) -> bool:
        return bool(self.yes_ask) or bool(self.no_ask)

    @property
    def url(self) -> str:
        series = (self.event_ticker or self.ticker).split("-")[0].lower()
        return f"https://kalshi.com/markets/{series}/x/{(self.event_ticker or self.ticker).lower()}"

    @property
    def top_liquidity_usd(self) -> float:
        """Best of the two sides' top-of-book USD, from the market listing (no orderbook call)."""
        return max((self.yes_ask or 0) * self.yes_ask_size, (self.no_ask or 0) * self.no_ask_size)


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    slug: str
    outcomes: List[str]  # exactly 2
    token_ids: List[str]  # parallel to outcomes
    end_date: str
    description: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    fees_enabled: bool
    fee_rate: float
    fee_exponent: float
    min_order_size: float
    liquidity_usd: float = 0.0
    event_slug: str = ""
    tick_size: float = 0.01
    neg_risk: bool = False
    books: List[Book] = field(default_factory=lambda: [Book(), Book()])  # asks per outcome

    @property
    def url(self) -> str:
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}"
        return f"https://polymarket.com/market/{self.slug}"


@dataclass
class MatchPair:
    """Kalshi YES is economically identical to Polymarket outcome `poly_yes_index`."""

    kalshi_ticker: str
    poly_condition_id: str
    poly_yes_index: int
    confidence: float
    reason: str = ""


@dataclass
class Leg:
    platform: str  # "Kalshi" | "Polymarket"
    market_id: str  # ticker or condition id
    side: str  # "YES"/"NO" for Kalshi, outcome name for Polymarket
    side_label: str  # human-readable side
    contracts: int
    avg_price: float
    limit_price: float  # worst price level touched (use as limit)
    cost: float  # contracts * prices, excl. fees
    fee: float
    top_liquidity_usd: float
    token_id: str = ""  # Polymarket outcome token for this leg

    @property
    def stake(self) -> float:
        return self.cost + self.fee


@dataclass
class Opportunity:
    pair: MatchPair
    event: str
    direction: str  # "A" (Kalshi YES + Poly NO-equivalent) | "B" (Kalshi NO + Poly YES-equivalent)
    top_effective_cost: float  # per $1 payout at top of book, incl. fees
    kalshi: Optional[Leg] = None
    poly: Optional[Leg] = None
    contracts: int = 0
    total_cost: float = 0.0
    payout: float = 0.0
    profit: float = 0.0
    roi_pct: float = 0.0
    effective_cost: float = 0.0  # total_cost / payout for the sized trade
    is_arb: bool = False
    reject_reason: str = ""
    sizing_note: str = ""  # what stopped N growing: "Kalshi budget" / "Polymarket budget" / "order-book depth"
    # depth / VWAP diagnostics for the strict ("bulletproof") check
    n_max: int = 0  # contracts the budgets + visible depth allow, before the ROI floor
    budget_k: float = 0.0
    budget_p: float = 0.0
    max_stake_k: float = 0.0  # all-in USD each leg would take at n_max
    max_stake_p: float = 0.0
    depth_limited: bool = False  # the book ran out before the budget did
    roi_limited: bool = False  # sized below n_max because deeper levels drop ROI under the floor
    roi_at_max: float = 0.0  # ROI if the whole budget were filled (VWAP of the full walk)
    days_to_settle: Optional[int] = None  # until the LATER settlement date, min 1; None if a date is unknown
    annualized_roi: Optional[float] = None  # roi_pct × 365 / days_to_settle for the sized trade
    required_roi: float = 0.0  # net ROI this opportunity must clear: max(MIN_NET_ROI, MIN_ANNUALIZED × days/365)
    # strict validation (filled in by strict.py)
    bulletproof: bool = False
    strict_reasons: List[str] = field(default_factory=list)
    strict_checks: Dict[str, Any] = field(default_factory=dict)
    # top-of-book snapshot for this direction (shown in the near-arb table)
    kalshi_side: str = ""
    poly_outcome: str = ""
    kalshi_top_price: float = 0.0
    poly_top_price: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.pair.kalshi_ticker}|{self.pair.poly_condition_id}|{self.direction}"
