"""Cross-platform arbitrage math: fees, book walking, equal-payout sizing.

A pair of binary contracts that settle identically lets us buy complementary outcomes:
    Direction A: Kalshi YES + Polymarket outcome equivalent to Kalshi NO
    Direction B: Kalshi NO  + Polymarket outcome equivalent to Kalshi YES
Each leg is sized against its own platform budget (per-platform cap, open exposure and live cash
balance, see execution.py). Exactly one leg pays $1 per contract. Buying the SAME number of contracts N on both legs makes the
payout N dollars in every world, so net profit = N - (all-in cost of both legs) is guaranteed
(ignoring resolution-mismatch and execution risk, see README).
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

from config import Settings
from models import Book, KalshiMarket, Leg, MatchPair, Opportunity, PolyMarket
from risk import _ts

FeeFn = Callable[[float, float], float]  # (contracts, price) -> USD fee for that fill
Budgets = Tuple[float, float]  # (Kalshi leg USD, Polymarket leg USD), fees included


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100.0


def kalshi_taker_fee(contracts: float, price: float, rate: float = 0.07) -> float:
    """Kalshi: fee = ceil_to_cent(rate × C × P × (1 − P)), charged per fill."""
    if contracts <= 0:
        return 0.0
    return ceil_cent(rate * contracts * price * (1.0 - price))


def poly_taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket: fee = C × feeRate × (p × (1 − p))^exponent, in USDC, rounded to 5 dp (min 0.00001)."""
    if shares <= 0 or rate <= 0:
        return 0.0
    fee = round(shares * rate * (price * (1.0 - price)) ** exponent, 5)
    return fee if fee >= 0.00001 else 0.0


# ---------------------------------------------------------------------------
# Book walking
# ---------------------------------------------------------------------------
def fill_cost(book: Book, contracts: float, fee_fn: FeeFn) -> Optional[Tuple[float, float, float, float]]:
    """Cost to buy `contracts` by sweeping asks. Fees computed per price level (each level is a fill).

    Returns (cost_ex_fees, fees, avg_price, worst_price) or None if the book is too thin.
    """
    remaining = contracts
    cost = fees = 0.0
    worst = 0.0
    for lvl in book.asks:
        if remaining <= 1e-9:
            break
        take = min(remaining, lvl.size)
        cost += take * lvl.price
        fees += fee_fn(take, lvl.price)
        worst = lvl.price
        remaining -= take
    if remaining > 1e-9:
        return None
    return cost, fees, cost / contracts, worst


def lockup_days(km: KalshiMarket, pm: PolyMarket, now: Optional[float] = None) -> Optional[int]:
    """Whole days until the LATER of the two settlement dates (capital is tied up until both pay), min 1."""
    import time as _time

    kd, pd = _ts(km.expected_expiration or km.close_time), _ts(pm.end_date)
    if kd is None or pd is None:
        return None
    return max(1, math.ceil((max(kd, pd) - (now or _time.time())) / 86400))


# ---------------------------------------------------------------------------
# Deterministic hurdle math (orderbook prices, fees and lock-up only: no probability guessing)
# ---------------------------------------------------------------------------
def break_even_prob(cost_per_dollar: Optional[float]) -> Optional[float]:
    """Win rate (%) at which the position breaks even: all-in VWAP cost + fees per $1 of payout × 100.

    For a pair that settles identically this is the probability that the pair pays $1 at all, so a
    pure arb sits below 100% and its spread under 100% is the guaranteed edge. For a date-offset
    spread it is the share of worlds in which you must collect $1 to get your money back.
    """
    if cost_per_dollar is None or not math.isfinite(cost_per_dollar) or cost_per_dollar <= 0:
        return None
    return round(cost_per_dollar * 100.0, 3)


