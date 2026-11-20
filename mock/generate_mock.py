"""Generate mock/*.json payloads in the real Kalshi / Polymarket API shapes.

Usage: python mock/generate_mock.py [output_dir]   (defaults to this folder)
"""
import hashlib
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
OUT.mkdir(exist_ok=True)


def tok(seed):  # deterministic 77-digit CLOB token id
    return str(int(hashlib.sha256(seed.encode()).hexdigest(), 16))[:77]


def cid(seed):
    return "0x" + hashlib.sha256(("cond" + seed).encode()).hexdigest()


def d(x):
    return f"{x:.4f}"


# ---------------------------------------------------------------- Kalshi
# (ticker, event, title, yes_sub, close, rules, yes_bids[(p,q)], no_bids[(p,q)])
K = [
    ("KXFEDDECISION-26DEC-H0", "KXFEDDECISION-26DEC", "Fed decision in Dec 2026?", "Hold (0bps)",
     "2026-12-09T18:59:00Z",
     "If the Federal Reserve maintains the federal funds target range (no change) at its December 9, 2026 FOMC meeting, then the market resolves to Yes.",
     [(0.55, 800), (0.56, 500)], [(0.40, 300), (0.42, 400)]),
    ("KXNBAGAME-26NOV20LALBOS-LAL", "KXNBAGAME-26NOV20LALBOS", "Los Angeles L at Boston Winner?", "Los Angeles L",
     "2026-12-04T03:30:00Z",
     "If Los Angeles Lakers wins the Los Angeles L at Boston professional basketball game originally scheduled for Nov 20, 2026, then the market resolves to Yes.",
     [(0.44, 900), (0.45, 1200)], [(0.53, 700), (0.54, 1000)]),
    ("KXRECSSNBER-26", "KXRECSSNBER-26", "Recession in 2026?", "Recession in 2026",
     "2027-06-30T14:00:00Z",
     "If the NBER Business Cycle Dating Committee announces that a US recession began in calendar year 2026, then the market resolves to Yes.",
     [(0.06, 1500), (0.07, 2000)], [(0.91, 800), (0.92, 1200)]),
    ("KXOSCARPIC-27-ODY", "KXOSCARPIC-27", "Best Picture winner at the 2027 Oscars?", "The Odyssey",
     "2027-03-14T23:00:00Z",
     "If The Odyssey wins Best Picture at the 99th Academy Awards (2027 Oscars), then the market resolves to Yes.",
     [(0.18, 1500), (0.19, 900)], [(0.78, 60), (0.80, 100)]),
    ("KXPRESPARTY-2028-D", "KXPRESPARTY-2028", "Which party will win the 2028 presidential election?", "Democratic party",
     "2028-11-07T15:00:00Z",
     "If the Democratic Party nominee wins the 2028 United States presidential election, then the market resolves to Yes.",
     [(0.51, 5000), (0.52, 8000)], [(0.45, 6000), (0.46, 9000)]),
    ("KXBTCD-26DEC31-T119999.99", "KXBTCD-26DEC31", "Bitcoin price on Dec 31, 2026 at 5pm EST?", "$120,000 or above",
     "2026-12-31T22:00:00Z",
     "If the CF Benchmarks Bitcoin Real-Time Index (BRTI) is above 119999.99 on Dec 31, 2026 at 5pm EST, then the market resolves to Yes.",
     [(0.28, 1000), (0.29, 700)], [(0.69, 900), (0.70, 1100)]),
    ("KXNCAAMBTOTAL-26NOV18OAKASU-117", "KXNCAAMBTOTAL-26NOV18OAKASU", "Full Game: Over 116.5 points scored",
     "Over 116.5 points", "2026-11-21T02:00:00Z",
     "If over 116.5 total points are scored in the Oakland vs Arizona St. men's college basketball game originally scheduled for Nov 18, 2026, then the market resolves to Yes.",
     [(0.86, 1000)], [(0.10, 900)]),
    ("KXSPACEXMARS-30", "KXSPACEXMARS", "Will SpaceX land on Mars before 2030?", "Before 2030",
     "2029-12-31T23:59:00Z", "If SpaceX lands a spacecraft on Mars before Jan 1, 2030, then the market resolves to Yes.",
     [], []),
]

