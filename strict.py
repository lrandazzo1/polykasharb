"""Strict "Bulletproof Arbitrage" qualification.

An opportunity is BULLETPROOF only if every check passes:
    1. net ROI ≥ MIN_NET_ROI_PCT, priced at the VWAP of the actual fill (the book walked to the size)
    2. strict rule verification by Claude ≥ STRICT_MATCH_CONFIDENCE and "identical" on all five criteria
       (proposition, outcome alignment, resolution source, deadline/cutoff, edge cases)
    3. settlement dates differ by ≤ MAX_SETTLE_DIFF_DAYS
    4. capital lock-up (days until the later settlement) ≤ MAX_LOCKUP_DAYS
    5. full depth: both books fill the whole per-platform budget at ≥ the ROI floor (REQUIRE_FULL_DEPTH),
       and both top-of-book levels hold ≥ MIN_TOP_LIQUIDITY_USD

Everything else lands on the Watchlist with the exact reasons it failed. Only bulletproof
opportunities trigger LIVE ARBITRAGE alerts and can be auto-traded.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from config import Settings
from models import KalshiMarket, Opportunity, PolyMarket
from risk import _ts

HARD_RISK = ("numbers differ", "'by' vs 'on' deadline")  # deterministic red flags from risk.py


def _money(x: float) -> str:
    return f"${x:,.0f}" if x >= 10 else f"${x:,.2f}"


def classify(o: Opportunity, km: Optional[KalshiMarket], pm: Optional[PolyMarket],
             verdict: Optional[Dict[str, Any]], risk_tags: List[Dict[str, str]], s: Settings, now: float) -> bool:
    """Fill o.bulletproof / o.strict_reasons / o.strict_checks. Returns True if the pair is worth a strict
    Claude verification (money on the table and every non-Claude check already passes)."""
    reasons: List[str] = []
    checks: Dict[str, Any] = {}

    # 1. net ROI at VWAP
    checks["roi"] = bool(o.is_arb)
    if not o.is_arb and not (o.reject_reason or "").startswith("thin top-of-book"):
        reasons.append(o.reject_reason or "not an arb")  # thin books are explained by the depth check below

    # 3. settlement-date difference / 4. lock-up
    kd = _ts((km.expected_expiration or km.close_time) if km else "")
    pd = _ts(pm.end_date if pm else "")
    if kd is None or pd is None:
        checks["settle_diff"] = checks["lockup"] = False
        reasons.append("Settlement date unknown on " + ("Kalshi" if kd is None else "Polymarket"))
    else:
        diff = abs(kd - pd) / 86400
        checks["settle_diff_days"] = round(diff, 1)
        checks["settle_diff"] = diff <= s.max_settle_diff_days + 1e-9
        if not checks["settle_diff"]:
            reasons.append(f"Settle dates {diff:.0f}d apart (max {s.max_settle_diff_days:g}d)")
        lock = max(1, math.ceil((max(kd, pd) - now) / 86400))
        checks["lockup_days"] = lock
        checks["lockup"] = lock <= s.max_lockup_days
        if not checks["lockup"]:
            reasons.append(f"Lock-up {lock}d > {s.max_lockup_days:g}d")

    # 5. depth: the whole budget must fill at the ROI floor, and the top levels must not be thin
    depth_ok = True
    if o.contracts or o.n_max:
        if s.require_full_depth and o.depth_limited:
            depth_ok = False
            k_short = o.max_stake_k < o.budget_k - 0.5
            leg, have, cap = (("Kalshi", o.max_stake_k, o.budget_k) if k_short
                              else ("Polymarket", o.max_stake_p, o.budget_p))
            reasons.append(f"Thin book: {leg} depth fills only {_money(have)} of {_money(cap)}")
        elif s.require_full_depth and o.roi_limited and o.kalshi and o.poly:
            depth_ok = False
            reasons.append(f"VWAP: only {_money(o.kalshi.stake)} of {_money(o.max_stake_k)} Kalshi / "
                           f"{_money(o.poly.stake)} of {_money(o.max_stake_p)} Polymarket clears the "
                           f"{o.required_roi:.2f}% net floor (full fill would be {o.roi_at_max:.2f}%)")
    if o.kalshi and o.poly:
        thin = [(n, l.top_liquidity_usd) for n, l in (("Kalshi", o.kalshi), ("Polymarket", o.poly))
                if l.top_liquidity_usd < s.min_liquidity_usd]
        if thin:
            depth_ok = False
            reasons.append("Thin book: only " + ", ".join(f"{_money(v)} at top ({n})" for n, v in thin))
    checks["depth"] = depth_ok

    # deterministic wording red flags
    hard = [t for t in risk_tags if t["label"] in HARD_RISK]
    for t in hard:
        reasons.append(f"Rule mismatch: {t['label']} ({t['detail']})")
    checks["wording"] = not hard

    # 2. strict Claude verification
    first_pass = o.pair.confidence * 100
    if verdict is None:
        checks["match"] = False
        checks["match_conf"] = None
        reasons.append(f"Rules not strictly verified yet (first-pass match {first_pass:.0f}% < "
                       f"{s.strict_match_confidence:g}% needed)")
    else:
        conf = float(verdict.get("confidence") or 0)
        checks["match_conf"] = conf
        ok = bool(verdict.get("identical")) and conf >= s.strict_match_confidence
        checks["match"] = ok
        if not ok:
            failed = [k.replace("_", " ") for k, v in (verdict.get("checks") or {}).items() if v != "pass"]
            issue = (verdict.get("issues") or [""])[0]
            what = (f" ({', '.join(failed)}" + (f": {issue}" if issue else "") + ")") if failed or issue else ""
            reasons.append(f"Match {conf:.0f}% < {s.strict_match_confidence:g}%{what}" if conf < s.strict_match_confidence
                           else f"Rules differ{what}")

    o.strict_reasons = [r for r in reasons if r]
    o.strict_checks = checks
    o.bulletproof = not o.strict_reasons
    # spend Claude usage only where it could flip the pair to bulletproof
    others_ok = (checks["roi"] and checks.get("settle_diff") and checks.get("lockup") and checks["wording"])
    return bool(others_ok and verdict is None)

