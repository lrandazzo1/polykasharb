# Kalshi ⇄ Polymarket Arbitrage Scanner

Continuously scans open Kalshi and Polymarket markets for equivalent contracts. It confirms matches
with your local **Claude Code CLI**, which runs on your Claude Pro subscription and needs no API key.
For each pair it prices both cross-directions after both platforms' fees and sizes the trade inside
a strict **per-platform bankroll** (default **$50 on Kalshi + $50 on Polymarket**). It reports
opportunities whose locked-in return beats **6% annualized** (with a 0.35% raw net-ROI floor). It can alert you, simulate the trades, or, if you
explicitly arm it, place both legs itself. Everything runs in a local web dashboard at
**http://localhost:8000**.

> **Read [Risks](#risks-this-tool-cannot-remove) before enabling `live_auto`.** Automated trading
> can lose money: one leg can fill without the other, and two "identical" contracts can settle
> differently. This tool is not financial advice.

```
kalshi_client.py       Kalshi v2 REST, read-only: open markets (cursor pagination) + orderbooks
polymarket_client.py   Polymarket Gamma (metadata) + CLOB (POST /books), read-only
matcher.py             TF-IDF blocking + game-slug matching → Claude CLI confirmation → matches_cache.json
arb_engine.py          fee models, order-book walking, per-platform-budget sizing, ROI threshold
kalshi_executor.py     signed Kalshi requests: cash balance, V2 orders, cancels
polymarket_executor.py Polymarket SDK (polymarket-client): pUSD balance, FOK orders, geoblock check
execution.py           the three execution modes, caps, safety gates, leg-risk handling, trades.json
mock_exchange.py       simulated exchanges for --mock (exercise live_auto with no money)
scanner.py             the background loops + live controls
server.py              FastAPI app: dashboard + JSON control API
static/index.html      the dashboard (Tailwind + vanilla JS from CDN; no build step)
notifier.py            Discord webhook, terminal bell, log feed
main.py                CLI flags; starts the web server and opens your browser
mock/                  sample payloads in the real API shapes + canned Claude answers
```

## Setup

Requires Python 3.9+ for scanning, alerts, dry runs and Kalshi trading. **Polymarket order
placement needs Python 3.11+** (see [Polymarket setup](#polymarket-setup)).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt     # httpx, python-dotenv, fastapi, uvicorn, cryptography
copy .env.example .env              # then edit: Discord webhook, keys, mode, caps
```

For live matching, install Claude Code with the native Windows installer and log in once with your
Pro account:

```powershell
irm https://claude.ai/install.ps1 | iex
& "$env:USERPROFILE\.local\bin\claude.exe" auth login     # only if not already logged in
```

You don't need to fix PATH. The scanner finds the CLI by itself (`claude_cli.py`). It looks for, in
order: `CLAUDE_CLI_PATH`, `%USERPROFILE%\.local\bin\claude.exe`,
`%LOCALAPPDATA%\Programs\claude\claude.exe`, `claude` on PATH, then `%APPDATA%\npm\claude.cmd`. It
probes each with `claude --version` and skips installs that don't run, such as an npm package whose
native binary never downloaded. It also reads `claude auth status` to confirm you're logged in.

The matcher runs `claude -p --model haiku --output-format json` with no console window and sends the
prompt on stdin. It removes `ANTHROPIC_API_KEY` from the child process's environment, so the CLI
always uses your subscription login. If the CLI is missing, not logged in, usage-limited or timing
out, the dashboard shows a red banner saying what to do, and `scanner.log` records the same message.
**Controls → Matching** shows which CLI is in use and its login state.

## Start it

```powershell
python main.py --mock              # dashboard on sample data + a simulated exchange
python main.py                     # live data; EXECUTION_MODE from .env (default alert_only)
```

`python main.py` starts the scanner and the web server together, then opens the dashboard. Press
**Ctrl+C** to stop. Shutdown blocks new trades, waits for an in-flight trade to finish both legs,
kills any running Claude CLI call and saves the match cache.

| Flag | Default | Purpose |
|---|---|---|
| `--mock` | off | Sample payloads and a simulated exchange: no network, no money, no Claude usage |
| `--live-matcher` | off | With `--mock`: match the samples with the real Claude CLI |
| `--mode` | `alert_only` | `alert_only`, `dry_run` or `live_auto` (overrides `EXECUTION_MODE`) |
| `--max-per-platform` | 50 | Hard exposure cap per platform, USD (also adjustable live) |
| `--min-annualized` | 6.0 | Minimum annualized ROI %: net ROI × 365 / days to the later settlement (also adjustable live) |
| `--min-roi` | 0.35 | Raw net ROI floor % after all fees (also adjustable live). `--min-profit` still works as an alias |
| `--min-liquidity` | 50 | Minimum top-of-book USD on each leg (also adjustable live) |
| `--interval` / `--price-interval` | 3 | Seconds between price/orderbook passes, min 3 (also adjustable live) |
| `--catalog-interval` | 300 | Seconds between full market-list downloads, min 60 (also adjustable live) |
| `--match-interval` | 900 | Seconds between Claude matching rounds, min 300 (also adjustable live). `--match-refresh` (minutes) still works |
| `--port` / `--host` | 8000 / 127.0.0.1 | Where the dashboard listens |
| `--no-browser` | off | Don't open a browser tab automatically |
| `--once` | off | Headless: one match round + one scan, print results, exit. Never executes trades |

Settings changed in the dashboard last until you restart; `.env` and the flags set the defaults.

### Desktop launchers (no terminal window)

Two scripts on the Desktop run the scanner in the background with `.venv\Scripts\pythonw.exe`:

* **`Launch_Scanner.bat`** starts it and opens http://localhost:8000 once it answers. If it's
  already running, it just opens the dashboard. If startup fails, a popup points to `scanner.log`.
* **`Stop_Scanner.bat`** calls `POST /api/shutdown`, the same safe stop as Ctrl+C: auto-trading is
  blocked immediately, any trade in flight finishes both legs, and the cache is saved. Only if the
  scanner doesn't answer is it force-killed (by the PID in `scanner.pid`).

Both have the project path and port 8000 at the top. Add launch flags in `set "ARGS="`.

## Execution modes

Set `EXECUTION_MODE` in `.env`, pass `--mode`, or switch on the dashboard (**Controls → Execution
mode**).

| Mode | What it does | Orders sent? |
|---|---|---|
| `alert_only` (default) | Finds arbs ≥ `MIN_ANNUALIZED_ROI` (and ≥ `MIN_NET_ROI_PCT` net), sizes them inside the caps and your balances, alerts you (dashboard + Discord) | Never |
| `dry_run` | Runs the full execution path: fresh order books, re-sizing, balance checks, the exact order payloads, and request signing when keys are set. Records **simulated** fills that count against the caps (and against your balance), so it behaves like live | Never |
| `live_auto` | Places both legs when every safety gate passes | **Yes, real money** |

`live_auto` **always starts disarmed**, both on startup with no saved state and when you switch to it
on the dashboard (which also needs you to type `LIVE`). Press **Enable auto-trade** to arm it. The
on/off state is saved in `execution_state.json`. An emergency stop survives restarts, and so does a
halt caused by a problem.

### Safety gates (every one must pass before a trade)

0. The opportunity is **bulletproof** (see [Bulletproof arbitrage](#bulletproof-arbitrage)). Only
   bulletproof opportunities are alerted or traded.
1. Annualized ROI ≥ `MIN_ANNUALIZED_ROI` (6%) and net ROI ≥ `MIN_NET_ROI_PCT` (0.35%), **after all
   fees on both platforms**. Both top-of-book
   levels ≥ `MIN_TOP_LIQUIDITY_USD`.
2. Each leg ≤ the smaller of: (`MAX_PER_PLATFORM_USD` − your open positions on that platform) and
   that platform's **live cash balance**. Open positions are this tool's own unsettled trades in
   `trades.json`. Their cap room is released only when both markets have **settled**.
3. You **approved** the pair on the dashboard (`REQUIRE_PAIR_APPROVAL=1`). The approval is tied to
   the exact Polymarket market; if a re-match changes the pair, it no longer applies.
4. Claude's match confidence ≥ `EXECUTION_MIN_CONFIDENCE` (0.95).
5. No open position on that pair already, and no trade on that pair/direction within
   `EXECUTION_COOLDOWN_SECONDS`.
6. Live only: both platforms connected and allowed to trade, and balances fresher than ~2 minutes.
7. Right before sending, both order books are **re-fetched and the trade re-priced**. If it no longer
   clears every gate, nothing is sent.

At most one trade runs at a time, and at most one per scan.

### How a live trade is placed (and leg-risk protection)

1. **Leg 1, Kalshi:** fill-or-kill limit BUY at the worst order-book level the sizing walked. If
   it isn't filled, the trade stops with no exposure.
2. **Leg 2, Polymarket:** fill-or-kill market BUY of the matching outcome, capped at its limit
   price.
3. **If leg 2 fails**, the filled Kalshi contracts are **sold back immediately** with a reduce-only
   order (IOC) floored at fill price − `FLATTEN_MAX_SLIPPAGE`. You get a CRITICAL log line and an
   `@here` Discord alert. Two such unwinds within an hour halt auto-trading.
4. If the unwind can't complete (**UNHEDGED**), or either leg's state is **unknown** after a network
   error, auto-trading **halts** and you get a CRITICAL alert. Check both accounts yourself before
   re-enabling.

Order submissions are **never retried automatically**, because a blind retry could double a position.
After an ambiguous Kalshi error, the order is looked up by its unique `client_order_id` instead.

### Emergency stop

The red **■ Emergency stop** button in the header (shown whenever execution is armed) halts new
trades instantly and persists across restarts. `Stop_Scanner.bat` / `POST /api/shutdown` also blocks
trading immediately before shutting down.

## Kalshi setup (balances + orders)

1. Create an API key in your Kalshi account settings
   ([guide](https://docs.kalshi.com/getting_started/api_keys)). Kalshi gives you a **key ID** and a
   **private key** file. The private key is shown only once.
2. Store the private key file somewhere private, then set in `.env`:
   ```
   KALSHI_API_KEY_ID=your-key-id
   KALSHI_PRIVATE_KEY_PATH=C:\Users\you\secrets\kalshi.key
   ```
   Both Ed25519 and RSA keys are supported. The key needs the `read` and `write::trade` scopes.
3. Restart. The dashboard's Kalshi card shows **Connected** and your cash balance.

To rehearse against Kalshi's **demo exchange** with demo keys, set
`KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2`. That base URL also applies to market
scanning, so use it only for testing.

Technical notes: requests are signed as `timestamp_ms + METHOD + /trade-api/v2/path` (no query
string). Orders use Kalshi's V2 endpoint `POST /portfolio/events/orders`; the legacy
`/portfolio/orders` write endpoints return `410 Gone`.

## Polymarket setup

**Check eligibility first.** Polymarket's international exchange does not accept new orders from
several regions, **including the United States** (close-only). The app checks
`https://polymarket.com/api/geoblock` at startup and before every order. When it reports `blocked`,
Polymarket execution stays disabled and the dashboard says so. Scanning, alerts and dry runs keep
working. Don't try to work around the block; it is a condition of using the platform.

If you are in an eligible region:

1. Use **Python 3.11 or newer**. Polymarket's official SDK, `polymarket-client`, requires it; the
   older `py-clob-client` was archived in May 2026 and no longer works. Recreate the venv with a
   3.11+ Python and install:
   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt polymarket-client
   ```
2. In `.env`, set `POLY_PRIVATE_KEY` (the key that signs for your Polymarket account) and
   `POLY_WALLET_ADDRESS` (your **existing** Polymarket wallet that holds pUSD). The wallet address is
   required so the SDK never deploys a new wallet on its own.
3. Your wallet needs pUSD collateral and trading approvals (allowances). Polymarket sets these up
   when you first trade on polymarket.com. The dashboard shows a clear error if they're missing.
4. Restart. The Polymarket card shows **Connected**, your pUSD balance and the region status.

**Timing note:** Polymarket starts moving markets to its new "V2" contracts and pUSD collateral in
phases from **Nov 23, 2026**. This integration uses the SDK's `token_id` path, which works for
current (CTF) markets. **Run `dry_run` first** to confirm signing works for the markets you care
about before arming `live_auto`.

## Bulletproof arbitrage

An opportunity is shown under **Live arbitrage**, alerted, and eligible for auto-trading only when
**all** of these pass. Everything else goes to the Watchlist with the reasons:

| Check | Setting (default) | How it's measured |
|---|---|---|
| Annualized ROI | `MIN_ANNUALIZED_ROI` (6%) | Net ROI × 365 / days until the **later** settlement date (min 1 day). Both books are walked level by level up to your per-platform budget, so ROI is priced at the **VWAP** of the actual fill, with both platforms' fees |
| Net ROI floor | `MIN_NET_ROI_PCT` (0.35%) | A raw minimum, so a 1-day contract with a $0.02 spread (huge on paper once annualized) doesn't qualify after fees and slippage. Together the two rules mean net ROI ≥ max(0.35%, 6% × days / 365): 0.35% up to about 21 days, 1.48% at 90 days |
| Full depth | `REQUIRE_FULL_DEPTH` (on) | The whole budget must fill. If a book runs out first you get *Thin book: Kalshi depth fills only $X of $50*. If deeper levels drag ROI under the required net floor, the trade is sized down to the part that still qualifies and you get *VWAP: only $X of $Y clears the 0.35% net floor*. Top-of-book must also hold ≥ `MIN_TOP_LIQUIDITY_USD` |
| Rules identical | `STRICT_MATCH_CONFIDENCE` (98) | A **strict Claude check** reads both full rulebooks and grades proposition, outcome alignment, resolution source, deadline/cutoff and edge cases (postponement, ties, voids, official vs unofficial…). All five must pass, at ≥ 98% confidence |
| Settlement dates | `MAX_SETTLE_DIFF_DAYS` (1) | Kalshi expected expiration vs Polymarket end date |
| Lock-up | `MAX_LOCKUP_DAYS` (90) | Days until the later settlement date |
| Wording | always | No "numbers differ" or "'by' vs 'on'" red flags from the deterministic checks |

All thresholds can be changed in **Controls → Bulletproof checks** or in `.env`.

**How the strict check is spent.** The first-pass matcher (Haiku, titles plus short rule snippets)
proposes pairs. The strict check (Sonnet by default, `STRICT_VERIFY_MODEL`, full rulebooks) runs only
on pairs that are profitable *and* already pass the date, lock-up and wording checks, so Claude usage
goes where money is on the table. Pairs are checked four at a time, at most
`STRICT_VERIFY_MAX_PER_HOUR` calls per hour. Verdicts are stored in `matches_cache.json` and keyed to
a hash of both rulebooks. Any change to either rulebook triggers a fresh check, and so does
`STRICT_VERIFY_TTL_HOURS`. Before any order, the pair is re-priced on fresh books and every check
runs again.

Expect bulletproof arbs to be rare. That's the point. In testing, Sonnet rated a plausible-looking Fed
pair only 72% identical: one rulebook said the target *range* is unchanged, the other checked only
the *upper bound*.

## The dashboard

* **Header.** Mock badge, execution-mode badge, **■ Emergency stop** (when armed) or **Enable
  auto-trade** (when halted), Pause / Resume of price polling, and Controls.
* **KPIs.** Scanner status and countdown, matched pairs, live arbs, and the **cap per platform**
  (editing it re-sizes everything instantly).
* **Accounts & execution.** One card each for Kalshi and Polymarket: connection status (Connected,
  Read-only / unconfigured, Unsupported here, Region blocked, or Error), cash balance, **deployed vs.
  cap**, what's available for the next leg and whether the cap or the balance is the limit. A third
  card shows the mode, the armed or halted state, the ROI target and trade counts.
* **Live arbitrage · bulletproof** (green cards, best net ROI first). Only opportunities that pass
  every strict check. Each card lists the checks it passed (ROI at VWAP, rules identical %, settle
  difference, lock-up, full depth). For each arb: its category, rule-risk
  tags, ROI, guaranteed $ profit, both legs (side, limit price, stake including fees, liquidity,
  market link), what limited the size, **⚠ Verify rulebooks** (both platforms' rules plus Claude's
  reasoning), **Approve for auto-trade**, **Block pair**, and in dry_run / live_auto a line saying
  whether it will execute and, if not, why. Cards with a high-risk tag get a red outline and a
  "Rule risk: high" badge.
* **Watchlist / rule-risk spreads.** The top 25 by net ROI that fail at least one strict check, with
  **every** failure listed under *Why not an arb*. For example: `Settle dates 92d apart (max 1d)`,
  `Match 85% < 98% (resolution source: official vs unofficial results)`, `Thin book: only $12 at top
  (Kalshi)`, `Annualized ROI 4.6% < 6% (net 1.01% over 80d)`, `Net ROI 0.20% < 0.35%`,
  `Lock-up 499d > 90d`. Both sections default to **annualized ROI, highest first**. Pairs that are profitable
  but not bulletproof carry an amber **ARB** badge. Click a column to sort.
* **Annualized ROI** (column in the spreads table, stat on every arb card, plus a sort control:
  Net ROI / Annualized / Profit / Lock-up) compares short and long capital lock-ups at a glance:
  * `days_to_settle` = whole days until the **later** of Kalshi's expected expiration and
    Polymarket's end date, minimum 1. Your money is tied up until both legs pay.
  * Annualized ROI = net ROI × 365 / `days_to_settle`, simple and not compounded.
  * Shows `—` and "no date" if either platform has no settlement date.
  * Qualification is by annualized ROI ≥ `MIN_ANNUALIZED_ROI`, plus net ROI ≥ `MIN_NET_ROI_PCT`
    (see [Bulletproof arbitrage](#bulletproof-arbitrage)). Short lock-ups annualize to huge numbers
    (1 day at 0.5% ≈ 183%), which is exactly why the raw net floor exists.
* **Trade history.** Live and simulated trades: legs, fills, exposure, expected profit and status
  (`filled`, `simulated`, `flattened`, `aborted`, `unhedged`, `unknown`). The same events go to
  `scanner.log` as `[TRADE …]` lines, and full records with exact order payloads go to `trades.json`.
* **Controls drawer.** Min net ROI, min liquidity, the three polling cadences, the execution-mode switch with the
  active safety rules spelled out, re-match, **coverage by category** (matched / priced pairs per
  category and what the last Claude round checked), Discord status and blocked pairs.

### All categories, ranked by ROI

The scanner doesn't filter by topic. It looks at sports, politics and elections, economics,
financials, crypto, entertainment and pop culture, "mentions", science and tech, weather, world
events and the long tail:

* **Complete Kalshi catalog.** It downloads all ~205 pages of open markets. `KALSHI_MAX_PAGES` is
  only a safety cap.
* **Category labels.** Each market gets a category from Kalshi's `/series` list, refreshed every 6 h.
* **Fair Claude budget.** Each matching round's budget (`MATCH_BATCH_SIZE × MATCH_MAX_BATCHES`) is
  shared round-robin across categories, and across series within a category. Otherwise thousands of
  near-identical sports props, which always score highest on text similarity, would use the whole
  budget and the long tail would never get checked. Set `MATCH_STRATIFY=0` for the old
  best-similarity-first order.
* **Long-dated markets.** Contracts settling far out (politics, novelty) may list end dates months
  apart on the two venues, so candidates may differ by up to `MATCH_DATE_GAP_FRACTION` (25%) of the
  time left, as well as the fixed `MATCH_MAX_DATE_GAP_DAYS` (31).
* **Ranking.** Opportunities are ranked by net ROI after both platforms' fees, i.e. how far
  `Kalshi + Polymarket + fees` sits below $1.00 per $1 of payout, then annualized over the lock-up.
  The 6% annualized target, the 0.35% net floor and the
  $50-per-platform cap apply to every category the same way.

**Rule-risk tags** (in `risk.py`) mark the pairs whose settlement wording you should read first:

| Tag | Level | Meaning |
|---|---|---|
| numbers differ | high | The Kalshi strike (e.g. $120,000) doesn't appear in the Polymarket question ("5+" and "O/U 4.5" count as the same line) |
| 'by' vs 'on' deadline | high | One pays if it happens by a deadline, the other only at a point in time |
| settle dates Nd apart | high > 31 d, medium > 7 d | The two contracts list different settlement dates |
| novelty: \<category\> | medium | Entertainment, mentions, culture, social, world, exotics: wording is often subjective or source-dependent |
| political resolution | medium | Check sources, certification vs projection, recount and contest rules |
| player prop | medium | Void / did-not-play and stat-correction rules often differ |
| match NN% | medium | Claude was less than 92% sure the propositions are the same |
| long-dated | info | Settles more than 6 months out |

A pair with any **high** tag is never auto-traded until you approve it, even with
`REQUIRE_PAIR_APPROVAL=0`. The tags are hints, not proof: a pair without tags can still settle
differently.

The server only listens on 127.0.0.1. It rejects requests with any other `Host` header, and every
control endpoint requires a JSON body, so other websites can't drive it from your browser.

## Test right now with `--mock`

```powershell
python main.py --mock                       # dashboard + simulated exchange (Kalshi $45, Poly $60)
python main.py --mock --mode dry_run        # simulate trades
python main.py --mock --once                # terminal-only: one scan, print results, exit
```

| Sample pair | Expected result |
|---|---|
| Fed holds in Dec 2026 (Kalshi YES ⇄ Poly "No change… Yes") | **Bulletproof**, direction A, ~6.0% ROI, Kalshi leg limited by the $45 mock balance (canned strict verdict: 99% identical) |
| Taylor Swift & Travis Kelce married in 2026 (Entertainment) | Watchlist **ARB**, ~6% ROI: *Match 93% < 98% (resolution source: …)* |
| Lakers @ Celtics (Kalshi YES = Poly outcome "Lakers", index 1) | Watchlist **ARB**, ~5.4% ROI, outcomes inverted: *Settle dates 13d apart (max 1d)* |
| The Odyssey wins Best Picture | Watchlist: ~4.1% net (13% annualized), but lock-up 115d > 90d and thin Kalshi top-of-book |
| Recession in 2026 | Watchlist: *Annualized ROI 1.3% < 6% (net 0.79% over 223d)* |
| BTC ≥ $120k on Dec 31 vs BTC reaches $130k by Dec 31 | Matcher **rejects** it (different strike and "on" vs "by") |
| 10 more pairs across Economics, Crypto, Politics, World, Science and Tech, Companies, Sports | Near-misses that fill the spreads table |

To watch the whole live path safely: switch to **Live auto** in Controls (type `LIVE`), press
**Approve for auto-trade** on the Fed card, then **Enable auto-trade**. The mock exchange fills both
legs, balances drop and the trade appears in history. Leg-risk handling can be rehearsed too:

```powershell
$env:MOCK_EXEC_FAIL="leg2_reject"; python main.py --mock --mode live_auto   # leg 2 fails → Kalshi leg flattened
$env:MOCK_EXEC_FAIL="unhedged";    python main.py --mock --mode live_auto   # unwind fails → UNHEDGED + halt
```

Mock runs use `matches_cache.mock.json`, `trades.mock.json`, `execution_state.mock.json` and
`scanner.mock.log`, and reset them on every start, so your real files are never touched. To edit
the scenarios, change `mock/generate_mock.py` and run `python mock/generate_mock.py`.

## Running live

```powershell
python main.py                                   # alert_only, $50/side, ≥6% annualized
python main.py --mode dry_run                    # rehearse with your real balances, no orders
python main.py --max-per-platform 25 --min-annualized 8 --min-roi 0.5   # pickier
```

A sensible path: run `alert_only` for a while, then `dry_run` with your keys set, read the dry-run
records in `trades.json` (the exact payloads), and only then consider `live_auto`.

There are three independent loops, each strictly sequential: a slow pass delays the next one and
never overlaps it.

| Loop | Default | Minimum | What it does |
|---|---|---|---|
| **Price** (`PRICE_POLL_INTERVAL_SEC`) | 3 s | 3 s | Re-prices the already-matched pairs: refreshes balances, fetches orderbooks, sizes both directions, alerts, and in dry_run / live_auto hands the best eligible arb to the executor |
| **Catalog** (`CATALOG_POLL_INTERVAL_SEC`) | 300 s | 60 s | Downloads every open Kalshi market and every liquid Polymarket market (a few minutes live). Releases cap room for settled trades |
| **Matching** (`MATCH_INTERVAL_SEC`) | 900 s | 300 s | Claude matching on the latest catalog: TF-IDF + game-slug candidates, then Claude Haiku confirms equivalence and outcome alignment in batches of 25, saved to `matches_cache.json`. **Force re-match** runs it now |

How the 3-second price pass stays light:

* **Batched requests.** Kalshi books come from `GET /markets/orderbooks` with up to 100 tickers per
  call, and Polymarket books from `POST /books` with up to 100 tokens per call. About 200 matched
  pairs cost roughly 3 Kalshi and 5 Polymarket requests per pass. That's about 1 Kalshi request per
  second against a ~20/s allowance, and about 17 Polymarket requests per 10 s against 500.
* **Separate rate limiters.** The price loop has its own Kalshi limiter, so a catalog download
  can't starve it.
* **Fail fast.** A failed batch is skipped for that pass only (those pairs drop out until the next
  pass). Retries on the fast path wait at most 4 s.
* **Automatic backoff.** On HTTP 429 the price interval doubles (up to 8×) and the dashboard shows
  an amber "rate-limited" note. The interval relaxes back after 5 clean passes.
* **Quiet log.** Pass summaries go to `scanner.log` only when the arb count changes (and every 100
  passes), so a 3-second loop doesn't flood it.
* **Stable dashboard.** Only sections whose content changed are redrawn, so sorting, scroll
  position, hover and open rule panels survive every update.

3 s works best when the matched set is focused (a few hundred pairs). If passes take longer than the
interval, the status card counts them as "slow", and the next pass simply starts later.

**Claude Pro usage.** Each refresh checks at most `MATCH_BATCH_SIZE × MATCH_MAX_BATCHES_PER_REFRESH`
(500) Kalshi markets, best candidates first. A usage-limit message pauses matching until the next
refresh; scanning continues.

## The math

Both legs pay $1 per contract and exactly one wins, so N contracts on each leg pay **$N in every
outcome**:

```
Direction A: Kalshi YES + Polymarket outcome that equals Kalshi NO
Direction B: Kalshi NO  + Polymarket outcome that equals Kalshi YES

Kalshi fee     = ceil_to_cent(0.07 × C × P × (1 − P))          per fill
Polymarket fee = C × feeRate × (p × (1 − p))^exponent           per fill (live feeSchedule per market)

budget_K = min(MAX_PER_PLATFORM_USD − open Kalshi exposure,  Kalshi cash balance)
budget_P = min(MAX_PER_PLATFORM_USD − open Polymarket exposure, Polymarket cash balance)

N        = largest integer with  cost_K(N) + fees_K(N) ≤ budget_K
                             and  ceil_cent(cost_P(N)) + fees_P(N) ≤ budget_P
           (fills walk the order book level by level, so N never exceeds visible depth)
profit   = N − total_cost        ROI = profit / total_cost
days     = days until the later settlement date (min 1)
arb if   ROI ≥ max(MIN_NET_ROI_PCT, MIN_ANNUALIZED_ROI × days / 365)     (≡ annualized ≥ target, net ≥ floor)
         and top-of-book ≥ MIN_TOP_LIQUIDITY_USD on both legs
         and N ≥ Polymarket min order size
```

At 6% annualized over 30 days the required net ROI is 0.49%, i.e. an all-in cost ≤ about $0.995 per $1
of guaranteed payout; over 90 days it is 1.48% (≤ $0.985). Polymarket changes its
fee coefficients from time to time (next change Nov 20, 2026); `feeSchedule` is re-read every refresh.

### Break-even and MMF hurdle (deterministic, shown on every row)

Both numbers come only from order-book prices, fees and lock-up days. Nothing is derived from Claude's
match confidence or any guessed probability.

```
cost_per_$1      = all-in VWAP cost of both legs + fees / payout     (top of book when nothing sizes)
break_even_prob  = cost_per_$1 × 100                                 e.g. 97.3% req
mmf_hurdle_prob  = cost_per_$1 × (1 + MMF_BENCHMARK_RATE × days / 365) × 100   (default 4.5%)
```

`break_even_prob` is the share of outcomes in which the position must collect $1 to get its money back.
For a true arbitrage (identical rules, same settlement) the pair always collects $1, so anything below
100% is the locked-in edge. For a Watchlist date-offset spread, the gap between 100% and the MMF hurdle
is how much "both legs lose" probability you can tolerate before a money-market fund would have done better.

### Date-offset favorite check (settle dates more than MAX_SETTLE_DIFF_DAYS apart)

The heavy favorite is the higher-priced leg (sized VWAP when there is a fill, else top of book).

- **Favorite settles early (+Xd):** the expensive, likely-to-pay leg settles first, frees capital early,
  and only the cheap leg trails through the gap. Lower tail risk.
- **Warning: Favorite settles late (−Xd):** the cheap leg settles first, and the expensive leg stays
  exposed through the whole gap window. Elevated tail risk.

This is a Watchlist label only. It never makes a pair bulletproof: the strict gate still requires
settle dates ≤ `MAX_SETTLE_DIFF_DAYS` apart.

## Risks this tool cannot remove

* **Leg risk.** Books move between the scan and the order. Fill-or-kill orders, the pre-trade
  re-price and the automatic unwind limit this, but an unwind sells at a loss, and in fast markets
  or outages a leg can be left **unhedged**. The bot then halts and you must resolve it by hand.
* **Settlement-rule differences.** This is the biggest risk. Two contracts that look identical can
  settle differently: different data sources, deadlines, tie or postponement rules, or Kalshi
  "fair price" settlements. Claude is told to be strict, but **read both rulebooks before you
  approve a pair**. Approval exists so that a human, not the model, makes that call.
* **Platform eligibility.** Each platform decides who may trade. Polymarket blocks new orders from
  the US and other regions, and Kalshi has its own eligibility rules. You are responsible for
  using each platform only where you're allowed to.
* **Capital and timing.** You can only use the balance already funded on each side. Settlement
  timing differs, which ties up capital (the cap stays occupied until both sides settle).
* **Software risk.** Exchange APIs change (both changed in 2026). Use `dry_run` after any update or
  API change, and keep the caps small.
* Nothing here is financial advice.

## Files written

* `matches_cache.json`: match cache, including blocked and approved pairs.
* `trades.json`: every live and simulated trade with exact payloads. **Don't delete it while you
  hold positions**: it is how the per-platform cap knows what's deployed.
* `execution_state.json`: the auto-trade on/off state.
* `scanner.log`: rotating log, including `[TRADE …]` lines.
* `scanner.pid`: written while the app runs.
* `.env`: holds your keys. Never share or commit it.
