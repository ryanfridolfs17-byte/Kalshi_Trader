"""
Paper-only challenger strategies for observation mode.

These never place live trades. They exist so the bot can keep learning while
the production strategy stays tightly locked down.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import config
from weather_engine import CITIES

# Cities with historically negative P&L — excluded from S7 sweet spot
_S7_EXCLUDED_CITIES = {"DAL", "AUS", "PHX", "DEN", "DC", "CHI"}


def _no_price_cents(source):
    direct = int(source.get("no_price_cents", source.get("no_ask", 0)) or 0)
    if direct > 0:
        return direct
    yes_ask = int(source.get("yes_price_cents", source.get("yes_ask", source.get("last_price", 0))) or 0)
    return max(0, 100 - yes_ask)


class PaperChallengerEngine:
    @staticmethod
    def _paper_contracts_from_signal(signal, default_contracts=1):
        contracts = int(
            (signal or {}).get(
                "suggested_contracts",
                (signal or {}).get("contracts", (signal or {}).get("requested_contracts", 0)),
            ) or 0
        )
        if contracts > 0:
            return contracts
        return max(1, int(default_contracts or 1))

    @staticmethod
    def _paper_kelly_contracts(
        *,
        win_prob,
        price_cents,
        balance_cents,
        confirmation_multiplier=1.0,
        model_spread=None,
        is_confirmed=False,
        is_arb=False,
    ):
        if price_cents <= 0 or balance_cents <= 0:
            return 0
        if balance_cents < 500:
            return 0

        prob_win = min(0.99, max(0.01, float(win_prob)))
        gross_payout = 100 - int(price_cents)
        net_payout = gross_payout * (1.0 - config.KALSHI_FEE_PCT)
        cost = int(price_cents)
        if net_payout <= 0:
            return 0

        kelly = ((prob_win * net_payout) - ((1.0 - prob_win) * cost)) / net_payout
        if kelly <= 0:
            return 0

        fraction = kelly * max(0.0, float(confirmation_multiplier or 0.0))
        if model_spread is not None and model_spread > 0:
            fraction *= 1.0 / (1.0 + max(0.0, model_spread) / 5.0)

        bet_cents = fraction * int(balance_cents)
        if is_arb:
            max_pct = float(getattr(config, "PAPER_ARB_POSITION_PCT", config.ARB_POSITION_PCT) or config.ARB_POSITION_PCT)
        elif is_confirmed:
            max_pct = float(
                getattr(config, "PAPER_CONFIRMED_POSITION_PCT", config.CONFIRMED_POSITION_PCT)
                or config.CONFIRMED_POSITION_PCT
            )
        else:
            max_pct = float(
                getattr(config, "PAPER_MAX_POSITION_PCT", config.MAX_POSITION_PCT)
                or config.MAX_POSITION_PCT
            )
        max_bet = int(balance_cents) * max_pct
        max_abs = int(
            getattr(config, "PAPER_MAX_PER_TICKER_CENTS", config.MAX_PER_TICKER_CENTS)
            or config.MAX_PER_TICKER_CENTS
        )
        max_contracts = int(
            getattr(config, "PAPER_MAX_CONTRACTS_PER_TICKER", config.MAX_CONTRACTS_PER_TICKER)
            or config.MAX_CONTRACTS_PER_TICKER
        )

        bet_cents = min(bet_cents, max_bet)
        if max_abs > 0:
            bet_cents = min(bet_cents, max_abs)

        contracts = int(bet_cents / cost)
        if contracts <= 0:
            return 0
        if max_contracts > 0:
            contracts = min(contracts, max_contracts)
        return contracts

    @staticmethod
    def _paper_strategy_blockers(strategy_id, strategy_statuses=None):
        if not strategy_id:
            return []
        card = (strategy_statuses or {}).get(strategy_id) or {}
        blockers = list(card.get("paper_entry_blockers", []) or [])
        if card and card.get("paper_entry_enabled") is False:
            blockers.append("paper_disabled_by_config")
        deduped = []
        seen = set()
        for blocker in blockers:
            if blocker and blocker not in seen:
                deduped.append(blocker)
                seen.add(blocker)
        return deduped

    def generate(
        self,
        market,
        weather_signal,
        todays_high=None,
        observation_mode=False,
        next_day_shadow_signal=None,
        strategy_statuses=None,
        balance_cents=None,
    ):
        if not observation_mode or not getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False):
            return []

        challengers = []
        next_day = self._build_next_day_no_challenger(
            weather_signal,
            shadow_signal=next_day_shadow_signal,
            strategy_statuses=strategy_statuses,
            balance_cents=balance_cents,
        )
        if next_day:
            challengers.append(next_day)

        tight_next_day = self._build_tight_next_day_no_challenger(
            weather_signal,
            shadow_signal=next_day_shadow_signal,
            strategy_statuses=strategy_statuses,
            balance_cents=balance_cents,
        )
        if tight_next_day:
            challengers.append(tight_next_day)

        soft_lock = self._build_soft_settlement_lock(
            weather_signal,
            todays_high=todays_high,
            strategy_statuses=strategy_statuses,
            balance_cents=balance_cents,
        )
        if soft_lock:
            challengers.append(soft_lock)

        afternoon_no = self._build_afternoon_no_sweet_spot(
            weather_signal,
            strategy_statuses=strategy_statuses,
            balance_cents=balance_cents,
        )
        if afternoon_no:
            challengers.append(afternoon_no)

        return challengers

    def _build_next_day_no_challenger(
        self,
        signal,
        shadow_signal=None,
        strategy_statuses=None,
        balance_cents=None,
    ):
        if not getattr(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", False):
            return None
        if self._paper_strategy_blockers("S4-NextDayNoPaper", strategy_statuses):
            return None
        if not signal or signal.get("skip_reason") != "next_day_directional_blocked":
            return None
        if signal.get("side") != "no":
            return None
        if signal.get("strike_type") != "between":
            return None
        if not shadow_signal or shadow_signal.get("signal") != "buy":
            return None
        if shadow_signal.get("side") != "no":
            return None
        if shadow_signal.get("strike_type") != "between":
            return None

        price_cents = int(shadow_signal.get("price_cents", 0) or 0)
        if price_cents < int(getattr(config, "PAPER_CHALLENGER_MIN_PRICE_CENTS", 35) or 35):
            return None
        if price_cents > int(getattr(config, "PAPER_CHALLENGER_MAX_PRICE_CENTS", 80) or 80):
            return None

        fee_adj_edge = float(shadow_signal.get("fee_adjusted_edge", 0) or 0)
        if fee_adj_edge < float(getattr(config, "PAPER_CHALLENGER_MIN_FEE_ADJ_EDGE", 0.05) or 0.05):
            return None
        contracts = self._paper_contracts_from_signal(shadow_signal)

        challenger = dict(shadow_signal)
        challenger.update({
            "signal": "buy",
            "strategy": "S4-NextDayNoPaper",
            "execution_style": "taker",
            "suggested_contracts": contracts,
            "contracts": contracts,
            "requested_contracts": contracts,
            "current_price_cents": price_cents,
            "entry_price_cents": price_cents,
            "limit_price_cents": price_cents,
            "limit_price": price_cents,
            "risk_price_cents": price_cents,
            "skip_reason": None,
            "execution_status": "",
            "paper_only": True,
            "paper_source_strategy": signal.get("strategy", ""),
            "paper_source_skip_reason": signal.get("skip_reason", ""),
            "paper_shadow_strategy": shadow_signal.get("strategy", ""),
            "paper_shadow_mode": shadow_signal.get("shadow_mode", ""),
            "reasoning": (
                "[S4] Paper-only challenger for next-day NO after full shadow pass. "
                f"fee_adj_edge={fee_adj_edge:.3f}, price={price_cents}c."
            ),
        })
        return challenger

    def _build_tight_next_day_no_challenger(
        self,
        signal,
        shadow_signal=None,
        strategy_statuses=None,
        balance_cents=None,
    ):
        if not getattr(config, "PAPER_CHALLENGER_ALLOW_TIGHT_NEXT_DAY_NO", False):
            return None
        if self._paper_strategy_blockers("S6-TightNextDayNoPaper", strategy_statuses):
            return None
        if not signal or signal.get("skip_reason") != "next_day_directional_blocked":
            return None
        if signal.get("side") != "no":
            return None
        if signal.get("strike_type") != "between":
            return None
        if not shadow_signal or shadow_signal.get("signal") != "buy":
            return None
        if shadow_signal.get("side") != "no":
            return None
        if shadow_signal.get("strike_type") != "between":
            return None

        price_cents = int(shadow_signal.get("price_cents", 0) or 0)
        if price_cents < int(getattr(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_PRICE_CENTS", 48) or 48):
            return None
        if price_cents > int(getattr(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MAX_PRICE_CENTS", 66) or 66):
            return None

        raw_edge = float(shadow_signal.get("edge", 0) or 0)
        if raw_edge < float(getattr(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_EDGE", 0.12) or 0.12):
            return None

        fee_adj_edge = float(shadow_signal.get("fee_adjusted_edge", 0) or 0)
        if fee_adj_edge < float(getattr(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_FEE_ADJ_EDGE", 0.08) or 0.08):
            return None

        if str(shadow_signal.get("confirmation_verdict", "") or "").upper() != "CONFIRM":
            return None
        contracts = self._paper_contracts_from_signal(shadow_signal)

        challenger = dict(shadow_signal)
        challenger.update({
            "signal": "buy",
            "strategy": "S6-TightNextDayNoPaper",
            "execution_style": "taker",
            "suggested_contracts": contracts,
            "contracts": contracts,
            "requested_contracts": contracts,
            "current_price_cents": price_cents,
            "entry_price_cents": price_cents,
            "limit_price_cents": price_cents,
            "limit_price": price_cents,
            "risk_price_cents": price_cents,
            "skip_reason": None,
            "execution_status": "",
            "paper_only": True,
            "paper_source_strategy": signal.get("strategy", ""),
            "paper_source_skip_reason": signal.get("skip_reason", ""),
            "paper_shadow_strategy": shadow_signal.get("strategy", ""),
            "paper_shadow_mode": shadow_signal.get("shadow_mode", ""),
            "reasoning": (
                "[S6] Tight paper-only next-day NO after full shadow pass. "
                f"fee_adj_edge={fee_adj_edge:.3f}, raw_edge={raw_edge:.3f}, price={price_cents}c."
            ),
        })
        return challenger

    def _build_soft_settlement_lock(
        self,
        signal,
        todays_high=None,
        strategy_statuses=None,
        balance_cents=None,
    ):
        if not getattr(config, "PAPER_CHALLENGER_ALLOW_SOFT_SETTLEMENT_LOCK", False):
            return None
        if self._paper_strategy_blockers("S5-SoftSettlementLockPaper", strategy_statuses):
            return None
        if todays_high is None:
            return None
        if not signal:
            return None
        if signal.get("side") != "no":
            return None

        city_code = signal.get("city_code", "")
        city_info = CITIES.get(city_code, {})
        tz_name = city_info.get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(tz_name))
        if local_now.hour < int(getattr(config, "PAPER_CHALLENGER_SOFT_LOCK_MIN_LOCAL_HOUR", 10) or 10):
            return None

        target_date = signal.get("target_date", "")
        if target_date != local_now.strftime("%Y-%m-%d"):
            return None

        strike_type = signal.get("strike_type", "")
        cap_strike = signal.get("cap_strike")
        if strike_type != "between" or cap_strike is None:
            return None

        try:
            observed_high = float(todays_high)
            cap = float(cap_strike)
        except Exception:
            return None

        soft_buffer = float(getattr(config, "PAPER_CHALLENGER_SOFT_LOCK_BUFFER_F", 0.5) or 0.5)
        if observed_high <= cap + soft_buffer:
            return None

        price_cents = _no_price_cents(signal)
        if price_cents <= 0:
            return None
        if price_cents > int(getattr(config, "PAPER_CHALLENGER_SOFT_LOCK_MAX_PRICE_CENTS", 85) or 85):
            return None

        payout_cents = 100 - price_cents
        if payout_cents < int(getattr(config, "SETTLEMENT_LOCK_MIN_PAYOUT_CENTS", 8) or 8):
            return None

        breach_f = round(observed_high - cap, 2)
        our_prob_no = min(0.99, 0.96 + 0.01 * breach_f)
        market_prob_no = price_cents / 100.0
        fee_adj_edge = max(
            0.0,
            (our_prob_no - market_prob_no) - (config.KALSHI_FEE_PCT * min(market_prob_no, 1.0 - market_prob_no)),
        )
        if fee_adj_edge <= 0:
            return None
        contracts = self._paper_kelly_contracts(
            win_prob=our_prob_no,
            price_cents=price_cents,
            balance_cents=int(balance_cents or 0),
            is_confirmed=True,
        )
        if contracts <= 0:
            contracts = 1

        return {
            "signal": "buy",
            "ticker": signal.get("ticker", ""),
            "side": "no",
            "strategy": "S5-SoftSettlementLockPaper",
            "execution_style": "taker",
            "suggested_contracts": contracts,
            "contracts": contracts,
            "requested_contracts": contracts,
            "price_cents": price_cents,
            "current_price_cents": price_cents,
            "entry_price_cents": price_cents,
            "limit_price_cents": price_cents,
            "limit_price": price_cents,
            "risk_price_cents": price_cents,
            "edge": round(max(0.0, our_prob_no - market_prob_no), 4),
            "fee_adjusted_edge": round(fee_adj_edge, 4),
            "our_prob": round(1.0 - our_prob_no, 4),
            "market_prob": round(1.0 - market_prob_no, 4),
            "yes_price_cents": int(signal.get("yes_price_cents", 0) or 0),
            "no_price_cents": price_cents,
            "city_code": city_code,
            "target_date": target_date,
            "strike_type": strike_type,
            "floor_strike": signal.get("floor_strike"),
            "cap_strike": signal.get("cap_strike"),
            "confirmation_verdict": "SOFT_SETTLEMENT_LOCK",
            "todays_high_snapshot": observed_high,
            "paper_only": True,
            "paper_source_strategy": "S3-SettlementLock",
            "paper_source_skip_reason": "soft_lock_candidate",
            "reasoning": (
                "[S5] Paper-only soft settlement lock. "
                f"Observed high {observed_high:.1f}F > cap {cap:.1f}F at {price_cents}c."
            ),
        }

    def _build_afternoon_no_sweet_spot(self, signal, strategy_statuses=None, balance_cents=None):
        """S7: Paper challenger targeting the historically profitable afternoon NO profile.

        Triggers on enriched skip signals from the main weather strategy where the
        trade was close but blocked by time-of-day or marginal guards.  Does NOT
        require a shadow signal — reads forecast data directly from the skip dict.
        """
        if not getattr(config, "PAPER_CHALLENGER_ALLOW_AFTERNOON_NO_SWEET_SPOT", False):
            return None
        if self._paper_strategy_blockers("S7-AfternoonNOSweetSpot", strategy_statuses):
            return None
        if not signal:
            return None

        # Must be a skip signal with forecast data attached
        skip_reason = signal.get("skip_reason", "")
        if not skip_reason:
            return None
        # Only trigger on signals that were close but blocked
        allowed_skips = {
            "before_noon_directional",
            "edge_below_threshold",
            "no_side_guard",
            "rounding_buffer",
            "fee_adj_edge_below_threshold",
        }
        if skip_reason not in allowed_skips:
            return None

        # Must be NO-side bounded bucket
        if signal.get("side") != "no":
            return None
        if signal.get("strike_type") != "between":
            return None

        # Must have forecast data (enriched skip)
        our_prob = signal.get("our_prob")
        market_prob = signal.get("market_prob")
        if our_prob is None or market_prob is None:
            return None

        # Exclude known-losing cities
        city_code = signal.get("city_code", "")
        if city_code in _S7_EXCLUDED_CITIES:
            return None

        # Must NOT be a confirmed outcome (already handled by CASE 1 / S3)
        if signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME":
            return None

        # Time gate: after 2 PM in the city's local timezone, same-day only
        city_info = CITIES.get(city_code, {})
        tz_name = city_info.get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(tz_name))
        min_hour = int(getattr(config, "PAPER_CHALLENGER_S7_MIN_LOCAL_HOUR", 14) or 14)
        if local_now.hour < min_hour:
            return None

        target_date = signal.get("target_date", "")
        if target_date != local_now.strftime("%Y-%m-%d"):
            return None

        # Price band: NO 35-60c (the historically profitable sweet spot)
        price_cents = _no_price_cents(signal)
        if price_cents <= 0:
            return None
        min_price = int(getattr(config, "PAPER_CHALLENGER_S7_MIN_PRICE_CENTS", 35) or 35)
        max_price = int(getattr(config, "PAPER_CHALLENGER_S7_MAX_PRICE_CENTS", 60) or 60)
        if price_cents < min_price or price_cents > max_price:
            return None

        # Recalculate edge from enriched signal data
        # our_prob is YES-side probability; for NO we want 1-our_prob
        try:
            yes_prob = float(our_prob)
            no_win_prob = 1.0 - yes_prob
            market_prob_no = price_cents / 100.0
            raw_edge = no_win_prob - market_prob_no
            fee_cost = config.KALSHI_FEE_PCT * min(market_prob_no, 1.0 - market_prob_no)
            fee_adj_edge = raw_edge - fee_cost
        except (TypeError, ValueError):
            return None

        min_edge = float(getattr(config, "PAPER_CHALLENGER_S7_MIN_EDGE", 0.06) or 0.06)
        min_fee_adj = float(getattr(config, "PAPER_CHALLENGER_S7_MIN_FEE_ADJ_EDGE", 0.03) or 0.03)
        if raw_edge < min_edge:
            return None
        if fee_adj_edge < min_fee_adj:
            return None
        contracts = self._paper_kelly_contracts(
            win_prob=no_win_prob,
            price_cents=price_cents,
            balance_cents=int(balance_cents or 0),
            confirmation_multiplier=1.0,
            model_spread=signal.get("model_spread"),
        )
        if contracts <= 0:
            contracts = 1

        return {
            "signal": "buy",
            "ticker": signal.get("ticker", ""),
            "side": "no",
            "strategy": "S7-AfternoonNOSweetSpot",
            "execution_style": "taker",
            "suggested_contracts": contracts,
            "contracts": contracts,
            "requested_contracts": contracts,
            "price_cents": price_cents,
            "current_price_cents": price_cents,
            "entry_price_cents": price_cents,
            "limit_price_cents": price_cents,
            "limit_price": price_cents,
            "risk_price_cents": price_cents,
            "edge": round(raw_edge, 4),
            "fee_adjusted_edge": round(fee_adj_edge, 4),
            "our_prob": round(yes_prob, 4),
            "market_prob": round(1.0 - market_prob_no, 4),
            "yes_price_cents": int(signal.get("yes_price_cents", 0) or 0),
            "no_price_cents": price_cents,
            "city_code": city_code,
            "target_date": target_date,
            "strike_type": "between",
            "floor_strike": signal.get("floor_strike"),
            "cap_strike": signal.get("cap_strike"),
            "confirmation_verdict": signal.get("confirmation_verdict", ""),
            "predicted_high": signal.get("predicted_high"),
            "model_spread": signal.get("model_spread"),
            "std_dev": signal.get("std_dev"),
            "todays_high_snapshot": signal.get("todays_high_snapshot"),
            "market_title": signal.get("market_title", ""),
            "market_subtitle": signal.get("market_subtitle", ""),
            "event_ticker": signal.get("event_ticker", ""),
            "paper_only": True,
            "paper_source_strategy": signal.get("strategy", ""),
            "paper_source_skip_reason": skip_reason,
            "reasoning": (
                f"[S7] Afternoon NO sweet spot: {city_code} "
                f"edge={raw_edge:.3f} fee_adj={fee_adj_edge:.3f} "
                f"price={price_cents}c skip={skip_reason}."
            ),
            "skip_reason": None,
            "execution_status": "",
        }
