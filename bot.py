#!/usr/bin/env python3
"""Simple Stock Simulator trading bot.

Create a bot in a game at https://stocksimulator.xyz, copy its API key, then:

    export STOCKSIM_API_KEY=your-key
    pip install -r requirements.txt
    python bot.py

Or put STOCKSIM_API_KEY in a .env file next to this script.

Strategy: throw size at short-term dips and recycle quickly. Buys a chunk of
cash (not 1 share) when a watchlist name is even slightly red, caps each name
at a large slice of the account, and sells as soon as the position is modestly
green. Prices and cash from the API are integer cents. Limit is 200
requests per minute per key.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

BASE = os.environ.get("STOCKSIM_BASE", "https://stocksimulator.xyz/api/bot")

WATCHLIST = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "META",
    "TSLA",
    "AMD",
    "AVGO",
    "SPY",
    "QQQ",
]
CASH_FRACTION_PER_BUY = 0.12  # spend ~12% of cash on each dip buy
MAX_POSITION_FRACTION = 0.30  # up to 30% of account value in one name
DIP_BPS = 10  # buy when the quote is down at least 0.10% today
TAKE_PROFIT_BPS = 80  # sell when the holding is up at least 0.80% vs cost
POLL_OPEN_SECONDS = 20
POLL_CLOSED_SECONDS = 180


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
    amount = abs(amount)
    return f"{sign}${amount // 100:,}.{amount % 100:02d}"


def bps_to_pct(bps: int) -> float:
    """API percents are (change / previous_close) * 10000, i.e. basis points."""
    return bps / 100.0


class BotError(RuntimeError):
    pass


class StockSim:
    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{BASE}{path}"
        try:
            response = self.session.request(method, url, timeout=20, **kwargs)
        except requests.RequestException as exc:
            raise BotError(f"{method} {path} failed: {exc}") from exc

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

    def buy(self, symbol: str, quantity: int) -> tuple[int, Any]:
        response = self.session.post(
            f"{BASE}/buy",
            json={
                "stock_symbol": symbol,
                "quantity": quantity,
                "limit_price": None,
            },
            timeout=20,
        )
        return response.status_code, _json_or_text(response)

    def sell(self, symbol: str, quantity: int) -> tuple[int, Any]:
        response = self.session.post(
            f"{BASE}/sell",
            json={
                "stock_symbol": symbol,
                "quantity": quantity,
                "limit_price": None,
            },
            timeout=20,
        )
        return response.status_code, _json_or_text(response)


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text


def holdings_by_symbol(holdings: list[dict]) -> dict[str, dict]:
    return {h["stock_symbol"].upper(): h for h in holdings}


def pending_symbols(trades: list[dict]) -> set[str]:
    return {t["stock_symbol"].upper() for t in trades}


def shares_to_buy(
    cash: int,
    price: int,
    owned_value: int,
    account_value: int,
) -> int:
    if price <= 0 or cash < price:
        return 0
    budget = max(price, int(cash * CASH_FRACTION_PER_BUY))
    qty = budget // price
    room = int(account_value * MAX_POSITION_FRACTION) - owned_value
    if room < price:
        return 0
    return min(qty, cash // price, room // price)


def log_trade(action: str, status: int, body: Any) -> None:
    if status in (201, 202):
        kind = "filled" if status == 201 else "queued"
        if isinstance(body, dict):
            qty = body.get("quantity")
            symbol = body.get("stock_symbol")
            price = body.get("price")
            extra = f" @ {cents(price)}" if isinstance(price, int) else ""
            print(f"  {action} {qty} {symbol}{extra} ({kind})")
        else:
            print(f"  {action} {kind}: {body}")
        return
    print(f"  {action} failed ({status}): {body}")


def tick(api: StockSim) -> int:
    if not api.market_open():
        print("Market closed — waiting.")
        return POLL_CLOSED_SECONDS

    account = api.account()
    holdings = holdings_by_symbol(api.portfolio())
    pending = pending_symbols(api.pending())
    cash = int(account["cash"])
    account_value = int(account["value"])

    print(
        f"value={cents(account_value)}  cash={cents(cash)}  "
        f"change={cents(account['change'])}"
    )

    for symbol in WATCHLIST:
        if symbol in pending:
            print(f"  {symbol}: pending order, skip")
            continue

        quote = api.quote(symbol)
        price = int(quote["regular_market_price"])
        day_bps = int(quote.get("day_change_percent") or 0)
        holding = holdings.get(symbol)
        owned = int(holding["quantity"]) if holding else 0
        owned_value = int(holding["total_value"]) if holding else owned * price

        print(
            f"  {symbol} {cents(price)}  day={bps_to_pct(day_bps):+.2f}%  "
            f"held={owned}"
        )

        if holding:
            cost = int(holding["purchase_price"])
            if cost > 0 and (price - cost) * 10_000 >= TAKE_PROFIT_BPS * cost:
                status, body = api.sell(symbol, owned)
                log_trade("SELL", status, body)
                if status == 201:
                    fill = body.get("price") if isinstance(body, dict) else None
                    cash += int(fill if isinstance(fill, int) else price) * owned
                continue

        if day_bps > -DIP_BPS:
            continue

        qty = shares_to_buy(cash, price, owned_value, account_value)
        if qty <= 0:
            continue

        status, body = api.buy(symbol, qty)
        log_trade("BUY", status, body)
        if status == 201:
            cash -= price * qty

    return POLL_OPEN_SECONDS


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.environ.get("STOCKSIM_API_KEY", "").strip()
    if not api_key:
        print(
            "Set STOCKSIM_API_KEY to the bot key from the Games menu.",
            file=sys.stderr,
        )
        return 1

    api = StockSim(api_key)
    print(f"Trading {', '.join(WATCHLIST)} via {BASE}")

    while True:
        try:
            sleep_for = tick(api)
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
