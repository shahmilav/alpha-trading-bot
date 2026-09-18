#!/usr/bin/env python3
"""Alpha — high-reward Stock Simulator bot.

    python alpha.py

Trades through https://stocksimulator.xyz/api/bot. Prices and cash are integer
cents. The public bot API is capped at 200 requests per minute per key.

Strategy (short): cross-sectional momentum across a high-beta universe, with a
crash-harvest overlay, leveraged-ETF overlay in risk-on tapes, and volatility-
aware trailing stops. See the module docstring at the bottom of the file, or
the comments on `AlphaEngine`.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

BASE = os.environ.get("STOCKSIM_BASE", "https://stocksimulator.xyz/api/bot")
NY = ZoneInfo("America/New_York")
STATE_PATH = Path(__file__).with_name("alpha_state.json")

# User-provided bot key. STOCKSIM_ALPHA_API_KEY overrides this if set.
DEFAULT_API_KEY = "FT5WQn5SoDKvmitoJMmAVIwMQgOrLrSx"

# Diverse, high-convexity universe. Sleeve tags cap accidental concentration
# in names that all move as one trade (TQQQ + NVDA + AMD is not diversification).
UNIVERSE: dict[str, str] = {
    # Levered beta — the payoff engine when the tape is trending
    "TQQQ": "lev_ndx",
    "SOXL": "lev_semi",
    "TECL": "lev_tech",
    "TNA": "lev_small",
    "FAS": "lev_fin",
    # AI / semiconductors
    "NVDA": "ai",
    "AMD": "ai",
    "AVGO": "ai",
    "SMCI": "ai",
    "ARM": "ai",
    "MU": "ai",
    "PLTR": "ai",
    "TSM": "ai",
    # Crypto-adjacent (high beta to liquidity)
    "MSTR": "crypto",
    "COIN": "crypto",
    "IBIT": "crypto",
    "MARA": "crypto",
    "HOOD": "fintech",
    # High-beta growth
    "TSLA": "growth",
    "META": "growth",
    "AMZN": "growth",
    "NFLX": "growth",
    "APP": "growth",
    # Speculative convexity (space, quantum, nuclear)
    "RKLB": "spec",
    "IONQ": "spec",
    "OKLO": "spec",
    "SMR": "spec",
    "ASTS": "spec",
    # International / other lottery tickets
    "BABA": "china",
    "PDD": "china",
    "NIO": "china",
    "CCJ": "uranium",
    "VKTX": "biotech",
}

# Names we trust enough to buy when everything is on fire.
CORE_DIP: frozenset[str] = frozenset(
    {
        "NVDA",
        "AMD",
        "AVGO",
        "META",
        "TSLA",
        "PLTR",
        "HOOD",
        "COIN",
        "MSTR",
        "TQQQ",
        "SOXL",
        "AMZN",
        "TSM",
        "APP",
    }
)

MAX_NAME_WEIGHT = 0.38
MAX_SLEEVE_WEIGHT = 0.52
MIN_WEIGHT = 0.04
MIN_TRADE_CENTS = 25_000  # $250 — ignore noise rebalances
REBALANCE_BPS = 250  # 2.5% of NAV before we bother
HARD_STOP_BPS = 1_600  # -16% vs cost: even aggressive books cut a wreck
WINNER_KEEP_BPS = 180  # let a winner run even if it drops out of the rank
EXIT_COOLDOWN_SEC = 180

TRAIL_STOP_BPS = {
    "lev_ndx": 550,
    "lev_semi": 600,
    "lev_tech": 550,
    "lev_small": 700,
    "lev_fin": 650,
    "spec": 900,
    "biotech": 850,
    "china": 800,
    "crypto": 750,
    "uranium": 800,
}
TRAIL_STOP_DEFAULT = 650

CASH_BUFFER = {"RISK_ON": 0.02, "MIXED": 0.07, "PANIC": 0.10}
TARGET_N = {"RISK_ON": 7, "MIXED": 8, "PANIC": 5}

POLL_OPEN_SECONDS = 18
POLL_CLOSED_SECONDS = 180
OPEN_AUCTION_MINUTES = 15
NO_NEW_ENTRIES_AFTER = (15, 45)

# Stay under the 200/min key cap.
REQUESTS_PER_MINUTE = 170
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_TTL_SEC = 12 * 60
YAHOO_PER_TICK = 4

# day_change_percent from the API is already (change/prev_close)*10000 = bps.


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cents(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    amount = abs(int(amount))
    return f"{sign}${amount // 100:,}.{amount % 100:02d}"


def bps_to_pct(bps: float) -> float:
    return bps / 100.0


def now_ny() -> datetime:
    return datetime.now(NY)


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class BotError(RuntimeError):
    pass


class RateLimiter:
    """Sliding 60s window so we never kiss the 200 req/min ceiling."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self.times: deque[float] = deque()

    def wait(self) -> None:
        t = time.time()
        while self.times and t - self.times[0] >= 60:
            self.times.popleft()
        if len(self.times) >= self.max_per_minute:
            sleep_for = 60 - (t - self.times[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
            t = time.time()
            while self.times and t - self.times[0] >= 60:
                self.times.popleft()
        self.times.append(time.time())


class StockSim:
    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        self.limiter = RateLimiter(REQUESTS_PER_MINUTE)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{BASE}{path}"
        for attempt in range(5):
            self.limiter.wait()
            try:
                response = self.session.request(method, url, timeout=20, **kwargs)
            except requests.RequestException as exc:
                raise BotError(f"{method} {path} failed: {exc}") from exc

            if response.status_code == 429:
                retry = response.headers.get("Retry-After")
                delay = float(retry) if retry and retry.replace(".", "", 1).isdigit() else 20
                print(f"  rate-limited; sleeping {delay:.0f}s")
                time.sleep(delay)
                continue

            if response.status_code == 204 or not response.content:
                if response.ok:
                    return None
                raise BotError(f"{method} {path} -> {response.status_code}")

            try:
                body = response.json()
            except json.JSONDecodeError:
                body = response.text

            if not response.ok:
                raise BotError(f"{method} {path} -> {response.status_code}: {body}")
            return body

        raise BotError(f"{method} {path} -> 429 after retries")

    def account(self) -> dict:
        return self._request("GET", "/account")

    def portfolio(self) -> list[dict]:
        return self._request("GET", "/portfolio").get("holdings") or []

    def pending(self) -> list[dict]:
        return self._request("GET", "/pending").get("trades") or []

    def market_open(self) -> bool:
        return bool(self._request("GET", "/market_status"))

    def quote(self, symbol: str) -> dict:
        return self._request("GET", "/quote", params={"symbol": symbol})

    def cancel(self, trade_id: str) -> None:
        self._request("DELETE", f"/pending/{trade_id}")

    def _trade(
        self, action: str, symbol: str, quantity: int, limit_price: int | None
    ) -> tuple[int, Any]:
        self.limiter.wait()
        try:
            response = self.session.post(
                f"{BASE}/{action}",
                json={
                    "stock_symbol": symbol,
                    "quantity": int(quantity),
                    "limit_price": limit_price,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise BotError(f"POST /{action} failed: {exc}") from exc
        if response.status_code == 429:
            raise BotError("rate limited on trade")
        return response.status_code, _json_or_text(response)

    def buy(self, symbol: str, quantity: int, limit_price: int | None = None) -> tuple[int, Any]:
        return self._trade("buy", symbol, quantity, limit_price)

    def sell(self, symbol: str, quantity: int, limit_price: int | None = None) -> tuple[int, Any]:
        return self._trade("sell", symbol, quantity, limit_price)


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


@dataclass
class Quote:
    symbol: str
    price: int
    prev_close: int
    day_bps: int
    name: str
    kind: str


@dataclass
class YahooStats:
    mom_5d_bps: float = 0.0
    dist_20d_high_bps: float = 0.0
    atr_pct: float = 0.0
    fetched_at: float = 0.0


@dataclass
class Score:
    symbol: str
    raw: float
    z: float
    day_bps: int
    roc_bps: float
    bounce: bool


@dataclass
class EngineState:
    samples: dict[str, deque[tuple[float, int]]] = field(default_factory=dict)
    peaks: dict[str, int] = field(default_factory=dict)
    last_exit: dict[str, float] = field(default_factory=dict)
    yahoo: dict[str, YahooStats] = field(default_factory=dict)
    yahoo_cursor: int = 0

    def history(self, symbol: str) -> deque[tuple[float, int]]:
        buf = self.samples.get(symbol)
        if buf is None:
            buf = deque(maxlen=240)
            self.samples[symbol] = buf
        return buf

    def record(self, symbol: str, price: int) -> None:
        if price <= 0:
            return
        self.history(symbol).append((time.time(), price))

    def dump(self) -> dict:
        return {
            "peaks": self.peaks,
            "last_exit": self.last_exit,
            "yahoo_cursor": self.yahoo_cursor,
            "samples": {
                s: list(pairs)[-80:]
                for s, pairs in self.samples.items()
                if pairs
            },
        }

    @classmethod
    def load(cls, path: Path) -> EngineState:
        state = cls()
        if not path.is_file():
            return state
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return state
        state.peaks = {k: int(v) for k, v in (raw.get("peaks") or {}).items()}
        state.last_exit = {k: float(v) for k, v in (raw.get("last_exit") or {}).items()}
        state.yahoo_cursor = int(raw.get("yahoo_cursor") or 0)
        for sym, pairs in (raw.get("samples") or {}).items():
            buf = state.history(sym)
            for ts, px in pairs:
                try:
                    buf.append((float(ts), int(px)))
                except (TypeError, ValueError):
                    continue
        return state

    def save(self, path: Path) -> None:
        try:
            path.write_text(json.dumps(self.dump()))
        except OSError as exc:
            print(f"  could not persist state: {exc}")


class YahooTape:
    """Optional multi-day context. Failures are ignored; live quotes still trade."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            }
        )

    def stats(self, symbol: str) -> YahooStats | None:
        url = YAHOO_CHART.format(symbol=symbol)
        try:
            response = self.session.get(
                url,
                params={"range": "3mo", "interval": "1d", "events": "div,splits"},
                timeout=12,
            )
        except requests.RequestException:
            return None
        if not response.ok:
            return None
        try:
            result = response.json()["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None
        closes = [c for c in (quote.get("close") or []) if isinstance(c, (int, float)) and c > 0]
        highs = [c for c in (quote.get("high") or []) if isinstance(c, (int, float)) and c > 0]
        lows = [c for c in (quote.get("low") or []) if isinstance(c, (int, float)) and c > 0]
        if len(closes) < 6:
            return None
        last = closes[-1]
        mom = (last / closes[-6] - 1.0) * 10_000
        window = closes[-20:] if len(closes) >= 20 else closes
        dist = (last / max(window) - 1.0) * 10_000
        atr_pct = 0.0
        n = min(len(closes), len(highs), len(lows), 15)
        if n >= 6:
            trs: list[float] = []
            start = len(closes) - n
            for i in range(start + 1, len(closes)):
                h = highs[i] if i < len(highs) else closes[i]
                l = lows[i] if i < len(lows) else closes[i]
                pc = closes[i - 1]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            if trs and last > 0:
                atr_pct = (sum(trs) / len(trs)) / last
        return YahooStats(
            mom_5d_bps=mom,
            dist_20d_high_bps=dist,
            atr_pct=atr_pct,
            fetched_at=time.time(),
        )


def zscores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    xs = list(values.values())
    mu = statistics.fmean(xs)
    if len(xs) < 2:
        return {k: 0.0 for k in values}
    sd = statistics.pstdev(xs)
    if sd < 1e-9:
        return {k: 0.0 for k in values}
    return {k: (v - mu) / sd for k, v in values.items()}


def rank_weights(n: int, decay: float = 0.70) -> list[float]:
    raw = [decay**i for i in range(n)]
    total = sum(raw) or 1.0
    return [x / total for x in raw]


def session_phase(ts: datetime) -> str:
    minutes = ts.hour * 60 + ts.minute
    open_min = 9 * 60 + 30
    close_min = 16 * 60
    if minutes < open_min or minutes >= close_min:
        return "closed"
    if minutes < open_min + OPEN_AUCTION_MINUTES:
        return "open_auction"
    late_h, late_m = NO_NEW_ENTRIES_AFTER
    if minutes >= late_h * 60 + late_m:
        return "late"
    return "rth"


def log_trade(action: str, status: int, body: Any) -> None:
    if status in (201, 202):
        kind = "filled" if status == 201 else "queued"
        if isinstance(body, dict):
            qty = body.get("quantity")
            symbol = body.get("stock_symbol") or body.get("stockSymbol")
            price = body.get("price")
            extra = f" @ {cents(price)}" if isinstance(price, int) else ""
            print(f"  {action} {qty} {symbol}{extra} ({kind})")
        else:
            print(f"  {action} {kind}: {body}")
        return
    print(f"  {action} failed ({status}): {body}")


class AlphaEngine:
    """Build a target book, then trade the difference.

    Regime
      RISK_ON  — median name green and breadth high. Press leveraged ETFs and
                 the strongest relative-strength names. Almost fully invested.
      PANIC    — tape is washing out. Buy the core high-beta names that dumped
                 the hardest, preferring ones that have started to bounce.
      MIXED    — barbell: ride a few leaders, harvest a couple of air-pockets.

    Score
      Live day return + short-horizon ROC from our own quote tape + optional
      5-day Yahoo momentum and distance-to-20d-high. Cross-sectionally z-scored
      so a quiet tape still has a ranking.

    Risk
      Large on purpose: up to 38% in one name, 52% in one sleeve, ~98% invested
      when the tape is trending. Trailing stops scaled by sleeve; a hard -16%
      vs cost still exists so a single wreck cannot zero the account.
    """

    def __init__(self, api: StockSim) -> None:
        self.api = api
        self.state = EngineState.load(STATE_PATH)
        self.yahoo = YahooTape()
        self.symbols = list(UNIVERSE)

    def _refresh_yahoo(self) -> None:
        """Fetch a few stale Yahoo charts each tick."""
        n = len(self.symbols)
        fetched = 0
        start = self.state.yahoo_cursor % n
        for offset in range(n):
            if fetched >= YAHOO_PER_TICK:
                break
            symbol = self.symbols[(start + offset) % n]
            existing = self.state.yahoo.get(symbol)
            if existing and time.time() - existing.fetched_at < YAHOO_TTL_SEC:
                continue
            stats = self.yahoo.stats(symbol)
            if stats:
                self.state.yahoo[symbol] = stats
            fetched += 1
        self.state.yahoo_cursor = (start + max(fetched, 1)) % n

    def scan(self) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for symbol in self.symbols:
            try:
                raw = self.api.quote(symbol)
            except BotError as exc:
                print(f"  {symbol}: quote failed ({exc})")
                continue
            price = int(raw.get("regular_market_price") or 0)
            if price <= 0:
                continue
            q = Quote(
                symbol=symbol,
                price=price,
                prev_close=int(raw.get("previous_close") or 0),
                day_bps=int(raw.get("day_change_percent") or 0),
                name=str(raw.get("long_name") or symbol),
                kind=str(raw.get("instrument_type") or ""),
            )
            quotes[symbol] = q
            self.state.record(symbol, price)
        return quotes

    def short_roc_bps(self, symbol: str) -> float:
        hist = self.state.history(symbol)
        if len(hist) < 4:
            return 0.0
        # ~3 samples back: a few minutes of our own tape.
        old = hist[-4][1]
        last = hist[-1][1]
        if old <= 0:
            return 0.0
        return (last - old) * 10_000 / old

    def bouncing(self, symbol: str) -> bool:
        hist = self.state.history(symbol)
        if len(hist) < 5:
            return False
        last = hist[-1][1]
        prev = hist[-3][1]
        older = hist[-5][1]
        return last > prev >= older * 0.998

    def score_universe(self, quotes: dict[str, Quote]) -> dict[str, Score]:
        raw: dict[str, float] = {}
        roc: dict[str, float] = {}
        for symbol, q in quotes.items():
            roc_bps = self.short_roc_bps(symbol)
            roc[symbol] = roc_bps
            y = self.state.yahoo.get(symbol)
            mom5 = y.mom_5d_bps if y else 0.0
            near_high = y.dist_20d_high_bps if y else 0.0
            # Intraday relative strength dominates; multi-day trend is a tilt.
            raw[symbol] = (
                0.48 * q.day_bps
                + 0.22 * roc_bps
                + 0.18 * mom5
                + 0.12 * near_high
            )
        zs = zscores(raw)
        out: dict[str, Score] = {}
        for symbol, q in quotes.items():
            out[symbol] = Score(
                symbol=symbol,
                raw=raw[symbol],
                z=zs.get(symbol, 0.0),
                day_bps=q.day_bps,
                roc_bps=roc[symbol],
                bounce=self.bouncing(symbol),
            )
        return out

    def regime(self, quotes: dict[str, Quote]) -> str:
        if not quotes:
            return "MIXED"
        days = [q.day_bps for q in quotes.values()]
        median = statistics.median(days)
        breadth = sum(1 for d in days if d > 0) / len(days)
        if median >= 55 and breadth >= 0.55:
            return "RISK_ON"
        if median <= -110 or breadth <= 0.32:
            return "PANIC"
        return "MIXED"

    def picks(self, regime: str, scores: dict[str, Score]) -> list[str]:
        ranked = sorted(scores.values(), key=lambda s: s.z, reverse=True)
        n = TARGET_N[regime]
        if regime == "RISK_ON":
            leaders = [s.symbol for s in ranked if s.z > -0.15][:n]
            # Force a levered name into the book when the tape is risk-on.
            lev = [
                s.symbol
                for s in ranked
                if UNIVERSE[s.symbol].startswith("lev_") and s.z > 0
            ]
            if lev and lev[0] not in leaders:
                if len(leaders) >= n:
                    leaders[-1] = lev[0]
                else:
                    leaders.append(lev[0])
            return leaders

        if regime == "PANIC":
            dumps = [
                s
                for s in scores.values()
                if s.symbol in CORE_DIP and s.day_bps <= -120
            ]
            # Prefer names that dumped and are turning up.
            dumps.sort(key=lambda s: (s.bounce, -s.day_bps), reverse=True)
            chosen = [s.symbol for s in dumps[:n]]
            if len(chosen) < 3:
                # Nothing dumped enough — just hold the least-ugly core names.
                core = [s for s in ranked if s.symbol in CORE_DIP]
                chosen = [s.symbol for s in core[:n]]
            return chosen

        leaders = [s.symbol for s in ranked if s.z >= 0.25][: max(4, n - 2)]
        dumps = sorted(
            (s for s in scores.values() if s.day_bps <= -180 and s.symbol not in leaders),
            key=lambda s: s.day_bps,
        )
        for s in dumps[:2]:
            if s.symbol not in leaders:
                leaders.append(s.symbol)
        return leaders[:n]

    def target_weights(self, regime: str, picks: list[str], scores: dict[str, Score]) -> dict[str, float]:
        if not picks:
            return {}
        investable = 1.0 - CASH_BUFFER[regime]
        if regime == "PANIC":
            bounces = sum(1 for s in picks if scores[s].bounce)
            if bounces == 0:
                investable *= 0.55  # wait for a tick of life before going all-in
        raw = rank_weights(len(picks), decay=0.68 if regime == "RISK_ON" else 0.82)
        if regime == "PANIC":
            # Size into the dump, not into the rank — but only when names are actually red.
            depths = [max(0.0, -scores[s].day_bps) for s in picks]
            total = sum(depths)
            if total > 0:
                raw = [d / total for d in depths]

        sleeve_used: dict[str, float] = defaultdict(float)
        weights: dict[str, float] = {}
        remaining = investable
        for symbol, w in zip(picks, raw):
            sleeve = UNIVERSE[symbol]
            capped = min(w * investable, MAX_NAME_WEIGHT, MAX_SLEEVE_WEIGHT - sleeve_used[sleeve], remaining)
            if capped < MIN_WEIGHT:
                continue
            weights[symbol] = capped
            sleeve_used[sleeve] += capped
            remaining -= capped
        return weights

    def trail_bps(self, symbol: str) -> int:
        sleeve = UNIVERSE.get(symbol, "")
        atr = self.state.yahoo.get(symbol)
        base = TRAIL_STOP_BPS.get(sleeve, TRAIL_STOP_DEFAULT)
        if atr and atr.atr_pct > 0:
            # Wider trail on violent names so we don't get shaken out of the meat.
            vol_boost = int(clamp(atr.atr_pct * 10_000 * 0.35, 0, 400))
            return base + vol_boost
        return base

    def should_stop(self, holding: dict, quote: Quote) -> str | None:
        symbol = quote.symbol
        qty = int(holding["quantity"])
        if qty <= 0 or quote.price <= 0:
            return None
        cost = int(holding.get("purchase_price") or 0)
        peak = max(self.state.peaks.get(symbol, quote.price), quote.price, cost)
        self.state.peaks[symbol] = peak
        if cost > 0 and (quote.price - cost) * 10_000 <= -HARD_STOP_BPS * cost:
            return "hard-stop"
        trail = self.trail_bps(symbol)
        if peak > 0 and (peak - quote.price) * 10_000 >= trail * peak:
            # Only trail after the name has actually worked, otherwise a fresh
            # buy that immediately ticks down would get stopped for noise.
            if cost > 0 and (peak - cost) * 10_000 >= 80 * cost:
                return "trail-stop"
        return None

    def keep_winner(self, holding: dict, quote: Quote, in_target: bool) -> bool:
        if in_target:
            return False
        cost = int(holding.get("purchase_price") or 0)
        if cost <= 0:
            return False
        pnl_bps = (quote.price - cost) * 10_000 / cost
        return pnl_bps >= WINNER_KEEP_BPS

    def cancel_stale(self, pending: list[dict], keep: set[tuple[str, str]]) -> None:
        for trade in pending:
            symbol = str(trade.get("stock_symbol") or "").upper()
            action = str(trade.get("action") or "").upper()
            trade_id = str(trade.get("id") or "")
            if not trade_id:
                continue
            if (symbol, action) in keep:
                continue
            try:
                self.api.cancel(trade_id)
                print(f"  cancel stale {action} {symbol} ({trade_id})")
            except BotError as exc:
                print(f"  cancel failed {trade_id}: {exc}")

    def tick(self) -> int:
        if not self.api.market_open():
            print(f"{now_ny():%H:%M:%S} ET  market closed — waiting.")
            self.state.save(STATE_PATH)
            return POLL_CLOSED_SECONDS

        phase = session_phase(now_ny())
        account = self.api.account()
        holdings = {h["stock_symbol"].upper(): h for h in self.api.portfolio()}
        pending = self.api.pending()
        pending_by_sym = {str(t.get("stock_symbol") or "").upper(): t for t in pending}
        cash = int(account["cash"])
        nav = int(account["value"]) or 1

        self._refresh_yahoo()
        quotes = self.scan()
        if not quotes:
            print("  no quotes — backing off")
            return POLL_OPEN_SECONDS

        scores = self.score_universe(quotes)
        regime = self.regime(quotes)
        pick_list = self.picks(regime, scores)
        targets = self.target_weights(regime, pick_list, scores)

        size_scale = 0.55 if phase == "open_auction" else 1.0
        if size_scale < 1.0:
            targets = {s: w * size_scale for s, w in targets.items()}
        allow_new = phase != "late"
        if not allow_new:
            # Late session: only manage what we already own.
            targets = {s: w for s, w in targets.items() if s in holdings}

        days = [q.day_bps for q in quotes.values()]
        median = statistics.median(days)
        breadth = sum(1 for d in days if d > 0) / len(days)
        print(
            f"{now_ny():%H:%M:%S} ET  {regime} {phase}  "
            f"value={cents(nav)}  cash={cents(cash)}  "
            f"change={cents(int(account.get('change') or 0))}  "
            f"breadth={breadth:.0%}  median={bps_to_pct(median):+.2f}%"
        )
        top = sorted(scores.values(), key=lambda s: s.z, reverse=True)[:8]
        print(
            "  leaders "
            + "  ".join(
                f"{s.symbol} z={s.z:+.2f} d={bps_to_pct(s.day_bps):+.2f}%"
                for s in top
            )
        )

        # Honour stops first. They override the target book.
        forced_exits: set[str] = set()
        for symbol, holding in list(holdings.items()):
            q = quotes.get(symbol)
            if not q:
                continue
            why = self.should_stop(holding, q)
            if why:
                print(f"  {symbol}: {why} from {cents(self.state.peaks.get(symbol, q.price))}")
                forced_exits.add(symbol)
                targets.pop(symbol, None)

        # Keep running winners that fell out of the rank this tick.
        for symbol, holding in holdings.items():
            q = quotes.get(symbol)
            if not q or symbol in forced_exits:
                continue
            if self.keep_winner(holding, q, symbol in targets):
                current_w = int(holding["total_value"]) / nav
                targets.setdefault(symbol, min(current_w, MAX_NAME_WEIGHT))

        keep_pending: set[tuple[str, str]] = set()
        # Sells first so buys have cash. Snapshot: a full fill pops the name.
        for symbol, holding in list(holdings.items()):
            q = quotes.get(symbol)
            if not q:
                continue
            owned = int(holding["quantity"])
            owned_value = int(holding.get("total_value") or owned * q.price)
            target_w = targets.get(symbol, 0.0)
            target_value = int(nav * target_w)
            if symbol in forced_exits:
                qty = owned
            else:
                delta = owned_value - target_value
                if delta < max(MIN_TRADE_CENTS, nav * REBALANCE_BPS // 10_000):
                    continue
                if target_w <= 0:
                    qty = owned
                else:
                    qty = min(owned, max(1, delta // q.price))
            if qty <= 0:
                continue
            if symbol in pending_by_sym and str(pending_by_sym[symbol].get("action")).upper() == "SELL":
                keep_pending.add((symbol, "SELL"))
                continue
            status, body = self.api.sell(symbol, qty)
            log_trade("SELL", status, body)
            if status == 201:
                fill = body.get("price") if isinstance(body, dict) else None
                cash += int(fill if isinstance(fill, int) else q.price) * qty
                leftover = owned - qty
                if leftover <= 0:
                    self.state.peaks.pop(symbol, None)
                    self.state.last_exit[symbol] = time.time()
                    holdings.pop(symbol, None)
                else:
                    holding["quantity"] = leftover
            elif status == 202:
                keep_pending.add((symbol, "SELL"))

        # Buys.
        for symbol, target_w in sorted(targets.items(), key=lambda kv: kv[1], reverse=True):
            q = quotes.get(symbol)
            if not q or q.price <= 0:
                continue
            last_x = self.state.last_exit.get(symbol, 0.0)
            if last_x and time.time() - last_x < EXIT_COOLDOWN_SEC and symbol not in CORE_DIP:
                continue
            holding = holdings.get(symbol)
            owned = int(holding["quantity"]) if holding else 0
            owned_value = int(holding["total_value"]) if holding else owned * q.price
            target_value = int(nav * target_w)
            delta = target_value - owned_value
            if delta < max(MIN_TRADE_CENTS, nav * REBALANCE_BPS // 10_000):
                continue
            if not allow_new and owned == 0:
                continue
            budget = min(delta, cash)
            qty = budget // q.price
            if qty <= 0:
                continue
            if symbol in pending_by_sym and str(pending_by_sym[symbol].get("action")).upper() == "BUY":
                keep_pending.add((symbol, "BUY"))
                continue

            limit: int | None = None
            sc = scores.get(symbol)
            if (
                regime == "PANIC"
                and sc
                and not sc.bounce
                and sc.day_bps <= -150
            ):
                # Pay less than last if the knife is still falling.
                limit = max(1, int(q.price * 0.988))

            status, body = self.api.buy(symbol, qty, limit)
            log_trade("BUY", status, body)
            if status == 201:
                fill = body.get("price") if isinstance(body, dict) else None
                px = int(fill if isinstance(fill, int) else q.price)
                cash -= px * qty
                self.state.peaks[symbol] = max(self.state.peaks.get(symbol, px), px)
            elif status == 202:
                keep_pending.add((symbol, "BUY"))

        self.cancel_stale(pending, keep_pending)
        self.state.save(STATE_PATH)
        return 12 if phase == "open_auction" else POLL_OPEN_SECONDS


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = (os.environ.get("STOCKSIM_ALPHA_API_KEY") or DEFAULT_API_KEY).strip()
    if not api_key:
        print("Set STOCKSIM_ALPHA_API_KEY or DEFAULT_API_KEY.", file=sys.stderr)
        return 1

    api = StockSim(api_key)
    engine = AlphaEngine(api)
    print(
        f"Alpha running {len(UNIVERSE)} names via {BASE}\n"
        f"sleeves: {', '.join(sorted(set(UNIVERSE.values())))}"
    )

    while True:
        try:
            sleep_for = engine.tick()
        except BotError as exc:
            print(f"API error: {exc}")
            sleep_for = POLL_OPEN_SECONDS
        print(f"Next check in {sleep_for}s\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
