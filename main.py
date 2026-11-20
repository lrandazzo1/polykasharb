"""Kalshi ⇄ Polymarket arbitrage scanner — entry point.

    python main.py                       # live scanner + web dashboard at http://localhost:8000
    python main.py --mock                # dashboard on bundled sample payloads (no network, no Claude usage)
    python main.py --mock --live-matcher # sample payloads, matched by the real Claude CLI
    python main.py --mock --once         # headless: one scan, print results, exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import CATALOG_POLL_MIN, EXECUTION_MODES, MATCH_INTERVAL_MIN, PRICE_POLL_MIN, ROOT, Settings
from notifier import LogBuffer, format_alert


def parse_args(argv=None) -> Settings:
    s = Settings()
    ap = argparse.ArgumentParser(description="Kalshi ⇄ Polymarket arbitrage scanner (web dashboard)")
    ap.add_argument("--mock", action="store_true", help="use bundled sample payloads (offline)")
    ap.add_argument("--live-matcher", action="store_true",
                    help="with --mock: resolve the sample markets with the real Claude CLI instead of canned answers")
    ap.add_argument("--interval", "--price-interval", dest="interval", type=float, default=s.interval,
                    help=f"seconds between price/orderbook passes, min {PRICE_POLL_MIN:g} (default %(default)s)")
    ap.add_argument("--catalog-interval", type=float, default=s.catalog_interval,
                    help=f"seconds between full market-list downloads, min {CATALOG_POLL_MIN:g} (default %(default)s)")
    ap.add_argument("--match-interval", type=float, default=s.match_interval,
                    help=f"seconds between Claude matching rounds, min {MATCH_INTERVAL_MIN:g} (default %(default)s)")
    ap.add_argument("--max-per-platform", type=float, default=s.max_per_platform,
                    help="hard cap on exposure per platform, USD (default %(default)s)")
    ap.add_argument("--min-roi", "--min-profit", dest="min_roi", type=float, default=s.min_roi_pct,
                    help="raw net ROI floor %% after all fees (default %(default)g)")
    ap.add_argument("--min-annualized", type=float, default=s.min_annualized_roi_pct,
                    help="minimum annualized ROI %% (net ROI × 365 / days to settle; default %(default)g)")
    ap.add_argument("--mode", choices=EXECUTION_MODES, default=s.execution_mode,
                    help="execution mode (default %(default)s, from EXECUTION_MODE)")
    ap.add_argument("--min-liquidity", type=float, default=s.min_liquidity_usd,
                    help="minimum top-of-book USD on each leg (default %(default)s)")
    ap.add_argument("--match-refresh", type=float, default=None,
                    help="(legacy) minutes between Claude matching rounds; same as --match-interval × 60")
    ap.add_argument("--host", default=s.host, help="dashboard bind address (default %(default)s)")
    ap.add_argument("--port", type=int, default=s.port, help="dashboard port (default %(default)s)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    ap.add_argument("--once", action="store_true", help="headless: match, run a single scan, print results, exit")
    a = ap.parse_args(argv)

    s.mock, s.live_matcher_in_mock, s.once = a.mock, a.live_matcher, a.once
    s.interval = max(PRICE_POLL_MIN, a.interval)
    s.catalog_interval = max(CATALOG_POLL_MIN, a.catalog_interval)
    s.match_interval = max(MATCH_INTERVAL_MIN, a.match_refresh * 60 if a.match_refresh else a.match_interval)
    s.max_per_platform = a.max_per_platform
    s.min_roi_pct = a.min_roi
    s.min_annualized_roi_pct = a.min_annualized
    s.execution_mode = a.mode
    s.min_liquidity_usd = a.min_liquidity
    s.host, s.port, s.open_browser = a.host, a.port, not a.no_browser
    if s.mock:
        s.cache_path = ROOT / "matches_cache.mock.json"  # never pollute the real cache
        s.log_path = ROOT / "scanner.mock.log"
        s.ledger_path = ROOT / "trades.mock.json"
        s.exec_state_path = ROOT / "execution_state.mock.json"
    return s


def setup_logging(s: Settings, logbuf: LogBuffer) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    fh = RotatingFileHandler(s.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    handlers = [fh, logbuf]
    if sys.stdout is not None:  # pythonw.exe (Launch_Scanner.bat) has no console: log to file only
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        handlers.append(sh)
    logbuf.setLevel(logging.INFO)
    for h in handlers:
        root.addHandler(h)
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:  # Windows consoles default to cp1252; keep ⇄/— in log lines from crashing the handler
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


async def run_once(s: Settings, logbuf: LogBuffer) -> None:
    from scanner import ScannerService

    svc = ScannerService(s, logbuf)
    await svc.run_once()
    ex = svc.execm.snapshot()
    for name in ("kalshi", "poly"):
        v = ex[name]
        bal = "n/a" if v["balance"] is None else f"${v['balance']:.2f}"
        print(f"{name:>6}: {v['status']} · balance {bal} · deployed ${v['deployed']:.2f}/${v['cap']:.2f} · {v['detail']}")
    print(f"\n=== {len(svc.arbs)} BULLETPROOF arbs (all strict checks pass) · {len(svc.matcher.pairs(svc.kalshi, svc.poly))} matched pairs ===")
    for o in svc.arbs:
        print(format_alert(o))
    print(f"\n=== watchlist / rule-risk spreads (top {len(svc.near)}) ===")
    for o in svc.near:
        eff = o.effective_cost if o.contracts else o.top_effective_cost
        print(f"  {(1 - eff) * 100:+6.2f}%  K {o.kalshi_side} @{o.kalshi_top_price:.2f} + P {o.poly_outcome} "
              f"@{o.poly_top_price:.3f}  {o.event[:60]}")
        for r in o.strict_reasons:
            print(f"           - {r}")


async def serve(s: Settings, logbuf: LogBuffer) -> None:
    import uvicorn

    from scanner import ScannerService
    from server import create_app

    url = f"http://{'localhost' if s.host in ('127.0.0.1', '0.0.0.0', '::') else s.host}:{s.port}"
    svc = ScannerService(s, logbuf)
    app = create_app(svc, url, s.open_browser)
    # timeout_graceful_shutdown: an open dashboard tab polls every second and would otherwise keep
    # uvicorn "waiting for connections to close" forever; after 5 s it proceeds to the clean lifespan stop.
    config = uvicorn.Config(app, host=s.host, port=s.port, log_level="info", access_log=False,
                            log_config=None, lifespan="on", timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    app.state.server = server  # lets POST /api/shutdown (Stop_Scanner.bat) stop it gracefully
    # PID file so Stop_Scanner.bat can force-kill a scanner that stops answering HTTP.
    pid_path = ROOT / ("scanner.mock.pid" if s.mock else "scanner.pid")
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    try:
        # Server.serve() on our own loop keeps the default Proactor loop on Windows, which the
        # Claude CLI subprocess needs (uvicorn.run() can switch to a selector loop).
        await server.serve()
    finally:
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    s = parse_args(argv)
    logbuf = LogBuffer()
    setup_logging(s, logbuf)
    try:
        asyncio.run(run_once(s, logbuf) if s.once else serve(s, logbuf))
    except KeyboardInterrupt:
        pass
    except Exception:  # e.g. bad EXECUTION_MODE or an unreadable trades.json: make it visible in scanner.log
        logging.getLogger("main").exception("Fatal error, scanner not started")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
