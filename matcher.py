"""Entity resolution between Kalshi and Polymarket markets.

Two stages, so the Claude Pro budget is spent only where it matters:
  1. A local TF-IDF blocking pass proposes the top-K Polymarket candidates per Kalshi market.
  2. The local Claude Code CLI (`claude -p --model haiku --output-format json`, prompt on stdin)
     confirms which candidate (if any) is the *same* proposition and aligns outcomes.
Answers are cached in matches_cache.json keyed by Kalshi ticker; a ticker is only re-asked when
new candidates appear or the entry is older than MATCH_CACHE_TTL_HOURS.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import multiprocessing
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_cli import INSTALL_HINT, CliInfo, child_env, discover
from config import Settings
from models import KalshiMarket, MatchPair, PolyMarket

log = logging.getLogger("matcher")

# ---------------------------------------------------------------------------
# Stage 1: lexical candidate generation
# ---------------------------------------------------------------------------
_MONTHS = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
    "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november",
    "dec": "december",
}
_STOP = set(
    "the a an of in on at to for by be is are was will or and with from as this that it its than "
    "market resolve resolves resolved yes no if then else any other per otherwise which who what when "
    "how before after during between more less over under above below there their price source "
    "official according data".split()
)
_TOKEN_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?|[a-z][a-z'\-]*")


def _tokens(text: str) -> List[str]:
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        tok = raw.replace(",", "").replace("$", "").replace("'s", "").strip("-'")
        tok = _MONTHS.get(tok, tok)
        if len(tok) < 2 and not tok.isdigit():
            continue
        if tok in _STOP:
            continue
        out.append(tok)
    return out


def _weighted_terms(primary: str, secondary: str) -> Counter:
    c: Counter = Counter()
    for t in _tokens(primary):
        c[t] += 2.0
    for t in _tokens(secondary):
        c[t] += 1.0
    return c


def kalshi_text(m: KalshiMarket) -> Tuple[str, str]:
    ticker_words = " ".join(re.split(r"[-_]", m.ticker))
    return f"{m.title} {m.subtitle}", f"{m.rules[:400]} {ticker_words}"


def poly_text(p: PolyMarket) -> Tuple[str, str]:
    return f"{p.question} {' '.join(p.outcomes)}", f"{p.slug.replace('-', ' ')} {p.description[:400]}"


def _ts(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim >6 fractional digits for py3.9 fromisoformat
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


KDoc = Tuple[str, str, str]  # (primary text, secondary text, settlement date)
PDoc = Tuple[str, str, str, str]  # (primary text, secondary text, end date, slug)


def kalshi_doc(m: KalshiMarket) -> KDoc:
    return (*kalshi_text(m), m.expected_expiration or m.close_time)


def poly_doc(p: PolyMarket) -> PDoc:
    return (*poly_text(p), p.end_date, p.slug)


def generate_candidates(
    kalshi: Dict[str, KalshiMarket],
    poly: Dict[str, PolyMarket],
    top_k: int,
    min_score: float,
    max_date_gap_days: float = 0,
) -> Dict[str, List[Tuple[str, float]]]:
    """Convenience wrapper over generate_candidates_docs for market objects."""
    return generate_candidates_docs(
        {t: kalshi_doc(m) for t, m in kalshi.items()}, {c: poly_doc(p) for c, p in poly.items()},
        top_k, min_score, max_date_gap_days,
    )


def generate_candidates_docs(
    kalshi: Dict[str, KDoc],
    poly: Dict[str, PDoc],
    top_k: int,
    min_score: float,
    max_date_gap_days: float = 0,
    gap_fraction: float = 0.0,
    now: float = 0.0,
) -> Dict[str, List[Tuple[str, float]]]:
    """Return {kalshi_ticker: [(condition_id, cosine), ...]} best first.

    TF-IDF cosine via posting-list accumulation. Very common tokens count toward vector norms but
    are not traversed. If max_date_gap_days > 0, candidates whose settlement dates differ by more
    than that are dropped. Pure function on plain tuples so it can run in a worker process.
    """
    if not kalshi or not poly:
        return {}
    pvecs = {cid: _weighted_terms(d[0], d[1]) for cid, d in poly.items()}
    df: Counter = Counter()
    for vec in pvecs.values():
        df.update(vec.keys())
    n = len(pvecs)
    idf = {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}
    max_idf = math.log(n + 1) + 1.0
    max_df = max(50, int(n * 0.01))

    postings: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    pnorm: Dict[str, float] = {}
    for cid, vec in pvecs.items():
        w = {t: tf * idf[t] for t, tf in vec.items()}
        pnorm[cid] = math.sqrt(sum(v * v for v in w.values())) or 1.0
        for t, wt in w.items():
            if df[t] <= max_df:
                postings[t].append((cid, wt))

    gap_s = max_date_gap_days * 86400 if max_date_gap_days > 0 else 0
    pdates = {cid: _ts(d[2]) for cid, d in poly.items()} if gap_s else {}

    result: Dict[str, List[Tuple[str, float]]] = {}
    for ticker, kd in kalshi.items():
        kvec = _weighted_terms(kd[0], kd[1])
        kw = {t: tf * idf.get(t, max_idf) for t, tf in kvec.items()}
        knorm = math.sqrt(sum(v * v for v in kw.values())) or 1.0
        acc: Dict[str, float] = defaultdict(float)
        for t, v in kw.items():
            for cid, wt in postings.get(t, ()):
                acc[cid] += v * wt
        if not acc:
            continue
        kdate = _ts(kd[2]) if gap_s else None
        scored = []
        for cid, dot in acc.items():
            score = dot / (knorm * pnorm[cid])
            if score < min_score:
                continue
            if gap_s and kdate is not None and pdates.get(cid) is not None:
                pd = pdates[cid]
                # long-dated markets: allow a gap proportional to the time left (e.g. 25% of 2 years)
                allowed = max(gap_s, gap_fraction * (max(kdate, pd) - now)) if gap_fraction and now else gap_s
                if abs(kdate - pd) > allowed:
                    continue
            scored.append((cid, round(score, 4)))
        if scored:
            result[ticker] = heapq.nlargest(top_k, scored, key=lambda x: x[1])

    # Structural pass: Kalshi game tickers (KXNBAGAME-26NOV23UTALAL-LAL) map onto Polymarket
    # game slugs (nba-uta-lal-2026-11-23) even when titles share no words ("Utah wins" vs "Jazz vs. Lakers").
    by_slug = {d[3]: cid for cid, d in poly.items() if d[3]}
    for ticker in kalshi:
        for slug in game_slugs(ticker):
            cid = by_slug.get(slug)
            if cid:
                lst = [c for c in result.get(ticker, []) if c[0] != cid]
                result[ticker] = ([(cid, 1.0)] + lst)[:top_k]
                break
    return result


def stratified_pick(todo: List[str], score, group_of, budget: int) -> List[str]:
    """Pick up to `budget` tickers, round-robin across categories and, inside a category, across series.

    Each series queue is best-score first, and each category's queue interleaves its series. Categories
    take turns (strongest first), so ~20 categories each get a share instead of one topic (e.g. 400 NFL
    receiving props) consuming the whole round. Unused share flows to categories that still have work.
    """
    from collections import deque

    by_cat: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for t in todo:
        cat, series = group_of(t)
        by_cat[cat][series].append(t)
    cat_q: Dict[str, deque] = {}
    for cat, series_map in by_cat.items():
        queues = [deque(sorted(lst, key=lambda t: -score(t))) for lst in series_map.values()]
        queues.sort(key=lambda q: -score(q[0]))
        merged: List[str] = []
        while queues:
            nxt = []
            for q in queues:
                merged.append(q.popleft())
                if q:
                    nxt.append(q)
            queues = nxt
        cat_q[cat] = deque(merged)
    order = sorted(cat_q, key=lambda c: -score(cat_q[c][0]))
    out: List[str] = []
    while len(out) < budget and order:
        nxt = []
        for c in order:
            if len(out) >= budget:
                break
            out.append(cat_q[c].popleft())
            if cat_q[c]:
                nxt.append(c)
        order = nxt
    return out


_GAME_RE = re.compile(r"^KX([A-Z0-9]+?)GAME$")
_EVENT_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,8})$")
_MON = {m[:3].upper(): i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def game_slugs(ticker: str) -> List[str]:
    """Candidate Polymarket slugs for a Kalshi head-to-head game ticker (empty if not a game)."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return []
    league = _GAME_RE.match(parts[0])
    ev = _EVENT_RE.match(parts[1])
    if not league or not ev or ev.group(2) not in _MON:
        return []
    lg = league.group(1).lower()
    date = f"20{ev.group(1)}-{_MON[ev.group(2)]:02d}-{ev.group(3)}"
    teams = ev.group(4).lower()
    splits = {(teams[:i], teams[i:]) for i in range(2, len(teams) - 1) if 2 <= len(teams) - i <= 4 and i <= 4}
    out = []
    for a, b in sorted(splits, key=lambda s: abs(len(s[0]) - len(s[1]))):  # 3+3 first
        out += [f"{lg}-{a}-{b}-{date}", f"{lg}-{b}-{a}-{date}"]
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class MatchCache:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if data.get("version") == self.VERSION:
                self.entries = data.get("entries", {})
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            log.warning("Could not read %s (%r); starting with an empty cache", self.path.name, exc)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = {"version": self.VERSION, "updated_at": time.time(), "entries": self.entries}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)  # atomic: a Ctrl+C mid-write never corrupts the cache

    def needs_check(self, ticker: str, candidate_ids: List[str], ttl_s: float) -> bool:
        e = self.entries.get(ticker)
        if not e:
            return True
        if e.get("blocked"):
            return False  # user dismissed this pair: never spend Claude usage on it again
        if time.time() - e.get("checked_at", 0) > ttl_s:
            return True
        return bool(set(candidate_ids) - set(e.get("checked_candidates", [])))


