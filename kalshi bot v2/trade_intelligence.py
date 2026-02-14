"""
TRADE INTELLIGENCE MODULE v3.1
====================================
Five additions that materially improve performance:

  1. EXIT STRATEGY — Sell positions before settlement when:
     - Edge has disappeared or reversed
     - Profitable exit available (take profit)
     - Intraday data makes outcome clear (cut losses early)

  2. BIAS CORRECTION — Track forecast vs actual outcomes per station.
     Over time, learn systematic biases and adjust probabilities.
     E.g., Central Park reads 1-2°F warmer than models predict.

  3. TIME-OF-DAY SIZING — Bet bigger early morning when models
     are fresh and the market hasn't priced them in yet.
     Bet smaller in afternoon when less uncertainty remains.

  4. INTRADAY TEMPERATURE TRACKING — Fetch actual current temp
     from NWS observation stations. If it's 2 PM and temp already
     hit 42°F, any bucket below 42 is dead → exit those positions.

  5. SETTLEMENT TRACKING — Check settled markets, record wins/losses,
     update bias correction data, calculate real P&L.
"""

import json
import os
import math
import requests
from datetime import datetime, timezone, timedelta
from weather_engine import CITIES
import config


# NWS Observation API (free, no key needed)
NWS_OBS_API = "https://api.weather.gov/stations/{station}/observations/latest"

# Files for persistent learning data
BIAS_DATA_FILE = "bias_history.json"
PNL_DATA_FILE = "pnl_history.json"