def mmf_hurdle_prob(cost_per_dollar: Optional[float], days: Optional[int], mmf_rate_pct: float = 4.5) -> Optional[float]:
    """Win rate (%) needed to beat a risk-free money-market fund over the lock-up:
    cost_per_dollar × (1 + rate × days / 365) × 100 (simple interest, like the annualized ROI)."""
    be = break_even_prob(cost_per_dollar)
    if be is None or days is None:
        return None
    return round(be * (1.0 + mmf_rate_pct / 100.0 * days / 365.0), 3)


def favorite_offset(k_price: float, p_price: float, k_settle: Optional[float], p_settle: Optional[float],
                    min_gap_days: float = 1.0) -> Optional[Dict[str, object]]:
    """Leg ordering when the two settlement dates differ by more than `min_gap_days`.

    The heavy favorite is the higher-priced leg. If it settles FIRST, the likely payout arrives early and
    only the cheap leg trails (lower tail risk). If the cheap leg settles first, the expensive leg stays
    exposed through the whole gap window (elevated tail risk). Purely from prices and dates.
    """
    if k_settle is None or p_settle is None or not k_price or not p_price:
        return None
    gap = abs(k_settle - p_settle) / 86400.0
    if gap <= min_gap_days + 1e-9:
        return None
    if abs(k_price - p_price) < 1e-9:
        return {"status": "even", "gap_days": round(gap, 1), "favorite": "", "favorite_price": k_price,
                "label": f"No favorite: both legs at {k_price * 100:.0f}¢ ({gap:.0f}d offset)"}
    fav, fav_price, cheap_price = (("Kalshi", k_price, p_price) if k_price > p_price else ("Polymarket", p_price, k_price))
    fav_ts, cheap_ts = (k_settle, p_settle) if fav == "Kalshi" else (p_settle, k_settle)
    early = fav_ts < cheap_ts
    d = round(gap)
    return {
        "status": "early" if early else "late",
        "gap_days": round(gap, 1),
        "favorite": fav,
        "favorite_price": round(fav_price, 4),
        "cheap_price": round(cheap_price, 4),
        "label": (f"Favorite settles early (+{d}d): {fav} {fav_price * 100:.0f}¢ pays first" if early else
                  f"Warning: Favorite settles late (-{d}d): {fav} {fav_price * 100:.0f}¢ exposed after the "
                  f"{cheap_price * 100:.0f}¢ leg settles"),
    }


