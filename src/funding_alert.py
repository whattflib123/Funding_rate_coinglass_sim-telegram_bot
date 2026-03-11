#!/usr/bin/env python3
"""Render BTCUSDT 4h candle + funding + spot/perp CVD and optionally send to Telegram."""

from __future__ import annotations

import argparse
import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests
from matplotlib.patches import Rectangle

UTC = timezone.utc
UTC_PLUS_8 = timezone(timedelta(hours=8))
FOUR_HOURS = timedelta(hours=4)


@dataclass
class KlineRow:
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    quote_volume: float
    taker_buy_quote_volume: float


def oi_weighted_funding(rows: Iterable[Dict[str, Any]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        numerator += float(row["funding_rate"]) * float(row["open_interest_usd"])
        denominator += float(row["open_interest_usd"])
    if denominator == 0:
        raise ValueError("Total open interest is zero; cannot compute weighted average.")
    return numerator / denominator


def parse_ymd_utc(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)


def load_env_file(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_4h_bucket(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    bucket_hour = (ts.hour // 4) * 4
    return ts.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)


def default_utc_range() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    end = normalize_4h_bucket(now)
    start = end - timedelta(days=14)
    return start, end


def fetch_json(url: str, params: Dict[str, Any], timeout: float = 20.0) -> Any:
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def fetch_binance_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((end + FOUR_HOURS).timestamp() * 1000)
    current = start_ms
    out: Dict[datetime, float] = {}
    while current < end_ms:
        data = fetch_json(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            {"symbol": "BTCUSDT", "startTime": current, "endTime": end_ms - 1, "limit": 1000},
        )
        if not data:
            break
        for item in data:
            ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=UTC)
            out[ts] = float(item["fundingRate"])
        current = int(data[-1]["fundingTime"]) + 1
    return out


def fetch_bybit_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((end + FOUR_HOURS).timestamp() * 1000)
    cursor: Optional[str] = None
    out: Dict[datetime, float] = {}
    while True:
        params: Dict[str, Any] = {
            "category": "linear",
            "symbol": "BTCUSDT",
            "startTime": start_ms,
            "endTime": end_ms - 1,
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
            ts = datetime.fromtimestamp(int(item["fundingRateTimestamp"]) / 1000, tz=UTC)
            out[ts] = float(item["fundingRate"])
        next_cursor = payload.get("result", {}).get("nextPageCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return out


def fetch_okx_funding_history(start: datetime, end: datetime) -> Dict[datetime, float]:
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((end + FOUR_HOURS).timestamp() * 1000)
    before: Optional[str] = None
    out: Dict[datetime, float] = {}
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
            if raw_ms >= end_ms:
                continue
            out[datetime.fromtimestamp(raw_ms / 1000, tz=UTC)] = float(item["fundingRate"])
        oldest_ms = int(items[-1]["fundingTime"])
        if reached_older or oldest_ms < start_ms:
            break
        before = str(oldest_ms)
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
    return open_interest_btc * float(ticker_list[0]["lastPrice"])


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


def safe_fetch(name: str, fn) -> Any:
    try:
        return fn()
    except Exception as exc:
        print(f"Warning: {name} fetch failed: {exc}")
        return None


def combine_funding_by_timestamp(
    funding_by_exchange: Dict[str, Dict[datetime, float]],
    oi_usd_by_exchange: Dict[str, Optional[float]],
) -> Dict[datetime, float]:
    all_timestamps = sorted({ts for series in funding_by_exchange.values() for ts in series.keys()})
    combined: Dict[datetime, float] = {}
    for ts in all_timestamps:
        weighted_rows = []
        fallback_rates = []
        for exchange, series in funding_by_exchange.items():
            rate = series.get(ts)
            if rate is None:
                continue
            fallback_rates.append(float(rate))
            oi = oi_usd_by_exchange.get(exchange)
            if oi is not None and oi > 0:
                weighted_rows.append({"funding_rate": rate, "open_interest_usd": oi})
        if weighted_rows:
            combined[ts] = oi_weighted_funding(weighted_rows)
        elif fallback_rates:
            combined[ts] = sum(fallback_rates) / len(fallback_rates)
    return combined


def resolve_funding_for_bucket(ts: datetime, funding_series: Dict[datetime, float]) -> Optional[float]:
    timestamps = sorted(funding_series.keys())
    if not timestamps:
        return None
    idx = bisect_right(timestamps, ts) - 1
    if idx < 0:
        return None
    latest_ts = timestamps[idx]
    if ts - latest_ts > timedelta(hours=12):
        return None
    return funding_series[latest_ts]


def fetch_klines(symbol: str, base_url: str, path: str, start: datetime, end: datetime) -> List[KlineRow]:
    payload = fetch_json(
        f"{base_url}{path}",
        {
            "symbol": symbol,
            "interval": "4h",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int((end + FOUR_HOURS).timestamp() * 1000) - 1,
            "limit": 1000,
        },
    )
    out: List[KlineRow] = []
    for item in payload:
        out.append(
            KlineRow(
                timestamp=datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC),
                open_price=float(item[1]),
                high_price=float(item[2]),
                low_price=float(item[3]),
                close_price=float(item[4]),
                quote_volume=float(item[7]),
                taker_buy_quote_volume=float(item[10]),
            )
        )
    return out


def compute_cvd_rows(klines: List[KlineRow]) -> List[Dict[str, Any]]:
    cvd = 0.0
    rows: List[Dict[str, Any]] = []
    for row in sorted(klines, key=lambda item: item.timestamp):
        delta = (2.0 * row.taker_buy_quote_volume) - row.quote_volume
        cvd += delta
        rows.append(
            {
                "timestamp": row.timestamp,
                "delta_notional": delta,
                "cvd_notional": cvd,
                "close_price": row.close_price,
                "open": row.open_price,
                "high": row.high_price,
                "low": row.low_price,
                "close": row.close_price,
            }
        )
    return rows


def merge_history(
    candle_rows: List[KlineRow],
    funding_series: Dict[datetime, float],
    spot_cvd_rows: List[Dict[str, Any]],
    perp_cvd_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    spot_by_ts = {row["timestamp"]: row for row in spot_cvd_rows}
    perp_by_ts = {row["timestamp"]: row for row in perp_cvd_rows}
    merged: List[Dict[str, Any]] = []
    for candle in candle_rows:
        funding_rate = resolve_funding_for_bucket(candle.timestamp, funding_series)
        spot_row = spot_by_ts.get(candle.timestamp)
        perp_row = perp_by_ts.get(candle.timestamp)
        if funding_rate is None:
            continue
        merged.append(
            {
                "timestamp": candle.timestamp,
                "open": candle.open_price,
                "high": candle.high_price,
                "low": candle.low_price,
                "close": candle.close_price,
                "funding_rate": funding_rate,
                "spot_cvd": None if spot_row is None else spot_row["cvd_notional"],
                "perp_cvd": None if perp_row is None else perp_row["cvd_notional"],
                "spot_delta": None if spot_row is None else spot_row["delta_notional"],
                "perp_delta": None if perp_row is None else perp_row["delta_notional"],
            }
        )
    return merged


def format_chinese_amount(value: float) -> str:
    abs_value = abs(value)
    units = [(1e12, "兆"), (1e8, "億"), (1e4, "萬"), (1e3, "千")]
    for threshold, label in units:
        if abs_value >= threshold:
            return f"{value / threshold:+,.1f}{label}"
    return f"{value:+,.0f}"


def trend_emoji(value: float) -> str:
    return "🟢" if value >= 0 else "🔴"


def trend_text(value: float) -> str:
    return "走強" if value >= 0 else "走弱"


def funding_emoji(value: float) -> str:
    return "📈" if value >= 0 else "📉"


def funding_threshold_text(funding_rate: float) -> str:
    funding_pct = funding_rate * 100
    if funding_pct > 0.005:
        return "🔴 資金費率門檻: 已超過做空門檻"
    if funding_pct < -0.005:
        return "🟢 資金費率門檻: 已超過做多門檻"
    return "⚪ 資金費率門檻: 未達做多/做空門檻"


def build_interpretation(spot_change: float, perp_change: float) -> str:
    spot_up = spot_change >= 0
    perp_up = perp_change >= 0
    if spot_up and perp_up:
        return "告警解讀: 現貨與合約同步偏多，多方動能一致。"
    if (not spot_up) and (not perp_up):
        return "告警解讀: 現貨與合約同步偏空，空方動能一致。"
    if spot_up and (not perp_up):
        return "告警解讀: 現貨偏多、合約偏空，盤面出現背離，留意價格可能重新定價。"
    return "告警解讀: 合約偏多、現貨偏弱，槓桿動能較強，留意追價風險。"


def compute_recent_change(rows: List[Dict[str, Any]], key: str, lookback: timedelta = FOUR_HOURS) -> float:
    valid_rows = [row for row in rows if row.get(key) is not None]
    if not valid_rows:
        return 0.0
    cutoff = valid_rows[-1]["timestamp"] - lookback
    latest = valid_rows[-1][key]
    baseline = valid_rows[0][key]
    delta_key = f"{key.split('_')[0]}_delta"
    for row in valid_rows:
        if row["timestamp"] >= cutoff:
            delta_value = row.get(delta_key)
            baseline = row[key] if delta_value is None else row[key] - delta_value
            break
    return latest - baseline


def print_history(rows: List[Dict[str, Any]]) -> None:
    print("BTCUSDT 4h candle + funding + CVD (UTC)")
    print("timestamp (UTC)     close        funding_rate     spot_cvd          perp_cvd")
    for row in rows:
        spot_text = "N/A" if row["spot_cvd"] is None else f"{row['spot_cvd']:.0f}"
        perp_text = "N/A" if row["perp_cvd"] is None else f"{row['perp_cvd']:.0f}"
        print(
            f"{row['timestamp']:%Y-%m-%d %H:%M}    "
            f"{row['close']:10.2f}   {row['funding_rate']:+.8f}    "
            f"{spot_text:>14}    {perp_text:>14}"
        )


def draw_candles(ax: plt.Axes, rows: List[Dict[str, Any]]) -> None:
    width = (4 / 24) * 0.72
    for row in rows:
        x = mdates.date2num(row["timestamp"])
        up = row["close"] >= row["open"]
        color = "#18a058" if up else "#d03050"
        ax.vlines(x, row["low"], row["high"], color=color, linewidth=1.0)
        body_bottom = min(row["open"], row["close"])
        body_height = max(abs(row["close"] - row["open"]), 1.0)
        ax.add_patch(
            Rectangle(
                (x - width / 2, body_bottom),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.8,
            )
        )


def plot_history(rows: List[Dict[str, Any]], out_path: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    times = [row["timestamp"] for row in rows]
    funding_pct = [float(row["funding_rate"]) * 100 for row in rows]
    funding_colors = ["#18a058" if value >= 0 else "#d03050" for value in funding_pct]
    has_spot_cvd = any(row["spot_cvd"] is not None for row in rows)
    has_perp_cvd = any(row["perp_cvd"] is not None for row in rows)
    axes_count = 2 + int(has_spot_cvd) + int(has_perp_cvd)
    height_ratios = [2.2, 1.0]
    if has_spot_cvd:
        height_ratios.append(1.0)
    if has_perp_cvd:
        height_ratios.append(1.0)
    fig, axes = plt.subplots(
        axes_count,
        1,
        figsize=(15, 3.1 * axes_count + 2.5),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios},
        constrained_layout=True,
    )
    if axes_count == 2:
        ax_candle, ax_funding = axes
        remaining_axes: List[plt.Axes] = []
    else:
        ax_candle = axes[0]
        ax_funding = axes[1]
        remaining_axes = list(axes[2:])
    fig.patch.set_facecolor("#f8f5ef")

    ax_candle.set_facecolor("#fffdf8")
    draw_candles(ax_candle, rows)
    ax_candle.set_title("BTCUSDT 4H Candles + Funding + Spot/Perp CVD", loc="left", fontsize=14, fontweight="bold")
    ax_candle.set_ylabel("Price (USDT)")
    ax_candle.grid(axis="y", alpha=0.2)

    bar_width = (4 / 24) * 0.62
    ax_funding.set_facecolor("#fffdf8")
    ax_funding.bar(times, funding_pct, width=bar_width, color=funding_colors, alpha=0.9)
    ax_funding.axhline(0, color="#444444", linewidth=0.9)
    ax_funding.set_ylabel("Funding (%)")
    ax_funding.grid(axis="y", alpha=0.18)
    ax_funding.legend(
        handles=[
            Rectangle((0, 0), 1, 1, facecolor="#18a058", edgecolor="#18a058", label="Funding +"),
            Rectangle((0, 0), 1, 1, facecolor="#d03050", edgecolor="#d03050", label="Funding -"),
        ],
        loc="upper left",
        ncol=2,
        frameon=False,
    )

    next_ax_index = 0
    target_ax = ax_funding
    if has_spot_cvd:
        ax_spot = remaining_axes[next_ax_index]
        next_ax_index += 1
        spot_times = [row["timestamp"] for row in rows if row["spot_cvd"] is not None]
        spot_values = [row["spot_cvd"] for row in rows if row["spot_cvd"] is not None]
        ax_spot.set_facecolor("#fffdf8")
        spot_line = ax_spot.plot(spot_times, spot_values, color="#0f766e", linewidth=2.0, label="Spot CVD")[0]
        ax_spot.axhline(0, color="#444444", linewidth=0.9, alpha=0.6)
        ax_spot.set_ylabel("Spot CVD")
        ax_spot.grid(axis="y", alpha=0.18)
        ax_spot.legend(handles=[spot_line], loc="upper left", frameon=False)
        target_ax = ax_spot
    if has_perp_cvd:
        ax_perp = remaining_axes[next_ax_index]
        perp_times = [row["timestamp"] for row in rows if row["perp_cvd"] is not None]
        perp_values = [row["perp_cvd"] for row in rows if row["perp_cvd"] is not None]
        ax_perp.set_facecolor("#fffdf8")
        perp_line = ax_perp.plot(perp_times, perp_values, color="#b45309", linewidth=2.0, label="Perp CVD")[0]
        ax_perp.axhline(0, color="#444444", linewidth=0.9, alpha=0.6)
        ax_perp.set_ylabel("Perp CVD")
        ax_perp.grid(axis="y", alpha=0.18)
        ax_perp.legend(handles=[perp_line], loc="upper left", frameon=False)
        target_ax = ax_perp

    target_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=UTC))
    target_ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    target_ax.set_xlabel("Time (UTC)")

    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def send_telegram_alert(bot_token: str, chat_id: str, message: str, image_path: str, timeout_sec: int = 20) -> None:
    send_photo_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(image_path, "rb") as image_file:
        response = requests.post(
            send_photo_url,
            data={"chat_id": chat_id, "caption": f"{message}\n\n📊 BTCUSDT 4H Funding + CVD 圖表"},
            files={"photo": image_file},
            timeout=timeout_sec,
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {payload}")


def build_message(rows: List[Dict[str, Any]]) -> str:
    latest = rows[-1]
    funding_rate = float(latest["funding_rate"])
    latest_price = float(latest["close"])
    now_utc8 = datetime.now(UTC_PLUS_8)
    message_lines = [
        "BTCUSDT Funding + CVD 告警通知\n"
        f"時間(UTC+8): {now_utc8:%Y-%m-%d %H:%M}\n"
        f"比特幣價格: {latest_price:,.2f} USDT\n"
        f"{funding_emoji(funding_rate)} 最新綜合 Funding: {funding_rate * 100:+.4f}%\n"
        f"{funding_threshold_text(funding_rate)}\n"
    ]
    has_spot_cvd = any(row["spot_cvd"] is not None for row in rows)
    has_perp_cvd = any(row["perp_cvd"] is not None for row in rows)
    if has_spot_cvd:
        spot_change = compute_recent_change(rows, "spot_cvd")
        latest_spot = next(row["spot_cvd"] for row in reversed(rows) if row["spot_cvd"] is not None)
        message_lines.append(
            f"{trend_emoji(spot_change)} 現貨近4小時 CVD: {trend_text(spot_change)} ({format_chinese_amount(spot_change)} USDT)\n"
        )
        message_lines.append(f"現貨最新 CVD: {format_chinese_amount(latest_spot)} USDT\n")
    if has_perp_cvd:
        perp_change = compute_recent_change(rows, "perp_cvd")
        latest_perp = next(row["perp_cvd"] for row in reversed(rows) if row["perp_cvd"] is not None)
        message_lines.append(
            f"{trend_emoji(perp_change)} 合約近4小時 CVD: {trend_text(perp_change)} ({format_chinese_amount(perp_change)} USDT)\n"
        )
        message_lines.append(f"合約最新 CVD: {format_chinese_amount(latest_perp)} USDT\n")
    if has_spot_cvd and has_perp_cvd:
        message_lines.append(f"{build_interpretation(compute_recent_change(rows, 'spot_cvd'), compute_recent_change(rows, 'perp_cvd'))}\n")
    return "".join(message_lines)


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
    combined_funding = combine_funding_by_timestamp(funding_by_exchange, oi_usd_by_exchange)

    spot_klines = fetch_klines("BTCUSDT", "https://api.binance.com", "/api/v3/klines", start, end)
    perp_klines = fetch_klines("BTCUSDT", "https://fapi.binance.com", "/fapi/v1/klines", start, end)

    spot_cvd_rows = compute_cvd_rows(spot_klines)
    perp_cvd_rows = compute_cvd_rows(perp_klines)
    return merge_history(spot_klines, combined_funding, spot_cvd_rows, perp_cvd_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="Start date, format: YYYY-MM-DD (UTC)")
    parser.add_argument("--end", help="End date, format: YYYY-MM-DD (UTC)")
    parser.add_argument("--out", default="assets/runtime_funding_cvd_4h.png", help="Output PNG path")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alert with chart")
    args = parser.parse_args()

    load_env_file(".env")

    if args.start and args.end:
        start = parse_ymd_utc(args.start)
        end = parse_ymd_utc(args.end)
    elif not args.start and not args.end:
        start, end = default_utc_range()
    else:
        raise ValueError("Please provide both --start and --end, or omit both for default 7-day UTC range.")

    history = build_real_history(start, end)
    if not history:
        raise RuntimeError("No data fetched for the selected range.")

    print_history(history)
    plot_history(history, args.out)
    print(f"\nPNG saved to: {args.out}")

    if args.notify:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            print("Warning: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env; skip notify.")
        else:
            try:
                send_telegram_alert(bot_token, chat_id, build_message(history), args.out)
                print("Telegram alert sent (message + chart).")
            except Exception as exc:
                print(f"Warning: Telegram alert failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