# ---------------------------------------------------------------------------
# Stage 2: Claude CLI
# ---------------------------------------------------------------------------
class ClaudeUnavailable(RuntimeError):
    pass


class ClaudeNotLoggedIn(RuntimeError):
    """The CLI runs but has no valid login."""


class ClaudeTimeout(RuntimeError):
    """The CLI didn't answer within CLAUDE_TIMEOUT_SECONDS."""


class ClaudeBusy(RuntimeError):
    """Usage/rate limit hit — stop this refresh round, try again next round."""


class ClaudeError(RuntimeError):
    pass


_AUTH_RE = re.compile(r"not logged in|please run /login|run /login|invalid api key|authentication_error|"
                      r"oauth token (has )?expired|login (has )?expired|unauthori[sz]ed|\b401\b", re.I)
_BUSY_RE = re.compile(r"usage limit|rate.?limit|overloaded|too many requests|\b429\b|limit reached|try again later", re.I)

PROMPT_HEADER = """You are an exact-match resolver for prediction markets. For each Kalshi market below, decide \
which Polymarket candidate (if any) is the SAME proposition, i.e. both contracts are guaranteed to settle \
identically in every possible world.

Rules:
- Same underlying event, same subject/person/team, same threshold/strike/range, same deadline or date, same \
resolution source when it matters. "Above 120k" vs "above 130k", "by Dec 31" vs "on Dec 31", different game \
dates, or different counts are NOT matches.
- If no candidate is an exact match, use null. Be conservative: a false match loses real money.
- Outcome alignment: "kalshi_yes_equals_outcome" is the 0-based index into that candidate's "outcomes" list \
that pays out exactly when Kalshi YES pays out (e.g. Kalshi "Lakers win" vs outcomes ["Celtics","Lakers"] -> 1; \
Kalshi "Fed holds" vs a Polymarket question "Will the Fed cut?" with ["Yes","No"] -> 1, but only if hold/cut \
are the only two possibilities).
- Return one object for EVERY Kalshi ticker, in this exact JSON shape and nothing else (no prose, no code fences):
[{"kalshi_ticker": "...", "match": "p3" or null, "kalshi_yes_equals_outcome": 0 or 1 or null, \
"confidence": 0.0-1.0, "reason": "<=12 words"}]

Data:
"""


