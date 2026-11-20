"""FastAPI web dashboard: serves static/index.html and a small JSON control API.

Bound to 127.0.0.1 by default. Host-header checking blocks DNS-rebinding, and every write endpoint
requires a JSON body, which a cross-site form post cannot send without a CORS preflight.
"""
from __future__ import annotations

import asyncio
import logging
import webbrowser
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import CATALOG_POLL_MIN, MATCH_INTERVAL_MIN, PRICE_POLL_MIN, ROOT
from scanner import ScannerService

log = logging.getLogger("server")
STATIC = ROOT / "static"


class SettingsIn(BaseModel):
    max_per_platform: Optional[float] = Field(None, ge=0, le=1_000_000)
    min_roi: Optional[float] = Field(None, ge=0, le=100)
    min_annualized: Optional[float] = Field(None, ge=0, le=100000)
    min_liquidity: Optional[float] = Field(None, ge=0, le=10_000_000)
    interval: Optional[float] = Field(None, ge=PRICE_POLL_MIN, le=3600)
    catalog_interval: Optional[float] = Field(None, ge=CATALOG_POLL_MIN, le=86400)
    match_interval: Optional[float] = Field(None, ge=MATCH_INTERVAL_MIN, le=86400)
    max_settle_diff_days: Optional[float] = Field(None, ge=0, le=3650)
    strict_match_confidence: Optional[float] = Field(None, ge=0, le=100)
    max_lockup_days: Optional[float] = Field(None, ge=1, le=36500)
    require_full_depth: Optional[bool] = None


class PairIn(BaseModel):
    kalshi_ticker: str = Field(..., min_length=1, max_length=200)


class PauseIn(BaseModel):
    paused: bool


class ModeIn(BaseModel):
    mode: str = Field(..., pattern="^(alert_only|dry_run|live_auto)$")
    confirm: str = ""  # must be "LIVE" to switch to live_auto


class HaltIn(BaseModel):
    reason: str = Field("Emergency stop pressed on the dashboard", max_length=200)


class EmptyIn(BaseModel):
    """Requires a JSON body, like every other write endpoint."""


class ShutdownIn(BaseModel):
    """Empty, but makes the endpoint require a JSON body like the other write endpoints."""


def create_app(svc: ScannerService, url: str, open_browser: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start()
        log.info("Dashboard running at %s", url)
        if open_browser:
            asyncio.get_running_loop().call_later(1.0, lambda: asyncio.ensure_future(_open(url)))
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(title="Arb Scanner", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def state():
        return svc.snapshot()

    @app.post("/api/settings")
    async def settings(body: SettingsIn):
        data = body.model_dump(exclude_none=True) if hasattr(body, "model_dump") else body.dict(exclude_none=True)
        svc.update_settings(**data)
        return svc.snapshot()

    @app.post("/api/pairs/block")
    async def block(body: PairIn):
        if not svc.set_blocked(body.kalshi_ticker, True):
            raise HTTPException(404, "unknown pair")
        return svc.snapshot()

    @app.post("/api/pairs/unblock")
    async def unblock(body: PairIn):
        if not svc.set_blocked(body.kalshi_ticker, False):
            raise HTTPException(404, "unknown pair")
        return svc.snapshot()

    @app.post("/api/pairs/approve")
    async def approve(body: PairIn):
        if not svc.set_approved(body.kalshi_ticker, True):
            raise HTTPException(404, "unknown or blocked pair")
        return svc.snapshot()

    @app.post("/api/pairs/unapprove")
    async def unapprove(body: PairIn):
        if not svc.set_approved(body.kalshi_ticker, False):
            raise HTTPException(404, "unknown pair")
        return svc.snapshot()

    @app.post("/api/execution/mode")
    async def exec_mode(body: ModeIn):
        if body.mode == "live_auto" and body.confirm != "LIVE":
            raise HTTPException(400, "switching to live_auto requires confirm='LIVE'")
        svc.set_execution_mode(body.mode)
        return svc.snapshot()

    @app.post("/api/execution/halt")
    async def exec_halt(body: HaltIn):
        svc.halt_execution(body.reason)
        return svc.snapshot()

    @app.post("/api/execution/resume")
    async def exec_resume(body: EmptyIn):
        svc.resume_execution()
        return svc.snapshot()

    @app.post("/api/execution/reset_simulation")
    async def exec_reset(body: EmptyIn):
        svc.reset_simulation()
        return svc.snapshot()

    @app.post("/api/rematch")
    async def rematch():
        if not svc.request_rematch():
            raise HTTPException(409, "a matching round is already running")
        return svc.snapshot()

    @app.post("/api/pause")
    async def pause(body: PauseIn):
        svc.set_paused(body.paused)
        return svc.snapshot()

    @app.post("/api/shutdown")
    async def shutdown(body: ShutdownIn):
        """Graceful stop (used by Stop_Scanner.bat): uvicorn exits, the lifespan stops the scanner,
        kills any Claude CLI call and saves the cache."""
        srv = getattr(app.state, "server", None)
        if srv is None:
            raise HTTPException(503, "shutdown is not available in this mode")
        log.info("Shutdown requested over the API")
        # Safety first: block new trades immediately (in memory, so a restart keeps your saved
        # kill-switch state); the lifespan exit then waits for any in-flight trade to finish.
        svc.execm.shutting_down = True
        # Let this response go out first; uvicorn then drains connections and runs the lifespan exit.
        asyncio.get_running_loop().call_later(0.3, lambda: setattr(srv, "should_exit", True))
        return {"ok": True}

    app.state.server = None  # set by main.serve() to the running uvicorn.Server
    return app


async def _open(url: str) -> None:
    try:
        await asyncio.to_thread(webbrowser.open, url)
    except Exception as exc:
        log.warning("Could not open a browser automatically (%r); visit %s", exc, url)
