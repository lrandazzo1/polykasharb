"""Runtime settings: .env values, overridden by CLI flags."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent

PRICE_POLL_MIN = 3.0  # seconds; floor for the fast price/orderbook loop
CATALOG_POLL_MIN = 60.0
MATCH_INTERVAL_MIN = 300.0


def _env(name: str, default: str) -> str:
    val = os.getenv(name)
    return default if val is None or val.strip() == "" else val.strip()


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(float(_env(name, str(default))))


@dataclass
class Settings:
    # --- endpoints -------------------------------------------------------
    kalshi_base_url: str = field(
        default_factory=lambda: _env("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
    )
    gamma_base_url: str = field(default_factory=lambda: _env("POLY_GAMMA_URL", "https://gamma-api.polymarket.com"))
    clob_base_url: str = field(default_factory=lambda: _env("POLY_CLOB_URL", "https://clob.polymarket.com"))

    # --- scanning --------------------------------------------------------
    # Three independent cadences (seconds):
    #   interval          price/orderbook re-pricing of already-matched pairs (fast, cheap batch calls)
    #   catalog_interval  full Kalshi + Polymarket market-list download (slow: minutes, many pages)
    #   match_interval    Claude CLI matching of new/changed markets (uses Claude Pro usage)
    interval: float = field(default_factory=lambda: max(PRICE_POLL_MIN, _env_float(
        "PRICE_POLL_INTERVAL_SEC", _env_float("SCAN_INTERVAL", 3))))
    catalog_interval: float = field(default_factory=lambda: max(CATALOG_POLL_MIN, _env_float(
        "CATALOG_POLL_INTERVAL_SEC", 300)))
    match_interval: float = field(default_factory=lambda: max(MATCH_INTERVAL_MIN, _env_float(
        "MATCH_INTERVAL_SEC", _env_float("MATCH_REFRESH_MINUTES", 15) * 60)))
    # Hard cap on total exposure per platform: no single leg, and no sum of open legs, may exceed it.
    max_per_platform: float = field(default_factory=lambda: _env_float("MAX_PER_PLATFORM_USD", 50))
    # Qualification is by ANNUALIZED ROI (net ROI × 365 / days until the later settlement date), with a
    # small raw net-ROI floor so 1-day contracts with microscopic spreads don't qualify on paper.
    # Per opportunity this is one required net ROI: max(MIN_NET_ROI, MIN_ANNUALIZED_ROI × days / 365).
    min_annualized_roi_pct: float = field(default_factory=lambda: _env_float("MIN_ANNUALIZED_ROI", 6.0))
    min_roi_pct: float = field(default_factory=lambda: _env_float("MIN_NET_ROI_PCT", _env_float("MIN_NET_ROI", 0.35)))
    min_liquidity_usd: float = field(default_factory=lambda: _env_float("MIN_TOP_LIQUIDITY_USD", 50))
    alert_cooldown: float = field(default_factory=lambda: _env_float("ALERT_COOLDOWN_SECONDS", 600))
    near_arb_count: int = field(default_factory=lambda: _env_int("WATCHLIST_COUNT", 25))
    # --- strict "bulletproof" qualification (all must pass for a LIVE ARBITRAGE alert / auto-trade) ---
    max_settle_diff_days: float = field(default_factory=lambda: _env_float("MAX_SETTLE_DIFF_DAYS", 1))
    strict_match_confidence: float = field(default_factory=lambda: _env_float("STRICT_MATCH_CONFIDENCE", 98))
    max_lockup_days: float = field(default_factory=lambda: _env_float("MAX_LOCKUP_DAYS", 90))
    require_full_depth: bool = field(default_factory=lambda: _env("REQUIRE_FULL_DEPTH", "1") not in ("0", "false", "no"))
    # strict rule verification by Claude: full rulebooks, five criteria; only for pairs with money on the table
    strict_verify_model: str = field(default_factory=lambda: _env("STRICT_VERIFY_MODEL", "sonnet"))
    strict_verify_max_per_hour: int = field(default_factory=lambda: _env_int("STRICT_VERIFY_MAX_PER_HOUR", 30))
    strict_verify_ttl_hours: float = field(default_factory=lambda: _env_float("STRICT_VERIFY_TTL_HOURS", 72))

    # --- fees ------------------------------------------------------------
    kalshi_fee_rate: float = field(default_factory=lambda: _env_float("KALSHI_TAKER_FEE_RATE", 0.07))
    # "auto" = read each market's feeSchedule from Gamma; or a number to force a rate for all markets
    poly_fee_rate_override: str = field(default_factory=lambda: _env("POLY_TAKER_FEE_RATE", "auto"))

    # --- HTTP ------------------------------------------------------------
    http_timeout: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT", 20))
    http_retries: int = field(default_factory=lambda: _env_int("HTTP_RETRIES", 4))
    # Kalshi's Basic tier allows ~20 reads/s; catalog paging and price polling get separate limiters
    kalshi_rps: float = field(default_factory=lambda: _env_float("KALSHI_REQUESTS_PER_SECOND", 10))
    kalshi_price_rps: float = field(default_factory=lambda: _env_float("KALSHI_PRICE_REQUESTS_PER_SECOND", 4))
    # Safety cap only (the full open catalog is ~205 pages of 1,000): never a topic filter
    kalshi_max_pages: int = field(default_factory=lambda: _env_int("KALSHI_MAX_PAGES", 1000))
    poly_max_pages: int = field(default_factory=lambda: _env_int("POLY_MAX_PAGES", 3000))

    # --- matcher (Claude CLI) ---------------------------------------------
    # "" = auto-discover (native installer, PATH, npm shim). Set a full path to force one.
    claude_cli: str = field(default_factory=lambda: _env("CLAUDE_CLI_PATH", ""))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "haiku"))
    claude_extra_args: str = field(default_factory=lambda: _env("CLAUDE_EXTRA_ARGS", ""))
    claude_timeout: float = field(default_factory=lambda: _env_float("CLAUDE_TIMEOUT_SECONDS", 180))
    claude_retries: int = field(default_factory=lambda: _env_int("CLAUDE_RETRIES", 2))
    match_batch_size: int = field(default_factory=lambda: _env_int("MATCH_BATCH_SIZE", 25))
    match_max_batches: int = field(default_factory=lambda: _env_int("MATCH_MAX_BATCHES_PER_REFRESH", 20))
    match_candidates: int = field(default_factory=lambda: _env_int("MATCH_CANDIDATES_PER_MARKET", 3))
    match_min_similarity: float = field(default_factory=lambda: _env_float("MATCH_MIN_SIMILARITY", 0.35))
    match_max_date_gap_days: float = field(default_factory=lambda: _env_float("MATCH_MAX_DATE_GAP_DAYS", 31))
    # Long-dated (novelty / politics) markets: also allow a settlement-date gap up to this fraction of
    # the time remaining, since "before 2028" style contracts often list end dates months apart
    match_date_gap_fraction: float = field(default_factory=lambda: _env_float("MATCH_DATE_GAP_FRACTION", 0.25))
    # Spread each round's Claude budget across categories (and series within one) instead of letting the
    # highest-similarity topic (usually sports props) take all of it
    match_stratify: bool = field(default_factory=lambda: _env("MATCH_STRATIFY", "1") not in ("0", "false", "no"))
    match_min_confidence: float = field(default_factory=lambda: _env_float("MATCH_MIN_CONFIDENCE", 0.85))
    match_cache_ttl_hours: float = field(default_factory=lambda: _env_float("MATCH_CACHE_TTL_HOURS", 168))
    cache_path: Path = field(default_factory=lambda: ROOT / _env("MATCH_CACHE_FILE", "matches_cache.json"))

    # --- alerts ------------------------------------------------------------
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL", ""))
    terminal_bell: bool = field(default_factory=lambda: _env("TERMINAL_BELL", "1") not in ("0", "false", "no"))

    # --- execution ---------------------------------------------------------
    # alert_only (default) | dry_run | live_auto
    execution_mode: str = field(default_factory=lambda: _env("EXECUTION_MODE", "alert_only").lower())
    # live_auto only trades pairs you approved on the dashboard, with at least this match confidence
    require_pair_approval: bool = field(
        default_factory=lambda: _env("REQUIRE_PAIR_APPROVAL", "1") not in ("0", "false", "no"))
    exec_min_confidence: float = field(default_factory=lambda: _env_float("EXECUTION_MIN_CONFIDENCE", 0.95))
    exec_cooldown: float = field(default_factory=lambda: _env_float("EXECUTION_COOLDOWN_SECONDS", 600))
    # Unwinding an orphaned Kalshi leg: sell no lower than (fill price − this many dollars per contract)
    flatten_max_slippage: float = field(default_factory=lambda: _env_float("FLATTEN_MAX_SLIPPAGE", 0.10))
    balance_refresh_seconds: float = field(default_factory=lambda: _env_float("BALANCE_REFRESH_SECONDS", 30))
    # Kalshi API key (Settings → API keys on kalshi.com). Private key as a PEM file path or inline PEM.
    kalshi_key_id: str = field(default_factory=lambda: _env("KALSHI_API_KEY_ID", ""))
    kalshi_private_key_path: str = field(default_factory=lambda: _env("KALSHI_PRIVATE_KEY_PATH", ""))
    kalshi_private_key_pem: str = field(default_factory=lambda: _env("KALSHI_PRIVATE_KEY", ""))
    # Polymarket: EVM signer key + the existing wallet address that holds your pUSD (required, so the
    # SDK never deploys a new wallet on its own)
    poly_private_key: str = field(default_factory=lambda: _env("POLY_PRIVATE_KEY", ""))
    poly_wallet_address: str = field(default_factory=lambda: _env("POLY_WALLET_ADDRESS", ""))
    poly_geoblock_url: str = field(
        default_factory=lambda: _env("POLY_GEOBLOCK_URL", "https://polymarket.com/api/geoblock"))
    ledger_path: Path = field(default_factory=lambda: ROOT / _env("TRADES_FILE", "trades.json"))
    exec_state_path: Path = field(default_factory=lambda: ROOT / _env("EXECUTION_STATE_FILE", "execution_state.json"))
    # --mock only: simulated balances and failure injection for the mock exchange
    mock_kalshi_balance: float = field(default_factory=lambda: _env_float("MOCK_KALSHI_BALANCE", 45))
    mock_poly_balance: float = field(default_factory=lambda: _env_float("MOCK_POLY_BALANCE", 60))
    # none | leg2_reject (Polymarket leg fails, Kalshi leg is flattened) | unhedged (flatten fails too)
    mock_exec_fail: str = field(default_factory=lambda: _env("MOCK_EXEC_FAIL", "none").lower())

    # --- modes -------------------------------------------------------------
    mock: bool = False
    live_matcher_in_mock: bool = False
    once: bool = False
    host: str = field(default_factory=lambda: _env("WEB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("WEB_PORT", 8000))
    open_browser: bool = True
    log_path: Path = field(default_factory=lambda: ROOT / _env("LOG_FILE", "scanner.log"))


EXECUTION_MODES = ("alert_only", "dry_run", "live_auto")