def build_batch(
    tickers: List[str],
    cands: Dict[str, List[Tuple[str, float]]],
    kalshi: Dict[str, KalshiMarket],
    poly: Dict[str, PolyMarket],
) -> Tuple[str, Dict[str, str]]:
    """Return (prompt, alias->condition_id). Short aliases keep prompts small and avoid copy errors."""
    alias_of: Dict[str, str] = {}
    poly_items = []
    k_items = []
    for t in tickers:
        km = kalshi[t]
        aliases = []
        for cid, _score in cands[t]:
            if cid not in alias_of:
                alias_of[cid] = f"p{len(alias_of)}"
                pm = poly[cid]
                poly_items.append({
                    "id": alias_of[cid], "question": pm.question, "outcomes": pm.outcomes,
                    "end_date": pm.end_date[:10], "rules": pm.description[:350],
                })
            aliases.append(alias_of[cid])
        k_items.append({
            "kalshi_ticker": t, "title": km.title, "yes_means": km.subtitle or km.title,
            "close": km.close_time[:10], "rules": km.rules[:350], "candidates": aliases,
        })
    data = json.dumps({"kalshi": k_items, "polymarket": poly_items}, ensure_ascii=False, separators=(",", ":"))
    return PROMPT_HEADER + data, {a: c for c, a in alias_of.items()}


