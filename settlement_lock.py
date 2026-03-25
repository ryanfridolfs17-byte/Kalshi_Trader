"""
SETTLEMENT LOCK PAPER SCOREBOARD
================================
Paper-only detector for weather-market setups that look close to
deterministic under the official settlement geometry.

We only score "hard lock" states:
  1. NO on bounded buckets / "or below" markets after the observed high
     has already exceeded the upper bound + hard buffer.
  2. YES on "or above" threshold markets after the observed high has
     already exceeded the floor + hard buffer.

These are persisted to paper_locks.json and reconciled against settled
market outcomes so we can evaluate them before re-enabling live trading.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from weather_engine import WeatherEngine, CITIES


class SettlementLockPaper:
    """Track paper-only settlement-lock candidates and outcomes."""

    def __init__(self, kalshi_client=None, weather_engine=None):
        self.client = kalshi_client
        self.weather = weather_engine or WeatherEngine()
        self.state = self._load_state()
        self._ensure_defaults()

    def _ensure_defaults(self):
        defaults = {
            "active": {},
            "history": [],
            "last_reconciled_at": "",
            "summary": {},
        }
        for key, value in defaults.items():
            if key not in self.state:
                self.state[key] = value if not isinstance(value, (dict, list)) else type(value)(value)

    def evaluate_market(self, market, todays_high=None):
        """Return a paper-only hard-lock candidate dict or None."""
        if todays_high is None:
            return None

        parsed = self.weather.parse_market_bucket(market)
        if not parsed:
            return None

        city_code = parsed["city_code"]
        temp_low = parsed["temp_low"]
        temp_high = parsed["temp_high"]
        target_date = parsed.get("target_date")
        if not target_date:
            return None

        city_info = CITIES.get(city_code, {})
        tz_name = city_info.get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(tz_name))
        if target_date != local_now.strftime("%Y-%m-%d"):
            return None

        ticker = market.get("ticker", "")
        hard_buffer = int(getattr(config, "ROUNDING_BUFFER_HARD_F", 1) or 1)
        yes_price = market.get("yes_ask", 0) or market.get("last_price", 0) or 0
        no_price = market.get("no_ask", 0) or max(0, 100 - yes_price)

        lock_side = ""
        lock_type = ""
        price_cents = 0
        reason = ""

        # "or above" threshold market -> YES locks once threshold is clearly exceeded.
        if temp_high == 200 and todays_high >= temp_low + hard_buffer:
            lock_side = "yes"
            lock_type = "threshold_cross"
            price_cents = int(yes_price or 0)
            reason = (
                f"Observed high {todays_high:.0f}F >= floor {temp_low}F + "
                f"{hard_buffer}F hard buffer"
            )
        # Bounded bucket or "or below" market -> NO locks once the upper bound is clearly broken.
        elif temp_high < 200 and todays_high > temp_high + hard_buffer:
            lock_side = "no"
            lock_type = "upper_bound_breached"
            price_cents = int(no_price or 0)
            reason = (
                f"Observed high {todays_high:.0f}F > ceiling {temp_high}F + "
                f"{hard_buffer}F hard buffer"
            )

        if not lock_side or price_cents <= 0:
            return None

        payout_cents = 100 - price_cents
        min_payout = int(getattr(config, "SETTLEMENT_LOCK_MIN_PAYOUT_CENTS", 8) or 8)
        if payout_cents < min_payout:
            return None

        return {
            "ticker": ticker,
            "title": market.get("title", ""),
            "city_code": city_code,
            "target_date": target_date,
            "lock_side": lock_side,
            "lock_type": lock_type,
            "price_cents": price_cents,
            "payout_cents": payout_cents,
            "gross_edge": round((100 - price_cents) / 100.0, 4),
            "observed_high_f": float(todays_high),
            "temp_low": temp_low,
            "temp_high": temp_high,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }

    def record_candidates(self, candidates):
        """Persist or refresh active paper candidates."""
        if not candidates:
            self._refresh_summary()
            self._save_state()
            return

        changed = False
        now_iso = datetime.now(timezone.utc).isoformat()
        active = self.state.setdefault("active", {})
        for cand in candidates:
            ticker = cand.get("ticker", "")
            if not ticker:
                continue
            existing = active.get(ticker)
            if not existing:
                active[ticker] = {
                    **cand,
                    "status": "active",
                    "seen_count": 1,
                    "first_seen_at": now_iso,
                    "last_seen_at": now_iso,
                    "entry_price_cents": cand.get("price_cents", 0),
                    "best_price_cents": cand.get("price_cents", 0),
                    "paper_contracts": 1,
                }
                changed = True
                continue

            existing["last_seen_at"] = now_iso
            existing["seen_count"] = int(existing.get("seen_count", 0) or 0) + 1
            existing["observed_high_f"] = max(
                float(existing.get("observed_high_f", 0) or 0),
                float(cand.get("observed_high_f", 0) or 0),
            )
            existing["price_cents"] = cand.get("price_cents", existing.get("price_cents", 0))
            best = int(existing.get("best_price_cents", existing.get("entry_price_cents", 0)) or 0)
            cur = int(cand.get("price_cents", 0) or 0)
            if best <= 0 or (cur > 0 and cur < best):
                existing["best_price_cents"] = cur
            existing["reason"] = cand.get("reason", existing.get("reason", ""))
            changed = True

        if changed:
            self._refresh_summary()
            self._save_state()

    def reconcile_settlements(self):
        """Resolve active paper candidates against settled market outcomes."""
        if not self.client:
            return

        changed = False
        active = self.state.get("active", {})
        history = self.state.get("history", [])
        now = datetime.now(timezone.utc)
        today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        for ticker, cand in list(active.items()):
            result = self._get_market_result(ticker)
            if result in ("yes", "no"):
                entry_price = int(cand.get("entry_price_cents", 0) or 0)
                best_price = int(cand.get("best_price_cents", entry_price) or 0)
                lock_side = cand.get("lock_side", "")
                entry_pnl = (100 - entry_price) if lock_side == result else -entry_price
                best_pnl = (100 - best_price) if lock_side == result else -best_price
                resolved = dict(cand)
                resolved.update({
                    "status": "win" if lock_side == result else "loss",
                    "market_result": result,
                    "resolved_at": now.isoformat(),
                    "entry_profit_cents": entry_pnl,
                    "best_profit_cents": best_pnl,
                })
                history.append(resolved)
                del active[ticker]
                changed = True
                continue

            target_date = cand.get("target_date", "")
            if target_date and target_date < today_et:
                stale_cutoff = datetime.fromisoformat(cand.get("last_seen_at", now.isoformat()).replace("Z", "+00:00"))
                if now - stale_cutoff > timedelta(days=2):
                    expired = dict(cand)
                    expired.update({
                        "status": "expired_unknown",
                        "market_result": "",
                        "resolved_at": now.isoformat(),
                        "entry_profit_cents": 0,
                        "best_profit_cents": 0,
                    })
                    history.append(expired)
                    del active[ticker]
                    changed = True

        if changed:
            self.state["history"] = history[-300:]
            self.state["last_reconciled_at"] = now.isoformat()
            self._refresh_summary()
            self._save_state()

    def get_summary(self):
        self._refresh_summary()
        return dict(self.state)

    def top_active(self, limit=5):
        active = list(self.state.get("active", {}).values())
        active.sort(key=lambda c: (-(c.get("payout_cents", 0) or 0), c.get("ticker", "")))
        return active[:limit]

    def _get_market_result(self, ticker):
        try:
            market_data = self.client.get_market(ticker)
            if not market_data:
                return None
            market = market_data.get("market", market_data)
            status = market.get("status", "")
            result = market.get("result", "")
            if status in ("settled", "finalized", "closed") and result in ("yes", "no"):
                return result
        except Exception:
            return None
        return None

    def _refresh_summary(self):
        history = self.state.get("history", [])
        wins = [h for h in history if h.get("status") == "win"]
        losses = [h for h in history if h.get("status") == "loss"]
        self.state["summary"] = {
            "active_count": len(self.state.get("active", {})),
            "resolved_count": len(wins) + len(losses),
            "wins": len(wins),
            "losses": len(losses),
            "entry_profit_cents": sum(h.get("entry_profit_cents", 0) for h in history),
            "best_profit_cents": sum(h.get("best_profit_cents", 0) for h in history),
            "last_reconciled_at": self.state.get("last_reconciled_at", ""),
        }

    def _load_state(self):
        try:
            if os.path.exists(config.PAPER_LOCKS_FILE):
                with open(config.PAPER_LOCKS_FILE, "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            config.atomic_json_save(config.PAPER_LOCKS_FILE, self.state)
        except Exception:
            pass
