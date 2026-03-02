#!/usr/bin/env python3
"""Render BTCUSDT 8h candlestick + funding bars with real Binance data (default).

Usage:
python3 simulate_btcusdt_oi_weighted_funding.py
python3 simulate_btcusdt_oi_weighted_funding.py --start 2026-02-20 --end 2026-03-02 --out btcusdt_8h.png
python3 simulate_btcusdt_oi_weighted_funding.py --simulated
python3 simulate_btcusdt_oi_weighted_funding.py --notify

For notify mode, set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import matplotlib.pyplot as plt
import requests


def oi_weighted_funding(rows: Iterable[Dict[str, Any]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        fr = float(row["funding_rate"])
        oi = float(row["open_interest_usd"])
        numerator += fr * oi
        denominator += oi
    if denominator == 0:
        raise ValueError("Total open interest is zero; cannot compute weighted average.")
    return numerator / denominator


def parse_ymd_utc(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def load_env_file(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_8h_bucket(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    bucket_hour = (ts.hour // 8) * 8
    return ts.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)


def simulated_exchange_rows(ts: datetime) -> List[Dict[str, float]]:
    # 8h points -> deterministic waves to produce positive and negative funding periods.
    step = int(ts.timestamp() // (8 * 3600))
    base = 0.00004 * math.sin(step / 3.0) + 0.000015 * math.cos(step / 7.0)
    return [
        {"exchange": "Binance", "funding_rate": base + 0.000012, "open_interest_usd": 2_100_000_000},
        {"exchange": "Bybit", "funding_rate": base - 0.000003, "open_interest_usd": 1_600_000_000},
        {"exchange": "OKX", "funding_rate": base - 0.000010, "open_interest_usd": 1_200_000_000},
        {"exchange": "Bitget", "funding_rate": base + 0.000007, "open_interest_usd": 700_000_000},
    ]


def simulate_ohlc(ts: datetime, prev_close: float) -> Dict[str, float]:
    step = int(ts.timestamp() // (8 * 3600))
    drift = 25.0 * math.sin(step / 8.0)
    impulse = 70.0 * math.sin(step / 2.3) + 35.0 * math.cos(step / 5.1)
    close = max(1000.0, prev_close + drift + impulse)
    high = max(prev_close, close) + (35.0 + 25.0 * abs(math.sin(step / 3.0)))
    low = min(prev_close, close) - (35.0 + 22.0 * abs(math.cos(step / 4.0)))
    return {"open": prev_close, "high": high, "low": max(500.0, low), "close": close}


def build_simulated_history(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    if end < start:
        raise ValueError("--end must be the same as or later than --start")

    rows: List[Dict[str, Any]] = []
    prev_close = 96_000.0
    ts = start
    while ts <= end + timedelta(days=1) - timedelta(hours=8):
        exchanges = simulated_exchange_rows(ts)
        weighted = oi_weighted_funding(exchanges)
        ohlc = simulate_ohlc(ts, prev_close)
        prev_close = float(ohlc["close"])
        rows.append({"timestamp": ts, "funding_rate": weighted, **ohlc})
        ts += timedelta(hours=8)
    return rows


def fetch_json(url: str, params: Dict[str, Any]) -> Any:
    full_url = f"{url}?{urlencode(params)}"
    req = Request(full_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_binance_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    # End is inclusive date at 23:59:59.999 UTC.
    start_ms = int(start.timestamp() * 1000)
    end_exclusive_ms = int((end + timedelta(days=1)).timestamp() * 1000)
    current = start_ms
    out: Dict[datetime, float] = {}

    while current < end_exclusive_ms:
        data = fetch_json(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            {
                "symbol": "BTCUSDT",
                "startTime": current,
                "endTime": end_exclusive_ms - 1,
                "limit": 1000,
            },
        )
        if not data:
            break

        for item in data:
            funding_time_ms = int(item["fundingTime"])
            ts = normalize_8h_bucket(datetime.fromtimestamp(funding_time_ms / 1000, tz=timezone.utc))
            out[ts] = float(item["fundingRate"])

        current = int(data[-1]["fundingTime"]) + 1

    return out


def fetch_bybit_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    start_ms = int(start.timestamp() * 1000)
    end_exclusive_ms = int((end + timedelta(days=1)).timestamp() * 1000)
    out: Dict[datetime, float] = {}
    cursor: Optional[str] = None

    while True:
        params: Dict[str, Any] = {
            "category": "linear",
            "symbol": "BTCUSDT",
            "startTime": start_ms,
            "endTime": end_exclusive_ms - 1,
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        payload = fetch_json("https://api.bybit.com/v5/market/funding/history", params)
        if int(payload.get("retCode", -1)) != 0:
            break

        items = payload.get("result", {}).get("list", [])
        if not items:
            break

        for item in items:
            ts = normalize_8h_bucket(
                datetime.fromtimestamp(int(item["fundingRateTimestamp"]) / 1000, tz=timezone.utc)
            )
            out[ts] = float(item["fundingRate"])

        next_cursor = payload.get("result", {}).get("nextPageCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return out


def fetch_okx_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    start_ms = int(start.timestamp() * 1000)
    end_exclusive_ms = int((end + timedelta(days=1)).timestamp() * 1000)
    out: Dict[datetime, float] = {}
    before: Optional[str] = None

    while True:
        params: Dict[str, Any] = {"instId": "BTC-USDT-SWAP", "limit": 100}
        if before:
            params["before"] = before
        payload = fetch_json("https://www.okx.com/api/v5/public/funding-rate-history", params)
        if str(payload.get("code")) != "0":
            break

        items = payload.get("data", [])
        if not items:
            break

        reached_older = False
        for item in items:
            raw_ms = int(item["fundingTime"])
            if raw_ms < start_ms:
                reached_older = True
                continue
            if raw_ms >= end_exclusive_ms:
                continue
            ts = normalize_8h_bucket(datetime.fromtimestamp(raw_ms / 1000, tz=timezone.utc))
            out[ts] = float(item["fundingRate"])

        oldest_ms = int(items[-1]["fundingTime"])
        if reached_older or oldest_ms < start_ms:
            break
        before = str(oldest_ms)

    return out


def fetch_binance_klines_8h(start: datetime, end: datetime) -> Dict[datetime, Dict[str, float]]:
    start_ms = int(start.timestamp() * 1000)
    end_exclusive_ms = int((end + timedelta(days=1)).timestamp() * 1000)
    current = start_ms
    out: Dict[datetime, Dict[str, float]] = {}

    while current < end_exclusive_ms:
        data = fetch_json(
            "https://fapi.binance.com/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "8h",
                "startTime": current,
                "endTime": end_exclusive_ms - 1,
                "limit": 1500,
            },
        )
        if not data:
            break

        for k in data:
            open_time_ms = int(k[0])
            ts = normalize_8h_bucket(datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc))
            out[ts] = {
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            }

        current = int(data[-1][0]) + 1

    return out


def fetch_binance_oi_usd() -> Optional[float]:
    payload = fetch_json(
        "https://fapi.binance.com/futures/data/openInterestHist",
        {"symbol": "BTCUSDT", "period": "5m", "limit": 1},
    )
    if not payload:
        return None
    return float(payload[-1]["sumOpenInterestValue"])


def fetch_bybit_oi_usd() -> Optional[float]:
    oi_payload = fetch_json(
        "https://api.bybit.com/v5/market/open-interest",
        {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 1},
    )
    if int(oi_payload.get("retCode", -1)) != 0:
        return None
    oi_list = oi_payload.get("result", {}).get("list", [])
    if not oi_list:
        return None
    open_interest_btc = float(oi_list[0]["openInterest"])

    ticker_payload = fetch_json(
        "https://api.bybit.com/v5/market/tickers",
        {"category": "linear", "symbol": "BTCUSDT"},
    )
    if int(ticker_payload.get("retCode", -1)) != 0:
        return None
    ticker_list = ticker_payload.get("result", {}).get("list", [])
    if not ticker_list:
        return None
    last_price = float(ticker_list[0]["lastPrice"])
    return open_interest_btc * last_price


def fetch_okx_oi_usd() -> Optional[float]:
    payload = fetch_json(
        "https://www.okx.com/api/v5/public/open-interest",
        {"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
    )
    if str(payload.get("code")) != "0":
        return None
    data = payload.get("data", [])
    if not data:
        return None
    return float(data[0]["oiUsd"])


def combine_funding_rates(
    ts: datetime, funding_by_exchange: Dict[str, Dict[datetime, float]], oi_usd_by_exchange: Dict[str, Optional[float]]
) -> Optional[float]:
    rates: List[tuple[str, float]] = []
    for exchange, funding_series in funding_by_exchange.items():
        if ts in funding_series:
            rates.append((exchange, float(funding_series[ts])))
    if not rates:
        return None

    weighted_rows = []
    for exchange, rate in rates:
        oi = oi_usd_by_exchange.get(exchange)
        if oi is not None and oi > 0:
            weighted_rows.append({"funding_rate": rate, "open_interest_usd": oi})

    if weighted_rows:
        return oi_weighted_funding(weighted_rows)
    return sum(rate for _, rate in rates) / len(rates)


def safe_fetch(name: str, fn) -> Any:
    try:
        return fn()
    except Exception as exc:
        print(f"Warning: {name} fetch failed: {exc}")
        return None


def build_real_history(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    funding_by_exchange: Dict[str, Dict[datetime, float]] = {}
    for exchange, fn in {
        "binance": lambda: fetch_binance_funding_history(start, end),
        "bybit": lambda: fetch_bybit_funding_history(start, end),
        "okx": lambda: fetch_okx_funding_history(start, end),
    }.items():
        data = safe_fetch(f"{exchange} funding", fn)
        if isinstance(data, dict) and data:
            funding_by_exchange[exchange] = data

    oi_usd_by_exchange: Dict[str, Optional[float]] = {
        "binance": safe_fetch("binance OI", fetch_binance_oi_usd),
        "bybit": safe_fetch("bybit OI", fetch_bybit_oi_usd),
        "okx": safe_fetch("okx OI", fetch_okx_oi_usd),
    }
    klines_by_ts = fetch_binance_klines_8h(start, end)
    rows: List[Dict[str, Any]] = []

    for ts in sorted(klines_by_ts.keys()):
        funding_rate = combine_funding_rates(ts, funding_by_exchange, oi_usd_by_exchange)
        if funding_rate is None:
            continue

        kline = klines_by_ts.get(ts)
        rows.append(
            {
                "timestamp": ts,
                "funding_rate": funding_rate,
                "open": kline["open"],
                "high": kline["high"],
                "low": kline["low"],
                "close": kline["close"],
            }
        )
    return rows


def print_history(rows: List[Dict[str, Any]], mode_label: str) -> None:
    print(f"BTCUSDT funding history (8h, UTC) - {mode_label}")
    print("timestamp (UTC)     close        funding_rate     percent")
    for row in rows:
        rate = float(row["funding_rate"])
        print(
            f"{row['timestamp']:%Y-%m-%d %H:%M}    "
            f"{float(row['close']):10.2f}   {rate:+.8f}    {rate * 100:+.5f}%"
        )


def plot_bars(rows: List[Dict[str, Any]], out_path: str, mode_label: str) -> None:
    x_labels = [row["timestamp"].strftime("%m-%d %H:%M") for row in rows]
    y_values = [float(row["funding_rate"]) * 100 for row in rows]
    bar_colors = ["#18a058" if v >= 0 else "#d03050" for v in y_values]
    x = list(range(len(rows)))

    fig, (ax_candle, ax_bar) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [2.2, 1.0]}
    )

    # Candlestick (US style): up=green, down=red
    for i, row in enumerate(rows):
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        color = "#18a058" if c >= o else "#d03050"
        ax_candle.vlines(i, l, h, color=color, linewidth=1.0)
        body_bottom = min(o, c)
        body_height = max(abs(c - o), 1.0)
        ax_candle.bar(i, body_height, bottom=body_bottom, color=color, width=0.62)

    ax_candle.set_title(f"BTCUSDT 8h Candles + Funding (UTC) - {mode_label}")
    ax_candle.set_ylabel("Price (USDT)")
    ax_candle.grid(axis="y", alpha=0.22)

    ax_bar.bar(x, y_values, color=bar_colors, width=0.82)
    ax_bar.axhline(0, color="#444444", linewidth=0.9)
    ax_bar.set_ylabel("Funding (%)")
    ax_bar.set_xlabel("Time (UTC)")
    ax_bar.grid(axis="y", alpha=0.22)

    step = max(1, len(x_labels) // 14)
    ticks = list(range(0, len(x_labels), step))
    ax_bar.set_xticks(ticks=ticks, labels=[x_labels[i] for i in ticks], rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def default_utc_range() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=13)
    return start, end


def send_telegram_alert(
    bot_token: str, chat_id: str, message: str, image_path: str, timeout_sec: int = 20
) -> None:
    send_message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    msg_resp = requests.post(
        send_message_url, data={"chat_id": chat_id, "text": message}, timeout=timeout_sec
    )
    msg_resp.raise_for_status()
    msg_payload = msg_resp.json()
    if not msg_payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {msg_payload}")

    send_photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(image_path, "rb") as image_file:
        photo_resp = requests.post(
            send_photo_url,
            data={"chat_id": chat_id, "caption": "BTCUSDT funding chart"},
            files={"photo": image_file},
            timeout=timeout_sec,
        )
    photo_resp.raise_for_status()
    photo_payload = photo_resp.json()
    if not photo_payload.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {photo_payload}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="Start date, format: YYYY-MM-DD (UTC)")
    parser.add_argument("--end", help="End date, format: YYYY-MM-DD (UTC)")
    parser.add_argument("--out", default="btcusdt_funding_8h.png", help="Output PNG path")
    parser.add_argument("--simulated", action="store_true", help="Use deterministic simulated data")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alert when latest funding is negative")
    args = parser.parse_args()

    load_env_file(".env")

    if args.start and args.end:
        start = parse_ymd_utc(args.start)
        end = parse_ymd_utc(args.end)
    elif not args.start and not args.end:
        start, end = default_utc_range()
    else:
        raise ValueError("Please provide both --start and --end, or omit both for default 14-day UTC range.")

    if (end - start).days < 13:
        start = end - timedelta(days=13)
        print("Provided range is shorter than 14 days; auto-extended start to keep at least 14 days.")

    min_points = 14 * 3

    if args.simulated:
        history = build_simulated_history(start, end)
        mode_label = "SIMULATED"
    else:
        history = build_real_history(start, end)
        # Ensure at least 14 days worth of 8h points when using real APIs.
        while len(history) < min_points:
            expanded_start = start - timedelta(days=7)
            if expanded_start < end - timedelta(days=120):
                break
            start = expanded_start
            history = build_real_history(start, end)
        mode_label = "REAL (Binance+Bybit+OKX, OI-weighted where available)"

    if not history:
        raise RuntimeError("No data fetched for the selected range.")

    print_history(history, mode_label)
    plot_bars(history, args.out, mode_label)
    print(f"\nPNG saved to: {args.out}")

    if args.notify:
        latest = history[-1]
        latest_rate = float(latest["funding_rate"])
        if latest_rate < 0:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if not bot_token or not chat_id:
                print("Warning: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env; skip notify.")
            else:
                message = (
                    "BTCUSDT funding alert (< 0)\n"
                    f"Time (UTC): {latest['timestamp']:%Y-%m-%d %H:%M}\n"
                    f"Funding: {latest_rate:+.8f} ({latest_rate * 100:+.5f}%)\n"
                    f"Source: {mode_label}"
                )
                try:
                    send_telegram_alert(bot_token, chat_id, message, args.out)
                    print("Telegram alert sent (message + chart).")
                except Exception as exc:
                    print(f"Warning: Telegram alert failed: {exc}")
        else:
            print("Latest funding is >= 0; no Telegram alert sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
