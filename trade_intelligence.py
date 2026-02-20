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
from zoneinfo import ZoneInfo
from weather_engine import CITIES
import config


# NWS Observation API (free, no key needed)
NWS_OBS_API = "https://api.weather.gov/stations/{station}/observations/latest"

# Files for persistent learning data
BIAS_DATA_FILE = os.path.join(config.STATE_DIR, "bias_history.json")
PNL_DATA_FILE = config.PNL_HISTORY_FILE


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
                parsed = self._parse_position_bucket(ticker, weather_engine, pos.get("title", ""))
                if parsed:
                    temp_low = parsed["temp_low"]
                    temp_high = parsed["temp_high"]

                    # If we bought YES on a bucket and the current temp
                    # already EXCEEDS the bucket's high, the bucket can
                    # still win (high was recorded earlier). But if the
                    # current temp is way BELOW the bucket and it's late
                    # afternoon, the bucket is dead.
                    city_tz = CITIES.get(city_code, {}).get("timezone", "America/New_York")
                    now_hour = datetime.now(ZoneInfo(city_tz)).hour

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
                parsed = self._parse_position_bucket(ticker, weather_engine, pos.get("title", ""))
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
    # 1b. PORTFOLIO REVIEW (graduated position management)
    # ═══════════════════════════════════════════════════════

    def review_portfolio(self, open_positions, weather_engine,
                         volatility_engine=None, signal_confirmer=None,
                         spx_confirmer=None):
        """
        Full re-evaluation of every open position using fresh model data.
        Returns a list of action recommendations:
          hold  — edge still healthy, do nothing
          pare  — edge decaying, sell some contracts
          full_exit — edge gone or reversed, sell everything
          hedge — exit via opposite side (better liquidity path)
        """
        actions = []

        for pos in open_positions:
            ticker = pos.get("ticker", "")
            side = pos.get("side", "")
            city_code = pos.get("city_code", "")
            contracts = pos.get("contracts", 1)
            cost_cents = pos.get("cost_cents", 0)
            entry_edge = pos.get("edge", 0)
            entry_price = cost_cents / max(contracts, 1)

            if not ticker:
                continue

            # Track position age (used for whipsaw guard, bypassed for confirmed losses)
            age_minutes = None
            timestamp = pos.get("timestamp")
            if timestamp:
                try:
                    placed_at = datetime.fromisoformat(timestamp)
                    age_minutes = (datetime.now() - placed_at).total_seconds() / 60
                except Exception:
                    pass

            # Determine market type
            is_weather = city_code and city_code in CITIES
            is_sp500 = city_code == "SP500"

            if not is_weather and not is_sp500:
                continue

            # Get current market price
            current_price = self._get_current_price(ticker, side)
            if current_price is None:
                continue

            # ─── WEATHER OBSERVATION CHECKS (run BEFORE age gate — confirmed losses exit immediately) ───
            if is_weather and weather_engine:
                actual_temp = self.get_current_temperature(city_code)
                todays_high = self.get_todays_high_so_far(city_code)
                if actual_temp is not None or todays_high is not None:
                    parsed = self._parse_position_bucket(ticker, weather_engine, pos.get("title", ""))
                    if parsed:
                        temp_low = parsed["temp_low"]
                        temp_high = parsed["temp_high"]
                        target_date = parsed.get("target_date")
                        # Use city's local timezone for date/hour — NOT UTC.
                        # At 7 PM CT (Dallas), UTC is already Feb 20, but the
                        # market date is still Feb 19. Using UTC breaks is_today.
                        city_tz_name = CITIES.get(city_code, {}).get("timezone", "America/New_York")
                        city_now = datetime.now(ZoneInfo(city_tz_name))
                        today_str = city_now.strftime("%Y-%m-%d")
                        now_hour = city_now.hour
                        is_today = (target_date == today_str) if target_date else True
                        obs_high = todays_high if todays_high is not None else actual_temp

                        # HIGH ALREADY EXCEEDED BUCKET — YES is a guaranteed loss
                        # Daily high can only go up, never down.
                        if is_today and side == "yes" and obs_high is not None and obs_high > temp_high:
                            actions.append({
                                "ticker": ticker, "action": "full_exit", "urgency": "high",
                                "reason": f"High already {obs_high:.1f}F, exceeds bucket {temp_low}-{temp_high}F — guaranteed loss",
                                "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                                "city_code": city_code, "side": side, "contracts": contracts,
                                "cost_cents": cost_cents,
                            })
                            continue

                        # YES + temp far below bucket — dynamic threshold by time of day
                        # Earlier in the day needs a larger gap (temp could still rise)
                        # Later in the day, even a small gap is fatal
                        if is_today and side == "yes" and actual_temp is not None:
                            if now_hour >= 18:
                                gap_needed = 1   # After 6 PM: 1°F gap enough
                            elif now_hour >= 15:
                                gap_needed = 3   # After 3 PM: 3°F gap
                            elif now_hour >= 12:
                                gap_needed = 6   # After noon: 6°F gap
                            else:
                                gap_needed = 10  # Morning: 10°F gap (temp can still rise a lot)

                            if actual_temp < temp_low - gap_needed:
                                actions.append({
                                    "ticker": ticker, "action": "full_exit", "urgency": "high",
                                    "reason": f"Temp {actual_temp:.0f}F at {now_hour}:00, {temp_low - actual_temp:.0f}F below bucket {temp_low}-{temp_high}F",
                                    "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                                    "city_code": city_code, "side": side, "contracts": contracts,
                                    "cost_cents": cost_cents,
                                })
                                continue

                        # NO + daily high already in bucket — confirmed loss
                        # Must check obs_high (daily max), NOT actual_temp (current reading).
                        # At 7 PM the current temp drops to 65°F but daily high was 78°F.
                        if is_today and side == "no" and now_hour >= 12:
                            check_val = obs_high if obs_high is not None else actual_temp
                            if check_val is not None and temp_low <= check_val <= temp_high:
                                actions.append({
                                    "ticker": ticker, "action": "full_exit", "urgency": "high",
                                    "reason": f"Daily high {check_val:.0f}F is IN bucket {temp_low}-{temp_high}F — NO loses",
                                    "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                                    "city_code": city_code, "side": side, "contracts": contracts,
                                    "cost_cents": cost_cents,
                                })
                                continue

                        # ROUNDING BUFFER EXIT for NO positions:
                        # NWS 5-min stations have ±1°F conversion error. The real temperature
                        # (from raw 1-min readings used for settlement) can be higher than
                        # the displayed value. If observed high is within 1°F of bucket floor,
                        # the REAL temp may already be inside the bucket.
                        if is_today and side == "no" and obs_high is not None and now_hour >= 14:
                            if obs_high >= temp_low - config.ROUNDING_BUFFER_HARD_F and obs_high < temp_low:
                                actions.append({
                                    "ticker": ticker, "action": "full_exit", "urgency": "high",
                                    "reason": (f"Rounding buffer exit: observed high {obs_high:.0f}F is within "
                                              f"{config.ROUNDING_BUFFER_HARD_F}°F of bucket floor {temp_low}F — "
                                              f"real temp may already be inside bucket"),
                                    "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                                    "city_code": city_code, "side": side, "contracts": contracts,
                                    "cost_cents": cost_cents,
                                })
                                continue

            # Skip positions that are too young for probabilistic review (whipsaw guard)
            # Note: observation-based confirmed losses above bypass this gate
            if age_minutes is not None and age_minutes < config.MIN_REVIEW_AGE_MINUTES:
                continue

            # ─── WEATHER-SPECIFIC PROFIT CHECKS ───
            if is_weather:
                # Take profit check (both YES and NO positions)
                if current_price > 0 and entry_price > 0:
                    profit_pct = (current_price - entry_price) / max(entry_price, 1)
                    if profit_pct >= config.TAKE_PROFIT_PCT:
                        actions.append({
                            "ticker": ticker, "action": "full_exit", "urgency": "medium",
                            "reason": f"Take profit: up {profit_pct:.0%} (entry {entry_price:.0f}c, now {current_price}c)",
                            "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                            "city_code": city_code, "side": side, "contracts": contracts,
                            "cost_cents": cost_cents,
                        })
                        continue

            # ─── FRESH PROBABILITY RE-EVALUATION ───
            new_prob = None
            dist = None
            parsed = None

            if is_weather and weather_engine:
                parsed = self._parse_position_bucket(ticker, weather_engine, pos.get("title", ""))
                if parsed:
                    dist = weather_engine.get_temperature_distribution(
                        city_code, parsed.get("target_date")
                    )
                    if dist:
                        dist = self.apply_bias_to_distribution(dist, city_code)
                        new_prob = weather_engine.calculate_bucket_probability(
                            dist, parsed["temp_low"], parsed["temp_high"]
                        )

                    # Override stale ensemble with observation reality for TODAY's markets.
                    # After 2 PM local, the daily high is largely set. If obs_high is available,
                    # use it to correct the probability — the ensemble forecast from 6+ hours
                    # ago doesn't know the actual temperature.
                    target_date = parsed.get("target_date")
                    city_tz_name = CITIES.get(city_code, {}).get("timezone", "America/New_York")
                    city_now = datetime.now(ZoneInfo(city_tz_name))
                    today_str = city_now.strftime("%Y-%m-%d")
                    now_h = city_now.hour
                    if target_date == today_str and now_h >= 14:
                        obs_h = self.get_todays_high_so_far(city_code)
                        if obs_h is not None:
                            t_lo = parsed["temp_low"]
                            t_hi = parsed["temp_high"]
                            if t_lo <= obs_h <= t_hi:
                                # High is IN the bucket — near certain YES
                                new_prob = 0.95
                            elif obs_h > t_hi:
                                # High exceeded bucket — this bucket lost
                                new_prob = 0.02
                            elif obs_h >= t_lo - 2:
                                # High is close to bucket floor — uncertain
                                new_prob = 0.40
                            # else: obs_h well below bucket, ensemble is reasonable

            elif is_sp500 and volatility_engine:
                parsed = volatility_engine.parse_market_bracket(
                    {"ticker": ticker, "title": "", "subtitle": "", "event_ticker": ticker}
                )
                if parsed:
                    dist = volatility_engine.get_price_distribution(parsed.get("target_date"))
                    if dist:
                        new_prob = volatility_engine.calculate_bracket_probability(
                            dist, parsed["price_low"], parsed["price_high"]
                        )

            if new_prob is None:
                continue

            # ─── CONFIRMER RE-CHECK (NWS settlement source validation) ───
            # Re-run the signal confirmer on open positions to catch cases where
            # NWS now disagrees with our thesis.  NWS is the settlement source —
            # if it says we're wrong, exit regardless of ensemble edge.
            if is_weather and signal_confirmer and parsed and current_price > 0:
                city_info = CITIES.get(city_code)
                if city_info:
                    try:
                        # Confirmer expects YES-side values: bucket probability and YES price.
                        # For NO positions, current_price is the NO bid — convert to YES price.
                        yes_price = current_price if side == "yes" else (100 - current_price)
                        recheck = signal_confirmer.confirm_signal(
                            city_info=city_info,
                            target_date=parsed.get("target_date"),
                            temp_low=parsed["temp_low"],
                            temp_high=parsed["temp_high"],
                            ensemble_prob=new_prob,
                            market_price_cents=yes_price,
                        )
                        if recheck.get("verdict") == "REJECT":
                            nws_detail = recheck.get("summary", "NWS disagrees")
                            # Safety guard: only auto-exit if position is already losing.
                            # If profitable or near breakeven, downgrade to medium urgency
                            # so it logs a recommendation but doesn't auto-sell winners.
                            entry_price = cost_cents / max(contracts, 1)
                            pnl_pct = ((current_price - entry_price) / max(entry_price, 1)) if entry_price > 0 else 0
                            if pnl_pct >= -0.10:
                                # Profitable or small loss — warn but don't auto-sell
                                urgency = "medium"
                                reason_prefix = "Confirmer REJECT (position not losing, manual review recommended)"
                            else:
                                # Already losing significantly — auto-exit
                                urgency = "high"
                                reason_prefix = "Confirmer REJECT on re-check"
                            actions.append({
                                "ticker": ticker, "action": "full_exit", "urgency": urgency,
                                "reason": f"{reason_prefix}: {nws_detail}",
                                "current_price": current_price, "new_edge": 0,
                                "entry_edge": entry_edge,
                                "city_code": city_code, "side": side,
                                "contracts": contracts, "cost_cents": cost_cents,
                                "review_detail": {
                                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                                    "action": "full_exit",
                                    "confirmer_verdict": recheck.get("verdict"),
                                    "confirmer_summary": nws_detail,
                                    "pnl_pct_at_review": round(pnl_pct, 4),
                                    "urgency_reason": "auto" if urgency == "high" else "manual_review",
                                },
                            })
                            continue
                    except Exception as e:
                        print(f"    [REVIEW] Confirmer re-check failed for {ticker}: {e}")

            # Absolute probability floor — exit if ensemble strongly contradicts position
            # regardless of entry edge. Catches positions entered on stale/bad data.
            if side == "yes" and new_prob < 0.15:
                actions.append({
                    "ticker": ticker, "action": "full_exit", "urgency": "high",
                    "reason": f"Ensemble prob {new_prob:.0%} contradicts YES position (below 15% floor)",
                    "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                    "city_code": city_code, "side": side, "contracts": contracts,
                    "cost_cents": cost_cents,
                })
                continue
            if side == "no" and new_prob > 0.85:
                actions.append({
                    "ticker": ticker, "action": "full_exit", "urgency": "high",
                    "reason": f"Ensemble prob {new_prob:.0%} contradicts NO position (above 85% floor)",
                    "current_price": current_price, "new_edge": 0, "entry_edge": entry_edge,
                    "city_code": city_code, "side": side, "contracts": contracts,
                    "cost_cents": cost_cents,
                })
                continue

            # Calculate current edge based on position side
            market_prob = current_price / 100.0
            if side == "yes":
                new_edge = new_prob - market_prob
            else:
                # For NO positions: our edge is (1 - bucket_prob) vs what we paid
                new_edge = (1.0 - new_prob) - market_prob

            # Default entry_edge if not recorded
            if entry_edge <= 0:
                entry_edge = config.MIN_EDGE

            # ─── GRADUATED RESPONSE ───
            edge_decay_pct = max(0, min(1.0, (entry_edge - new_edge) / entry_edge)) if entry_edge > 0 else 1.0

            # Build review detail for dashboard
            review_detail = self._build_review_detail(
                pos, current_price, new_prob, new_edge,
                entry_edge, edge_decay_pct, dist, parsed, side
            )

            base = {
                "ticker": ticker, "current_price": current_price,
                "new_edge": new_edge, "entry_edge": entry_edge,
                "city_code": city_code, "side": side, "contracts": contracts,
                "cost_cents": cost_cents,
                "review_detail": review_detail,
            }

            if new_edge >= entry_edge * config.EDGE_DECAY_PARE_THRESHOLD:
                # Edge still healthy — hold
                actions.append({**base, "action": "hold", "urgency": "low",
                    "reason": f"Edge healthy: {new_edge:.1%} (was {entry_edge:.1%})"})

            elif new_edge > 0:
                # Edge decayed significantly but still positive — pare down
                sell_count = self._calculate_pare_contracts(pos, edge_decay_pct)
                actions.append({**base, "action": "pare", "urgency": "medium",
                    "reason": f"Edge decayed: {new_edge:.1%} (was {entry_edge:.1%}, {edge_decay_pct:.0%} decay)",
                    "sell_contracts": sell_count})

            elif new_edge > config.EDGE_REVERSAL_THRESHOLD:
                # Edge gone (near zero) — full exit
                actions.append({**base, "action": "full_exit", "urgency": "medium",
                    "reason": f"Edge gone: {new_edge:.1%} (was {entry_edge:.1%})"})

            else:
                # Edge reversed hard — choose best exit path
                hedge_info = self._evaluate_hedge(ticker, pos, current_price)
                if hedge_info["use_hedge"]:
                    actions.append({**base, "action": "hedge", "urgency": "high",
                        "reason": f"Edge reversed to {new_edge:.1%} — exiting via opposite side",
                        "hedge_side": hedge_info["hedge_side"],
                        "hedge_price": hedge_info["hedge_price"],
                        "hedge_contracts": min(hedge_info["hedge_contracts"], contracts)})
                else:
                    actions.append({**base, "action": "full_exit", "urgency": "high",
                        "reason": f"Edge reversed to {new_edge:.1%} (was {entry_edge:.1%})"})

        return actions

    def _calculate_pare_contracts(self, position, edge_decay_pct):
        """Calculate how many contracts to sell in a partial exit.
        Sells proportional to edge decay, always keeps at least 1 contract."""
        total = position.get("contracts", 1)
        if total <= 1:
            return 1  # Only 1 contract — full exit is the only option

        sell_fraction = min(0.75, edge_decay_pct)  # Cap at 75%
        sell_count = max(1, round(total * sell_fraction))
        sell_count = min(sell_count, total - 1)  # Keep at least 1
        return sell_count

    def _get_market_prices(self, ticker):
        """Fetch both sides' current prices for a ticker."""
        if not self.client:
            return None
        try:
            data = self.client.get_market(ticker)
            if data:
                market = data.get("market", {})
                return {
                    "yes_bid": market.get("yes_bid", 0) or 0,
                    "yes_ask": market.get("yes_ask", 0) or 0,
                    "no_bid": market.get("no_bid", 0) or 0,
                    "no_ask": market.get("no_ask", 0) or 0,
                }
        except Exception:
            pass
        return None

    def _evaluate_hedge(self, ticker, position, current_price):
        """Evaluate whether exiting via the opposite side is better than selling.

        On Kalshi, buying the opposite side when you hold a position effectively
        closes it. This is useful when our side's bid is thin but the opposite
        side's ask has liquidity.
        """
        side = position.get("side", "yes")
        contracts = position.get("contracts", 1)

        prices = self._get_market_prices(ticker)
        if not prices:
            return {"use_hedge": False}

        if side == "yes":
            our_bid = prices["yes_bid"]
            opp_ask = prices["no_ask"]
            hedge_side = "no"
        else:
            our_bid = prices["no_bid"]
            opp_ask = prices["yes_ask"]
            hedge_side = "yes"

        # Prefer opposite side when:
        # 1. Our bid is 0 (can't sell directly)
        # 2. Opposite ask offers a better effective exit
        #    Selling YES at yes_bid=40 gives us 40c back
        #    Buying NO at no_ask=55 costs 55c but pays 100c if NO wins (locking in 100-55=45c)
        #    The "lock-in" value of buying opposite = 100 - opp_ask
        if our_bid == 0 and opp_ask > 0 and opp_ask < 95:
            return {
                "use_hedge": True,
                "hedge_side": hedge_side,
                "hedge_price": opp_ask,
                "hedge_contracts": contracts,
            }

        if our_bid > 0 and opp_ask > 0:
            sell_value = our_bid
            hedge_lock_value = 100 - opp_ask  # guaranteed payout minus cost
            if hedge_lock_value > sell_value:
                return {
                    "use_hedge": True,
                    "hedge_side": hedge_side,
                    "hedge_price": opp_ask,
                    "hedge_contracts": contracts,
                }

        return {"use_hedge": False}

    # ═══════════════════════════════════════════════════════
    # 1c. REVIEW DETAIL BUILDERS
    # ═══════════════════════════════════════════════════════

    def _build_review_detail(self, pos, current_price, new_prob, new_edge,
                             entry_edge, edge_decay_pct, dist, parsed, side):
        """Package all review intermediate data into a structured dict for the dashboard."""
        entry_price = pos.get("cost_cents", 0) / max(pos.get("contracts", 1), 1)
        # _get_current_price() returns the bid for our side (yes_bid or no_bid),
        # so P&L = (what we'd sell for - what we paid) for both YES and NO.
        pnl_pct = ((current_price - entry_price) / max(entry_price, 1)) if entry_price > 0 else 0

        market_prob = current_price / 100.0

        detail = {
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "current_price": current_price,
            "entry_price": round(entry_price, 1),
            "pnl_pct": round(pnl_pct, 4),
            "current_edge": round(new_edge, 4),
            "entry_edge": round(entry_edge, 4),
            "edge_decay_pct": round(edge_decay_pct, 4),
            "current_prob": round(new_prob, 4) if new_prob is not None else None,
            "market_prob": round(market_prob, 4),
            "is_underwater": pnl_pct < -0.10,
        }

        # Add weather forecast data if available
        if dist:
            detail.update({
                "forecast_mean": dist.get("forecasted_high_mean"),
                "forecast_median": dist.get("forecasted_high_median"),
                "forecast_min": dist.get("forecasted_high_min"),
                "forecast_max": dist.get("forecasted_high_max"),
                "forecast_spread": dist.get("spread"),
                "forecast_std_dev": dist.get("std_dev"),
                "forecast_confidence": dist.get("confidence"),
                "ensemble_members": len(dist.get("raw_highs", [])),
                "sources_used": dist.get("sources_used", []),
                "bias_applied": dist.get("bias_applied", 0),
            })

        # Add bucket info if available
        if parsed:
            detail.update({
                "bucket_low": parsed.get("temp_low") or parsed.get("price_low"),
                "bucket_high": parsed.get("temp_high") or parsed.get("price_high"),
                "target_date": parsed.get("target_date"),
            })

        # Generate human-readable explanation
        detail["explanation"] = self._build_explanation(detail, side, pos)

        return detail

    def _build_explanation(self, detail, side, pos):
        """Generate a human-readable paragraph explaining the position status."""
        parts = []

        # P&L status
        pnl_pct = detail.get("pnl_pct", 0)
        if pnl_pct < -0.10:
            parts.append(f"Position is underwater at {pnl_pct:.0%}.")
        elif pnl_pct < 0:
            parts.append(f"Position is slightly down at {pnl_pct:.0%}.")
        else:
            parts.append(f"Position is up {pnl_pct:.0%}.")

        # Edge health
        current_edge = detail.get("current_edge", 0)
        entry_edge = detail.get("entry_edge", 0)
        decay = detail.get("edge_decay_pct", 0)
        if current_edge > 0 and decay < 0.5:
            parts.append(f"Edge is healthy at {current_edge:.1%} (was {entry_edge:.1%}, {decay:.0%} decay).")
        elif current_edge > 0:
            parts.append(f"Edge has decayed significantly to {current_edge:.1%} (was {entry_edge:.1%}, {decay:.0%} decay).")
        elif current_edge > -0.05:
            parts.append(f"Edge has disappeared ({current_edge:.1%}).")
        else:
            parts.append(f"Edge has reversed to {current_edge:.1%} — position is against us.")

        # Forecast detail (weather only)
        mean = detail.get("forecast_mean")
        if mean is not None:
            bucket_low = detail.get("bucket_low")
            bucket_high = detail.get("bucket_high")
            spread = detail.get("forecast_spread")
            confidence = detail.get("forecast_confidence")
            members = detail.get("ensemble_members", 0)

            if bucket_low is not None and bucket_high is not None:
                parts.append(f"Ensemble forecast: {mean:.1f}F (range {detail.get('forecast_min', 0):.0f}-{detail.get('forecast_max', 0):.0f}F, {members} members).")
                if side == "yes":
                    if mean >= bucket_low and mean <= bucket_high:
                        parts.append(f"Forecast mean is inside the {bucket_low}-{bucket_high}F bucket — favorable for YES.")
                    elif mean < bucket_low:
                        parts.append(f"Forecast mean {mean:.1f}F is below the {bucket_low}-{bucket_high}F bucket by {bucket_low - mean:.1f}F.")
                    else:
                        parts.append(f"Forecast mean {mean:.1f}F is above the {bucket_low}-{bucket_high}F bucket by {mean - bucket_high:.1f}F.")
                elif side == "no":
                    if mean >= bucket_low and mean <= bucket_high:
                        parts.append(f"Forecast mean is inside the {bucket_low}-{bucket_high}F bucket — unfavorable for our NO position.")
                    else:
                        dist_from_bucket = min(abs(mean - bucket_low), abs(mean - bucket_high))
                        parts.append(f"Forecast mean is {dist_from_bucket:.1f}F away from {bucket_low}-{bucket_high}F bucket — favorable for NO.")

                if confidence:
                    conf_label = "high" if confidence > 0.7 else "moderate" if confidence > 0.4 else "low"
                    parts.append(f"Forecast confidence is {conf_label} ({confidence:.0%}).")

        # Probability vs market
        current_prob = detail.get("current_prob")
        market_prob = detail.get("market_prob")
        if current_prob is not None and market_prob is not None:
            if side == "yes":
                parts.append(f"Model says {current_prob:.0%} chance vs market price of {market_prob:.0%}.")
            else:
                parts.append(f"Model says {1-current_prob:.0%} NO probability vs market price of {market_prob:.0%}.")

        # Bias correction
        bias = detail.get("bias_applied", 0)
        if bias and abs(bias) >= 0.5:
            direction = "warmer" if bias > 0 else "cooler"
            parts.append(f"Bias correction applied: {abs(bias):.1f}F {direction} than models.")

        return " ".join(parts)

    # ═══════════════════════════════════════════════════════
    # 2. BIAS CORRECTION
    # ═══════════════════════════════════════════════════════

    def get_bias_adjustment(self, city_code):
        """
        Return the average bias (in °F) for a station.
        Positive = station reads warmer than models predict.
        Negative = station reads cooler.

        Two modes:
        1. Streak detection (fast): if last 3+ records all bias same direction
           with ≥0.5°F magnitude, apply the streak average immediately.
           Catches trends like haze/wildfire conditions or cold snaps early.
        2. Rolling average (standard): 30-day average, requires 5+ data points.

        Returns 0.0 if not enough data yet.
        """
        city_key = f"bias_{city_code}"
        if city_key not in self.bias_data:
            return 0.0

        records = self.bias_data[city_key]
        if not records:
            return 0.0

        # Check for consecutive-day streak (3+ days same direction, ≥0.5°F each)
        recent_7 = records[-7:]
        if len(recent_7) >= 3:
            streak_biases = []
            streak_dir = None
            for r in reversed(recent_7):
                bias = r["actual"] - r["predicted"]
                direction = "hot" if bias > 0 else "cold"
                if streak_dir is None:
                    streak_dir = direction
                if direction == streak_dir and abs(bias) >= 0.5:
                    streak_biases.append(bias)
                else:
                    break

            if len(streak_biases) >= 3:
                streak_avg = round(sum(streak_biases) / len(streak_biases), 1)
                print(f"    [BIAS] {city_code}: {len(streak_biases)}-day {streak_dir} streak "
                      f"(avg {streak_avg:+.1f}°F) — applying streak adjustment")
                return streak_avg

        # Fall back to standard rolling average (need 5+ data points)
        if len(records) < 5:
            return 0.0

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

        # DST-safe local hour via zoneinfo
        local_hour = datetime.now(ZoneInfo(tz_name)).hour

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
        Cached for 5 minutes — high can only go up, so stale data is safe.
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        station = city["nws_station"]

        # Cache for 5 minutes (high temp can only increase, so stale = conservative)
        cache_key = f"high_{station}"
        if cache_key in self._obs_cache:
            cached = self._obs_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 300:  # 5 min
                return cached["temp"]

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

            # Use city's LOCAL date, not system time (which is UTC on Railway).
            # Without this, late-evening local observations with next-day UTC
            # timestamps pollute "today's" readings, and early-morning UTC
            # checks pick up yesterday's stale data.
            tz_name = city.get("timezone", "America/New_York")
            tz = ZoneInfo(tz_name)
            local_date = datetime.now(tz).strftime("%Y-%m-%d")
            todays_temps = []

            for obs in features:
                props = obs.get("properties", {})
                timestamp = props.get("timestamp", "")
                # Convert observation UTC timestamp to local date
                try:
                    obs_utc = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    obs_local_date = obs_utc.astimezone(tz).strftime("%Y-%m-%d")
                except Exception:
                    obs_local_date = ""
                if obs_local_date == local_date:
                    temp_c = props.get("temperature", {}).get("value")
                    if temp_c is not None:
                        temp_f = round(temp_c * 9 / 5 + 32)
                        todays_temps.append(temp_f)

            if todays_temps:
                high = max(todays_temps)
                self._obs_cache[cache_key] = {
                    "temp": high,
                    "fetched_at": datetime.now(),
                }
                return high

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

            # Skip trades that never actually filled (resting/cancelled/error).
            # These are limit orders that were placed but never matched.
            status = trade.get("status", "")
            if any(x in status for x in ("resting", "cancelled", "error", "submitted")):
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
                    trade["settled_at"] = datetime.now(timezone.utc).isoformat()
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
                trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                trade["result"] = "win"
                trade["payout_cents"] = payout
                trade["profit_cents"] = profit

                risk_manager.record_win(profit)
                risk_manager.release_exposure(ticker, cost_cents, trade.get("city_code", ""))
                print(f"  ✓ WIN: {ticker} → +${profit/100:.2f}")
            else:
                # LOSS
                trade["settled"] = True
                trade["settled_at"] = datetime.now(timezone.utc).isoformat()
                trade["result"] = "loss"
                trade["payout_cents"] = 0
                trade["profit_cents"] = -cost_cents

                risk_manager.record_loss(cost_cents)
                risk_manager.release_exposure(ticker, cost_cents, trade.get("city_code", ""))
                print(f"  ✗ LOSS: {ticker} → -${cost_cents/100:.2f}")

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

            # Build edge attribution record
            self._record_attribution(trade, settlement)

            settled.append(trade)

        # NOTE: P&L is NOT saved here. _sync_pnl_from_kalshi() is the
        # single writer to pnl_history.json — it rebuilds P&L from
        # Kalshi fills/settlements/balance every cycle.

        return settled

    def _record_attribution(self, trade, settlement):
        """Record edge attribution data for a settled trade."""
        try:
            ticker = trade.get("ticker", "")
            city_code = trade.get("city_code", "")
            actual_temp = settlement.get("actual_temp") if settlement else None
            entry_ts = trade.get("timestamp", "")
            now_iso = datetime.now(timezone.utc).isoformat()

            # Calculate holding hours
            holding_hours = 0
            if entry_ts:
                try:
                    entry_dt = datetime.fromisoformat(entry_ts)
                    holding_hours = round((datetime.now() - entry_dt).total_seconds() / 3600, 1)
                except Exception:
                    pass

            # Fetch deterministic model forecasts for comparison
            forecast_sources = {}
            ensemble_mean = trade.get("predicted_high")
            if city_code and city_code in CITIES:
                city = CITIES[city_code]
                trade_date = self._extract_date_from_ticker(ticker)
                if trade_date:
                    for model_key, api_url in _DETERMINISTIC_APIS.items():
                        try:
                            params = {
                                "latitude": city["lat"],
                                "longitude": city["lon"],
                                "daily": "temperature_2m_max",
                                "temperature_unit": "fahrenheit",
                                "timezone": city.get("timezone", "auto"),
                                "start_date": trade_date,
                                "end_date": trade_date,
                            }
                            resp = requests.get(api_url, params=params, timeout=10)
                            if resp.status_code == 200:
                                temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
                                if temps and temps[0] is not None:
                                    forecast = round(temps[0])
                                    correct = False
                                    if actual_temp is not None:
                                        # "Correct" = within 2°F of actual
                                        correct = abs(forecast - actual_temp) <= 2
                                    forecast_sources[model_key] = {
                                        "forecast": forecast,
                                        "correct": correct,
                                    }
                        except Exception:
                            continue

            # Forecast error
            forecast_error = None
            if ensemble_mean is not None and actual_temp is not None:
                forecast_error = round(abs(ensemble_mean - actual_temp), 1)

            record = {
                "ticker": ticker,
                "timestamp_entry": entry_ts,
                "timestamp_settled": now_iso,
                "holding_hours": holding_hours,
                "side": trade.get("side", ""),
                "entry_price_cents": trade.get("price_cents", 0),
                "cost_cents": trade.get("cost_cents", 0),
                "result": trade.get("result", ""),
                "profit_cents": trade.get("profit_cents", 0),
                "edge_at_entry": trade.get("edge", 0),
                "forecast_sources": forecast_sources,
                "actual_high": actual_temp,
                "ensemble_mean": ensemble_mean,
                "forecast_error_f": forecast_error,
                "spread_at_entry_cents": trade.get("spread_at_entry_cents", 0),
                "city_code": city_code,
                "confirmation_verdict": trade.get("confirmation", ""),
            }

            # Append to attribution file
            attr_data = self._load_json(config.EDGE_ATTRIBUTION_FILE, default=[])
            attr_data.append(record)
            self._save_json(config.EDGE_ATTRIBUTION_FILE, attr_data)
        except Exception:
            pass  # Don't let attribution errors block settlement

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
                actual_temp = self._fetch_actual_high_for_ticker(ticker)
                return {"result": result, "actual_temp": actual_temp}

        except Exception:
            pass

        return None

    def _fetch_actual_high_for_ticker(self, ticker):
        """
        Fetch the actual daily high temperature for a settled weather ticker.
        Parses city and date from the ticker, then queries Open-Meteo archive API.
        Returns temperature in °F or None.
        """
        # Map ticker prefix to city code
        ticker_to_city = {
            "KXHIGHNY": "NYC",
            "KXHIGHCHI": "CHI",
            "KXHIGHMIA": "MIA",
            "KXHIGHAUS": "AUS",
            "KXHIGHLAX": "LAX",
            "KXHIGHDEN": "DEN",
            "KXHIGHPHIL": "PHI",
            "KXHIGHTATL": "ATL",
            "KXHIGHTBOS": "BOS",
            "KXHIGHTDAL": "DAL",
            "KXHIGHTDC": "DC",
            "KXHIGHTHOU": "HOU",
            "KXHIGHTLV": "LV",
            "KXHIGHTMIN": "MIN",
            "KXHIGHTNOLA": "NOLA",
            "KXHIGHTOKC": "OKC",
            "KXHIGHTPHX": "PHX",
            "KXHIGHTSATX": "SATX",
            "KXHIGHTSEA": "SEA",
            "KXHIGHTSFO": "SFO",
        }
        prefix = ticker.split("-")[0] if "-" in ticker else ticker
        city_code = ticker_to_city.get(prefix)
        if not city_code or city_code not in CITIES:
            return None

        # Parse market date
        date_str = self._extract_date_from_ticker(ticker)
        if not date_str:
            return None

        city = CITIES[city_code]
        try:
            url = "https://archive-api.open-meteo.com/v1/archive"
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city.get("timezone", "auto"),
                "start_date": date_str,
                "end_date": date_str,
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    return round(temps[0], 1)
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

        # Today's numbers (from Kalshi sync)
        today_w = self.pnl_data.get("today_wins", 0)
        today_l = self.pnl_data.get("today_losses", 0)
        today_pnl = self.pnl_data.get("today_pnl_cents", 0)
        today_total = today_w + today_l

        # Account-level (balance vs deposits — the real P&L)
        account_balance = self.pnl_data.get("account_balance_cents")
        account_pnl = self.pnl_data.get("account_pnl_cents")
        open_cost = self.pnl_data.get("open_position_cost_cents", 0)
        open_count = self.pnl_data.get("open_positions", 0)

        print(f"\n  ┌─ Profit & Loss ────────────────────────────────")

        # Account P&L first (most important number)
        if account_balance is not None:
            print(f"  │  Balance:        ${account_balance/100:.2f}")
        if account_pnl is not None:
            print(f"  │  Account P&L:    ${account_pnl/100:+.2f}")
        if open_cost > 0:
            print(f"  │  Open positions:  {open_count} (${open_cost/100:.2f} at risk)")
        if account_balance is not None or account_pnl is not None:
            print(f"  │  ─────────────────────────────────────────")

        if today_total > 0:
            print(f"  │  Today:          {today_w}W/{today_l}L  P&L: ${today_pnl/100:+.2f}")
            print(f"  │  ─────────────────────────────────────────")
        print(f"  │  Realized:       {self.pnl_data['wins']}W/{self.pnl_data['losses']}L ({win_rate:.0%})")
        print(f"  │  Realized P&L:   ${profit/100:+.2f}")
        if invested > 0:
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

    def _parse_position_bucket(self, ticker, weather_engine, title=""):
        """Parse a position's temperature bucket, falling back to API if needed.

        parse_market_bucket() requires the market title to extract the temp
        range, but stored positions often have empty titles. This helper
        fetches the market from the API when the initial parse fails.
        """
        # Try with whatever title/subtitle we have locally
        market_dict = {"ticker": ticker, "title": title, "subtitle": "", "event_ticker": ticker}
        parsed = weather_engine.parse_market_bucket(market_dict)
        if parsed:
            return parsed

        # Fallback: fetch market from API to get real title/subtitle
        if self.client:
            try:
                data = self.client.get_market(ticker)
                if data:
                    market = data.get("market", {})
                    market_dict = {
                        "ticker": ticker,
                        "title": market.get("title", ""),
                        "subtitle": market.get("subtitle", ""),
                        "event_ticker": market.get("event_ticker", ticker),
                    }
                    parsed = weather_engine.parse_market_bucket(market_dict)
                    if parsed:
                        return parsed
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
            config.atomic_json_save(filepath, data)
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
