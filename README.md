# alpha-trading-bot 

A high-risk, high-reward trading bot for [stocksimulator.xyz](https://stocksimulator.xyz). It ranks a universe of high-beta names against each other, leans into leaders when the tape is strong, and buys washed-out core names when it is not.

```bash
python alpha.py
```

Trades go through `https://stocksimulator.xyz/api/bot`.



Alpha is **cross-sectional momentum** bot.

Each tick it quotes a fixed universe, scores stocks relative to the others, classifies the market as `RISK_ON`, `MIXED`, or `PANIC`, and builds a **target book** (handful of names and weights). It trades the difference.

## Universe

~35 names, each tagged to a **sleeve**. Sleeve and single-name caps are enforced when sizing.

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

A smaller set (NVDA, AMD, AVGO, META, TSLA, PLTR, HOOD, COIN, MSTR, TQQQ, SOXL, AMZN, TSM, APP) is what Alpha is still willing to buy when everything is down.

---

## Scoring

Each name gets a raw score, then a **z-score** across the universe so a quiet day still has a ranking:

| Weight | Signal |
|---|---|
| 48% | Today's return (`day_change_percent` from the API, already in basis points) |
| 22% | Short rate-of-change from Alpha's own quote tape (~3 samples back) |
| 18% | 5-day Yahoo momentum |
| 12% | Distance to the 20-day high (Yahoo) |

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

- **38%** max in one name
- **52%** max in one sleeve
- **4%** min weight — smaller slices are dropped
- **~$250** or **2.5% of NAV** minimum before a rebalance is worth sending