# ---------------------------------------------------------------- extra near-miss pairs (fill the spreads table)
# (seed, kalshi ticker, kalshi title, poly question, close, k_yes_ask, k_no_ask, p_yes_ask, p_no_ask, poly fee rate)
EXTRA = [
    ("fedjan", "KXFEDDECISION-27JAN-C25", "Will the Fed cut rates by 25bps in January 2027?", "Fed cuts rates by 25 bps after January 2027 meeting?", "2027-01-27T19:00:00Z", 0.34, 0.67, 0.325, 0.665, 0.05),
    ("swift", "KXSWIFTKELCE-26", "Will Taylor Swift and Travis Kelce get married in 2026?", "Taylor Swift and Travis Kelce married in 2026?", "2026-12-31T23:59:00Z", 0.22, 0.79, 0.215, 0.70, 0.05),
    ("starship", "KXSTARSHIPORBIT-26", "Will Starship reach orbit before 2027?", "SpaceX Starship reaches orbit before 2027?", "2026-12-31T23:59:00Z", 0.81, 0.21, 0.79, 0.205, 0.05),
    ("shutdown", "KXGOVSHUT-26", "Will the US government shut down before 2027?", "US government shutdown before 2027?", "2026-12-31T23:59:00Z", 0.12, 0.89, 0.115, 0.885, 0.04),
    ("time", "KXTIMEPOY-26-MUSK", "Time Person of the Year 2026? Elon Musk", "Will Elon Musk be named Time Person of the Year 2026?", "2026-12-10T15:00:00Z", 0.09, 0.92, 0.085, 0.918, 0.05),
    ("fold", "KXAPPLEFOLD-26", "Will Apple release a foldable iPhone in 2026?", "Apple releases foldable iPhone in 2026?", "2026-12-31T23:59:00Z", 0.41, 0.60, 0.395, 0.605, 0.04),
    ("btc150", "KXBTCMAXY-26-150000", "Will Bitcoin hit $150,000 in 2026?", "Will Bitcoin hit $150k in 2026?", "2026-12-31T23:59:00Z", 0.07, 0.94, 0.062, 0.942, 0.07),
    ("gpt6", "KXGPT6-26", "Will OpenAI release GPT-6 in 2026?", "OpenAI releases GPT-6 in 2026?", "2026-12-31T23:59:00Z", 0.18, 0.83, 0.172, 0.832, 0.04),
    ("zputin", "KXZELENSKYPUTIN-26", "Will Zelensky and Putin meet in 2026?", "Zelensky and Putin meet in person in 2026?", "2026-12-31T23:59:00Z", 0.06, 0.95, 0.052, 0.952, 0.0),
    ("chiefs", "KXSB-27-KC", "Will the Kansas City Chiefs win Super Bowl LXI?", "Will the Chiefs win Super Bowl LXI?", "2027-02-14T23:30:00Z", 0.11, 0.90, 0.104, 0.899, 0.05),
]
for seed, tk, title, q, close, kya, kna, pya, pna, rate in EXTRA:
    K.append((tk, tk.rsplit("-", 1)[0], title, "", close,
              f"If {title.rstrip('?').replace('Will ', '', 1)}, then the market resolves to Yes.",
              [(round(1 - kna, 2), 1800), (round(1 - kna - 0.01, 2), 2500)],
              [(round(1 - kya, 2), 1500), (round(1 - kya - 0.01, 2), 2200)]))

markets, books = [], {}
for t, ev, title, sub, close, rules, ybids, nbids in K:
    yes_ask = 1 - max(p for p, _ in nbids) if nbids else 0
    no_ask = 1 - max(p for p, _ in ybids) if ybids else 0
    markets.append({
        "ticker": t, "event_ticker": ev, "market_type": "binary", "status": "active",
        "title": title, "yes_sub_title": sub, "no_sub_title": sub, "close_time": close,
        "expiration_time": close, "rules_primary": rules, "price_level_structure": "linear_cent",
        "yes_ask_dollars": d(yes_ask), "no_ask_dollars": d(no_ask),
        "yes_bid_dollars": d(max([p for p, _ in ybids], default=0)),
        "no_bid_dollars": d(max([p for p, _ in nbids], default=0)),
        "yes_ask_size_fp": f"{max(nbids)[1] if nbids else 0:.2f}",
        "yes_bid_size_fp": f"{max(ybids)[1] if ybids else 0:.2f}",
        "expected_expiration_time": close,
        "last_price_dollars": d(max([p for p, _ in ybids], default=0)),
        "volume_fp": "12500.00", "open_interest_fp": "8400.00", "notional_value_dollars": "1.0000",
    })
    books[t] = {"orderbook_fp": {
        "yes_dollars": [[d(p), f"{q:.2f}"] for p, q in sorted(ybids)],
        "no_dollars": [[d(p), f"{q:.2f}"] for p, q in sorted(nbids)],
    }}
(OUT / "kalshi_markets.json").write_text(json.dumps({"cursor": "", "markets": markets}, indent=2))
(OUT / "kalshi_orderbooks.json").write_text(json.dumps(books, indent=2))

