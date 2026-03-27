"""
Historical settlement-lock replay using Kalshi hourly candlesticks + IEM ASOS temps.

This is the fastest high-signal backtest for the current alpha thesis:
same-day hard-lock weather opportunities.
"""

import csv
import io
import json
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import config
from kalshi_client import KalshiClient
from settlement_lock import SettlementLockPaper
from weather_engine import CITIES

RESULTS_FILE = os.path.join(config.STATE_DIR, "settlement_lock_replay.json")


def _iter_dates(start_date, end_date):
    cur = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _event_ticker(series_ticker, day):
    return "%s-%s" % (series_ticker, day.strftime("%y%b%d").upper())


def _dollars_to_cents(value):
    if value in (None, "", "M"):
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _normalize_live_candle(candle):
    price = candle.get("price", {}) or {}
    yes_ask = candle.get("yes_ask", {}) or {}
    yes_bid = candle.get("yes_bid", {}) or {}
    yes_ask_close = _dollars_to_cents(yes_ask.get("close_dollars"))
    yes_bid_close = _dollars_to_cents(yes_bid.get("close_dollars"))
    no_ask_close = max(0, 100 - yes_bid_close) if yes_bid_close > 0 else 0
    return {
        "end_period_ts": int(candle.get("end_period_ts", 0) or 0),
        "yes_ask_cents": yes_ask_close,
        "yes_bid_cents": yes_bid_close,
        "no_ask_cents": no_ask_close,
        "last_price_cents": _dollars_to_cents(price.get("close_dollars")),
    }


def _normalize_historical_candle(candle):
    price = candle.get("price", {}) or {}
    yes_ask = candle.get("yes_ask", {}) or {}
    yes_bid = candle.get("yes_bid", {}) or {}
    yes_ask_close = _dollars_to_cents(yes_ask.get("close"))
    yes_bid_close = _dollars_to_cents(yes_bid.get("close"))
    no_ask_close = max(0, 100 - yes_bid_close) if yes_bid_close > 0 else 0
    return {
        "end_period_ts": int(candle.get("end_period_ts", 0) or 0),
        "yes_ask_cents": yes_ask_close,
        "yes_bid_cents": yes_bid_close,
        "no_ask_cents": no_ask_close,
        "last_price_cents": _dollars_to_cents(price.get("close")),
    }


