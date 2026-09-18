# Alpha

A high-reward trading bot for [Stock Simulator](https://stocksimulator.xyz). It ranks a universe of high-beta names against each other, leans into leaders when the tape is strong, and buys washed-out core names when it is not.

```bash
python alpha.py
```

Trades go through `https://stocksimulator.xyz/api/bot`.



Alpha is **cross-sectional momentum** bot.

Each tick it quotes a fixed universe, scores stocks relative to the others, classifies the market as `RISK_ON`, `MIXED`, or `PANIC`, and builds a **target book** — a handful of names with weights. Then it trades the difference: sells first so cash is free, then buys, honouring stops before anything else.

That is the whole strategy. The rest is how it decides who is a leader, how hard to press, and when to get out.

A simpler sibling, `bot.py`, buys slight reds and sells slight greens on a mega-cap watchlist. This README is about Alpha.

---

## The loop

`AlphaEngine.tick()` runs forever:

1. If the market is closed, persist state and sleep 3 minutes.
2. Pull account, holdings, and pending orders.
3. Refresh a few Yahoo daily charts (4 names per tick, 12-minute TTL). Failures are ignored; live quotes still trade.
4. Quote the whole universe and append each price to a short in-memory tape.
5. Score names, pick a regime, choose targets, size them.
6. **Stops first** — they override the target book.
7. Keep running winners that just fell out of the rank.
8. **Sells before buys.**
9. Cancel stale pending orders, write `alpha_state.json`, sleep.

Cadence is about **18s** while the market is open (**12s** in the first 15 minutes).

Session phase also changes behaviour:

| Phase | When (ET) | Effect |
|---|---|---|
| `open_auction` | 9:30–9:45 | Size everything at 55% |
| `rth` | 9:45–15:45 | Full size, new entries allowed |
| `late` | 15:45–16:00 | No new names; only manage what it already owns |
| `closed` | otherwise | Sleep |

---

## Universe

~35 names, each tagged to a **sleeve** so TQQQ + NVDA + AMD do not count as diversification. Sleeve and single-name caps are enforced when sizing.

| Sleeve | Names |
|---|---|
| Levered beta | TQQQ, SOXL, TECL, TNA, FAS |
| AI / semis | NVDA, AMD, AVGO, SMCI, ARM, MU, PLTR, TSM |
| Crypto | MSTR, COIN, IBIT, MARA |
| Fintech | HOOD |
| Growth | TSLA, META, AMZN, NFLX, APP |
| Spec | RKLB, IONQ, OKLO, SMR, ASTS |
| China | BABA, PDD, NIO |
| Uranium | CCJ |
| Biotech | VKTX |

A smaller **CORE_DIP** set (NVDA, AMD, AVGO, META, TSLA, PLTR, HOOD, COIN, MSTR, TQQQ, SOXL, AMZN, TSM, APP) is what Alpha is willing to buy when everything is down.

---

## Scoring

Each name gets a raw score, then a **z-score** across the universe so a quiet day still has a ranking:

| Weight | Signal |
|---|---|
| 48% | Today's return (`day_change_percent` from the API, already in basis points) |
| 22% | Short rate-of-change from Alpha's own quote tape (~3 samples back) |
| 18% | 5-day Yahoo momentum |
| 12% | Distance to the 20-day high (Yahoo) |

Yahoo is optional context. If a chart fetch fails, the live quote still participates.

A name is **bouncing** if the last few tape prints turned up after a dip. That flag matters in PANIC: Alpha prefers dumped names that have started to turn.

---

## Regime

Regime is the median day-return plus **breadth** (share of names that are green):

| Regime | Trigger | Book |
|---|---|---|
| **RISK_ON** | median ≥ +0.55% and ≥55% green | ~7 names, force a levered ETF in if one is scoring well, ~2% cash |
| **PANIC** | median ≤ −1.10% or ≤32% green | CORE_DIP names that dumped hardest; prefer bounces; ~10% cash (more if nothing has bounced yet) |
| **MIXED** | anything else | A few leaders plus up to two hard dumps, ~7% cash |

In RISK_ON, leaders are the top z-scores (anything above −0.15). In PANIC, size follows dump depth rather than rank — but only while names are actually red. If nothing dumped enough, it holds the least-ugly core names instead of sitting in cash.

---

## Sizing

Target weights decay by rank (steeper in RISK_ON, flatter otherwise). Then caps apply:

- **38%** max in one name
- **52%** max in one sleeve
- **4%** min weight — smaller slices are dropped
- **~$250** or **2.5% of NAV** minimum before a rebalance is worth sending

In PANIC with zero bounces, investable capital is cut to 55% of the already-elevated cash buffer: wait for a tick of life before going all-in.

Late session strips new names out of the target book. Open auction scales every weight by 0.55.

---

## Risk

Stops override the target book.

- **Hard stop:** −16% vs cost. Always.
- **Trailing stop:** from the *peak price seen while held*, not from cost. Base trail is sleeve-dependent (~5.5–9%) and widens with Yahoo ATR so violent names are not shaken out of the meat. The trail only arms after the name has been up at least **0.80%** vs cost, so a fresh buy that ticks down is not immediately stopped.
- **Winner keep:** if a name drops out of the rank this tick but is still ≥1.80% above cost, keep the current weight (capped at 38%).
- **Exit cooldown:** 3 minutes before re-buying a name it just sold. CORE_DIP names skip this.
- **Falling-knife limits:** in PANIC, a name that is still dumping and has not bounced can be bought with a limit ~1.2% below last.

Sells that fully flatten a position clear its peak and record an exit timestamp.

---

## `alpha_state.json`

On-disk memory so trailing stops, cooldowns, and short-horizon momentum survive a restart. Written every tick (and when the market is closed). Yahoo stats are **not** saved; they are re-fetched.

| Field | What it is |
|---|---|
| `peaks` | High-water mark in **cents** for names currently (or recently) held. Trailing stops compare last price to this. Cleared on a full sell. |
| `last_exit` | Unix time of the last full sell. Feeds the 3-minute re-entry cooldown. |
| `yahoo_cursor` | Round-robin index into the universe so each tick refreshes the next 4 stale Yahoo charts. |
| `samples` | Rolling quote tape: `[unix_time, price_in_cents]` pairs, last **80** per symbol (in memory the deque is 240). Used for short ROC and bounce detection. |

Deleting the file does not stop the bot. It forgets peaks, cooldowns, and the short tape until they rebuild. Trailing stops will be looser until new highs are recorded, and ROC/bounce signals stay flat for the first few quote ticks.

---

## API notes

Alpha talks only to the public bot API (`/account`, `/portfolio`, `/pending`, `/market_status`, `/quote`, `/buy`, `/sell`, `DELETE /pending/:id`). A sliding 60-second window keeps it under 170 requests/min; 429s sleep on `Retry-After`.

Buy/sell return **201** (filled) or **202** (queued). Pending orders for a name block a second order in the same direction; anything else pending is cancelled as stale.

`STOCKSIM_BASE` overrides the API root if you are pointing at a local server.