STRICT_PROMPT = """You are auditing whether two prediction-market contracts settle IDENTICALLY. Real money is \
placed on both at once; if they can ever settle differently, the trader loses. Be adversarial.

For each pair, read BOTH full rulebooks and grade every criterion "pass", "fail" or "unclear":
1. proposition: same event, subject/person/team, threshold/strike/range and measurement window.
2. outcome_alignment: the named Polymarket outcome pays out exactly when Kalshi YES pays out.
3. resolution_source: same authority / data source (e.g. official vs unofficial result, AP call vs \
certification, a specific index, exchange or website, revisions).
4. deadline: same cutoff date AND time/timezone; "by" vs "on"; same observation date.
5. edge_cases: postponement, cancellation, ties, overtime, void / did-not-play, early resolution, \
"fair price" or 50/50 settlements, ambiguous outcomes.
Any difference that could make one pay and the other not is "fail". Missing information is "unclear".
"identical" is true ONLY if all five pass. "confidence" (0-100) is your certainty that they settle \
identically in every possible world: subtract heavily for any unclear item or subtle wording difference \
(e.g. official vs unofficial tournament, different cutoffs or sources).

Return JSON only, no prose or code fences, one object per pair:
[{"id":"...","identical":true|false,"confidence":0-100,"checks":{"proposition":"pass","outcome_alignment":"pass",\
"resolution_source":"pass","deadline":"pass","edge_cases":"pass"},"issues":["<=15 words each"]}]

Pairs:
"""

STRICT_CRITERIA = ("proposition", "outcome_alignment", "resolution_source", "deadline", "edge_cases")


def strict_hash(km: KalshiMarket, pm: PolyMarket, yes_idx: int) -> str:
    import hashlib

    blob = "\x1f".join((km.ticker, km.title, km.subtitle, km.rules, km.expected_expiration, pm.condition_id,
                        pm.question, pm.description, pm.end_date, str(yes_idx)))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def build_strict_batch(items) -> str:
    rows = []
    for ticker, km, pm, yes_idx in items:
        rows.append({
            "id": ticker,
            "kalshi": {"title": km.title, "yes_means": km.subtitle or km.title, "rules": km.rules[:3500],
                       "expected_settlement": km.expected_expiration, "close": km.close_time},
            "polymarket": {"question": pm.question, "outcome_paying_when_kalshi_yes": pm.outcomes[yes_idx],
                           "other_outcome": pm.outcomes[1 - yes_idx], "rules": pm.description[:3500],
                           "end_date": pm.end_date},
        })
    return STRICT_PROMPT + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ClaudeError(f"no JSON array in response: {text[:200]!r}")
    arr = json.loads(text[start : end + 1])
    if not isinstance(arr, list):
        raise ClaudeError("response JSON is not an array")
    return [x for x in arr if isinstance(x, dict)]


def parse_cli_stdout(stdout: str) -> str:
    """`--output-format json` prints a result envelope; return its `result` text."""
    try:
        data = json.loads(stdout)
    except ValueError:
        raise ClaudeError(f"CLI printed non-JSON output: {stdout[:200]!r}")
    if isinstance(data, list):  # some versions emit the whole message list
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    text = str(data.get("result") or "")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        msg = text or str(data.get("subtype"))
        if _AUTH_RE.search(msg):
            raise ClaudeNotLoggedIn(msg[:200])
        if _BUSY_RE.search(msg):
            raise ClaudeBusy(msg[:200])
        raise ClaudeError(msg[:300])
    return text


