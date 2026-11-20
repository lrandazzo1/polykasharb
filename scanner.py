"""Scanner service: the two background loops plus the live controls the web dashboard calls.

UI-agnostic — `server.py` exposes it over HTTP, `main.py --once` runs it headless.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from arb_engine import ArbEngine, break_even_prob, favorite_offset, mmf_hurdle_prob
from config import PRICE_POLL_MIN, ROOT, Settings
from execution import ExecutionManager, Ledger
from http_utils import make_client, throttle_count
from kalshi_client import KalshiClient
from kalshi_executor import KalshiExecutor
from matcher import Matcher
from models import KalshiMarket, Leg, MatchPair, Opportunity, PolyMarket
from notifier import LogBuffer, Notifier
from polymarket_client import PolymarketClient
from polymarket_executor import PolymarketExecutor
from risk import _ts, risk_level, risk_tags
from strict import classify

log = logging.getLogger("scanner")
MOCK_DIR = ROOT / "mock"


class ScannerService:
    def __init__(self, s: Settings, logbuf: LogBuffer):
        self.s = s
        self.logbuf = logbuf
        self.http = make_client(s.http_timeout)
        mock_dir = MOCK_DIR if s.mock else None
        self.kalshi_api = KalshiClient(s, self.http, mock_dir)
        self.poly_api = PolymarketClient(s, self.http, mock_dir)
        offline = None
        if s.mock and not s.live_matcher_in_mock:
            offline = json.loads((MOCK_DIR / "claude_answers.json").read_text("utf-8"))
        if s.mock and s.cache_path.exists():
            s.cache_path.unlink()  # mock runs always exercise the matcher from scratch
        self.matcher = Matcher(s, offline_answers=offline)
        if offline is not None and (MOCK_DIR / "claude_strict.json").exists():
            self.matcher.offline_strict = {v["id"]: v for v in
                                           json.loads((MOCK_DIR / "claude_strict.json").read_text("utf-8"))}
        self._verify_queue: Dict[str, MatchPair] = {}  # pairs waiting for a strict rule check
        self.engine = ArbEngine(s)
        self.notifier = Notifier(s, self.http)
        self.execute_trades = not s.once  # --once never executes, whatever EXECUTION_MODE says
        if s.mock:
            from mock_exchange import MockKalshiExecutor, MockPolymarketExecutor

            for f in (s.ledger_path, s.exec_state_path):  # every mock run starts from a clean slate
                if f.exists():
                    f.unlink()
            k_exec: KalshiExecutor = MockKalshiExecutor(s, self.http, lambda t: self.kalshi.get(t))
            p_exec: PolymarketExecutor = MockPolymarketExecutor(s, self.http, self._poly_by_token)
        else:
            k_exec, p_exec = KalshiExecutor(s, self.http), PolymarketExecutor(s, self.http)
        self.execm = ExecutionManager(s, k_exec, p_exec, Ledger(s.ledger_path), self.notifier,
                                      self.matcher.is_approved)
        self.execm.risk_of = self._risk_level
        self._exec_ready = False

        self.kalshi: Dict[str, KalshiMarket] = {}
        self.poly: Dict[str, PolyMarket] = {}
        self.live_pairs: List[MatchPair] = []  # pairs with fresh books from the last scan
        self.arbs: List[Opportunity] = []
        self.near: List[Opportunity] = []

        # status
        self.version = 0
        self.scan_no = 0
        self.last_scan_at = 0.0
        self.last_scan_secs = 0.0
        self.next_scan_at: Optional[float] = None
        self.paused = False
        self.matching = False
        self.alerts_sent = 0
        self.error = ""

        # cadences: catalog (market lists) / match (Claude) / price (orderbooks), each its own loop
        self.catalog_refreshing = False
        self.catalog_at = 0.0
        self.catalog_secs = 0.0
        self.next_catalog_at: Optional[float] = None
        self.last_match_at = 0.0
        self.next_match_at: Optional[float] = None
        self.price_overruns = 0  # passes that took longer than the interval (never overlapped, just late)
        self.backoff = 1.0  # multiplier on the price interval while an API is returning HTTP 429
        self._clean_passes = 0
        self.throttle_note = ""
        self._last_arb_count = -1
        self.catalog_ready = asyncio.Event()
        self._catalog_wake = asyncio.Event()

        self.universe_ready = asyncio.Event()
        self.first_match_done = asyncio.Event()
        self.stop_event = asyncio.Event()
        self._wake_scan = asyncio.Event()
        self._rematch = asyncio.Event()
        self._tasks: List[asyncio.Task] = []

    # =====================================================================
    # lifecycle
    # =====================================================================
    async def start(self) -> None:
        self._tasks = [asyncio.ensure_future(self._catalog_loop()), asyncio.ensure_future(self._match_loop()),
                       asyncio.ensure_future(self._scan_loop()), asyncio.ensure_future(self._verify_loop())]

    async def stop(self) -> None:
        self.stop_event.set()
        await self.execm.shutdown()  # blocks new trades; lets an in-flight trade finish both legs
        self.matcher.kill()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.http.aclose()
        try:
            self.matcher.cache.save()
        except OSError as exc:
            log.error("Could not save match cache on shutdown: %r", exc)
        log.info("Scanner stopped cleanly")

    async def run_once(self) -> None:
        """Headless: one full market refresh + match round, then one price scan (never executes)."""
        await self._init_execution()
        if self.matcher.offline is None:
            await self.matcher.check_cli()
        await self._refresh_catalog()
        await self._run_match()
        await self.scan_once()
        if self._verify_queue:  # no background loop in --once: run the strict rule checks inline
            items = [(t, self.kalshi[t], self.poly[pr.poly_condition_id], pr.poly_yes_index)
                     for t, pr in list(self._verify_queue.items())[:8]]
            self._verify_queue.clear()
            if await self.matcher.strict_verify(items):
                self.recompute()
        await self.stop()

    # =====================================================================
    # live controls (called by the web API)
    # =====================================================================
    def update_settings(self, max_per_platform: Optional[float] = None, min_roi: Optional[float] = None,
                        min_annualized: Optional[float] = None,
                        min_liquidity: Optional[float] = None, interval: Optional[float] = None,
                        catalog_interval: Optional[float] = None, match_interval: Optional[float] = None,
                        max_settle_diff_days: Optional[float] = None, strict_match_confidence: Optional[float] = None,
                        max_lockup_days: Optional[float] = None, require_full_depth: Optional[bool] = None) -> None:
        changed = []
        if max_per_platform is not None and max_per_platform != self.s.max_per_platform:
            self.s.max_per_platform = max_per_platform
            changed.append(f"max per platform=${max_per_platform:,.2f}")
        if min_annualized is not None and abs(self.s.min_annualized_roi_pct - min_annualized) > 1e-9:
            self.s.min_annualized_roi_pct = min_annualized
            changed.append(f"min annualized ROI={min_annualized:g}%")
        if min_roi is not None and abs(self.s.min_roi_pct - min_roi) > 1e-9:
            self.s.min_roi_pct = min_roi
            changed.append(f"min net ROI={min_roi:.2f}%")
        if min_liquidity is not None and min_liquidity != self.s.min_liquidity_usd:
            self.s.min_liquidity_usd = min_liquidity
            changed.append(f"min liquidity=${min_liquidity:,.0f}")
        if interval is not None and interval != self.s.interval:
            self.s.interval = interval
            if self.next_scan_at is not None and self.last_scan_at:
                self.next_scan_at = self.last_scan_at + interval
            self._wake_scan.set()
            changed.append(f"price poll={interval:g}s")
        if catalog_interval is not None and catalog_interval != self.s.catalog_interval:
            self.s.catalog_interval = catalog_interval
            if self.next_catalog_at is not None and self.catalog_at:
                self.next_catalog_at = self.catalog_at + catalog_interval
            changed.append(f"catalog refresh={catalog_interval:.0f}s")
        if match_interval is not None and match_interval != self.s.match_interval:
            self.s.match_interval = match_interval
            if self.next_match_at is not None and self.last_match_at:
                self.next_match_at = self.last_match_at + match_interval
            changed.append(f"Claude matching={match_interval:.0f}s")
        for name, val, fmt in (("max_settle_diff_days", max_settle_diff_days, "{:g}d"),
                               ("strict_match_confidence", strict_match_confidence, "{:g}%"),
                               ("max_lockup_days", max_lockup_days, "{:g}d"),
                               ("require_full_depth", require_full_depth, "{}")):
            if val is not None and getattr(self.s, name) != val:
                setattr(self.s, name, val)
                changed.append(f"{name}={fmt.format(val)}")
        if changed:
            log.info("Settings updated: %s", ", ".join(changed))
            self.recompute()

    def recompute(self) -> None:
        """Re-price every pair from the books already in memory (no network) — instant."""
        pairs = [p for p in self.live_pairs if not self.matcher.cache.entries.get(p.kalshi_ticker, {}).get("blocked")]
        self.live_pairs = pairs
        self._price_all(pairs)
        self._bump()

    def set_blocked(self, kalshi_ticker: str, blocked: bool) -> bool:
        ok = self.matcher.set_blocked(kalshi_ticker, blocked)
        if ok:
            if blocked:
                self.recompute()
            else:
                self._bump()
                self.request_scan()  # pair rejoins on the next poll (needs fresh books)
        return ok

    def set_approved(self, kalshi_ticker: str, approved: bool) -> bool:
        ok = self.matcher.set_approved(kalshi_ticker, approved)
        if ok:
            self._bump()
        return ok

    def set_execution_mode(self, mode: str) -> None:
        self.execm.set_mode(mode)
        self.recompute()

    def halt_execution(self, reason: str) -> None:
        self.execm.halt(reason)
        self._bump()

    def resume_execution(self) -> None:
        self.execm.resume()
        self._bump()
        self.request_scan()

    def reset_simulation(self) -> int:
        n = self.execm.reset_simulation()
        self.recompute()
        return n

    def request_rematch(self) -> bool:
        if self.matching:
            return False
        self.matcher.last_refresh = 0
        self.next_match_at = time.time()
        self._rematch.set()
        log.info("Manual re-match requested")
        return True

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        log.info("Price polling %s", "paused" if paused else "resumed")
        if not paused:
            self.request_scan()
        self._bump()

    def request_scan(self) -> None:
        self.next_scan_at = time.time()
        self._wake_scan.set()

    # =====================================================================
    # loops
    # =====================================================================
    async def _quick_start(self) -> None:
        """Load just the cached matched markets so scanning starts in seconds, not after a full refresh."""
        cached = {t: e["poly_condition_id"] for t, e in self.matcher.cache.entries.items()
                  if e.get("poly_condition_id") and not e.get("blocked")}
        if not cached:
            return
        try:
            self.matcher.status = f"quick start: loading {len(cached)} cached pairs"
            k, p, _ = await asyncio.gather(
                self.kalshi_api.fetch_markets_by_ticker(cached.keys()),
                self.poly_api.fetch_markets_by_condition(set(cached.values())),
                self.kalshi_api.load_series_categories(),
            )
            self.kalshi_api.tag_categories(k)
            self.kalshi, self.poly = k, p
            self.universe_ready.set()
            log.info("Quick start: %d cached pairs loaded; full market refresh continues in background", len(cached))
        except Exception as exc:
            log.warning("Quick start failed (%r); waiting for full refresh", exc)

    async def _refresh_catalog(self) -> None:
        """Full market-list download (slow). Books already fetched by the price loop carry over."""
        self.catalog_refreshing = True
        self._bump()
        t0 = time.time()
        lim = self.kalshi_api.limiter  # start each download halfway back toward the configured rate
        lim.interval = max(lim.base, lim.interval * 0.5)
        try:
            if not self.matching:
                self.matcher.status = "fetching market lists"
            await self.kalshi_api.load_series_categories()  # cached; refreshed every 6 h
            k, p = await asyncio.gather(self.kalshi_api.fetch_open_markets(), self.poly_api.fetch_active_markets())
            self.kalshi_api.tag_categories(k)
            for tk, km in k.items():  # keep live books so a catalog swap never blanks the arbs
                old = self.kalshi.get(tk)
                if old is not None:
                    km.yes_book, km.no_book = old.yes_book, old.no_book
            for cid, pm in p.items():
                old = self.poly.get(cid)
                if old is not None and old.token_ids == pm.token_ids:
                    pm.books = old.books
            self.kalshi, self.poly = k, p
            self.error = ""
            self.catalog_at, self.catalog_secs = time.time(), time.time() - t0
            self.universe_ready.set()
            self.catalog_ready.set()
            if not self.matching and self.matcher.status == "fetching market lists":
                self.matcher.status = "idle"
            cats = Counter(m.category for m in k.values())
            log.info("Catalog refreshed: %d Kalshi / %d Polymarket markets in %.0fs · Kalshi by category: %s",
                     len(k), len(p), self.catalog_secs, ", ".join(f"{c} {n:,}" for c, n in cats.most_common(8)))
            await self.execm.release_settled(self.kalshi_api.market_status, self.poly_api.is_resolved)
        finally:
            self.catalog_refreshing = False
            self._bump()

    async def _run_match(self) -> None:
        """One Claude matching round over the current catalog (uses Claude Pro usage)."""
        self.matching = True
        self._bump()
        try:
            await self.matcher.refresh(self.kalshi, self.poly)
        finally:
            self.matching = False
            self.last_match_at = time.time()
            self.first_match_done.set()
            self._bump()
        self.request_scan()  # price newly matched pairs right away

    async def _catalog_loop(self) -> None:
        if self.matcher.offline is None:
            await self.matcher.check_cli()  # show CLI / login problems on the dashboard right away
        await self._quick_start()
        while not self.stop_event.is_set():
            try:
                await self._refresh_catalog()
                retry = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep running on any upstream failure
                log.exception("Catalog refresh failed: %r", exc)
                self.error = f"market list refresh failed: {exc!r}"[:200]
                retry = 60.0
            self.next_catalog_at = time.time() + (retry or self.s.catalog_interval)
            await self._sleep_until(lambda: self.next_catalog_at, self._catalog_wake)

    async def _match_loop(self) -> None:
        while not self.catalog_ready.is_set() and not self.stop_event.is_set():
            await self._wait(self.catalog_ready, 1.0)
        while not self.stop_event.is_set():
            try:
                await self._run_match()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Matching round failed: %r", exc)
                self.error = f"matching failed: {exc!r}"[:200]
            self._rematch.clear()
            self.next_match_at = time.time() + self.s.match_interval
            await self._sleep_until(lambda: self.next_match_at, self._rematch)

    async def _sleep_until(self, deadline, wake: asyncio.Event) -> None:
        """Sleep until deadline() (re-read every second, so interval edits apply at once) or `wake`."""
        while not self.stop_event.is_set():
            remaining = (deadline() or 0) - time.time()
            if remaining <= 0 or wake.is_set():
                return
            await self._wait(wake, min(1.0, remaining))

    async def _init_execution(self) -> None:
        if not self._exec_ready:
            self._exec_ready = True
            try:
                await self.execm.init()
            except Exception as exc:
                log.exception("[Execution] init failed: %r", exc)
                self.execm.halt(f"execution init failed: {exc!r}"[:200], persist=False)

    async def _scan_loop(self) -> None:
        """Fast price loop. Strictly sequential: a slow pass delays the next one, it never overlaps it."""
        await self._init_execution()
        while not self.universe_ready.is_set() and not self.stop_event.is_set():
            await self._wait(None, 0.5)
        while not self.stop_event.is_set():
            if not self.paused:
                start = time.time()
                self.next_scan_at = None
                try:
                    await self.scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Price pass failed: %r", exc)
                    self.error = f"price pass failed: {exc!r}"[:200]
                self._update_backoff(start)
                eff = self.s.interval * self.backoff
                if time.time() - start > eff:
                    self.price_overruns += 1
                # breathing room: never start the next pass less than 0.5 s after this one ended
                self.next_scan_at = max(start + eff, time.time() + 0.5)
            else:
                self.next_scan_at = None
            self._bump()
            # sleep until next_scan_at; interval edits / resume / unblock wake us early
            while not self.stop_event.is_set():
                if not self.paused and self.next_scan_at is not None and time.time() >= self.next_scan_at:
                    break
                timeout = 1.0 if self.next_scan_at is None else min(1.0, max(0.05, self.next_scan_at - time.time()))
                self._wake_scan.clear()
                await self._wait(self._wake_scan, timeout)

    def _update_backoff(self, pass_start: float) -> None:
        """Double the price interval (max 8×) after a pass that hit HTTP 429; relax after 5 clean passes."""
        window = time.time() - pass_start + 0.5
        # only the price loop's own requests count here; catalog paging throttles itself (adaptive limiter)
        k429, p429 = throttle_count("kalshi", window, "price"), throttle_count("polymarket", window, "price")
        if k429 or p429:
            self.backoff, self._clean_passes = min(8.0, self.backoff * 2), 0
            log.warning("Rate-limited (HTTP 429: Kalshi %d, Polymarket %d); price poll backs off to %.0fs",
                        k429, p429, self.s.interval * self.backoff)
        else:
            self._clean_passes += 1
            if self.backoff > 1 and self._clean_passes >= 5:
                self.backoff, self._clean_passes = max(1.0, self.backoff / 2), 0
                log.info("No rate limiting for 5 passes; price poll back to %.0fs", self.s.interval * self.backoff)
        recent_k, recent_p = throttle_count("kalshi", 60, "price"), throttle_count("polymarket", 60, "price")
        bulk = throttle_count("kalshi", 60, "bulk") + throttle_count("polymarket", 60, "bulk")
        notes = []
        if self.backoff > 1 or recent_k or recent_p:
            who = " and ".join(n for n, c in (("Kalshi", recent_k), ("Polymarket", recent_p)) if c) or "API"
            notes.append(f"{who} rate-limited price requests (HTTP 429 ×{recent_k + recent_p} in the last minute): "
                         f"price poll backed off to every {self.s.interval * self.backoff:.0f}s")
        if bulk or (self.catalog_refreshing and self.kalshi_api.limiter.slowed):
            cause = f"{bulk}× HTTP 429 in the last minute" if bulk else "after earlier HTTP 429s"
            notes.append(f"Market-list download rate-limited ({cause}); paging slowed to "
                         f"{1 / self.kalshi_api.limiter.interval:.1f} req/s. Price polling is unaffected")
        self.throttle_note = " · ".join(notes)

    async def scan_once(self) -> None:
        t0 = time.time()
        pairs = self.matcher.pairs(self.kalshi, self.poly)
        live: List[MatchPair] = []
        if pairs:
            tokens = [tok for pr in pairs for tok in self.poly[pr.poly_condition_id].token_ids]
            # batched: 100 Kalshi books per GET /markets/orderbooks, 100 tokens per POST /books
            kbooks, pbooks = await asyncio.gather(
                self.kalshi_api.fetch_orderbooks_batch(pr.kalshi_ticker for pr in pairs),
                self.poly_api.fetch_books(tokens, fast=True),
            )
            for pr in pairs:
                km, pm = self.kalshi.get(pr.kalshi_ticker), self.poly.get(pr.poly_condition_id)
                if km is None or pm is None:  # catalog swapped while we were fetching
                    continue
                if pr.kalshi_ticker not in kbooks or not all(t in pbooks for t in pm.token_ids):
                    continue
                km.yes_book, km.no_book = kbooks[pr.kalshi_ticker]
                pm.books = [pbooks[t] for t in pm.token_ids]
                live.append(pr)
        self.live_pairs = live
        await self.execm.maybe_refresh_balances()
        self._price_all(live)  # self.arbs = bulletproof only; everything else goes to the watchlist
        self.alerts_sent += await self.notifier.maybe_alert(self.arbs)
        if self.execute_trades and self.execm.mode != "alert_only" and self.arbs:
            await self.execm.on_scan(self.arbs, self._refresh_pair)
            # re-size everything against the budgets left after any trade
            self._price_all(live)
        self.scan_no += 1
        self.last_scan_at, self.last_scan_secs = t0, time.time() - t0
        # A 3-second loop would flood the log: summarise when something changes, else every 100 passes.
        lvl = logging.INFO if len(self.arbs) != self._last_arb_count or self.scan_no % 100 == 1 else logging.DEBUG
        log.log(lvl, "Price pass #%d: %d/%d pairs priced in %.2fs, %d arbs", self.scan_no, len(live), len(pairs),
                self.last_scan_secs, len(self.arbs))
        self._last_arb_count = len(self.arbs)
        self._bump()

    def _price_all(self, pairs: List[MatchPair]) -> None:
        """Price every pair (VWAP to the budget), then split: BULLETPROOF -> self.arbs, the rest -> watchlist."""
        econ_arbs, near = self.engine.scan(pairs, self.kalshi, self.poly, self.execm.budgets())
        now = time.time()
        bullet: List[Opportunity] = []
        watch: List[Opportunity] = []
        for o in econ_arbs + near:
            if self._classify(o, now):
                self._verify_queue.setdefault(o.pair.kalshi_ticker, o.pair)
            (bullet if o.bulletproof else watch).append(o)
        # capital efficiency first: highest annualized ROI on top (unknown dates last)
        bullet.sort(key=lambda o: -_ann(o))
        watch.sort(key=lambda o: -_ann(o))
        self.arbs, self.near = bullet, watch[: self.s.near_arb_count]

    def _classify(self, o: Opportunity, now: float) -> bool:
        km, pm = self.kalshi.get(o.pair.kalshi_ticker), self.poly.get(o.pair.poly_condition_id)
        verdict = self.matcher.strict_verdict(o.pair.kalshi_ticker, km, pm, o.pair.poly_yes_index) if km and pm else None
        tags = risk_tags(km, pm, o.pair, now) if km and pm else []
        return classify(o, km, pm, verdict, tags, self.s, now)

    async def _verify_loop(self) -> None:
        """Strict Claude rule checks for pairs that are profitable and pass every other strict check."""
        while not self.stop_event.is_set():
            await self._wait(None, 3.0)
            if not self._verify_queue or self.stop_event.is_set():
                continue
            batch = []
            for ticker in list(self._verify_queue)[:4]:
                pr = self._verify_queue.pop(ticker)
                km, pm = self.kalshi.get(ticker), self.poly.get(pr.poly_condition_id)
                if km and pm and self.matcher.strict_verdict(ticker, km, pm, pr.poly_yes_index) is None:
                    batch.append((ticker, km, pm, pr.poly_yes_index))
            if not batch:
                continue
            try:
                n = await self.matcher.strict_verify(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("[Strict] verification failed: %r", exc)
                n = 0
            if n:
                self.recompute()

    async def _refresh_pair(self, o: Opportunity) -> Optional[Opportunity]:
        """Fresh books for one pair right before execution, re-sized with the current budgets."""
        km, pm = self.kalshi.get(o.pair.kalshi_ticker), self.poly.get(o.pair.poly_condition_id)
        if not km or not pm:
            return None
        kb, pb = await asyncio.gather(self.kalshi_api.fetch_orderbook(km.ticker),
                                      self.poly_api.fetch_books(pm.token_ids))
        if kb is None or not all(tk in pb for tk in pm.token_ids):
            return None
        km.yes_book, km.no_book = kb
        pm.books = [pb[tk] for tk in pm.token_ids]
        for fresh in self.engine.evaluate(o.pair, km, pm, self.execm.budgets()):
            if fresh.direction == o.direction:
                self._classify(fresh, time.time())  # every strict check again, on the fresh books
                if not fresh.bulletproof:
                    fresh.is_arb = False
                    fresh.reject_reason = "; ".join(fresh.strict_reasons)[:300]
                return fresh
        return None

    def _risk_level(self, o: Opportunity) -> str:
        km, pm = self.kalshi.get(o.pair.kalshi_ticker), self.poly.get(o.pair.poly_condition_id)
        return risk_level(risk_tags(km, pm, o.pair, time.time())) if km and pm else "high"

    def _poly_by_token(self, token_id: str) -> Optional[PolyMarket]:
        for pm in self.poly.values():
            if token_id in pm.token_ids:
                return pm
        return None

    async def _wait(self, event: Optional[asyncio.Event], seconds: float) -> None:
        """Sleep until `event`/stop is set or `seconds` pass (1 s slices so Ctrl+C stays snappy)."""
        end = time.time() + seconds
        while not self.stop_event.is_set() and time.time() < end:
            if event is not None and event.is_set():
                return
            waiters = [asyncio.ensure_future(self.stop_event.wait())]
            if event is not None:
                waiters.append(asyncio.ensure_future(event.wait()))
            done, pending = await asyncio.wait(waiters, timeout=min(1.0, max(0.01, end - time.time())),
                                               return_when=asyncio.FIRST_COMPLETED)
            for w in pending:
                w.cancel()
            if done:
                return

    def _bump(self) -> None:
        self.version += 1

    # =====================================================================
    # serialisation for the dashboard
    # =====================================================================
    @property
    def status(self) -> str:
        if self.paused:
            return "Paused"
        if self.matching:
            return "Matching in Background"
        return "Scanning"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "server_time": time.time(),
            "status": self.status,
            "matcher_status": self.matcher.status,
            "matcher_error": self.matcher.last_error,
            "matcher_alert": self.matcher.alert,
            "claude_cli": self.matcher.cli.public() if self.matcher.cli else None,
            "matcher_offline": self.matcher.offline is not None,
            "matching": self.matching,
            "paused": self.paused,
            "error": self.error,
            "scan_no": self.scan_no,
            "price_overruns": self.price_overruns,
            "backoff": self.backoff,
            "throttle_note": self.throttle_note,
            "catalog_refreshing": self.catalog_refreshing,
            "catalog_at": self.catalog_at,
            "catalog_secs": round(self.catalog_secs, 1),
            "next_catalog_at": self.next_catalog_at,
            "last_match_at": self.last_match_at,
            "next_match_at": self.next_match_at,
            "last_scan_at": self.last_scan_at,
            "last_scan_secs": round(self.last_scan_secs, 2),
            "next_scan_at": self.next_scan_at,
            "kalshi_count": len(self.kalshi),
            "poly_count": len(self.poly),
            "matched_pairs": len(self.matcher.pairs(self.kalshi, self.poly)),
            "categories": self._category_counts(),
            "strict_queue": len(self._verify_queue),
            "strict_calls_left": self.matcher.strict_budget_left(),
            "last_round_mix": self.matcher.last_round_mix,
            "priced_pairs": len(self.live_pairs),
            "alerts_sent": self.alerts_sent,
            "settings": {
                "max_per_platform": self.s.max_per_platform,
                "min_roi": self.s.min_roi_pct,
                "min_annualized": self.s.min_annualized_roi_pct,
                "mmf_rate": self.s.mmf_rate_pct,
                "min_liquidity": self.s.min_liquidity_usd,
                "interval": self.s.interval,
                "catalog_interval": self.s.catalog_interval,
                "match_interval": self.s.match_interval,
                "price_poll_min": PRICE_POLL_MIN,
                "max_settle_diff_days": self.s.max_settle_diff_days,
                "strict_match_confidence": self.s.strict_match_confidence,
                "max_lockup_days": self.s.max_lockup_days,
                "require_full_depth": self.s.require_full_depth,
                "strict_verify_model": self.s.strict_verify_model,
                "discord": bool(self.s.discord_webhook_url),
                "mock": self.s.mock,
                "live_matcher": self.s.live_matcher_in_mock,
            },
            "execution": self.execm.snapshot(),
            "arbs": [self._opp(o, exec_status=True) for o in self.arbs],
            "near": [self._opp(o) for o in self.near],
            "blocked": self.matcher.blocked(),
            "log": list(self.logbuf.records)[-25:],
        }

    def _category_counts(self) -> List[Dict[str, Any]]:
        """Matched / priced pairs per category, so you can see the scan really spans topics."""
        matched, priced = Counter(), Counter()
        for pr in self.matcher.pairs(self.kalshi, self.poly):
            matched[self.kalshi[pr.kalshi_ticker].category or "Other"] += 1
        for pr in self.live_pairs:
            km = self.kalshi.get(pr.kalshi_ticker)
            priced[(km.category if km else "") or "Other"] += 1
        return [{"category": c, "matched": n, "priced": priced.get(c, 0)} for c, n in matched.most_common()]

    def _verdict_view(self, o: Opportunity, km, pm) -> Optional[Dict[str, Any]]:
        v = self.matcher.strict_verdict(o.pair.kalshi_ticker, km, pm, o.pair.poly_yes_index) if km and pm else None
        return {k: v[k] for k in ("identical", "confidence", "checks", "issues", "model", "at")} if v else None

    @staticmethod
    def _lockup(o: Opportunity, km: Optional[KalshiMarket], pm: Optional[PolyMarket], eff: float) -> Dict[str, Any]:
        """Capital lock-up and simple annualized ROI.

        days_to_settle = whole days until the LATER of Kalshi's expected expiration and Polymarket's end
        date (your money is tied up until both legs pay), minimum 1. Annualized ROI = net ROI × 365 / days,
        simple (not compounded). Unknown if either side has no settlement date, rather than guessing.
        """
        kd = _ts((km.expected_expiration or km.close_time) if km else "")
        pd = _ts(pm.end_date if pm else "")
        if kd is None or pd is None:
            return {"days_to_settle": None, "annualized_roi": None, "settle_at": None}
        later = max(kd, pd)
        days = max(1, math.ceil((later - time.time()) / 86400))
        if o.contracts:
            net = o.roi_pct
        elif math.isfinite(eff) and eff > 0:
            net = (1 / eff - 1) * 100
        else:
            return {"days_to_settle": days, "annualized_roi": None, "settle_at": later}
        return {"days_to_settle": days, "annualized_roi": round(net * 365.0 / days, 2), "settle_at": later}

    def _opp(self, o: Opportunity, exec_status: bool = False) -> Dict[str, Any]:
        km = self.kalshi.get(o.pair.kalshi_ticker)
        pm = self.poly.get(o.pair.poly_condition_id)
        eff = o.effective_cost if o.contracts else o.top_effective_cost
        tags = risk_tags(km, pm, o.pair, time.time()) if km and pm else []
        lock = self._lockup(o, km, pm, eff)
        cpd = eff if math.isfinite(eff) else None
        # leg prices for the favorite check: the sized VWAP when there is a fill, else top of book
        k_px = o.kalshi.avg_price if o.kalshi and o.contracts else o.kalshi_top_price
        p_px = o.poly.avg_price if o.poly and o.contracts else o.poly_top_price
        offset = favorite_offset(k_px, p_px, _ts((km.expected_expiration or km.close_time) if km else ""),
                                 _ts(pm.end_date if pm else ""), self.s.max_settle_diff_days)
        return {
            "key": o.key,
            "kalshi_ticker": o.pair.kalshi_ticker,
            "condition_id": o.pair.poly_condition_id,
            "event": o.event,
            "poly_question": pm.question if pm else "",
            "direction": o.direction,
            "is_arb": o.is_arb,
            "reject_reason": o.reject_reason,
            "roi_pct": round(o.roi_pct, 4),
            "profit": round(o.profit, 4),
            "total_cost": round(o.total_cost, 4),
            "payout": o.payout,
            "contracts": o.contracts,
            "cost_per_dollar": round(eff, 5) if math.isfinite(eff) else None,
            "net_pct": round((1 - eff) * 100, 4) if math.isfinite(eff) else None,
            # net ROI after fees, the same measure the threshold uses: sized ROI for arbs, top-of-book otherwise
            "net_roi": round(o.roi_pct if o.contracts else ((1 / eff - 1) * 100 if math.isfinite(eff) and eff > 0 else -100), 4),
            "category": (km.category if km else "") or "Other",
            **lock,
            # deterministic hurdles from prices, fees and lock-up only
            "break_even_prob": break_even_prob(cpd),
            "mmf_hurdle_prob": mmf_hurdle_prob(cpd, lock["days_to_settle"], self.s.mmf_rate_pct),
            "offset_favorite": offset,
            "risk_tags": tags,
            "risk_level": risk_level(tags),
            "kalshi_side": o.kalshi_side,
            "poly_outcome": o.poly_outcome,
            "kalshi_top_price": o.kalshi_top_price,
            "poly_top_price": o.poly_top_price,
            "kalshi": _leg(o.kalshi),
            "poly": _leg(o.poly),
            "kalshi_url": km.url if km else "",
            "poly_url": pm.url if pm else "",
            "confidence": o.pair.confidence,
            "match_reason": o.pair.reason,
            "kalshi_rules": (km.rules if km else "")[:600],
            "poly_rules": (pm.description if pm else "")[:600],
            "kalshi_close": km.expected_expiration if km else "",
            "poly_end": pm.end_date if pm else "",
            "poly_fee_rate": pm.fee_rate if pm else 0,
            "sizing_note": o.sizing_note,
            "econ_arb": o.is_arb,
            "bulletproof": o.bulletproof,
            "strict_reasons": o.strict_reasons,
            "strict_checks": o.strict_checks,
            "strict": self._verdict_view(o, km, pm),
            "max_stake_k": round(o.max_stake_k, 2), "max_stake_p": round(o.max_stake_p, 2),
            "budget_k": round(o.budget_k, 2), "budget_p": round(o.budget_p, 2),
            "approved": self.matcher.is_approved(o.pair.kalshi_ticker),
            "exec_status": self.execm.eligibility(o) if exec_status else "",
        }


def _leg(leg: Optional[Leg]) -> Optional[Dict[str, Any]]:
    if leg is None:
        return None
    return {
        "market_id": leg.market_id,
        "side": leg.side,
        "label": leg.side_label,
        "contracts": leg.contracts,
        "avg_price": round(leg.avg_price, 5),
        "limit_price": leg.limit_price,
        "cost": round(leg.cost, 4),
        "fee": round(leg.fee, 5),
        "stake": round(leg.stake, 4),
        "top_liquidity_usd": round(leg.top_liquidity_usd, 2),
    }


def _net_roi(o: Opportunity) -> float:
    if o.contracts:
        return o.roi_pct
    eff = o.top_effective_cost
    return (1 / eff - 1) * 100 if math.isfinite(eff) and eff > 0 else -100.0


def _ann(o: Opportunity) -> float:
    """Annualized ROI for ranking: the sized trade's, else top-of-book's; unknown lock-up sorts last."""
    if o.annualized_roi is not None:
        return o.annualized_roi
    if not o.days_to_settle:
        return -1e12
    return _net_roi(o) * 365.0 / o.days_to_settle
