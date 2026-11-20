"""Execution: per-platform exposure caps, balance awareness, the three modes, and leg-risk handling.

    alert_only  find + size arbs inside the caps, alert, never touch an order endpoint (default)
    dry_run     everything live_auto does (fresh books, sizing, balance checks, order payloads,
                signatures when keys exist) but nothing is sent; simulated fills are recorded and
                count against the caps so a dry run behaves like the real thing
    live_auto   places both legs when every safety gate passes

Sequence for one live trade:
    1. Re-fetch both order books and re-size with current budgets; abort if no longer ≥ MIN_NET_ROI_PCT.
    2. Leg 1, Kalshi: fill-or-kill limit BUY at the worst level the sizing walked. Not filled → done,
       no exposure.
    3. Leg 2, Polymarket: fill-or-kill market BUY capped at its limit price.
    4. If leg 2 fails, the filled Kalshi contracts are sold back immediately (reduce-only IOC, floor
       = fill price − FLATTEN_MAX_SLIPPAGE). Anything that can't be unwound, or any leg whose state is
       unknown, halts auto-trading and raises a CRITICAL alert, because only a human should decide next.

Budgets: each leg ≤ min(MAX_PER_PLATFORM_USD − open exposure on that platform, live cash balance).
Open exposure = the cost of this tool's own unsettled positions (trades.json). It is released only
when both markets have settled, so the cap is a hard limit on what the bot can have at risk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from arb_engine import ceil_cent, kalshi_taker_fee
from config import EXECUTION_MODES, Settings
from kalshi_executor import KalshiAmbiguousError, KalshiExecError, KalshiExecutor, v2_order_body
from models import Opportunity
from notifier import Notifier
from polymarket_executor import PolyAmbiguousError, PolyExecError, PolymarketExecutor

log = logging.getLogger("execution")
trade_log = logging.getLogger("trades")

EXPOSED = ("filled", "unhedged", "unknown", "simulated", "partial")  # statuses that still tie up capital
RefreshFn = Callable[[Opportunity], Awaitable[Optional[Opportunity]]]


# ---------------------------------------------------------------------------
class Ledger:
    """Executed and simulated trades, persisted to trades.json (atomic writes)."""

    def __init__(self, path: Path):
        self.path = path
        self.trades: List[Dict[str, Any]] = []
        if path.exists():
            try:
                self.trades = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                # A corrupt ledger would silently zero the exposure count; refuse to guess.
                raise RuntimeError(f"Cannot read trade ledger {path}: {exc!r}. Fix or move the file.") from exc

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.trades, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def add(self, rec: Dict[str, Any]) -> None:
        self.trades.append(rec)
        self.save()

    def exposure(self, platform: str, simulated: bool) -> float:
        tot = 0.0
        for t in self.trades:
            if t.get("settled") or bool(t.get("simulated")) != simulated or t.get("status") not in EXPOSED:
                continue
            tot += float((t.get(platform) or {}).get("exposure") or 0.0)
        return round(tot, 4)

    def open_pair(self, kalshi_ticker: str, include_simulated: bool) -> bool:
        """An unsettled real position on this pair (plus simulated ones during a dry run)."""
        return any(t.get("kalshi_ticker") == kalshi_ticker and not t.get("settled") and t.get("status") in EXPOSED
                   and (include_simulated or not t.get("simulated")) for t in self.trades)

    def clear_simulated(self) -> int:
        before = len(self.trades)
        self.trades = [t for t in self.trades if not t.get("simulated")]
        self.save()
        return before - len(self.trades)


# ---------------------------------------------------------------------------
class ExecutionManager:
    def __init__(self, s: Settings, kalshi: KalshiExecutor, poly: PolymarketExecutor, ledger: Ledger,
                 notifier: Notifier, is_approved: Callable[[str], bool]):
        if s.execution_mode not in EXECUTION_MODES:
            raise ValueError(f"EXECUTION_MODE must be one of {', '.join(EXECUTION_MODES)} (got {s.execution_mode!r})")
        self.s = s
        self.kalshi = kalshi
        self.poly = poly
        self.ledger = ledger
        self.notifier = notifier
        self.is_approved = is_approved
        self.lock = asyncio.Lock()
        self.halted = False
        self.halt_reason = ""
        self.shutting_down = False
        self.last_exec: Dict[str, float] = {}
        self._skip_logged: Dict[str, Tuple[str, float]] = {}
        self._inflight: Optional[asyncio.Task] = None
        self.risk_of: Callable[[Opportunity], str] = lambda o: "low"  # set by the scanner (risk.py tags)
        self._load_state()

    @property
    def mode(self) -> str:
        return self.s.execution_mode

    @property
    def simulated(self) -> bool:
        """True when new trades are simulated (dry_run). alert_only never trades at all."""
        return self.mode == "dry_run"

    # ------------------------------------------------------------- state / kill switch
    def _load_state(self) -> None:
        p = self.s.exec_state_path
        if p.exists():
            try:
                st = json.loads(p.read_text("utf-8"))
                self.halted, self.halt_reason = bool(st.get("halted")), str(st.get("reason") or "")
            except (OSError, ValueError) as exc:
                log.error("[Execution] unreadable %s (%r): starting HALTED", p.name, exc)
                self.halted, self.halt_reason = True, "execution_state.json was unreadable"
        elif self.mode == "live_auto":
            self.halted, self.halt_reason = True, "live_auto starts disarmed: press Enable auto-trade"

    def _save_state(self) -> None:
        try:
            self.s.exec_state_path.write_text(json.dumps({"halted": self.halted, "reason": self.halt_reason,
                                                          "at": time.time()}), "utf-8")
        except OSError as exc:
            log.error("[Execution] could not persist kill-switch state: %r", exc)

    def halt(self, reason: str, persist: bool = True) -> None:
        if not self.halted or reason != self.halt_reason:
            log.warning("[Execution] AUTO-TRADE HALTED: %s", reason)
        self.halted, self.halt_reason = True, reason
        if persist:
            self._save_state()

    def resume(self) -> None:
        self.halted, self.halt_reason = False, ""
        self._save_state()
        log.warning("[Execution] auto-trade ENABLED (mode %s)", self.mode)

    def set_mode(self, mode: str) -> None:
        if mode not in EXECUTION_MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode == self.mode:
            return
        if mode == "live_auto":
            # Arriving in live mode never trades immediately: the operator must press Enable.
            self.halt("switched to live_auto: press Enable auto-trade to arm it")
        self.s.execution_mode = mode
        log.warning("[Execution] mode changed to %s", mode)

    async def shutdown(self, timeout: float = 60.0) -> None:
        """Stop new trades, then wait for an in-flight trade to finish both legs (never cut it mid-way)."""
        self.shutting_down = True
        task = self._inflight
        if task and not task.done():
            log.warning("[Execution] shutdown: waiting for the in-flight trade to finish")
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.TimeoutError:
                log.critical("[Execution] shutdown timed out with a trade in flight: CHECK POSITIONS MANUALLY")
            except Exception as exc:  # the trade's own errors were already logged inside it
                log.error("[Execution] in-flight trade ended with %r during shutdown", exc)
        await self.poly.close()

    # ------------------------------------------------------------- balances & budgets
    async def init(self) -> None:
        await asyncio.gather(self.kalshi.init(), self.poly.init())
        log.info("[Execution] mode=%s · cap $%.2f per platform · min annualized ROI %.1f%% · Kalshi %s · Polymarket %s%s",
                 self.mode, self.s.max_per_platform, self.s.min_annualized_roi_pct, self.kalshi.status, self.poly.status,
                 " · HALTED: " + self.halt_reason if self.halted else "")

    async def maybe_refresh_balances(self, force: bool = False) -> None:
        now = time.time()
        jobs = [ex.refresh_balance() for ex in (self.kalshi, self.poly)
                if ex.configured and (force or now - ex.balance_at >= self.s.balance_refresh_seconds)]
        if jobs:
            await asyncio.gather(*jobs)

    def _platform(self, name: str) -> Dict[str, Any]:
        ex = self.kalshi if name == "kalshi" else self.poly
        # Real positions always count. A dry run adds its simulated positions on top, and since
        # simulated fills never debit the real balance, subtracts them from cash to mirror live.
        live = self.ledger.exposure(name, simulated=False)
        sim = self.ledger.exposure(name, simulated=True) if self.simulated else 0.0
        deployed = round(live + sim, 4)
        room = max(0.0, self.s.max_per_platform - deployed)
        cash = ex.balance
        if cash is not None:
            cash -= sim
        avail = room if cash is None else max(0.0, min(room, cash))
        return {**ex.describe(), "cap": self.s.max_per_platform, "deployed": deployed,
                "available": round(avail, 2), "limited_by": "balance" if cash is not None and cash < room else "cap"}

    def budgets(self) -> Tuple[float, float]:
        return self._platform("kalshi")["available"], self._platform("poly")["available"]

    # ------------------------------------------------------------- gates
    def eligibility(self, o: Opportunity) -> str:
        """'' if this arb may be executed now in the current mode, else the reason it won't be."""
        if self.mode == "alert_only":
            return "alert_only mode"
        if self.shutting_down:
            return "shutting down"
        if self.halted:
            return "auto-trade halted"
        if not o.is_arb or not o.contracts:
            return o.reject_reason or "not an arb"
        if not getattr(o, "bulletproof", False):
            return "not bulletproof: " + "; ".join(getattr(o, "strict_reasons", []) or ["strict checks pending"])
        if o.roi_pct < max(o.required_roi, self.s.min_roi_pct) or (o.annualized_roi or 0) < self.s.min_annualized_roi_pct:
            return f"ROI {o.roi_pct:.2f}% ({o.annualized_roi or 0:.1f}% annualized) below target"
        if self.s.require_pair_approval and not self.is_approved(o.pair.kalshi_ticker):
            return "needs your approval"
        if not self.is_approved(o.pair.kalshi_ticker) and self.risk_of(o) == "high":
            return "high rule-risk tags: approve the pair after reading both rulebooks"
        if o.pair.confidence < self.s.exec_min_confidence:
            return f"match confidence {o.pair.confidence:.0%} < {self.s.exec_min_confidence:.0%}"
        if self.ledger.open_pair(o.pair.kalshi_ticker, self.simulated):
            return "already holding this pair"
        last = self.last_exec.get(o.key)
        if last and time.time() - last < self.s.exec_cooldown:
            return f"cooldown ({int(self.s.exec_cooldown - (time.time() - last))}s left)"
        if self.mode == "live_auto":
            for ex in (self.kalshi, self.poly):
                if not ex.can_trade:
                    return f"{ex.platform} not ready: {ex.detail}"
                if ex.balance is None or time.time() - ex.balance_at > 3 * self.s.balance_refresh_seconds + 30:
                    return f"{ex.platform} balance is stale"
        return ""

    # ------------------------------------------------------------- per-scan hook
    async def on_scan(self, arbs: List[Opportunity], refresh: RefreshFn) -> None:
        if self.mode == "alert_only" or self.lock.locked():
            return
        for o in sorted(arbs, key=lambda x: -x.roi_pct):
            why = self.eligibility(o)
            if why:
                self._note_skip(o, why)
                continue
            # One trade per scan; shielded so shutdown/cancel can't interrupt it between legs.
            self._inflight = asyncio.ensure_future(self._run_locked(o, refresh))
            await asyncio.shield(self._inflight)
            return

    def _note_skip(self, o: Opportunity, why: str) -> None:
        prev = self._skip_logged.get(o.key)
        if prev and prev[0] == why and time.time() - prev[1] < 600:
            return
        self._skip_logged[o.key] = (why, time.time())
        log.info("[Execution] not executing %s %s (%.2f%%): %s", o.pair.kalshi_ticker, o.direction, o.roi_pct, why)

    async def _run_locked(self, o: Opportunity, refresh: RefreshFn) -> None:
        async with self.lock:
            try:
                await self._execute(o, refresh)
            except Exception as exc:
                log.exception("[Execution] unexpected error executing %s", o.key)
                self.halt(f"unexpected execution error: {exc!r}"[:200])
                await self.notifier.send_text("Execution error: auto-trade halted", f"{o.event}\n{exc!r}", critical=True)

    # ------------------------------------------------------------- one trade
    async def _execute(self, o: Opportunity, refresh: RefreshFn) -> None:
        fresh = await refresh(o)
        if fresh is None or not fresh.is_arb:
            log.info("[Execution] %s gone on re-check: %s", o.key, fresh.reject_reason if fresh else "books unavailable")
            return
        why = self.eligibility(fresh)
        if why:
            self._note_skip(fresh, why)
            return
        o = fresh
        k, p = o.kalshi, o.poly
        self.last_exec[o.key] = time.time()
        n = o.contracts
        rec: Dict[str, Any] = {
            "id": str(uuid.uuid4())[:8], "ts": time.time(), "mode": self.mode, "simulated": self.simulated,
            "key": o.key, "kalshi_ticker": o.pair.kalshi_ticker, "condition_id": o.pair.poly_condition_id,
            "event": o.event, "direction": o.direction, "contracts": n,
            "expected_cost": round(o.total_cost, 4), "expected_profit": round(o.profit, 4),
            "expected_roi_pct": round(o.roi_pct, 3), "status": "pending", "note": "", "settled": False,
            "kalshi": {"outcome": k.side, "limit": k.limit_price, "est_cost": round(k.cost, 4),
                       "est_fee": round(k.fee, 4), "filled": 0, "exposure": 0.0},
            "poly": {"outcome": p.side_label, "token_id": p.token_id, "amount": round(p.cost, 2),
                     "max_price": p.limit_price, "est_fee": round(p.fee, 5), "shares": 0.0, "exposure": 0.0},
        }
        k_body = v2_order_body(o.pair.kalshi_ticker, k.side, "buy", n, k.limit_price, "fill_or_kill")
        rec["kalshi"]["payload"] = k_body
        rec["poly"]["payload"] = {"token_id": p.token_id, "side": "BUY", "amount": f"{p.cost:.2f}",
                                  "max_price": f"{p.limit_price}", "order_type": "FOK"}

        if self.simulated:
            await self._dry_run(o, rec, k_body)
            return

        # ---------------- LIVE: leg 1 (Kalshi) ----------------
        trade_log.warning("[TRADE %s] LIVE leg 1 Kalshi %s BUY %s ×%d @ ≤$%.2f (ROI %.2f%%)",
                          rec["id"], o.pair.kalshi_ticker, k.side, n, k.limit_price, o.roi_pct)
        try:
            kres = await self.kalshi.place(k_body, k.side)
        except KalshiExecError as exc:
            return self._finish(rec, "aborted", f"leg 1 rejected by Kalshi, no exposure: {exc}")
        except KalshiAmbiguousError as exc:
            await self._critical(rec, "unknown", f"leg 1 state UNKNOWN after network error ({exc}). "
                                 f"Check Kalshi order {k_body['client_order_id']} manually.")
            return
        filled = int(math.floor(kres["fill_count"] + 1e-9))
        rec["kalshi"].update(order_id=kres["order_id"], client_order_id=k_body["client_order_id"], filled=filled,
                             avg_price=kres["avg_price"], fees=round(kres["fees"], 4))
        if filled <= 0:
            return self._finish(rec, "aborted", "leg 1 not filled (fill-or-kill), no exposure")
        k_avg = kres["avg_price"] if kres["avg_price"] is not None else k.limit_price
        k_fees = kres["fees"] or kalshi_taker_fee(filled, k_avg, self.s.kalshi_fee_rate)
        rec["kalshi"]["exposure"] = round(filled * k_avg + k_fees, 4)
        rec["status"] = "partial"
        self.ledger.add(rec)  # persist the exposure before touching leg 2

        # ---------------- LIVE: leg 2 (Polymarket) ----------------
        amount = p.cost if filled == n else ceil_cent(filled * p.avg_price)
        trade_log.warning("[TRADE %s] LIVE leg 2 Polymarket BUY \"%s\" $%.2f FOK max $%s (for %d shares)",
                          rec["id"], p.side_label, amount, p.limit_price, filled)
        try:
            pres = await self.poly.market_buy_fok(p.token_id, amount, p.limit_price)
        except PolyAmbiguousError as exc:
            await self._critical(rec, "unknown", f"leg 2 state UNKNOWN ({exc}). Kalshi leg holds {filled} "
                                 f"{k.side}. Check Polymarket before doing anything.")
            return
        except PolyExecError as exc:
            log.critical("[TRADE %s] LEG RISK: Polymarket leg failed (%s); flattening %d Kalshi %s",
                         rec["id"], exc, filled, k.side)
            await self._flatten(rec, o, filled, k_avg, f"leg 2 failed: {exc}")
            return
        shares = float(pres["shares"])
        rec["poly"].update(order_id=pres["order_id"], shares=shares, spent=round(pres["spent"], 4),
                           exposure=round(pres["spent"] + p.fee, 4), status=pres["status"])
        gap = filled - int(math.floor(shares + 1e-6))
        if gap >= 1:
            log.critical("[TRADE %s] LEG RISK: Polymarket filled %.2f of %d shares; flattening %d Kalshi",
                         rec["id"], shares, filled, gap)
            await self._flatten(rec, o, gap, k_avg, f"leg 2 short by {gap} shares", partial=True)
            return
        self._finish(rec, "filled", f"hedged: {filled} Kalshi {k.side} + {shares:.2f} Polymarket \"{p.side_label}\"")
        await self.notifier.send_text(f"Trade executed: {o.event[:200]}",
                                      f"{filled} contracts each side · expected profit ${o.profit:.2f} "
                                      f"({o.roi_pct:.2f}% ROI)\nKalshi {k.side} avg ${k_avg:.2f} · Polymarket "
                                      f"\"{p.side_label}\" ${pres['spent']:.2f}")
        await self.maybe_refresh_balances(force=True)

    async def _dry_run(self, o: Opportunity, rec: Dict[str, Any], k_body: Dict[str, Any]) -> None:
        k, p = o.kalshi, o.poly
        notes = []
        if self.kalshi.signer:
            self.kalshi.signer.headers("POST", self.kalshi.base_path + "/portfolio/events/orders")
            notes.append("Kalshi request signed OK")
        else:
            notes.append("Kalshi unsigned (no key)")
        try:
            notes.append("Polymarket " + await self.poly.sign_market_buy(p.token_id, p.cost, p.limit_price))
        except Exception as exc:
            notes.append(f"Polymarket signing failed: {exc!r}"[:160])
            log.error("[Execution] dry-run Polymarket signing failed: %r", exc)
        rec["kalshi"].update(filled=o.contracts, avg_price=round(k.avg_price, 4), exposure=round(k.stake, 4))
        rec["poly"].update(shares=float(o.contracts), spent=round(p.cost, 4), exposure=round(p.stake, 4))
        self._finish(rec, "simulated", "; ".join(notes))
        trade_log.warning("[TRADE %s] DRY RUN would place: Kalshi %s | Polymarket %s", rec["id"],
                          json.dumps(k_body), json.dumps(rec["poly"]["payload"]))
        await self.notifier.send_text(f"Dry run: {o.event[:200]}",
                                      f"Would buy {o.contracts}× Kalshi {k.side} @ ≤${k.limit_price:.2f} "
                                      f"(${k.stake:.2f}) + Polymarket \"{p.side_label}\" ${p.stake:.2f}\n"
                                      f"Expected profit ${o.profit:.2f} ({o.roi_pct:.2f}% ROI). Nothing was sent.")

    async def _flatten(self, rec: Dict[str, Any], o: Opportunity, n: int, k_avg: float, why: str,
                       partial: bool = False) -> None:
        """Sell back `n` Kalshi contracts immediately (reduce-only IOC with a price floor)."""
        k = o.kalshi
        floor = max(0.01, round(k_avg - self.s.flatten_max_slippage, 2))
        body = v2_order_body(o.pair.kalshi_ticker, k.side, "sell", n, floor, "immediate_or_cancel", reduce_only=True)
        rec["flatten"] = {"contracts": n, "floor": floor, "payload": body}
        try:
            res = await self.kalshi.place(body, k.side)
            sold = int(math.floor(res["fill_count"] + 1e-9))
        except (KalshiExecError, KalshiAmbiguousError) as exc:
            log.error("[Execution] flatten order failed: %r", exc)
            sold, res = 0, {"avg_price": None, "fees": 0.0, "error": str(exc)}
        proceeds = sold * (res.get("avg_price") or floor) - (res.get("fees") or 0.0)
        rec["flatten"].update(sold=sold, avg_price=res.get("avg_price"), proceeds=round(proceeds, 4))
        left = n - sold
        held = rec["kalshi"]["filled"] - sold
        per = rec["kalshi"]["exposure"] / max(1, rec["kalshi"]["filled"])
        rec["kalshi"]["exposure"] = round(held * per, 4)
        if left > 0:
            await self._critical(rec, "unhedged", f"{why}; could only sell {sold}/{n} Kalshi {k.side}. "
                                 f"{left} contracts are UNHEDGED on {o.pair.kalshi_ticker}.")
            return
        loss = n * per - proceeds
        status = "filled" if partial else "flattened"
        self._finish(rec, status, f"{why}; sold back {sold} Kalshi {k.side} (realised ≈ -${loss:.2f})")
        log.critical("[TRADE %s] leg risk contained: %s", rec["id"], rec["note"])
        await self.notifier.send_text(f"Leg 2 failed, Kalshi leg flattened: {o.event[:180]}", rec["note"], critical=True)
        recent = [t for t in self.ledger.trades if t.get("flatten") and not t.get("simulated")
                  and time.time() - t.get("ts", 0) < 3600]
        if len(recent) >= 2:  # repeated unwinds cost money each time; stop and let a human look
            self.halt(f"{len(recent)} leg-2 failures in the last hour (each one unwound at a loss)")
        await self.maybe_refresh_balances(force=True)

    async def _critical(self, rec: Dict[str, Any], status: str, msg: str) -> None:
        self._finish(rec, status, msg)
        log.critical("[TRADE %s] %s", rec["id"], msg)
        self.halt(f"trade {rec['id']}: {status}. Resolve manually, then re-enable")
        await self.notifier.send_text(f"CRITICAL: {status.upper()} position, auto-trade halted", f"{rec['event']}\n{msg}",
                                      critical=True)
        await self.maybe_refresh_balances(force=True)

    def _finish(self, rec: Dict[str, Any], status: str, note: str) -> None:
        rec["status"], rec["note"] = status, note
        if rec not in self.ledger.trades:
            self.ledger.trades.append(rec)
        self.ledger.save()
        lvl = logging.WARNING if status in ("filled", "simulated", "flattened") else logging.ERROR
        trade_log.log(lvl, "[TRADE %s] %s %s ×%d: %s · %s", rec["id"], "SIM" if rec["simulated"] else "LIVE",
                      rec["kalshi_ticker"], rec["contracts"], status.upper(), note)

    # ------------------------------------------------------------- settlement release
    async def release_settled(self, kalshi_status: Callable[[str], Awaitable[str]],
                              poly_resolved: Callable[[str], Awaitable[bool]]) -> None:
        """Free cap room for positions whose markets have settled on BOTH platforms."""
        changed = False
        for t in self.ledger.trades:
            if t.get("settled") or t.get("status") not in EXPOSED:
                continue
            try:
                ks = await kalshi_status(t["kalshi_ticker"])
                pr = await poly_resolved(t["condition_id"])
            except Exception as exc:
                log.warning("[Execution] settlement check for %s failed: %r", t["kalshi_ticker"], exc)
                continue
            if ks in ("settled", "finalized") and pr:
                t["settled"], t["settled_at"] = True, time.time()
                changed = True
                log.info("[Execution] trade %s settled on both platforms; cap room released", t["id"])
        if changed:
            self.ledger.save()

    def reset_simulation(self) -> int:
        n = self.ledger.clear_simulated()
        self.last_exec.clear()
        log.info("[Execution] cleared %d simulated trades", n)
        return n

    # ------------------------------------------------------------- dashboard
    def snapshot(self) -> Dict[str, Any]:
        trades = sorted(self.ledger.trades, key=lambda t: -t.get("ts", 0))[:40]
        return {
            "mode": self.mode, "modes": list(EXECUTION_MODES), "halted": self.halted, "halt_reason": self.halt_reason,
            "busy": self.lock.locked(), "cap": self.s.max_per_platform, "min_roi": self.s.min_roi_pct,
            "min_annualized": self.s.min_annualized_roi_pct,
            "require_approval": self.s.require_pair_approval, "min_confidence": self.s.exec_min_confidence,
            "kalshi": self._platform("kalshi"), "poly": self._platform("poly"),
            "trades": [_trade_view(t) for t in trades],
        }


def _leg_view(leg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: v for k, v in (leg or {}).items() if k != "payload"}


def _trade_view(t: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in t.items() if k not in ("kalshi", "poly", "flatten")}
    out["kalshi"], out["poly"] = _leg_view(t.get("kalshi")), _leg_view(t.get("poly"))
    if t.get("flatten"):
        out["flatten"] = _leg_view(t["flatten"])
    return out