class TradeIntelligence:
    """
    Manages position exits, bias learning, time-of-day adjustments,
    intraday temperature monitoring, and settlement tracking.
    """

    def __init__(self, kalshi_client=None, weather_engine=None):
        self.client = kalshi_client
        self.weather = weather_engine
        self.bias_data = self._load_json(BIAS_DATA_FILE, default={})
        self.pnl_data = self._load_json(PNL_DATA_FILE, default={
            "trades": [],
            "total_invested_cents": 0,
            "total_returned_cents": 0,
            "total_profit_cents": 0,
            "wins": 0,
            "losses": 0,
        })
        self._obs_cache = {}

    # ═══════════════════════════════════════════════════════
    # 1. EXIT STRATEGY
    # ═══════════════════════════════════════════════════════

    def check_exits(self, open_positions, weather_engine):
        """
        Review all open positions and decide if any should be exited
        before settlement.

        Returns list of exit recommendations:
        [{"ticker": "...", "reason": "...", "action": "sell", "urgency": "high"}]
        """
        exits = []

        for pos in open_positions:
            ticker = pos.get("ticker", "")
            side = pos.get("side", "")
            entry_price = pos.get("cost_cents", 0) / max(pos.get("contracts", 1), 1)
            city_code = pos.get("city_code", "")

            if not city_code or city_code not in CITIES:
                continue

            # Get current market price
            current_price = self._get_current_price(ticker, side)
            if current_price is None:
                continue

            # Get current actual temperature
            actual_temp = self.get_current_temperature(city_code)

            # ─── EXIT RULE 1: TAKE PROFIT ───
            # If we're up 30%+ on the position, take profit
            if side == "yes" and current_price > 0:
                profit_pct = (current_price - entry_price) / max(entry_price, 1)
                if profit_pct >= 0.30:
                    exits.append({
                        "ticker": ticker,
                        "reason": f"Take profit: up {profit_pct:.0%} (entry {entry_price}¢, now {current_price}¢)",
                        "action": "sell",
                        "urgency": "medium",
                        "current_price": current_price,
                    })
                    continue

            # ─── EXIT RULE 2: INTRADAY TEMP ELIMINATES BUCKET ───
            if actual_temp is not None:
                # Parse what bucket this position is on
                parsed = weather_engine.parse_market_bucket({"ticker": ticker, "title": "", "subtitle": "", "event_ticker": ticker})
                if parsed:
                    temp_low = parsed["temp_low"]
                    temp_high = parsed["temp_high"]

                    # If we bought YES on a bucket and the current temp
                    # already EXCEEDS the bucket's high, the bucket can
                    # still win (high was recorded earlier). But if the
                    # current temp is way BELOW the bucket and it's late
                    # afternoon, the bucket is dead.
                    now_hour = datetime.now().hour

                    if side == "yes":
                        # If it's past 3 PM and current temp hasn't
                        # reached the bucket's low, this bucket is dying
                        if now_hour >= 15 and actual_temp < temp_low - 3:
                            exits.append({
                                "ticker": ticker,
                                "reason": (f"Cut loss: {now_hour}:00, current temp {actual_temp}°F "
                                          f"but bucket needs {temp_low}-{temp_high}°F"),
                                "action": "sell",
                                "urgency": "high",
                                "current_price": current_price,
                            })
                            continue

                    elif side == "no":
                        # If we bought NO and the temp is already in the
                        # bucket, our NO is losing value fast
                        if temp_low <= actual_temp <= temp_high and now_hour >= 12:
                            exits.append({
                                "ticker": ticker,
                                "reason": (f"Cut loss: temp {actual_temp}°F is IN the bucket "
                                          f"{temp_low}-{temp_high}°F at {now_hour}:00"),
                                "action": "sell",
                                "urgency": "high",
                                "current_price": current_price,
                            })
                            continue

            # ─── EXIT RULE 3: EDGE REVERSED ───
            # Re-evaluate the market with fresh ensemble data
            if city_code and weather_engine:
                parsed = weather_engine.parse_market_bucket({"ticker": ticker, "title": "", "subtitle": "", "event_ticker": ticker})
                if parsed:
                    dist = weather_engine.get_temperature_distribution(city_code, parsed.get("target_date"))
                    if dist:
                        new_prob = weather_engine.calculate_bucket_probability(
                            dist, parsed["temp_low"], parsed["temp_high"]
                        )
                        if new_prob is not None:
                            market_prob = current_price / 100.0

                            if side == "yes" and new_prob < market_prob - 0.05:
                                # We bought YES but now models say it's overpriced
                                exits.append({
                                    "ticker": ticker,
                                    "reason": (f"Edge reversed: ensemble now says {new_prob:.0%} "
                                              f"but market is at {market_prob:.0%}"),
                                    "action": "sell",
                                    "urgency": "medium",
                                    "current_price": current_price,
                                })
                            elif side == "no" and (1 - new_prob) < market_prob - 0.05:
                                exits.append({
                                    "ticker": ticker,
                                    "reason": f"Edge reversed on NO position",
                                    "action": "sell",
                                    "urgency": "medium",
                                    "current_price": current_price,
                                })

        return exits

    # ═══════════════════════════════════════════════════════
    # 2. BIAS CORRECTION
    # ═══════════════════════════════════════════════════════

    def get_bias_adjustment(self, city_code):
        """
        Return the average bias (in °F) for a station.
        Positive = station reads warmer than models predict.
        Negative = station reads cooler.

        Returns 0.0 if not enough data yet.
        """
        city_key = f"bias_{city_code}"
        if city_key not in self.bias_data:
            return 0.0

        records = self.bias_data[city_key]
        if len(records) < 5:
            return 0.0  # Need at least 5 data points

        # Use last 30 records (rolling window)
        recent = records[-30:]
        biases = [r["actual"] - r["predicted"] for r in recent]
        avg_bias = sum(biases) / len(biases)

        return round(avg_bias, 1)

    def record_bias_datapoint(self, city_code, predicted_high, actual_high):
        """
        Record a new forecast vs actual data point for bias learning.
        Called after settlement.
        """
        city_key = f"bias_{city_code}"
        if city_key not in self.bias_data:
            self.bias_data[city_key] = []

        self.bias_data[city_key].append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "predicted": predicted_high,
            "actual": actual_high,
            "bias": actual_high - predicted_high,
        })

        # Keep last 90 days of data
        self.bias_data[city_key] = self.bias_data[city_key][-90:]
        self._save_json(BIAS_DATA_FILE, self.bias_data)

    def apply_bias_to_distribution(self, distribution, city_code):
        """
        Shift the entire distribution by the learned bias.
        Returns adjusted distribution (or original if no bias data).
        """
        bias = self.get_bias_adjustment(city_code)
        if abs(bias) < 0.5:
            return distribution  # Negligible bias

        # Shift all raw highs by the bias amount
        adjusted_highs = [t + bias for t in distribution["raw_highs"]]

        # Rebuild the distribution with shifted data
        # (We import the builder from weather_engine)
        from weather_engine import WeatherEngine
        engine = WeatherEngine()
        adjusted = engine._build_distribution(
            city_code,
            distribution["target_date"],
            adjusted_highs,
            distribution["sources_used"],
        )
        adjusted["bias_applied"] = bias
        return adjusted

    # ═══════════════════════════════════════════════════════
    # 3. TIME-OF-DAY SIZING MULTIPLIER
    # ═══════════════════════════════════════════════════════

    def get_time_multiplier(self, city_code):
        """
        Return a sizing multiplier based on time of day.

        EARLY MORNING (6-9 AM local):  1.3x — Fresh model runs,
          markets haven't priced them in. Biggest edge window.
        MORNING (9 AM-12 PM):           1.0x — Normal
        AFTERNOON (12-4 PM):            0.7x — Less uncertainty,
          temperature is largely known. Smaller edge.
        EVENING (4 PM+):                0.4x — High temp usually
          already recorded. Very little edge left.
        OVERNIGHT (before 6 AM):        0.8x — Models run overnight,
          good edge but market is thin (low liquidity).
        """
        city = CITIES.get(city_code, {})
        tz_name = city.get("timezone", "America/New_York")

        # Approximate local hour (proper timezone would need pytz)
        utc_now = datetime.now(timezone.utc)
        # Simple offset: Eastern = -5, Central = -6
        if "Chicago" in tz_name or "Central" in tz_name:
            offset = -6
        else:
            offset = -5

        local_hour = (utc_now.hour + offset) % 24

        if 6 <= local_hour < 9:
            return 1.3, "Early morning — peak edge window"
        elif 9 <= local_hour < 12:
            return 1.0, "Morning — normal sizing"
        elif 12 <= local_hour < 16:
            return 0.7, "Afternoon — reduced edge"
        elif 16 <= local_hour < 22:
            return 0.4, "Evening — minimal edge, temp likely known"
        else:
            return 0.8, "Overnight — good models, thin market"

    # ═══════════════════════════════════════════════════════
    # 4. INTRADAY TEMPERATURE TRACKING
    # ═══════════════════════════════════════════════════════

    def get_current_temperature(self, city_code):
        """
        Fetch the latest observed temperature from NWS station.
        This is the ACTUAL current temperature, not a forecast.

        Returns temperature in °F or None.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        station = city["nws_station"]

        # Cache for 10 minutes
        cache_key = f"obs_{station}"
        if cache_key in self._obs_cache:
            cached = self._obs_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 600:  # 10 min
                return cached["temp"]

        try:
            url = NWS_OBS_API.format(station=station)
            headers = {
                "User-Agent": "KalshiBot/3.1 (trading-bot@example.com)",
                "Accept": "application/geo+json",
            }
            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code != 200:
                return None

            data = response.json()
            props = data.get("properties", {})

            # Temperature comes in Celsius from NWS API
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is None:
                return None

            # Convert to Fahrenheit
            temp_f = round(temp_c * 9 / 5 + 32)

            self._obs_cache[cache_key] = {
                "temp": temp_f,
                "fetched_at": datetime.now(),
            }

            return temp_f

        except Exception as e:
            return None

    def get_todays_high_so_far(self, city_code):
        """
        Fetch recent observations and find today's high so far.
        More reliable than a single latest reading.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        station = city["nws_station"]

        try:
            # Get last 24 hours of observations
            url = f"https://api.weather.gov/stations/{station}/observations"
            headers = {
                "User-Agent": "KalshiBot/3.1 (trading-bot@example.com)",
                "Accept": "application/geo+json",
            }
            params = {"limit": 48}  # ~24 hours of hourly observations
            response = requests.get(url, headers=headers, params=params, timeout=15)

            if response.status_code != 200:
                return None

            data = response.json()
            features = data.get("features", [])

            # Filter to today's observations and find max temp
            today = datetime.now().strftime("%Y-%m-%d")
            todays_temps = []

            for obs in features:
                props = obs.get("properties", {})
                timestamp = props.get("timestamp", "")
                if today in timestamp:
                    temp_c = props.get("temperature", {}).get("value")
                    if temp_c is not None:
                        temp_f = round(temp_c * 9 / 5 + 32)
                        todays_temps.append(temp_f)

            if todays_temps:
                return max(todays_temps)

        except Exception:
            pass

        return None

    # ═══════════════════════════════════════════════════════
    # 5. SETTLEMENT TRACKING & P&L
    # ═══════════════════════════════════════════════════════

    def check_settlements(self, trade_log, risk_manager, quant=None):
        """
        Check if any past trades have settled.
        In DRY_RUN mode, auto-settle trades when their market date has passed.
        Update P&L, bias data, and model accuracy weights.

        Returns list of newly settled trades.
        """
        settled = []
        today = datetime.now().strftime("%Y-%m-%d")

        for trade in trade_log:
            if trade.get("settled"):
                continue  # Already processed

            ticker = trade.get("ticker", "")
            if not ticker:
                continue

            # ─── DRY RUN: Auto-settle when the market date has passed ───
            if config.DRY_RUN:
                # Extract date from ticker like KXHIGHNY-26FEB12-B36.5
                trade_date = self._extract_date_from_ticker(ticker)
                if trade_date and trade_date < today:
                    # Market date has passed — check actual temp to determine result
                    city_code = trade.get("city_code", "")
                    actual_high = self.get_todays_high_so_far(city_code) if city_code else None

                    # If we can't get actual data, mark as expired
                    trade["settled"] = True
                    trade["result"] = "expired_dry_run"
                    trade["profit_cents"] = 0
                    print(f"  ⏰ EXPIRED (dry run): {ticker} — market date passed")

                    # Release the exposure
                    cost_cents = trade.get("cost_cents", 0)
                    risk_manager.release_exposure(ticker, cost_cents, city_code)
                    settled.append(trade)
                continue  # In DRY_RUN, skip the API settlement check

            # ─── LIVE MODE: Check via Kalshi API ───
            settlement = self._check_market_settlement(ticker)
            if not settlement:
                continue

            # Calculate P&L
            side = trade.get("side", "")
            contracts = trade.get("contracts", 0)
            cost_cents = trade.get("cost_cents", 0)
            result = settlement.get("result", "")  # "yes" or "no"

            if (side == "yes" and result == "yes") or (side == "no" and result == "no"):
                # WIN
                payout = contracts * 100  # $1 per contract
                profit = payout - cost_cents
                trade["settled"] = True
                trade["result"] = "win"
                trade["payout_cents"] = payout
                trade["profit_cents"] = profit

                self.pnl_data["total_returned_cents"] += payout
                self.pnl_data["total_profit_cents"] += profit
                self.pnl_data["wins"] += 1

                risk_manager.record_win(profit)
                risk_manager.release_exposure(ticker, cost_cents, trade.get("city_code", ""))
                print(f"  ✓ WIN: {ticker} → +${profit/100:.2f}")
            else:
                # LOSS
                trade["settled"] = True
                trade["result"] = "loss"
                trade["payout_cents"] = 0
                trade["profit_cents"] = -cost_cents

                self.pnl_data["total_profit_cents"] -= cost_cents
                self.pnl_data["losses"] += 1

                risk_manager.record_loss(cost_cents)
                risk_manager.release_exposure(ticker, cost_cents, trade.get("city_code", ""))
                print(f"  ✗ LOSS: {ticker} → -${cost_cents/100:.2f}")

            self.pnl_data["total_invested_cents"] += cost_cents

            # Record bias data if this was a weather market
            city_code = trade.get("city_code", "")
            actual_temp = settlement.get("actual_temp")
            if city_code and actual_temp is not None:
                predicted = trade.get("predicted_high")
                if predicted:
                    self.record_bias_datapoint(
                        city_code, predicted, actual_temp
                    )

                # Update per-model accuracy weights immediately
                if quant and actual_temp is not None:
                    _update_model_accuracy_from_settlement(
                        quant, city_code, actual_temp
                    )

            settled.append(trade)

        # Save updated P&L
        self._save_json(PNL_DATA_FILE, self.pnl_data)

        return settled

    def _check_market_settlement(self, ticker):
        """
        Check if a market has settled via Kalshi API.
        Returns {"result": "yes"/"no", "actual_temp": N} or None.
        """
        if not self.client:
            return None

        try:
            market_data = self.client.get_market(ticker)
            if not market_data:
                return None

            market = market_data.get("market", {})
            status = market.get("status", "")
            result = market.get("result", "")

            if status == "settled" and result:
                return {"result": result, "actual_temp": None}

        except Exception:
            pass

        return None

    def print_pnl(self):
        """Print profit/loss summary."""
        total = self.pnl_data["wins"] + self.pnl_data["losses"]
        if total == 0:
            print("  [P&L] No settled trades yet")
            return

        win_rate = self.pnl_data["wins"] / total if total > 0 else 0
        invested = self.pnl_data["total_invested_cents"]
        profit = self.pnl_data["total_profit_cents"]
        roi = (profit / invested * 100) if invested > 0 else 0

        print(f"\n  ┌─ Profit & Loss ────────────────────────────────")
        print(f"  │  Total trades settled: {total}")
        print(f"  │  Win rate:      {self.pnl_data['wins']}/{total} ({win_rate:.0%})")
        print(f"  │  Total invested: ${invested/100:.2f}")
        print(f"  │  Total profit:   ${profit/100:.2f}")
        print(f"  │  ROI:            {roi:.1f}%")

        # Bias info
        for city_code in config.WEATHER_CITIES:
            bias = self.get_bias_adjustment(city_code)
            if abs(bias) >= 0.5:
                direction = "warmer" if bias > 0 else "cooler"
                print(f"  │  {city_code} bias:    {abs(bias):.1f}°F {direction} than models")

        print(f"  └──────────────────────────────────────────────\n")

    def print_intraday_temps(self):
        """Print current observed temps for all cities."""
        print(f"  ┌─ Current Temperatures ───────────────────────")
        for city_code in config.WEATHER_CITIES:
            temp = self.get_current_temperature(city_code)
            high = self.get_todays_high_so_far(city_code)
            city_name = CITIES[city_code]["name"]
            if temp is not None:
                high_str = f", today's high so far: {high}°F" if high else ""
                print(f"  │  {city_code} ({city_name}): {temp}°F{high_str}")
            else:
                print(f"  │  {city_code}: No observation available")
        print(f"  └──────────────────────────────────────────────")

    # ═══════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════

    def _extract_date_from_ticker(self, ticker):
        """
        Extract the market date from a ticker like KXHIGHNY-26FEB12-B36.5
        Returns date string in YYYY-MM-DD format, or None.
        """
        try:
            parts = ticker.split("-")
            if len(parts) >= 2:
                date_part = parts[1]  # e.g., "26FEB12"
                # Parse: 2-digit year + 3-letter month + 2-digit day
                if len(date_part) >= 7:
                    year = int("20" + date_part[:2])
                    month_str = date_part[2:5].upper()
                    day = int(date_part[5:7])
                    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
                              "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
                              "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
                    month = months.get(month_str)
                    if month:
                        return f"{year}-{month:02d}-{day:02d}"
        except Exception:
            pass
        return None

    def _get_current_price(self, ticker, side):
        """Get current market price for a ticker."""
        if not self.client:
            return None
        try:
            data = self.client.get_market(ticker)
            if data:
                market = data.get("market", {})
                if side == "yes":
                    return market.get("yes_bid", 0) or market.get("last_price", 0)
                else:
                    return market.get("no_bid", 0) or (100 - (market.get("last_price", 0) or 0))
        except Exception:
            pass
        return None

    def _load_json(self, filepath, default=None):
        try:
            if os.path.exists(filepath):
                with open(filepath) as f:
                    return json.load(f)
        except Exception:
            pass
        return default if default is not None else {}

    def _save_json(self, filepath, data):
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
# MODULE-LEVEL HELPERS
# ═══════════════════════════════════════════════════════

