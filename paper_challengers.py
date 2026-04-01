"""
Paper-only challenger strategies for observation mode.

These never place live trades. They exist so the bot can keep learning while
the production strategy stays tightly locked down.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import config


def _no_price_cents(source):
    direct = int(source.get("no_price_cents", source.get("no_ask", 0)) or 0)
    if direct > 0:
        return direct
    yes_ask = int(source.get("yes_price_cents", source.get("yes_ask", source.get("last_price", 0))) or 0)
    return max(0, 100 - yes_ask)


class PaperChallengerEngine:
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
    ):
        if not observation_mode or not getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False):
            return []

        challengers = []
        next_day = self._build_next_day_no_challenger(
            weather_signal,
            shadow_signal=next_day_shadow_signal,
            strategy_statuses=strategy_statuses,
        )
        if next_day:
            challengers.append(next_day)

        soft_lock = self._build_soft_settlement_lock(
            weather_signal,
            todays_high=todays_high,
            strategy_statuses=strategy_statuses,
        )
        if soft_lock:
            challengers.append(soft_lock)

        return challengers

    def _build_next_day_no_challenger(self, signal, shadow_signal=None, strategy_statuses=None):
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

        challenger = dict(shadow_signal)
        challenger.update({
            "signal": "buy",
            "strategy": "S4-NextDayNoPaper",
            "execution_style": "taker",
            "suggested_contracts": 1,
            "contracts": 1,
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

    def _build_soft_settlement_lock(self, signal, todays_high=None, strategy_statuses=None):
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
        tz_name = "America/New_York"
        local_now = datetime.now(ZoneInfo(tz_name))
        if local_now.hour < int(getattr(config, "PAPER_CHALLENGER_SOFT_LOCK_MIN_LOCAL_HOUR", 12) or 12):
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

        if observed_high <= cap:
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

        return {
            "signal": "buy",
            "ticker": signal.get("ticker", ""),
            "side": "no",
            "strategy": "S5-SoftSettlementLockPaper",
            "execution_style": "taker",
            "suggested_contracts": 1,
            "contracts": 1,
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
            "ticker": signal.get("ticker", ""),
            "paper_only": True,
            "paper_source_strategy": "S3-SettlementLock",
            "paper_source_skip_reason": "soft_lock_candidate",
            "reasoning": (
                "[S5] Paper-only soft settlement lock. "
                f"Observed high {observed_high:.1f}F > cap {cap:.1f}F at {price_cents}c."
            ),
        }
