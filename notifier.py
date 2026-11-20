"""Alerts (Discord webhook + terminal bell) and an in-memory log buffer for the web dashboard."""
from __future__ import annotations

import logging
import time
import sys
from collections import deque
from typing import Any, Deque, Dict, List

import httpx

from config import Settings
from http_utils import request_json
from models import Opportunity

log = logging.getLogger("notifier")


def _short(cid: str) -> str:
    return f"{cid[:10]}…{cid[-6:]}" if len(cid) > 20 else cid


def format_alert(o: Opportunity) -> str:
    k, p = o.kalshi, o.poly
    return (
        f"ARB: {o.event}  |  Net ROI {o.roi_pct:.2f}%\n"
        f"  Action 1 · Kalshi {k.market_id}: BUY {k.side} ×{k.contracts} @ limit ${k.limit_price:.2f} "
        f"(avg ${k.avg_price:.4f}) = ${k.stake:.2f} stake (incl. ${k.fee:.2f} fee) · top liquidity ${k.top_liquidity_usd:,.0f}\n"
        f"  Action 2 · Polymarket {p.market_id}: BUY \"{p.side_label}\" ×{p.contracts} @ limit ${p.limit_price:.3f} "
        f"(avg ${p.avg_price:.4f}) = ${p.stake:.2f} stake (incl. ${p.fee:.4f} fee) · top liquidity ${p.top_liquidity_usd:,.0f}\n"
        f"  Guaranteed net profit ${o.profit:.2f} on ${o.total_cost:.2f} deployed (pays ${o.payout:.2f} either way)"
    )


class Notifier:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._last_sent: Dict[str, float] = {}
        self.sent_count = 0

    async def maybe_alert(self, opps: List[Opportunity]) -> int:
        now = time.time()
        sent = 0
        for o in opps:
            if now - self._last_sent.get(o.key, 0) < self.s.alert_cooldown:
                continue
            self._last_sent[o.key] = now
            sent += 1
            self.sent_count += 1
            log.warning("%s", format_alert(o))
            if self.s.terminal_bell and sys.stdout is not None:  # None under pythonw (no console)
                sys.stdout.write("")
                sys.stdout.flush()
            if self.s.discord_webhook_url:
                await self._discord(o)
        # forget stale keys so memory stays bounded
        self._last_sent = {k: t for k, t in self._last_sent.items() if now - t < self.s.alert_cooldown * 3}
        return sent

    async def _discord(self, o: Opportunity) -> None:
        k, p = o.kalshi, o.poly
        embed = {
            "title": f"Arbitrage: {o.event[:220]}",
            "description": f"**Net ROI {o.roi_pct:.2f}%** · guaranteed **${o.profit:.2f}** on ${o.total_cost:.2f}",
            "color": 0x2ECC71,
            "fields": [
                {
                    "name": "Action 1 — Kalshi",
                    "value": (
                        f"`{k.market_id}`\nBUY **{k.side}** ×{k.contracts} @ limit **${k.limit_price:.2f}**\n"
                        f"Stake **${k.stake:.2f}** (fee ${k.fee:.2f})\nTop liquidity ${k.top_liquidity_usd:,.0f}"
                    ),
                    "inline": True,
                },
                {
                    "name": "Action 2 — Polymarket",
                    "value": (
                        f"`{p.market_id}`\nBUY **{p.side_label}** ×{p.contracts} @ limit **${p.limit_price:.3f}**\n"
                        f"Stake **${p.stake:.2f}** (fee ${p.fee:.4f})\nTop liquidity ${p.top_liquidity_usd:,.0f}"
                    ),
                    "inline": True,
                },
                {"name": "Payout either way", "value": f"${o.payout:.2f}", "inline": False},
            ],
            "footer": {"text": f"match confidence {o.pair.confidence:.2f} · verify rules before trading"},
        }
        try:
            await request_json(self.http, "POST", self.s.discord_webhook_url,
                               json_body={"username": "Arb Scanner", "embeds": [embed]}, retries=2)
        except (httpx.HTTPError, ValueError) as exc:
            # Discord returns 204 No Content on success -> JSON decode error is expected and harmless
            if not isinstance(exc, ValueError):
                log.error("Discord webhook failed: %r", exc)


    async def send_text(self, title: str, text: str, critical: bool = False) -> None:
        """Execution events (fills, dry runs, leg-risk alerts) to Discord. Bell rings on critical ones."""
        if critical and self.s.terminal_bell and sys.stdout is not None:
            sys.stdout.write("")
            sys.stdout.flush()
        if not self.s.discord_webhook_url:
            return
        embed = {"title": title[:250], "description": text[:3900], "color": 0xE74C3C if critical else 0x3498DB}
        body = {"username": "Arb Scanner", "embeds": [embed]}
        if critical:
            body["content"] = "@here CRITICAL: check your positions"
        try:
            await request_json(self.http, "POST", self.s.discord_webhook_url, json_body=body, retries=2)
        except (httpx.HTTPError, ValueError) as exc:
            if not isinstance(exc, ValueError):  # 204 No Content -> JSON decode error is expected
                log.error("Discord webhook failed: %r", exc)


class LogBuffer(logging.Handler):
    """Keeps the most recent log records for the dashboard's event feed."""

    def __init__(self, maxlen: int = 40):
        super().__init__()
        self.records: Deque[Dict[str, Any]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # malformed log args must never break logging
            msg = str(record.msg)
        self.records.append({
            "t": record.created, "level": record.levelname, "name": record.name,
            "msg": msg.splitlines()[0][:300] if msg else "",
        })