# ---------------------------------------------------------------- Polymarket
# (seed, question, slug, outcomes, end, description, fee_rate, asks0[(p,q)], asks1[(p,q)])
P = [
    ("fed", "No change in Fed interest rates after December 2026 meeting?", "no-change-in-fed-interest-rates-after-december-2026-meeting",
     ["Yes", "No"], "2026-12-09T00:00:00Z",
     "This market will resolve to \"Yes\" if the upper bound of the target federal funds range is unchanged following the Federal Reserve's FOMC meeting scheduled for December 8-9, 2026. Otherwise \"No\".",
     0.05, [(0.64, 500), (0.65, 800)], [(0.335, 600), (0.345, 900)]),
    ("lal", "Lakers vs. Celtics", "nba-lal-bos-2026-11-20", ["Celtics", "Lakers"], "2026-11-21T00:30:00Z",
     "In the upcoming NBA game, scheduled for November 20, 2026 at 7:30 PM ET: If the Los Angeles Lakers win, the market will resolve to \"Lakers\". If the Boston Celtics win, the market will resolve to \"Celtics\".",
     0.05, [(0.61, 800), (0.62, 1000)], [(0.37, 700), (0.38, 1500)]),
    ("rec", "US recession in 2026?", "us-recession-in-2026", ["Yes", "No"], "2027-06-30T00:00:00Z",
     "This market will resolve to \"Yes\" if the NBER announces that a US recession began at any point in 2026. Otherwise \"No\".",
     0.05, [(0.055, 3000), (0.058, 4000)], [(0.95, 5000)]),
    ("ody", "Will 'The Odyssey' win Best Picture at the 2027 Oscars?", "will-the-odyssey-win-best-picture-at-the-2027-oscars",
     ["Yes", "No"], "2027-03-14T00:00:00Z",
     "This market will resolve to \"Yes\" if The Odyssey wins Best Picture at the 99th Academy Awards ceremony in 2027.",
     0.05, [(0.23, 1200)], [(0.74, 2000), (0.75, 3000)]),
    ("dem", "Will the Democratic Party win the 2028 US Presidential Election?", "will-the-democratic-party-win-the-2028-us-presidential-election",
     ["Yes", "No"], "2028-11-07T00:00:00Z",
     "This market will resolve to \"Yes\" if the Democratic Party's nominee wins the 2028 US presidential election.",
     0.04, [(0.53, 20000)], [(0.48, 20000)]),
    ("btc", "Will Bitcoin reach $130,000 by December 31, 2026?", "will-bitcoin-reach-130000-by-december-31-2026",
     ["Yes", "No"], "2026-12-31T00:00:00Z",
     "This market will resolve to \"Yes\" if any Binance 1-minute candle for BTC/USDT between now and December 31, 2026 11:59 PM ET has a final High price of $130,000 or higher.",
     0.07, [(0.38, 2000)], [(0.60, 2500)]),
    ("tay", "Will Taylor Swift announce a new album before 2027?", "will-taylor-swift-announce-a-new-album-before-2027",
     ["Yes", "No"], "2026-12-31T00:00:00Z",
     "This market will resolve to \"Yes\" if Taylor Swift officially announces a new studio album before January 1, 2027.",
     0.05, [(0.22, 900)], [(0.79, 900)]),
]
for seed, tk, title, q, close, kya, kna, pya, pna, rate in EXTRA:
    P.append((seed, q, q.lower().replace("?", "").replace("$", "").replace(" ", "-"), ["Yes", "No"], close[:10] + "T00:00:00Z",
              f"This market will resolve to \"Yes\" if {q.rstrip('?')}. Otherwise \"No\".", rate,
              [(pya, 2400), (round(pya + 0.002, 3), 3000)], [(pna, 2600), (round(pna + 0.002, 3), 3200)]))

gamma, clob = [], []
for i, (seed, q, slug, outcomes, end, desc, rate, a0, a1) in enumerate(P):
    toks = [tok(seed + "0"), tok(seed + "1")]
    c = cid(seed)
    gamma.append({
        "id": str(2400000 + i), "question": q, "conditionId": c, "slug": slug, "endDate": end,
        "description": desc, "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps([f"{a0[0][0]:.3f}", f"{a1[0][0]:.3f}"]),
        "clobTokenIds": json.dumps(toks), "active": True, "closed": False, "archived": False,
        "enableOrderBook": True, "acceptingOrders": True, "orderMinSize": 5, "orderPriceMinTickSize": 0.001,
        "bestBid": round(1 - a1[0][0], 3), "bestAsk": a0[0][0], "negRisk": False,
        "liquidityNum": 25000.0, "events": [{"slug": slug, "title": q}], "volume24hr": 180000.0,
        "feesEnabled": rate > 0, "feeType": "category_fees",
        "feeSchedule": {"exponent": 1, "rate": rate, "takerOnly": True, "rebateRate": 0.25},
    })
    for tid, asks, other in ((toks[0], a0, a1), (toks[1], a1, a0)):
        clob.append({
            "market": c, "asset_id": tid, "timestamp": "1795057043183", "hash": hashlib.md5(tid.encode()).hexdigest(),
            "bids": [{"price": f"{round(1 - p - 0.01, 3)}", "size": f"{qty:.2f}"} for p, qty in sorted(other, reverse=True)],
            # CLOB lists asks worst-first (descending); the client re-sorts
            "asks": [{"price": f"{p}", "size": f"{qty:.2f}"} for p, qty in sorted(asks, reverse=True)],
            "min_order_size": "5", "tick_size": "0.001", "neg_risk": False,
        })