def fetch_intraday_observations(city_code, target_date):
    """Fetch intraday ASOS temperatures and compute a running high timeline."""
    city = CITIES.get(city_code)
    if not city:
        return []

    station = city.get("nws_station", "")
    tz_name = city.get("timezone", "America/New_York")
    if not station:
        return []

    local_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz_name))
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    params = {
        "station": station,
        "data": "tmpf",
        "sts": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ets": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tz": "UTC",
        "format": "comma",
        "latlon": "no",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except Exception:
        return []

    rows = []
    lines = [line for line in resp.text.splitlines() if line and not line.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    for row in reader:
        ts = row.get("valid", "")
        tempf = row.get("tmpf", "")
        if not ts or tempf in ("", "M", None):
            continue
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            rows.append((int(dt.timestamp()), math.floor(float(tempf))))
        except (ValueError, TypeError):
            continue
    rows.sort(key=lambda item: item[0])
    return rows


def get_running_high(observations, ts, cursor, current_high):
    while cursor < len(observations) and observations[cursor][0] <= ts:
        current_high = max(current_high, observations[cursor][1]) if current_high is not None else observations[cursor][1]
        cursor += 1
    return cursor, current_high


def fetch_event_markets(client, event_ticker):
    recent = client.get_markets(limit=1000, event_ticker=event_ticker, status="settled")
    markets = (recent or {}).get("markets", [])
    if markets:
        return markets, False
    historical = client.get_historical_markets(limit=1000, event_ticker=event_ticker)
    markets = (historical or {}).get("markets", [])
    if markets:
        return markets, True
    return [], False


def fetch_event_candles(client, markets, start_ts, end_ts, historical=False, period_interval=60):
    if not markets:
        return {}
    if not historical:
        resp = client.get_market_candlesticks_batch(
            [m.get("ticker", "") for m in markets if m.get("ticker")],
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_interval,
            include_latest_before_start=True,
        ) or {}
        result = {}
        for entry in resp.get("markets", []):
            ticker = entry.get("market_ticker", "")
            if not ticker:
                continue
            result[ticker] = [_normalize_live_candle(c) for c in entry.get("candlesticks", [])]
        return result

    result = {}
    for market in markets:
        ticker = market.get("ticker", "")
        if not ticker:
            continue
        resp = client.get_historical_market_candlesticks(
            ticker,
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_interval,
        ) or {}
        result[ticker] = [_normalize_historical_candle(c) for c in resp.get("candlesticks", [])]
        time.sleep(0.1)
    return result


def replay_settlement_locks(start_date, end_date, city_codes=None, period_interval=60):
    city_codes = city_codes or list(config.WEATHER_CITIES)
    client = KalshiClient()
    paper = SettlementLockPaper(kalshi_client=client)

    all_trades = []
    by_city = Counter()
    by_day = defaultdict(lambda: {
        "trades": 0,
        "first_profit_cents": 0,
        "best_profit_cents": 0,
    })

    for day in _iter_dates(start_date, end_date):
        date_str = day.strftime("%Y-%m-%d")
        print("REPLAY %s" % date_str)
        for city_code in city_codes:
            city = CITIES.get(city_code)
            if not city:
                continue
            event_ticker = _event_ticker(city["series_ticker"], day)
            markets, historical = fetch_event_markets(client, event_ticker)
            if not markets:
                continue

            observations = fetch_intraday_observations(city_code, date_str)
            if not observations:
                continue

            end_time = max(
                int(datetime.fromisoformat((m.get("close_time") or m.get("expiration_time")).replace("Z", "+00:00")).timestamp())
                for m in markets
                if m.get("close_time") or m.get("expiration_time")
            )
            start_time = min(
                int(datetime.fromisoformat((m.get("open_time") or m.get("created_time")).replace("Z", "+00:00")).timestamp())
                for m in markets
                if m.get("open_time") or m.get("created_time")
            )
            candles = fetch_event_candles(
                client,
                markets,
                start_ts=start_time,
                end_ts=end_time,
                historical=historical,
                period_interval=period_interval,
            )

            city_trades = []

            for market in markets:
                ticker = market.get("ticker", "")
                market_candles = sorted(candles.get(ticker, []), key=lambda c: c.get("end_period_ts", 0))
                if not market_candles:
                    continue

                first_entry = None
                best_entry = None
                obs_cursor = 0
                running_high = None
                market_result = market.get("result", "")
                for candle in market_candles:
                    candle_ts = int(candle.get("end_period_ts", 0) or 0)
                    obs_cursor, running_high = get_running_high(observations, candle_ts, obs_cursor, running_high)
                    if running_high is None:
                        continue

                    market_snapshot = dict(market)
                    market_snapshot["yes_ask"] = candle.get("yes_ask_cents", 0)
                    market_snapshot["no_ask"] = candle.get("no_ask_cents", 0)
                    market_snapshot["last_price"] = candle.get("last_price_cents", 0)
                    observed_at = datetime.fromtimestamp(candle_ts, tz=timezone.utc)
                    candidate = paper.evaluate_market_snapshot(
                        market_snapshot,
                        todays_high=running_high,
                        require_same_day=False,
                        observed_at=observed_at,
                    )
                    if not candidate:
                        continue

                    candidate["market_result"] = market_result
                    candidate["observed_at_hour_local"] = observed_at.astimezone(ZoneInfo(city["timezone"])).strftime("%H:%M")
                    candidate["event_ticker"] = event_ticker
                    candidate["city_code"] = city_code
                    entry_price = int(candidate.get("price_cents", 0) or 0)
                    candidate["first_profit_cents"] = (
                        (100 - entry_price) if candidate.get("lock_side") == market_result else -entry_price
                    )
                    if first_entry is None:
                        first_entry = dict(candidate)
                    if best_entry is None or int(candidate.get("price_cents", 0) or 0) < int(best_entry.get("price_cents", 999) or 999):
                        best_entry = dict(candidate)

                if first_entry:
                    row = dict(first_entry)
                    row["best_price_cents"] = int(best_entry.get("price_cents", 0) or 0) if best_entry else int(first_entry.get("price_cents", 0) or 0)
                    row["best_profit_cents"] = (
                        (100 - row["best_price_cents"]) if row.get("lock_side") == market_result else -row["best_price_cents"]
                    )
                    city_trades.append(row)

            if city_trades:
                all_trades.extend(city_trades)
                by_city[city_code] += len(city_trades)
                by_day[date_str]["trades"] += len(city_trades)
                by_day[date_str]["first_profit_cents"] += sum(t.get("first_profit_cents", 0) for t in city_trades)
                by_day[date_str]["best_profit_cents"] += sum(t.get("best_profit_cents", 0) for t in city_trades)

    summary = {
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city_count": len(city_codes),
        "trade_count": len(all_trades),
        "cities_with_trades": dict(by_city.most_common()),
        "first_profit_cents": sum(t.get("first_profit_cents", 0) for t in all_trades),
        "best_profit_cents": sum(t.get("best_profit_cents", 0) for t in all_trades),
        "average_first_price_cents": round(
            sum(t.get("price_cents", 0) for t in all_trades) / len(all_trades), 2
        ) if all_trades else 0.0,
        "average_best_price_cents": round(
            sum(t.get("best_price_cents", 0) for t in all_trades) / len(all_trades), 2
        ) if all_trades else 0.0,
        "by_day": dict(sorted(by_day.items())),
        "top_examples": sorted(
            all_trades,
            key=lambda row: (-(row.get("first_profit_cents", 0) or 0), row.get("ticker", "")),
        )[:20],
    }

    payload = {
        "summary": summary,
        "trades": all_trades,
    }
    config.atomic_json_save(RESULTS_FILE, payload)
    return payload


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        start_arg, end_arg = sys.argv[1], sys.argv[2]
    else:
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        start = end - timedelta(days=30)
        start_arg = start.strftime("%Y-%m-%d")
        end_arg = end.strftime("%Y-%m-%d")

    results = replay_settlement_locks(start_arg, end_arg)
    summary = results.get("summary", {})
    print()
    print("Settlement-Lock Replay")
    print("======================")
    print("Range: %s -> %s" % (summary.get("start_date", ""), summary.get("end_date", "")))
    print("Trades: %d" % summary.get("trade_count", 0))
    print("First-entry P&L: %+dc ($%+.2f)" % (
        summary.get("first_profit_cents", 0),
        summary.get("first_profit_cents", 0) / 100.0,
    ))
    print("Best-seen P&L: %+dc ($%+.2f)" % (
        summary.get("best_profit_cents", 0),
        summary.get("best_profit_cents", 0) / 100.0,
    ))
    print("Top cities:", summary.get("cities_with_trades", {}))
    print("Saved to:", RESULTS_FILE)
