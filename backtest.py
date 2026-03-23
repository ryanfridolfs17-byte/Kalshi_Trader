#!/usr/bin/env python3
"""backtest.py — Historical forecast accuracy backtest.

Fetches 365 days of actual temperatures (IEM) and model forecasts (Open-Meteo)
for all 20 cities, computes per-city and per-model accuracy metrics.

Usage:
    python backtest.py                    # Default: last 365 days
    python backtest.py 2025-06-01 2025-12-31  # Custom date range

Output: backtest_results.json (compact, ~50KB)
"""

import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta

import requests

from weather_engine import CITIES

# ─── IEM station-to-network mapping ───
# IEM uses 3-letter codes (strip leading K from ICAO)
# Network format: {STATE}_ASOS
IEM_NETWORKS = {
    "NYC": "NY_ASOS",   # Central Park
    "MDW": "IL_ASOS",   # Chicago Midway
    "MIA": "FL_ASOS",   # Miami International
    "AUS": "TX_ASOS",   # Austin-Bergstrom
    "LAX": "CA_ASOS",   # Los Angeles
    "DEN": "CO_ASOS",   # Denver
    "PHL": "PA_ASOS",   # Philadelphia
    "ATL": "GA_ASOS",   # Atlanta
    "BOS": "MA_ASOS",   # Boston
    "DFW": "TX_ASOS",   # Dallas
    "DCA": "VA_ASOS",   # Washington DC
    "HOU": "TX_ASOS",   # Houston Hobby
    "LAS": "NV_ASOS",   # Las Vegas
    "MSP": "MN_ASOS",   # Minneapolis
    "MSY": "LA_ASOS",   # New Orleans
    "OKC": "OK_ASOS",   # Oklahoma City
    "PHX": "AZ_ASOS",   # Phoenix
    "SAT": "TX_ASOS",   # San Antonio
    "SEA": "WA_ASOS",   # Seattle
    "SFO": "CA_ASOS",   # San Francisco
}

# Map city_code → IEM 3-letter station
CITY_TO_IEM = {}
for code, info in CITIES.items():
    station_3 = info["nws_station"][1:]  # KNYC → NYC
    CITY_TO_IEM[code] = station_3

OPEN_METEO_MODELS = ["gfs_seamless", "ecmwf_ifs025", "gem_global"]
# ICON excluded: sparse data in historical archive
MODEL_DISPLAY = {
    "gfs_seamless": "GFS",
    "ecmwf_ifs025": "ECMWF",
    "gem_global": "GEM",
}

RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backtest_results.json"
)


# ─── Data Fetching ───

