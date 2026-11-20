"""Async HTTP helpers: retry with exponential backoff + a simple rate limiter."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger("http")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# channel -> timestamps of recent HTTP 429s. A channel is "<host>|price" for the fast price loop, or
# "<host>|bulk" for catalog paging and everything else, so catalog throttling never slows price polling.
_THROTTLE: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=200))


def note_throttle(url: str, channel: str = "bulk") -> None:
    _THROTTLE[f"{urlparse(url).netloc}|{channel}"].append(time.time())


def throttle_count(host_part: str, window: float = 60.0, channel: Optional[str] = None) -> int:
    """429s from hosts containing `host_part` within `window` seconds (optionally one channel only)."""
    cutoff = time.time() - window
    return sum(1 for key, ts in _THROTTLE.items()
               if host_part in key.split("|")[0] and (channel is None or key.endswith("|" + channel))
               for t in ts if t >= cutoff)


class RateLimiter:
    """Spaces requests at most `rate` per second (shared across coroutines). Adaptive: every HTTP 429
    slows it down (up to 1 request per 2 s); successful requests gradually restore the configured rate."""

    def __init__(self, rate: float):
        self.base = 1.0 / rate if rate > 0 else 0.0
        self.interval = self.base
        self._lock = asyncio.Lock()
        self._next = 0.0

    def penalize(self) -> None:
        if self.base:
            self.interval = min(2.0, max(self.interval * 1.5, self.base * 1.5))

    def relax(self) -> None:
        if self.interval > self.base:
            self.interval = max(self.base, self.interval * 0.97)

    @property
    def slowed(self) -> bool:
        return self.interval > self.base * 1.05

    async def acquire(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = now + self.interval


def make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": "arb-scanner/1.0", "Accept": "application/json"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
    )


def _retry_after(resp: httpx.Response) -> Optional[float]:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Any = None,
    retries: int = 4,
    base_delay: float = 0.75,
    limiter: Optional[RateLimiter] = None,
    max_delay: float = 30.0,
    channel: str = "bulk",
) -> Any:
    """Request and decode JSON. Retries transport errors, 429 and 5xx. Raises httpx errors otherwise."""
    attempt = 0
    while True:
        if limiter:
            await limiter.acquire()
        delay: Optional[float] = None
        try:
            resp = await client.request(method, url, params=params, json=json_body)
            if resp.status_code == 429:
                note_throttle(url, channel)
                if limiter:
                    limiter.penalize()
            if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                delay = _retry_after(resp)
                log.debug("HTTP %s from %s (attempt %d)", resp.status_code, url, attempt + 1)
            else:
                resp.raise_for_status()
                if limiter:
                    limiter.relax()
                return resp.json()
        except (httpx.TransportError, httpx.DecodingError) as exc:
            if attempt >= retries:
                raise
            log.debug("Transport error %r on %s (attempt %d)", exc, url, attempt + 1)
        attempt += 1
        if delay is None:
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
        await asyncio.sleep(min(delay, max_delay))