(OUT / "polymarket_markets.json").write_text(json.dumps(gamma, indent=2))
(OUT / "polymarket_books.json").write_text(json.dumps(clob, indent=2))

# ---------------------------------------------------------------- canned Claude answers (offline mock matcher)
answers = [
    {"kalshi_ticker": "KXFEDDECISION-26DEC-H0", "poly_condition_id": cid("fed"), "kalshi_yes_equals_outcome": 0,
     "confidence": 0.96, "reason": "Both: no rate change at Dec 2026 FOMC"},
    {"kalshi_ticker": "KXNBAGAME-26NOV20LALBOS-LAL", "poly_condition_id": cid("lal"), "kalshi_yes_equals_outcome": 1,
     "confidence": 0.97, "reason": "Same Nov 20 game; Kalshi YES = Lakers win"},
    {"kalshi_ticker": "KXRECSSNBER-26", "poly_condition_id": cid("rec"), "kalshi_yes_equals_outcome": 0,
     "confidence": 0.93, "reason": "Both NBER recession starting in 2026"},
    {"kalshi_ticker": "KXOSCARPIC-27-ODY", "poly_condition_id": cid("ody"), "kalshi_yes_equals_outcome": 0,
     "confidence": 0.95, "reason": "Same film, same 2027 Best Picture"},
    {"kalshi_ticker": "KXPRESPARTY-2028-D", "poly_condition_id": cid("dem"), "kalshi_yes_equals_outcome": 0,
     "confidence": 0.94, "reason": "Democrat wins 2028 presidency on both"},
    {"kalshi_ticker": "KXBTCD-26DEC31-T119999.99", "poly_condition_id": None, "kalshi_yes_equals_outcome": None,
     "confidence": 0.97, "reason": "Different strike (120k vs 130k) and 'on' vs 'by'"},
]
answers += [{"kalshi_ticker": tk, "poly_condition_id": cid(seed), "kalshi_yes_equals_outcome": 0,
             "confidence": 0.92, "reason": "Same proposition and deadline"} for seed, tk, *_ in EXTRA]
(OUT / "claude_answers.json").write_text(json.dumps(answers, indent=2))

# ---------------------------------------------------------------- canned strict rule verdicts (mock)
ALL_PASS = {c: "pass" for c in ("proposition", "outcome_alignment", "resolution_source", "deadline", "edge_cases")}
strict = [
    {"id": "KXFEDDECISION-26DEC-H0", "identical": True, "confidence": 99, "checks": ALL_PASS,
     "issues": []},
    {"id": "KXNBAGAME-26NOV20LALBOS-LAL", "identical": True, "confidence": 99, "checks": ALL_PASS, "issues": []},
    {"id": "KXSWIFTKELCE-26", "identical": False, "confidence": 93,
     "checks": {**ALL_PASS, "resolution_source": "fail"},
     "issues": ["Polymarket needs official confirmation; Kalshi accepts credible media reports"]},
]
(OUT / "claude_strict.json").write_text(json.dumps(strict, indent=2))

# ---------------------------------------------------------------- Kalshi /series categories (mock)
SERIES_CAT = {
    "KXFEDDECISION": "Economics", "KXNBAGAME": "Sports", "KXRECSSNBER": "Economics", "KXOSCARPIC": "Entertainment",
    "KXPRESPARTY": "Elections", "KXBTCD": "Crypto", "KXNCAAMBTOTAL": "Sports", "KXSPACEXMARS": "Science and Technology",
    "KXSWIFTKELCE": "Entertainment", "KXSTARSHIPORBIT": "Science and Technology", "KXGOVSHUT": "Politics",
    "KXTIMEPOY": "Entertainment", "KXAPPLEFOLD": "Companies", "KXBTCMAXY": "Crypto", "KXGPT6": "Science and Technology",
    "KXZELENSKYPUTIN": "World", "KXSB": "Sports",
}
(OUT / "kalshi_series.json").write_text(json.dumps([{"ticker": k, "category": v} for k, v in SERIES_CAT.items()], indent=2))
print("wrote", sorted(p.name for p in OUT.iterdir()))