def fetch_iem_actuals(station_3, network, start_date, end_date):
    """Fetch daily max temps from IEM. Returns {date_str: temp_f}."""
    s = datetime.strptime(start_date, "%Y-%m-%d")
    e = datetime.strptime(end_date, "%Y-%m-%d")

    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"
    params = {
        "network": network,
        "stations": station_3,
        "year1": s.year, "month1": s.month, "day1": s.day,
        "year2": e.year, "month2": e.month, "day2": e.day,
        "output": "csv",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print("  [IEM] Failed %s: %s" % (station_3, exc))
        return {}

    actuals = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        day = row.get("day", "")
        temp_str = row.get("max_temp_f", "")
        if not day or not temp_str or temp_str == "None":
            continue
        try:
            actuals[day] = math.floor(float(temp_str))  # Match NWS conservative rounding
        except (ValueError, TypeError):
            continue

    return actuals


def fetch_historical_forecasts(lat, lon, start_date, end_date):
    """Fetch historical model forecasts from Open-Meteo (monthly batches).

    Returns {date_str: {model_display_name: temp_f}}.
    """
    all_forecasts = {}
    s = datetime.strptime(start_date, "%Y-%m-%d")
    e = datetime.strptime(end_date, "%Y-%m-%d")

    # Process in monthly batches
    current = s.replace(day=1)
    while current <= e:
        month_end = (current + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        batch_start = max(current, s).strftime("%Y-%m-%d")
        batch_end = min(month_end, e).strftime("%Y-%m-%d")

        url = "https://historical-forecast-api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": batch_start,
            "end_date": batch_end,
            "daily": "temperature_2m_max",
            "models": ",".join(OPEN_METEO_MODELS),
            "temperature_unit": "fahrenheit",
        }

        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print("  [METEO] Failed %s..%s: %s" % (batch_start, batch_end, exc))
            current = (month_end + timedelta(days=1)).replace(day=1)
            time.sleep(1)
            continue

        daily = data.get("daily", {})
        dates = daily.get("time", [])

        for i, date_str in enumerate(dates):
            models = {}
            for model in OPEN_METEO_MODELS:
                key = "temperature_2m_max_%s" % model
                vals = daily.get(key, [])
                if i < len(vals) and vals[i] is not None:
                    models[MODEL_DISPLAY[model]] = round(vals[i], 1)
            if models:
                all_forecasts[date_str] = models

        current = (month_end + timedelta(days=1)).replace(day=1)
        time.sleep(0.5)  # Rate limiting

    return all_forecasts


# ─── Analysis ───

def compute_cumulative(results):
    """Compute cumulative stats from all daily records."""
    city_errors = {}    # city -> [(error, date)]
    model_errors = {}   # model -> [(error, date)]
    month_errors = {}   # month_num -> [abs_errors]
    all_errors = []

    for rec in results.get("daily_records", []):
        date_str = rec.get("date", "")
        try:
            month = datetime.strptime(date_str, "%Y-%m-%d").month
        except Exception:
            month = 0

        for city, cdata in rec.get("cities", {}).items():
            error = cdata.get("error")
            if error is None:
                continue

            all_errors.append(abs(error))

            if city not in city_errors:
                city_errors[city] = []
            city_errors[city].append(error)

            month_key = str(month)
            if month_key not in month_errors:
                month_errors[month_key] = []
            month_errors[month_key].append(abs(error))

            for model, mdata in cdata.get("models", {}).items():
                merr = mdata.get("error")
                if merr is not None:
                    if model not in model_errors:
                        model_errors[model] = []
                    model_errors[model].append(merr)

    # Per-city
    by_city = {}
    for city, errors in sorted(city_errors.items()):
        by_city[city] = {
            "mae": round(sum(abs(e) for e in errors) / len(errors), 2),
            "bias": round(sum(errors) / len(errors), 2),
            "n": len(errors),
        }

    # Per-model
    by_model = {}
    for model, errors in sorted(model_errors.items()):
        by_model[model] = {
            "mae": round(sum(abs(e) for e in errors) / len(errors), 2),
            "bias": round(sum(errors) / len(errors), 2),
            "n": len(errors),
        }

    # Per-month
    by_month = {}
    for m, errs in sorted(month_errors.items()):
        by_month[m] = {
            "mae": round(sum(errs) / len(errs), 2),
            "n": len(errs),
        }

    # Seasons
    season_map = {12: "winter", 1: "winter", 2: "winter",
                  3: "spring", 4: "spring", 5: "spring",
                  6: "summer", 7: "summer", 8: "summer",
                  9: "fall", 10: "fall", 11: "fall"}
    by_season = {}
    for m_str, data in by_month.items():
        try:
            season = season_map.get(int(m_str), "unknown")
        except ValueError:
            continue
        if season not in by_season:
            by_season[season] = {"total_error": 0, "n": 0}
        by_season[season]["total_error"] += data["mae"] * data["n"]
        by_season[season]["n"] += data["n"]
    for season in by_season:
        n = by_season[season]["n"]
        by_season[season] = {
            "mae": round(by_season[season]["total_error"] / n, 2) if n else 0,
            "n": n,
        }

    scored = len(all_errors)
    results["cumulative"] = {
        "total_days": len(results.get("daily_records", [])),
        "total_city_days": sum(
            len(r.get("cities", {})) for r in results.get("daily_records", [])
        ),
        "scored": scored,
        "overall_mae": round(sum(all_errors) / scored, 2) if scored else None,
        "by_city": by_city,
        "by_model": by_model,
        "by_month": by_month,
        "by_season": by_season,
    }


# ─── Main ───

def run_backtest(start_date, end_date):
    """Main backtest: fetch actuals + forecasts for all 20 cities, compute errors."""
    print("=" * 60)
    print("BACKTEST: %s to %s" % (start_date, end_date))
    print("Cities: %d" % len(CITIES))
    print("=" * 60)

    # Collect all data by date → city
    date_city_data = {}  # {date: {city: {predicted, actual, models}}}

    for city_code, city_info in CITIES.items():
        station_3 = CITY_TO_IEM[city_code]
        network = IEM_NETWORKS.get(station_3)
        if not network:
            print("  [SKIP] %s — no IEM network mapping" % city_code)
            continue

        # 1. Fetch IEM actuals
        print("  [%s] Fetching IEM actuals (%s/%s)..." % (city_code, station_3, network))
        actuals = fetch_iem_actuals(station_3, network, start_date, end_date)
        print("    Got %d daily actuals" % len(actuals))

        # 2. Fetch Open-Meteo historical forecasts
        print("  [%s] Fetching Open-Meteo historical forecasts..." % city_code)
        forecasts = fetch_historical_forecasts(
            city_info["lat"], city_info["lon"], start_date, end_date
        )
        print("    Got %d daily forecasts" % len(forecasts))

        # 3. Match and store
        matched = 0
        for date_str, actual in actuals.items():
            if date_str not in forecasts:
                continue
            models = forecasts[date_str]
            if not models:
                continue

            if date_str not in date_city_data:
                date_city_data[date_str] = {}

            ensemble_mean = sum(models.values()) / len(models)
            error = round(ensemble_mean - actual, 1)

            model_detail = {}
            for model_name, model_temp in models.items():
                model_detail[model_name] = {
                    "mean": model_temp,
                    "error": round(model_temp - actual, 1),
                }

            date_city_data[date_str][city_code] = {
                "predicted_high": round(ensemble_mean, 1),
                "actual_high": actual,
                "error": error,
                "models": model_detail,
            }
            matched += 1

        print("    Matched: %d days with both actual + forecast" % matched)
        time.sleep(0.3)

    # Build daily records
    daily_records = []
    for date_str in sorted(date_city_data.keys()):
        daily_records.append({
            "date": date_str,
            "cities": date_city_data[date_str],
        })

    results = {
        "backtest_period": {"start": start_date, "end": end_date},
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "daily_records": daily_records,
        "cumulative": {},
    }

    compute_cumulative(results)

    # Save
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

    # Print summary
    cum = results["cumulative"]
    print()
    print("=" * 60)
    print("BACKTEST COMPLETE")
    print("=" * 60)
    print("  Days: %d" % cum.get("total_days", 0))
    print("  City-days scored: %d" % cum.get("scored", 0))
    print("  Overall MAE: %.1fF" % (cum.get("overall_mae", 0) or 0))
    print()
    print("  Per-city MAE:")
    for city, data in sorted(cum.get("by_city", {}).items(),
                             key=lambda x: -x[1]["mae"]):
        print("    %s: MAE=%.1fF  bias=%+.1fF  (n=%d)" % (
            city.ljust(4), data["mae"], data["bias"], data["n"]))
    print()
    print("  Per-model MAE:")
    for model, data in sorted(cum.get("by_model", {}).items()):
        print("    %s: MAE=%.1fF  bias=%+.1fF  (n=%d)" % (
            model.ljust(6), data["mae"], data["bias"], data["n"]))
    print()
    print("  Per-season MAE:")
    for season, data in sorted(cum.get("by_season", {}).items()):
        print("    %s: MAE=%.1fF  (n=%d)" % (season.ljust(6), data["mae"], data["n"]))
    print()
    print("  Results saved to: %s" % RESULTS_FILE)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        s, e = sys.argv[1], sys.argv[2]
    else:
        # Default: last 365 days up to yesterday
        end = datetime.utcnow() - timedelta(days=1)
        start = end - timedelta(days=365)
        s = start.strftime("%Y-%m-%d")
        e = end.strftime("%Y-%m-%d")

    run_backtest(s, e)