# ---------------------------------------------------------------------------
class ArbEngine:
    def __init__(self, settings: Settings):
        self.s = settings

    def required_roi(self, days: Optional[int]) -> float:
        """Net ROI an opportunity must clear: the raw floor, or the annualized target over its lock-up."""
        if days is None:
            return self.s.min_roi_pct
        return max(self.s.min_roi_pct, self.s.min_annualized_roi_pct * days / 365.0)

    def roi_shortfall(self, roi: float, days: Optional[int]) -> str:
        """'' if `roi` qualifies over `days`, else the WHY-NOT-AN-ARB message."""
        if roi < self.s.min_roi_pct - 1e-9:
            return f"Net ROI {roi:.2f}% < {self.s.min_roi_pct:g}%"
        if days is None:
            return "Settlement date unknown: annualized ROI can't be computed"
        ann = roi * 365.0 / days
        if ann < self.s.min_annualized_roi_pct - 1e-9:
            return f"Annualized ROI {ann:.1f}% < {self.s.min_annualized_roi_pct:g}% (net {roi:.2f}% over {days}d)"
        return ""

    def _kalshi_fee(self, c: float, p: float) -> float:
        return kalshi_taker_fee(c, p, self.s.kalshi_fee_rate)

    def evaluate(self, pair: MatchPair, km: KalshiMarket, pm: PolyMarket,
                 budgets: Optional[Budgets] = None) -> List[Opportunity]:
        """`budgets` = (max USD for the Kalshi leg, max USD for the Polymarket leg), fees included."""
        if budgets is None:
            budgets = (self.s.max_per_platform, self.s.max_per_platform)
        out = []
        for direction, k_side, k_book, p_idx in (
            ("A", "YES", km.yes_book, 1 - pair.poly_yes_index),
            ("B", "NO", km.no_book, pair.poly_yes_index),
        ):
            out.append(self._evaluate_direction(pair, km, pm, direction, k_side, k_book, p_idx, budgets))
        return out

    def _evaluate_direction(
        self, pair: MatchPair, km: KalshiMarket, pm: PolyMarket,
        direction: str, k_side: str, k_book: Book, p_idx: int, budgets: Budgets,
    ) -> Opportunity:
        p_book = pm.books[p_idx]
        opp = Opportunity(pair=pair, event=km.display_title, direction=direction, top_effective_cost=math.inf,
                          kalshi_side=k_side, poly_outcome=pm.outcomes[p_idx])
        kb, pb = k_book.best, p_book.best
        if not kb or not pb:
            opp.reject_reason = "no asks on one side"
            return opp
        opp.kalshi_top_price, opp.poly_top_price = kb.price, pb.price

        def p_fee(c: float, p: float) -> float:
            return poly_taker_fee(c, p, pm.fee_rate, pm.fee_exponent)

        # Per-$1-payout cost at the top of book (unrounded fees) — used to rank near-arbs.
        opp.top_effective_cost = (
            kb.price + pb.price
            + self.s.kalshi_fee_rate * kb.price * (1 - kb.price)
            + pm.fee_rate * (pb.price * (1 - pb.price)) ** pm.fee_exponent
        )
        days = lockup_days(km, pm)
        floor = self.required_roi(days)
        opp.days_to_settle, opp.required_roi = days, floor

        # Largest integer N whose legs each fit their own platform budget and the visible depth.
        # The Polymarket leg is sent as a market BUY of a whole-cent USD amount, so it is costed that way.
        bk, bp = max(0.0, budgets[0]), max(0.0, budgets[1])

        def total(n: int):
            kf = fill_cost(k_book, n, self._kalshi_fee)
            pf = fill_cost(p_book, n, p_fee)
            if kf is None or pf is None:
                return None
            return kf, pf, ceil_cent(pf[0])

        def over_budget(res) -> str:
            kf, pf, p_spend = res
            if kf[0] + kf[1] > bk + 1e-9:
                return "Kalshi"
            if p_spend + pf[1] > bp + 1e-9:
                return "Polymarket"
            return ""

        lo, hi, best = 1, int(min(bk // kb.price, bp // pb.price)), None
        while lo <= hi:
            mid = (lo + hi) // 2
            res = total(mid)
            if res is not None and not over_budget(res):
                best, lo = (mid, res), mid + 1
            else:
                hi = mid - 1
        if best is None:
            top_roi = (1.0 / opp.top_effective_cost - 1.0) * 100 if opp.top_effective_cost > 0 else -100.0
            if self.roi_shortfall(top_roi, days):  # unprofitable anyway: say so rather than blame the budget
                opp.reject_reason = self.roi_shortfall(top_roi, days)
            elif bk < kb.price or bp < pb.price:
                side = "Kalshi" if bk < kb.price else "Polymarket"
                opp.reject_reason = f"no {side} budget left (cap / balance ${bk if side == 'Kalshi' else bp:.2f})"
            else:
                opp.reject_reason = "insufficient depth for 1 contract"
            return opp

        n_max, res_max = best
        nxt = total(n_max + 1)
        limit = "order-book depth" if nxt is None else (over_budget(nxt) + " budget") if over_budget(nxt) else ""

        def roi_of(n: int, res) -> float:
            t_ = res[0][0] + res[0][1] + res[2] + res[1][1]
            return (n - t_) / t_ * 100 if t_ > 0 else -100.0

        # VWAP worsens as the walk goes deeper. If the full size no longer clears the ROI floor, take the
        # largest size that still does (scanning down; tiny sizes can be worse because fees round up).
        n, res = n_max, res_max
        top_roi = (1.0 / opp.top_effective_cost - 1.0) * 100 if opp.top_effective_cost > 0 else -100.0
        if roi_of(n_max, res_max) < floor and top_roi >= floor and n_max > 1:
            step = max(1, n_max // 60)
            for cand in range(n_max - 1, 0, -step):
                r = total(cand)
                if r is not None and roi_of(cand, r) >= floor:
                    n, res = cand, r
                    break
        kf_m, pf_m, ps_m = res_max
        opp.n_max = n_max
        opp.budget_k, opp.budget_p = bk, bp
        opp.max_stake_k, opp.max_stake_p = kf_m[0] + kf_m[1], ps_m + pf_m[1]
        opp.depth_limited = limit == "order-book depth"
        opp.roi_at_max = roi_of(n_max, res_max)
        opp.roi_limited = n < n_max
        opp.sizing_note = (f"ROI floor: only {n} of {n_max} contracts clear {floor:.2f}% net"
                           if opp.roi_limited else limit)
        kf, pf, p_spend = res
        tot = kf[0] + kf[1] + p_spend + pf[1]
        opp.contracts = n
        opp.total_cost = round(tot, 4)
        opp.payout = float(n)
        opp.profit = round(n - tot, 4)
        opp.effective_cost = tot / n
        opp.roi_pct = (n - tot) / tot * 100 if tot > 0 else 0.0
        opp.kalshi = Leg(
            platform="Kalshi", market_id=km.ticker, side=k_side, side_label=k_side,
            contracts=n, avg_price=kf[2], limit_price=kf[3], cost=kf[0], fee=kf[1],
            top_liquidity_usd=k_book.top_liquidity_usd,
        )
        p_label = pm.outcomes[p_idx]
        opp.poly = Leg(
            platform="Polymarket", market_id=pm.condition_id,
            side="YES" if p_idx == pair.poly_yes_index else "NO", side_label=p_label,
            contracts=n, avg_price=pf[2], limit_price=pf[3], cost=p_spend, fee=pf[1],
            top_liquidity_usd=p_book.top_liquidity_usd, token_id=pm.token_ids[p_idx],
        )

        min_liq = self.s.min_liquidity_usd
        opp.annualized_roi = opp.roi_pct * 365.0 / days if days else None
        if self.roi_shortfall(opp.roi_pct, days):
            opp.reject_reason = self.roi_shortfall(opp.roi_pct, days)
        elif opp.kalshi.top_liquidity_usd < min_liq or opp.poly.top_liquidity_usd < min_liq:
            opp.reject_reason = (
                f"thin top-of-book (K ${opp.kalshi.top_liquidity_usd:.0f} / P ${opp.poly.top_liquidity_usd:.0f})"
            )
        elif pm.min_order_size and n < pm.min_order_size:
            opp.reject_reason = f"below Polymarket min size {pm.min_order_size:g}"
        else:
            opp.is_arb = True
        return opp

    def scan(
        self,
        pairs: List[MatchPair],
        kalshi: Dict[str, KalshiMarket],
        poly: Dict[str, PolyMarket],
        budgets: Optional[Budgets] = None,
    ) -> Tuple[List[Opportunity], List[Opportunity]]:
        """Returns (confirmed arbs by ROI desc, closest near-arbs)."""
        arbs: List[Opportunity] = []
        near: List[Opportunity] = []
        for pair in pairs:
            km, pm = kalshi.get(pair.kalshi_ticker), poly.get(pair.poly_condition_id)
            if not km or not pm:
                continue
            opps = self.evaluate(pair, km, pm, budgets)
            arbs.extend(o for o in opps if o.is_arb)
            if not any(o.is_arb for o in opps):
                cands = [o for o in opps if math.isfinite(o.top_effective_cost)]
                if cands:
                    near.append(min(cands, key=_closeness))
        arbs.sort(key=lambda o: -o.roi_pct)
        near.sort(key=_closeness)
        return arbs, near[: self.s.near_arb_count]


def _closeness(o: Opportunity) -> float:
    return o.effective_cost if o.contracts else o.top_effective_cost
