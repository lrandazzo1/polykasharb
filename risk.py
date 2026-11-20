"""Resolution-risk tags for a matched pair: quick, deterministic hints that two contracts might settle
differently, shown next to every arb and near-arb. They don't replace reading both rulebooks; they tell
you which spreads deserve that read first.

Levels: "high" (a concrete wording difference, so assume a mismatch until you've checked),
"medium" (a category where wording is often subjective or where void rules differ), "info".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from models import KalshiMarket, MatchPair, PolyMarket

# Categories where contracts are frequently subjective, source-dependent or long-tail.
NOVELTY_CATEGORIES = {"Entertainment", "Mentions", "Culture", "Social", "Exotics", "World", "Other"}
POLITICAL_CATEGORIES = {"Politics", "Elections"}
# Kalshi series fragments that denote player / stat props (void and DNP rules differ between venues)
_PROP_RE = re.compile(r"(REC|RECYDS|RUSH|PASS|TDS?|FIRSTTD|AST|REB|PTS|3PT|SOG|SAVES|HITS|HR|KS|STRIKEOUTS|YDS)"
                      r"(PROT)?$")
# a unit suffix must end the token: "5k" / "$2bn" yes, but not the "m" of "2026 meeting"
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\d,])\s*(k|m|bn|b|%|\+)?(?![a-z])", re.I)
_BY_RE = re.compile(r"\b(by|before|prior to|no later than|at any (?:point|time)|ever)\b", re.I)
_ON_RE = re.compile(r"\b(on|at the close of|as of|at \d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.I)


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_TIME_RE = re.compile(
    rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\bQ[1-4]\b", re.I)


def _numbers(text: str) -> Set[str]:
    """Strikes / thresholds in a title, ignoring dates and times (those are compared separately)."""
    out = set()
    for raw, suffix in _NUM_RE.findall(_DATE_TIME_RE.sub(" ", text or "")):
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}.get((suffix or "").lower(), 1)
        v *= mult
        if 1900 <= v <= 2100 and not suffix:  # years are compared by the date check instead
            continue
        pct = "%" if suffix == "%" else ""
        out.add(f"{v:g}{pct}")
        # "5+" (Kalshi) is the same line as "over 4.5" (Polymarket), and vice versa
        if suffix == "+":
            out.add(f"{v - 0.5:g}")
        elif v % 1 == 0.5 and not pct:  # O/U 4.5 ≡ 5+ for counts only, never for percentages
            out.add(f"{v + 0.5:g}")
    return out


def _ts(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def risk_tags(km: KalshiMarket, pm: PolyMarket, pair: MatchPair, now: float) -> List[Dict[str, str]]:
    tags: List[Dict[str, str]] = []
    k_text = f"{km.title} {km.subtitle}"
    p_text = pm.question

    # Kalshi puts the strike in the subtitle ("$120,000 or above"); it must appear on the Polymarket side
    kn = _numbers(km.subtitle) or _numbers(km.title)
    pn = _numbers(p_text)
    if kn and pn and not (kn & pn):
        tags.append({"label": "numbers differ", "level": "high",
                     "detail": f"Kalshi {', '.join(sorted(kn))} vs Polymarket {', '.join(sorted(pn))}"})
    k_by, p_by = bool(_BY_RE.search(k_text)), bool(_BY_RE.search(p_text))
    k_on, p_on = bool(_ON_RE.search(k_text)), bool(_ON_RE.search(p_text))
    if (k_by and p_on and not p_by) or (p_by and k_on and not k_by):
        tags.append({"label": "'by' vs 'on' deadline", "level": "high",
                     "detail": "One contract pays if it happens by a deadline, the other only at a point in time"})

    kd, pd = _ts(km.expected_expiration or km.close_time), _ts(pm.end_date)
    if kd and pd:
        gap = abs(kd - pd) / 86400
        if gap > 7:
            tags.append({"label": f"settle dates {gap:.0f}d apart", "level": "high" if gap > 31 else "medium",
                         "detail": f"Kalshi {km.expected_expiration[:10]} vs Polymarket {pm.end_date[:10]}"})
        if max(kd, pd) - now > 180 * 86400:
            tags.append({"label": "long-dated", "level": "info",
                         "detail": "Settles more than 6 months out: capital is tied up and rules may be amended"})

    cat = km.category or "Other"
    if cat in NOVELTY_CATEGORIES:
        tags.append({"label": f"novelty: {cat}", "level": "medium",
                     "detail": "Pop-culture / long-tail wording is often subjective or source-dependent"})
    elif cat in POLITICAL_CATEGORIES:
        tags.append({"label": "political resolution", "level": "medium",
                     "detail": "Check sources, certification vs projection, and recount / contest rules"})
    if _PROP_RE.search(km.series.replace("KX", "", 1)):
        tags.append({"label": "player prop", "level": "medium",
                     "detail": "Void / did-not-play and stat-correction rules often differ between venues"})
    if pair.confidence < 0.92:
        tags.append({"label": f"match {pair.confidence:.0%}", "level": "medium",
                     "detail": "Claude was less than 92% sure these are the same proposition"})
    return tags


def risk_level(tags: List[Dict[str, str]]) -> str:
    levels = {t["level"] for t in tags}
    return "high" if "high" in levels else "medium" if "medium" in levels else "low"