class Matcher:
    def __init__(self, settings: Settings, offline_answers: Optional[List[Dict[str, Any]]] = None):
        self.s = settings
        self.cache = MatchCache(settings.cache_path)
        self.offline = {a["kalshi_ticker"]: a for a in offline_answers} if offline_answers is not None else None
        self.status = "idle"
        self.last_refresh: float = 0.0
        self.last_error: str = ""
        self.alert: str = ""  # actionable message for the dashboard banner; "" when matching is healthy
        self.last_round_mix: Dict[str, int] = {}  # category -> tickers sent to Claude in the last round
        self.cli: Optional[CliInfo] = None
        self._cli_lock = asyncio.Lock()  # one Claude CLI process at a time (matching + strict checks)
        self._strict_calls: List[float] = []
        self.offline_strict: Dict[str, Dict[str, Any]] = {}  # --mock: canned strict verdicts
        self._proc: Optional[asyncio.subprocess.Process] = None

    # ---- public API ------------------------------------------------------
    def pairs(self, kalshi: Dict[str, KalshiMarket], poly: Dict[str, PolyMarket]) -> List[MatchPair]:
        out = []
        for ticker, e in self.cache.entries.items():
            cid = e.get("poly_condition_id")
            if not cid or e.get("blocked") or ticker not in kalshi or cid not in poly:
                continue
            if e.get("confidence", 0) < self.s.match_min_confidence or e.get("poly_yes_index") not in (0, 1):
                continue
            out.append(MatchPair(ticker, cid, int(e["poly_yes_index"]), float(e["confidence"]), e.get("reason", "")))
        return out

    def due(self) -> bool:
        return time.time() - self.last_refresh >= self.s.match_interval

    def set_blocked(self, ticker: str, blocked: bool) -> bool:
        """Mark a pair blocked (mismatched rules) in matches_cache.json. Returns False if unknown."""
        e = self.cache.entries.get(ticker)
        if e is None:
            return False
        if blocked:
            e["blocked"] = True
            e["blocked_at"] = time.time()
            e.pop("approved_for", None)  # a blocked pair can never stay approved for auto-trading
        else:
            e.pop("blocked", None)
            e.pop("blocked_at", None)
        self.cache.save()
        log.info("%s pair %s ⇄ %s", "Blocked" if blocked else "Unblocked", ticker, (e.get("poly_question") or "")[:60])
        return True

    def set_approved(self, ticker: str, approved: bool) -> bool:
        """Operator approval for auto-trading, bound to the exact Polymarket market it was given for:
        if a re-match ever changes the pair, the approval no longer applies."""
        e = self.cache.entries.get(ticker)
        if e is None or (approved and (e.get("blocked") or not e.get("poly_condition_id"))):
            return False
        if approved:
            e["approved_for"], e["approved_at"] = e["poly_condition_id"], time.time()
        else:
            e.pop("approved_for", None)
            e.pop("approved_at", None)
        self.cache.save()
        log.info("%s pair %s for auto-trade", "Approved" if approved else "Revoked approval of", ticker)
        return True

    def is_approved(self, ticker: str) -> bool:
        e = self.cache.entries.get(ticker) or {}
        return bool(e.get("approved_for")) and e.get("approved_for") == e.get("poly_condition_id") and not e.get("blocked")

    def blocked(self) -> List[Dict[str, Any]]:
        rows = [
            {"kalshi_ticker": t, "kalshi_title": e.get("kalshi_title"), "poly_question": e.get("poly_question"),
             "blocked_at": e.get("blocked_at", 0)}
            for t, e in self.cache.entries.items() if e.get("blocked")
        ]
        return sorted(rows, key=lambda r: -r["blocked_at"])

    async def refresh(self, kalshi: Dict[str, KalshiMarket], poly: Dict[str, PolyMarket]) -> int:
        """Resolve new/changed Kalshi tickers. Returns number of tickers newly resolved."""
        self.last_refresh = time.time()
        # Only markets that could ever satisfy the top-of-book liquidity rule are worth matching.
        min_liq = self.s.min_liquidity_usd
        k_liq = {t: m for t, m in kalshi.items() if m.has_quotes and m.top_liquidity_usd >= min_liq}
        p_liq = {c: p for c, p in poly.items() if p.liquidity_usd >= min_liq}
        self.status = f"blocking {len(k_liq):,}×{len(p_liq):,}"
        log.info("Matcher: candidate generation over %d Kalshi × %d Polymarket liquid markets", len(k_liq), len(p_liq))
        t0 = time.time()
        cands = await self._candidates_offloaded(
            {t: kalshi_doc(m) for t, m in k_liq.items()}, {c: poly_doc(p) for c, p in p_liq.items()}
        )
        log.info("Matcher: %d Kalshi markets have candidates (%.1fs)", len(cands), time.time() - t0)
        ttl = self.s.match_cache_ttl_hours * 3600
        todo = [t for t, c in cands.items() if self.cache.needs_check(t, [cid for cid, _ in c], ttl)]
        todo.sort(key=lambda t: -cands[t][0][1])
        budget = self.s.match_batch_size * self.s.match_max_batches
        if len(todo) > budget:
            if self.s.match_stratify:
                todo = stratified_pick(todo, lambda t: cands[t][0][1],
                                       lambda t: (kalshi[t].category or "Other", kalshi[t].series), budget)
            else:
                todo = todo[:budget]
            mix = Counter(kalshi[t].category or "Other" for t in todo)
            log.info("Matcher: %d tickers need checking; this round checks %d across %d categories: %s",
                     len(cands), len(todo), len(mix), ", ".join(f"{c} {n}" for c, n in mix.most_common()))
        self.last_round_mix = dict(Counter(kalshi[t].category or "Other" for t in todo))
        if not todo:
            self.status = f"idle · {len(cands)} candidates, all cached"
            return 0

        resolved = 0
        batches = [todo[i : i + self.s.match_batch_size] for i in range(0, len(todo), self.s.match_batch_size)]
        if self.offline is None and not await self.check_cli():
            log.info("Matcher round skipped: %s", self.status)
            return 0
        for n, batch in enumerate(batches, 1):
            self.status = f"Claude batch {n}/{len(batches)}"
            prompt, alias_map = build_batch(batch, cands, kalshi, poly)
            try:
                answers = await self._ask(prompt, batch, cands, alias_map)
            except ClaudeUnavailable as exc:
                self._set_problem("Claude CLI not found", str(exc), logging.ERROR)
                self.cli = None  # re-discover next round
                break
            except ClaudeNotLoggedIn as exc:
                self._set_problem("Claude CLI not logged in", self._login_help(str(exc)), logging.ERROR)
                if self.cli:
                    self.cli.logged_in = False
                break
            except ClaudeTimeout as exc:
                self._set_problem(
                    "Claude CLI timing out",
                    f"Claude CLI timed out {self.s.claude_retries + 1}× on batch {n} ({exc}). Matching paused "
                    f"until the next round. If it keeps happening, check that `claude -p` answers in a "
                    f"terminal, or raise CLAUDE_TIMEOUT_SECONDS (now {self.s.claude_timeout:.0f}s).",
                    logging.WARNING)
                break
            except ClaudeBusy as exc:
                self._set_problem("Claude busy/limited — retry next round",
                                  f"Claude usage limit or overload ({exc}). Matching resumes next round; "
                                  "scanning continues with the pairs already matched.", logging.WARNING)
                break
            except ClaudeError as exc:
                self.last_error = str(exc)
                log.warning("Claude batch %d failed, will retry next round: %s", n, exc)
                continue
            self.alert, self.last_error = "", ""  # a successful batch clears any earlier problem
            resolved += self._store(batch, cands, alias_map, answers, kalshi, poly)
            self.cache.save()
        else:
            self.status = f"idle · {len(self.pairs(kalshi, poly))} matched"
        log.info("Matcher round done: %d tickers resolved", resolved)
        return resolved

    def kill(self) -> None:
        """Kill the CLI *and its children* (claude.cmd -> node on Windows would otherwise be orphaned)."""
        proc = self._proc
        if not proc or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    # ---- internals ---------------------------------------------------------
    async def _candidates_offloaded(self, kdocs, pdocs) -> Dict[str, List[Tuple[str, float]]]:
        """Run the CPU-heavy blocking pass in a worker process so the scan loop never stalls."""
        args = (kdocs, pdocs, self.s.match_candidates, self.s.match_min_similarity, self.s.match_max_date_gap_days,
                self.s.match_date_gap_fraction, time.time())
        if len(kdocs) * len(pdocs) < 2_000_000:  # small (e.g. --mock): not worth a process
            return generate_candidates_docs(*args)
        pool = multiprocessing.get_context("spawn").Pool(1)
        try:
            res = pool.apply_async(generate_candidates_docs, args)
            while not res.ready():
                await asyncio.sleep(0.5)
            return res.get()
        finally:
            pool.terminate()  # instant even if cancelled mid-run (Ctrl+C)
            pool.join()

    def _store(self, batch, cands, alias_map, answers, kalshi, poly) -> int:
        by_ticker = {str(a.get("kalshi_ticker")): a for a in answers}
        stored = 0
        for t in batch:
            a = by_ticker.get(t)
            if a is None:
                continue  # model skipped it -> not cached, retried next round
            cand_ids = [cid for cid, _ in cands[t]]
            cid = alias_map.get(str(a.get("match"))) if a.get("match") else None
            idx = a.get("kalshi_yes_equals_outcome")
            try:
                conf = float(a.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if cid not in cand_ids or idx not in (0, 1):
                cid, idx = None, None
            self.cache.entries[t] = {
                "poly_condition_id": cid,
                "poly_yes_index": idx,
                "confidence": round(conf, 3),
                "reason": str(a.get("reason") or "")[:160],
                "kalshi_title": kalshi[t].display_title,
                "poly_question": poly[cid].question if cid else None,
                "poly_outcome_for_kalshi_yes": poly[cid].outcomes[idx] if cid else None,
                "checked_candidates": cand_ids,
                "checked_at": time.time(),
            }
            stored += 1
            if cid and conf >= self.s.match_min_confidence:
                log.info("MATCH %s ⇄ %s (YES = %r, conf %.2f)", t, poly[cid].question[:60], poly[cid].outcomes[idx], conf)
        return stored

    # ---- Claude CLI discovery / health -------------------------------------
    async def check_cli(self, force: bool = False) -> bool:
        """Find a working CLI and confirm it is logged in. Updates status/alert; never raises."""
        fresh = self.cli is not None and time.time() - self.cli.checked_at < 600
        if not force and fresh and self.cli.found and self.cli.logged_in is not False:
            return True
        prev = self.status
        self.status = "checking Claude CLI"
        info = await asyncio.to_thread(discover, self.s.claude_cli)
        self.cli = info
        if not info.found:
            skipped = ("; skipped broken installs: " + " | ".join(info.skipped)) if info.skipped else ""
            self._set_problem("Claude CLI not found",
                              f"Claude Code CLI was not found or doesn't run on this machine. {INSTALL_HINT}"
                              f"{skipped}", logging.ERROR)
            return False
        if info.logged_in is False:
            self._set_problem("Claude CLI not logged in", self._login_help("auth status: loggedIn=false"),
                              logging.ERROR)
            return False
        log.info("Claude CLI OK: %s (%s, via %s%s)", info.path, info.version, info.source,
                 f", {info.plan} plan" if info.plan else "")
        if info.skipped:
            log.warning("Skipped non-working Claude CLI installs: %s", " | ".join(info.skipped))
        self.alert, self.last_error = "", ""
        problem = prev.startswith("Claude CLI") or prev.startswith("Claude busy") or prev == "checking Claude CLI"
        self.status = "idle · Claude CLI ready" if problem else prev
        return True

    def _login_help(self, detail: str) -> str:
        cmd = self.cli.login_command() if self.cli else "claude auth login"
        return (f"Claude Code is installed but not logged in ({detail}). Open a terminal, run  {cmd}  "
                "and sign in with your Claude Pro account. Matching retries automatically next round, "
                "or press Force re-match.")

    def _set_problem(self, status: str, message: str, level: int) -> None:
        self.status, self.last_error, self.alert = status, message[:500], message[:500]
        log.log(level, "[Matcher] %s: %s", status, message)

    async def _ask(self, prompt, batch, cands, alias_map) -> List[Dict[str, Any]]:
        if self.offline is not None:
            return self._offline_answer(batch, cands, alias_map)
        last: Optional[Exception] = None
        for attempt in range(self.s.claude_retries + 1):
            try:
                return extract_json_array(await self._run_cli(prompt))
            except (ClaudeUnavailable, ClaudeBusy, ClaudeNotLoggedIn):
                raise
            except ClaudeTimeout as exc:
                last = exc
                log.warning("Claude attempt %d timed out: %s", attempt + 1, exc)
                if attempt >= self.s.claude_retries:
                    raise
                continue
            except (ClaudeError, ValueError) as exc:
                last = exc
                log.warning("Claude attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(5 * (attempt + 1))
        raise ClaudeError(str(last))

    def _offline_answer(self, batch, cands, alias_map) -> List[Dict[str, Any]]:
        """Mock mode: canned answers rendered exactly as Claude would (aliases), so parsing is exercised."""
        alias_of = {c: a for a, c in alias_map.items()}
        out = []
        for t in batch:
            exp = self.offline.get(t)
            if exp is None:
                out.append({"kalshi_ticker": t, "match": None, "kalshi_yes_equals_outcome": None,
                            "confidence": 0.95, "reason": "no equivalent proposition"})
                continue
            cid = exp.get("poly_condition_id")
            out.append({
                "kalshi_ticker": t,
                "match": alias_of.get(cid) if cid else None,
                "kalshi_yes_equals_outcome": exp.get("kalshi_yes_equals_outcome"),
                "confidence": exp.get("confidence", 0.95),
                "reason": exp.get("reason", ""),
            })
        return out

    # ---- strict rule verification ---------------------------------------------
    def strict_verdict(self, ticker: str, km: KalshiMarket, pm: PolyMarket, yes_idx: int) -> Optional[Dict[str, Any]]:
        """Cached strict verdict if it is still valid for exactly these rules, else None."""
        v = (self.cache.entries.get(ticker) or {}).get("strict")
        if not v or v.get("hash") != strict_hash(km, pm, yes_idx):
            return None
        if time.time() - v.get("at", 0) > self.s.strict_verify_ttl_hours * 3600:
            return None
        return v

    def strict_budget_left(self) -> int:
        cutoff = time.time() - 3600
        self._strict_calls = [t for t in self._strict_calls if t > cutoff]
        return max(0, self.s.strict_verify_max_per_hour - len(self._strict_calls))

    async def strict_verify(self, items) -> int:
        """items: [(ticker, km, pm, yes_idx)]. Stores verdicts in matches_cache.json; returns how many."""
        if not items:
            return 0
        if self.offline is not None:
            answers = [dict(self.offline_strict.get(t) or {"id": t, "identical": False, "confidence": 90,
                       "checks": {c: "unclear" for c in STRICT_CRITERIA}, "issues": ["mock: no canned verdict"]},
                       id=t) for t, *_ in items]
            model = "mock"
        else:
            if not self.strict_budget_left():
                return 0
            if not await self.check_cli():
                return 0
            self._strict_calls.append(time.time())
            model = self.s.strict_verify_model or self.s.claude_model
            try:
                answers = extract_json_array(await self._run_cli(build_strict_batch(items), model=model))
            except ClaudeNotLoggedIn as exc:
                self._set_problem("Claude CLI not logged in", self._login_help(str(exc)), logging.ERROR)
                return 0
            except (ClaudeError, ClaudeTimeout, ClaudeBusy, ClaudeUnavailable, ValueError) as exc:
                log.warning("[Strict] verification batch failed (%d pairs), will retry: %s", len(items), exc)
                return 0
        by_id = {str(a.get("id")): a for a in answers if isinstance(a, dict)}
        done = 0
        for ticker, km, pm, yes_idx in items:
            a = by_id.get(ticker)
            e = self.cache.entries.get(ticker)
            if a is None or e is None:
                continue
            checks = {c: str((a.get("checks") or {}).get(c, "unclear")).lower() for c in STRICT_CRITERIA}
            try:
                conf = float(a.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = conf * 100 if conf <= 1.0 else conf  # tolerate 0-1 answers
            identical = bool(a.get("identical")) and all(v == "pass" for v in checks.values())
            e["strict"] = {"identical": identical, "confidence": round(min(100.0, max(0.0, conf)), 1),
                           "checks": checks, "issues": [str(x)[:160] for x in (a.get("issues") or [])][:6],
                           "hash": strict_hash(km, pm, yes_idx), "at": time.time(), "model": model}
            done += 1
            log.info("[Strict] %s: %s, %.0f%% (%s)%s", ticker, "IDENTICAL" if identical else "NOT identical",
                     e["strict"]["confidence"], ", ".join(f"{k}={v}" for k, v in checks.items() if v != "pass") or
                     "all pass", (": " + e["strict"]["issues"][0]) if e["strict"]["issues"] else "")
        if done:
            self.cache.save()
        return done

    async def _run_cli(self, prompt: str, model: Optional[str] = None) -> str:
        async with self._cli_lock:
            return await self._run_cli_locked(prompt, model)

    async def _run_cli_locked(self, prompt: str, model: Optional[str] = None) -> str:
        if self.cli is None or not self.cli.found:
            raise ClaudeUnavailable(f"Claude Code CLI not found. {INSTALL_HINT}")
        exe = self.cli.path
        args = [exe, "-p", "--model", model or self.s.claude_model, "--output-format", "json"]
        if self.s.claude_extra_args:
            args += shlex.split(self.s.claude_extra_args, posix=os.name != "nt")
        env = child_env()  # drops ANTHROPIC_API_KEY: always the logged-in Pro subscription
        # Empty scratch dir so no project files / CLAUDE.md leak into the prompt context.
        cwd = Path(tempfile.gettempdir()) / "arb_scanner_claude_cwd"
        cwd.mkdir(exist_ok=True)
        extra: Dict[str, Any] = (
            # CREATE_NO_WINDOW: when launched via pythonw there is no console to inherit, and Windows
            # would otherwise pop up a new console window for every Claude CLI call.
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt" else {"start_new_session": True}
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(cwd), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **extra,
            )
        except OSError as exc:  # binary vanished / invalid since discovery
            raise ClaudeUnavailable(f"Could not start {exe}: {exc}. {INSTALL_HINT}") from exc
        try:
            out, err = await asyncio.wait_for(
                self._proc.communicate(prompt.encode("utf-8")), timeout=self.s.claude_timeout
            )
        except asyncio.TimeoutError:
            self.kill()
            await self._proc.wait()
            raise ClaudeTimeout(f"no answer within {self.s.claude_timeout:.0f}s")
        except asyncio.CancelledError:
            self.kill()
            raise
        rc = self._proc.returncode
        stdout = out.decode("utf-8", "replace").strip()
        stderr = err.decode("utf-8", "replace").strip()
        if rc != 0 and not stdout:
            msg = stderr or f"exit code {rc}"
            if _AUTH_RE.search(msg):
                raise ClaudeNotLoggedIn(msg[:200])
            if _BUSY_RE.search(msg):
                raise ClaudeBusy(msg[:200])
            raise ClaudeError(f"CLI failed: {msg[:300]}")
        return parse_cli_stdout(stdout)