# Mapping from signal_confirmer model keys → quant_analytics model keys
_CONFIRMER_TO_QUANT = {
    "nws_gfs": "gfs_ensemble",
    "ecmwf": "ecmwf_ifs",
    "icon": "icon_eps",
    "gem": "gem_ensemble",
}

# Open-Meteo deterministic forecast endpoints (same as signal_confirmer)
_DETERMINISTIC_APIS = {
    "nws_gfs": "https://api.open-meteo.com/v1/gfs",
    "ecmwf": "https://api.open-meteo.com/v1/ecmwf",
    "icon": "https://api.open-meteo.com/v1/dwd-icon",
    "gem": "https://api.open-meteo.com/v1/gem",
}


def _update_model_accuracy_from_settlement(quant, city_code, actual_temp):
    """
    After a trade settles, fetch what each deterministic model predicted
    for that day and record accuracy data in quant_analytics.

    This feeds the dynamic model weighting system so better models
    get higher weight over time.
    """
    city = CITIES.get(city_code)
    if not city:
        return

    # Use yesterday's date since settlements happen after the market date
    target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    for confirmer_key, api_url in _DETERMINISTIC_APIS.items():
        quant_key = _CONFIRMER_TO_QUANT.get(confirmer_key)
        if not quant_key:
            continue

        try:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city.get("timezone", "auto"),
                "start_date": target_date,
                "end_date": target_date,
            }

            response = requests.get(api_url, params=params, timeout=15)
            if response.status_code != 200:
                continue

            data = response.json()
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                forecast_high = round(temps[0])
                quant.record_model_accuracy(
                    city_code, quant_key, forecast_high, actual_temp
                )
        except Exception:
            continue
