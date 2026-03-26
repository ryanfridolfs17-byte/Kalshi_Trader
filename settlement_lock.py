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
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

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
            "market_metadata_cache": {},
            "retrospective": {
                "history": [],
                "runs": [],
                "summary": {},
            },
        }
        for key, value in defaults.items():
            if key not in self.state:
                self.state[key] = value if not isinstance(value, (dict, list)) else type(value)(value)
        retro = self.state.get("retrospective", {})
        if not isinstance(retro, dict):
            retro = {}
            self.state["retrospective"] = retro
        retro.setdefault("history", [])
        retro.setdefault("runs", [])
        retro.setdefault("summary", {})

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

    def get_retrospective_summary(self):
        retro = self.state.get("retrospective", {})
        return {
            "summary": dict(retro.get("summary", {})),
            "history": list(retro.get("history", [])),
            "runs": list(retro.get("runs", [])),
        }

    def backfill_from_learning_state(self, learning_state=None, refresh_metadata=False):
        """Build a conservative retrospective opportunity set from saved scans.

        This is intentionally *not* a true intraday replay. We only score
        opportunities where:
          1. We have a saved scan snapshot for the ticker.
          2. We have the final observed actual high for that city/date.
          3. The saved snapshot already contains the lock side and side-specific
             price we would need for a paper fill estimate.

        Threshold direction and exact bucket bounds are resolved from Kalshi's
        public market metadata instead of guessing from the ticker symbol.
        """
        if learning_state is None:
            learning_state = self._load_learning_state()

        scan_snapshots = learning_state.get("scan_snapshots", {})
        actual_temps = learning_state.get("actual_temps", {})
        now_iso = datetime.now(timezone.utc).isoformat()
        hard_buffer = int(getattr(config, "ROUNDING_BUFFER_HARD_F", 1) or 1)
        min_payout = int(getattr(config, "SETTLEMENT_LOCK_MIN_PAYOUT_CENTS", 8) or 8)

        history = []
        by_day = defaultdict(lambda: {
            "lockable": 0,
            "scorable": 0,
            "estimated_profit_cents": 0,
        })
        by_city = Counter()
        by_skip_reason = Counter()
        by_signal = Counter()
        by_lock_type = Counter()
        side_mismatch = 0
        missing_actual = 0
        metadata_failures = 0
        unique_tickers = set()

        for snapshot_day, day_snaps in sorted(scan_snapshots.items()):
            if not isinstance(day_snaps, dict):
                continue
            for snap in day_snaps.values():
                ticker = snap.get("ticker", "")
                city_code = snap.get("city_code", "")
                target_date = snap.get("target_date", "")
                if not ticker or not city_code or not target_date:
                    continue

                actual_high = actual_temps.get(f"{city_code}_{target_date}")
                if actual_high is None:
                    missing_actual += 1
                    continue

                metadata = self._metadata_from_snapshot(snap)
                if not metadata:
                    metadata = self._get_public_market_metadata(
                        ticker,
                        refresh=refresh_metadata,
                    )
                if not metadata:
                    metadata_failures += 1
                    continue

                candidate = self._evaluate_retrospective_snapshot(
                    snap,
                    metadata,
                    actual_high,
                    snapshot_day=snapshot_day,
                    hard_buffer=hard_buffer,
                    min_payout=min_payout,
                )
                if not candidate:
                    continue

                unique_tickers.add(ticker)
                by_day[snapshot_day]["lockable"] += 1
                by_city[city_code] += 1
                by_lock_type[candidate.get("lock_type", "")] += 1
                if candidate.get("skip_reason"):
                    by_skip_reason[candidate["skip_reason"]] += 1
                by_signal[candidate.get("signal", "") or "unknown"] += 1

                if candidate.get("can_score"):
                    by_day[snapshot_day]["scorable"] += 1
                    by_day[snapshot_day]["estimated_profit_cents"] += int(
                        candidate.get("estimated_profit_cents", 0) or 0
                    )
                else:
                    side_mismatch += 1

                history.append(candidate)

        history.sort(
            key=lambda row: (
                row.get("snapshot_day", ""),
                row.get("timestamp", ""),
                row.get("ticker", ""),
            )
        )
        scorable = [row for row in history if row.get("can_score")]
        total_profit = sum(int(row.get("estimated_profit_cents", 0) or 0) for row in scorable)
        avg_profit = round(total_profit / len(scorable), 2) if scorable else 0.0
        avg_price = round(
            sum(int(row.get("price_cents", 0) or 0) for row in scorable) / len(scorable),
            2,
        ) if scorable else 0.0

        top_examples = sorted(
            scorable,
            key=lambda row: (-(row.get("estimated_profit_cents", 0) or 0), row.get("ticker", "")),
        )[:10]

        summary = {
            "analysis_type": "retrospective_opportunity_set",
            "methodology": (
                "Uses final cached actual highs plus Kalshi public market metadata. "
                "Counts only snapshots where the saved side already matches the lock side "
                "and the saved price is usable. This is not a true intraday replay."
            ),
            "generated_at": now_iso,
            "scan_days_considered": len(scan_snapshots),
            "history_rows": len(history),
            "unique_lockable_tickers": len(unique_tickers),
            "scorable_candidates": len(scorable),
            "estimated_profit_cents": total_profit,
            "average_profit_cents": avg_profit,
            "average_price_cents": avg_price,
            "min_payout_cents": min_payout,
            "hard_buffer_f": hard_buffer,
            "missing_actual_snapshots": missing_actual,
            "metadata_failures": metadata_failures,
            "unscorable_lockable_candidates": side_mismatch,
            "by_day": dict(sorted(by_day.items())),
            "by_city": dict(by_city.most_common()),
            "by_skip_reason": dict(by_skip_reason.most_common()),
            "by_signal": dict(by_signal.most_common()),
            "by_lock_type": dict(by_lock_type.most_common()),
            "top_examples": top_examples,
        }

        retro = self.state.setdefault("retrospective", {})
        retro["history"] = history[-500:]
        retro["summary"] = summary
        runs = retro.setdefault("runs", [])
        runs.append({
            "generated_at": now_iso,
            "history_rows": len(history),
            "scorable_candidates": len(scorable),
            "estimated_profit_cents": total_profit,
            "unique_lockable_tickers": len(unique_tickers),
        })
        retro["runs"] = runs[-20:]
        self._save_state()
        return summary

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

    def _evaluate_retrospective_snapshot(
        self,
        snapshot,
        metadata,
        actual_high,
        snapshot_day,
        hard_buffer,
        min_payout,
    ):
        strike_type = metadata.get("strike_type", "")
        floor_strike = metadata.get("floor_strike")
        cap_strike = metadata.get("cap_strike")
        lock_side = ""
        lock_type = ""
        event_floor = None
        event_cap = None

        if strike_type == "greater" and floor_strike is not None:
            event_floor = int(floor_strike) + 1
            if actual_high >= event_floor + hard_buffer:
                lock_side = "yes"
                lock_type = "threshold_cross"
        elif strike_type == "between" and floor_strike is not None and cap_strike is not None:
            event_floor = int(floor_strike)
            event_cap = int(cap_strike)
            if actual_high > event_cap + hard_buffer:
                lock_side = "no"
                lock_type = "upper_bound_breached"

        if not lock_side:
            return None

        side = snapshot.get("side", "")
        price_cents = int(snapshot.get("price_cents", 0) or 0)
        payout_cents = 100 - price_cents if price_cents > 0 else 0
        can_score = side == lock_side and payout_cents >= min_payout
        score_block_reason = ""
        if not can_score:
            if side != lock_side:
                score_block_reason = "side_mismatch"
            elif price_cents <= 0:
                score_block_reason = "missing_price"
            else:
                score_block_reason = "payout_below_min"

        return {
            "ticker": snapshot.get("ticker", ""),
            "snapshot_day": snapshot_day,
            "timestamp": snapshot.get("timestamp", ""),
            "city_code": snapshot.get("city_code", ""),
            "target_date": snapshot.get("target_date", ""),
            "title": metadata.get("title", ""),
            "subtitle": metadata.get("subtitle", ""),
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
            "event_floor": event_floor,
            "event_cap": event_cap,
            "actual_high_f": int(actual_high),
            "lock_side": lock_side,
            "lock_type": lock_type,
            "side_seen": side,
            "signal": snapshot.get("signal", ""),
            "skip_reason": snapshot.get("skip_reason", ""),
            "confirmation_verdict": snapshot.get("confirmation_verdict", ""),
            "price_cents": price_cents,
            "payout_cents": payout_cents,
            "can_score": can_score,
            "score_block_reason": score_block_reason,
            "estimated_profit_cents": payout_cents if can_score else 0,
        }

    def _get_public_market_metadata(self, ticker, refresh=False):
        cache = self.state.setdefault("market_metadata_cache", {})
        if not refresh:
            cached = cache.get(ticker)
            if isinstance(cached, dict) and cached.get("strike_type"):
                return cached

        url = f"{config.PROD_API_URL}/markets/{ticker}"
        try:
            response = requests.get(url, timeout=15)
            if response.status_code != 200:
                return None
            payload = response.json()
            market = payload.get("market", payload)
            metadata = {
                "ticker": ticker,
                "title": market.get("title", ""),
                "subtitle": market.get("subtitle", ""),
                "event_ticker": market.get("event_ticker", ""),
                "status": market.get("status", ""),
                "result": market.get("result", ""),
                "strike_type": market.get("strike_type", ""),
                "floor_strike": market.get("floor_strike"),
                "cap_strike": market.get("cap_strike"),
                "close_time": market.get("close_time", ""),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            cache[ticker] = metadata
            return metadata
        except Exception:
            return None

    @staticmethod
    def _metadata_from_snapshot(snapshot):
        strike_type = snapshot.get("strike_type", "")
        floor_strike = snapshot.get("floor_strike")
        cap_strike = snapshot.get("cap_strike")
        if not strike_type:
            return None
        return {
            "ticker": snapshot.get("ticker", ""),
            "title": snapshot.get("market_title", ""),
            "subtitle": snapshot.get("market_subtitle", ""),
            "event_ticker": snapshot.get("event_ticker", ""),
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
        }

    def _load_learning_state(self):
        try:
            if os.path.exists(config.LEARNING_STATE_FILE):
                with open(config.LEARNING_STATE_FILE, "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            return {}
        return {}

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
