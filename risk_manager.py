"""
RISK MANAGER v3.0
======================
Safety system that protects your bankroll.
Every trade must pass ALL checks before execution.

CHECKS:
  1. Daily loss limit
  2. Total exposure cap
  3. Max open positions
  4. Per-city exposure limit (weather)
  5. Consecutive loss pause
  6. Daily trade count limit
  7. Cooldown between trades
  8. Manual approval threshold
"""

import json
import os
from datetime import datetime, timedelta, timezone
import config


class RiskManager:

    def __init__(self):
        self.state = {
            "daily_loss_cents": 0,
            "daily_trade_count": 0,
            "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
            "last_trade_time": None,
            "positions": [],
            "consecutive_losses": 0,
            "loss_pause_until": None,
            "city_exposure": {},  # {"NYC": 300, "CHI": 150, ...}
        }
        self._load_state()

    # ═══════════════════════════════════════════════════════
    # MAIN CHECK: Can we trade?
    # ═══════════════════════════════════════════════════════

    def check_trade(self, signal):
        """
        Run all safety checks. Returns (approved, reason).
        If approved is True, the trade can proceed.
        """
        self._maybe_reset_daily()

        edge = signal.get("edge", 0)
        price = signal.get("price_cents", 0)
        contracts = signal.get("suggested_contracts", 0)
        cost = price * contracts

        # 1. Daily loss limit
        if self.state["daily_loss_cents"] >= config.DAILY_LOSS_LIMIT_CENTS:
            return False, f"Daily loss limit hit (${self.state['daily_loss_cents']/100:.2f})"

        # 2. Total exposure
        total_exposure = sum(p.get("cost_cents", 0) for p in self.state["positions"])
        if total_exposure + cost > config.MAX_TOTAL_EXPOSURE_CENTS:
            return False, f"Exposure cap: ${total_exposure/100:.2f} + ${cost/100:.2f} > ${config.MAX_TOTAL_EXPOSURE_CENTS/100:.2f}"

        # 3. Max positions
        if len(self.state["positions"]) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max {config.MAX_OPEN_POSITIONS} positions reached"

        # 4. Per-city exposure (weather strategy)
        city = signal.get("city_code", "")
        if city:
            city_exp = self.state["city_exposure"].get(city, 0)
            if city_exp + cost > config.MAX_PER_CITY_CENTS:
                return False, f"{city} exposure: ${city_exp/100:.2f} + ${cost/100:.2f} > ${config.MAX_PER_CITY_CENTS/100:.2f}"

        # 4b. Correlated positions cap (same city + same date)
        if city:
            ticker = signal.get("ticker", "")
            signal_date = self._extract_date_from_ticker(ticker)
            if signal_date:
                corr_count = sum(
                    1 for p in self.state["positions"]
                    if p.get("city_code") == city
                    and self._extract_date_from_ticker(p.get("ticker", "")) == signal_date
                )
                if corr_count >= config.MAX_CORRELATED_POSITIONS:
                    return False, f"Max {config.MAX_CORRELATED_POSITIONS} correlated positions for {city} on {signal_date}"

        # 5. Consecutive loss pause
        if self.state["loss_pause_until"]:
            pause_until = datetime.fromisoformat(self.state["loss_pause_until"])
            if datetime.now() < pause_until:
                remaining = (pause_until - datetime.now()).total_seconds() / 60
                return False, f"Loss streak pause: {remaining:.0f} min remaining"

        if self.state["consecutive_losses"] >= config.CONSECUTIVE_LOSS_PAUSE:
            # Activate pause
            pause_until = datetime.now() + timedelta(minutes=config.CONSECUTIVE_LOSS_PAUSE_MINUTES)
            self.state["loss_pause_until"] = pause_until.isoformat()
            self._save_state()
            return False, f"{config.CONSECUTIVE_LOSS_PAUSE} losses in a row — pausing {config.CONSECUTIVE_LOSS_PAUSE_MINUTES} min"

        # 6. Daily trade count
        if self.state["daily_trade_count"] >= config.MAX_DAILY_TRADES:
            return False, f"Max {config.MAX_DAILY_TRADES} trades/day reached"

        # 7. Cooldown
        if self.state["last_trade_time"]:
            elapsed = (datetime.now() - datetime.fromisoformat(self.state["last_trade_time"])).total_seconds()
            if elapsed < config.TRADE_COOLDOWN:
                remaining = config.TRADE_COOLDOWN - elapsed
                return False, f"Cooldown: {remaining:.0f}s remaining"

        # 8. Manual approval
        if cost > config.APPROVAL_THRESHOLD_CENTS:
            return "NEEDS_APPROVAL", f"Trade costs ${cost/100:.2f} > ${config.APPROVAL_THRESHOLD_CENTS/100:.2f} — needs approval"

        # 9. Settlement proximity — no new positions within N hours of close
        close_time_str = signal.get("close_time")
        if close_time_str:
            try:
                close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                now_utc = datetime.now(timezone.utc)
                hours_until_close = (close_time - now_utc).total_seconds() / 3600
                if 0 < hours_until_close <= config.SETTLEMENT_PROXIMITY_HOURS:
                    if edge <= config.SETTLEMENT_PROXIMITY_EDGE_OVERRIDE:
                        return False, f"Too close to settlement ({hours_until_close:.1f}h, edge {edge:.0%} < {config.SETTLEMENT_PROXIMITY_EDGE_OVERRIDE:.0%})"
            except (ValueError, TypeError):
                pass  # Graceful degradation if close_time is unparseable

        # All checks passed
        return True, "All risk checks passed"

    # ═══════════════════════════════════════════════════════
    # RECORD KEEPING
    # ═══════════════════════════════════════════════════════

    def record_trade(self, ticker, side, cost_cents, contracts, city_code="",
                     title="", edge=0, expected_profit_cents=0, market_description=""):
        """Record a new position."""
        self.state["positions"].append({
            "ticker": ticker,
            "side": side,
            "cost_cents": cost_cents,
            "contracts": contracts,
            "city_code": city_code,
            "timestamp": datetime.now().isoformat(),
            "title": title,
            "edge": edge,
            "expected_profit_cents": expected_profit_cents,
            "market_description": market_description,
        })
        self.state["last_trade_time"] = datetime.now().isoformat()
        self.state["daily_trade_count"] += 1

        if city_code:
            self.state["city_exposure"][city_code] = self.state["city_exposure"].get(city_code, 0) + cost_cents

        self._save_state()

    def record_loss(self, amount_cents):
        """Record a loss."""
        self.state["daily_loss_cents"] += amount_cents
        self.state["consecutive_losses"] += 1
        self._save_state()

    def record_win(self, amount_cents):
        """Record a win — resets consecutive loss counter."""
        self.state["consecutive_losses"] = 0
        self.state["loss_pause_until"] = None
        self._save_state()

    def remove_position(self, ticker):
        """Remove a settled/closed position."""
        self.state["positions"] = [
            p for p in self.state["positions"] if p["ticker"] != ticker
        ]
        # Recalculate city exposure
        self.state["city_exposure"] = {}
        for p in self.state["positions"]:
            city = p.get("city_code", "")
            if city:
                self.state["city_exposure"][city] = self.state["city_exposure"].get(city, 0) + p["cost_cents"]
        self._save_state()

    def release_exposure(self, ticker, cost_cents, city_code=""):
        """Release exposure when a trade settles or expires."""
        # Reduce total exposure
        self.state["total_exposure_cents"] = max(
            0, self.state.get("total_exposure_cents", 0) - cost_cents
        )
        # Reduce city exposure
        if city_code and city_code in self.state.get("city_exposure", {}):
            self.state["city_exposure"][city_code] = max(
                0, self.state["city_exposure"][city_code] - cost_cents
            )
        # Remove from positions list
        self.remove_position(ticker)

    # ═══════════════════════════════════════════════════════
    # KILL SWITCH / OBSERVATION MODE
    # ═══════════════════════════════════════════════════════

    def check_kill_switch(self, trade_log):
        """Check if the bot should enter observation mode.
        Returns (is_observation, reason)."""
        # Manual override from config
        if config.OBSERVATION_MODE:
            self.state["observation_mode"] = True
            self.state["observation_reason"] = "Manual override (config)"
            self._save_state()
            return True, "Manual override (config)"

        # Already in observation mode (persisted)
        if self.state.get("observation_mode"):
            return True, self.state.get("observation_reason", "Kill switch active")

        # Check 1: Consecutive losses
        if self.state["consecutive_losses"] >= config.KILL_SWITCH_CONSECUTIVE_LOSSES:
            reason = f"{self.state['consecutive_losses']} consecutive losses"
            self.state["observation_mode"] = True
            self.state["observation_reason"] = reason
            self._save_state()
            return True, reason

        # Check 2: 7-day Sharpe ratio
        settled = [
            t for t in trade_log
            if t.get("settled") and t.get("profit_cents") is not None
            and t.get("result") not in ("expired_dry_run",)
        ]
        now = datetime.now()
        recent = [
            t for t in settled
            if t.get("timestamp") and
            (now - datetime.fromisoformat(t["timestamp"])).days <= 7
        ]
        if len(recent) >= 3:
            pnls = [t["profit_cents"] / 100.0 for t in recent]
            mean_pnl = sum(pnls) / len(pnls)
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
            stdev = variance ** 0.5
            sharpe = mean_pnl / stdev if stdev > 0 else 0
            if sharpe < config.KILL_SWITCH_MIN_SHARPE_7D:
                reason = f"7-day Sharpe {sharpe:.2f} < {config.KILL_SWITCH_MIN_SHARPE_7D}"
                self.state["observation_mode"] = True
                self.state["observation_reason"] = reason
                self._save_state()
                return True, reason

        return False, ""

    def resume_trading(self):
        """Reset observation mode and consecutive losses to resume trading."""
        self.state["observation_mode"] = False
        self.state["observation_reason"] = ""
        self.state["consecutive_losses"] = 0
        self.state["loss_pause_until"] = None
        self._save_state()

    # ═══════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════

    def print_status(self):
        """Print current risk state."""
        self._maybe_reset_daily()
        total_exp = sum(p.get("cost_cents", 0) for p in self.state["positions"])

        print(f"\n  ┌─ Risk Status ─────────────────────────────────")
        print(f"  │  Daily loss:     ${self.state['daily_loss_cents']/100:.2f} / ${config.DAILY_LOSS_LIMIT_CENTS/100:.2f}")
        print(f"  │  Exposure:       ${total_exp/100:.2f} / ${config.MAX_TOTAL_EXPOSURE_CENTS/100:.2f}")
        print(f"  │  Positions:      {len(self.state['positions'])} / {config.MAX_OPEN_POSITIONS}")
        print(f"  │  Trades today:   {self.state['daily_trade_count']} / {config.MAX_DAILY_TRADES}")
        print(f"  │  Loss streak:    {self.state['consecutive_losses']}")

        if self.state["city_exposure"]:
            city_str = ", ".join(f"{c}: ${v/100:.2f}" for c, v in self.state["city_exposure"].items())
            print(f"  │  City exposure:  {city_str}")

        if self.state["loss_pause_until"]:
            pause = datetime.fromisoformat(self.state["loss_pause_until"])
            if datetime.now() < pause:
                remaining = (pause - datetime.now()).total_seconds() / 60
                print(f"  │  ⚠ PAUSED:      {remaining:.0f} min remaining")

        print(f"  └────────────────────────────────────────────────\n")

    # ═══════════════════════════════════════════════════════
    # INTERNAL
    # ═══════════════════════════════════════════════════════

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight and clean up expired positions."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state["last_reset_date"] != today:
            self.state["daily_loss_cents"] = 0
            self.state["daily_trade_count"] = 0
            self.state["last_reset_date"] = today
            self.state["consecutive_losses"] = 0
            self.state["loss_pause_until"] = None
            self._cleanup_expired_positions()
            self._save_state()

    def _cleanup_expired_positions(self):
        """Remove positions whose market date has passed.
        Ticker format: KXHIGHNY-26FEB14-B46.5 → date portion is 26FEB14 (Feb 14, 2026).
        """
        today = datetime.now().date()
        active = []
        for p in self.state["positions"]:
            ticker = p.get("ticker", "")
            parts = ticker.split("-")
            if len(parts) >= 2:
                date_str = parts[1]  # e.g. "26FEB14"
                try:
                    market_date = datetime.strptime(date_str, "%y%b%d").date()
                    if market_date >= today:
                        active.append(p)
                    else:
                        print(f"  [RISK] Cleaned up expired position: {ticker} (settled {market_date})")
                except ValueError:
                    active.append(p)  # keep positions with unparseable dates
            else:
                active.append(p)
        self.state["positions"] = active
        # Recalculate city exposure from remaining positions
        self.state["city_exposure"] = {}
        for p in self.state["positions"]:
            city = p.get("city_code", "")
            if city:
                self.state["city_exposure"][city] = self.state["city_exposure"].get(city, 0) + p["cost_cents"]

    @staticmethod
    def _extract_date_from_ticker(ticker):
        """Extract date portion from ticker like KXHIGHNY-26FEB15-B42.5 → '26FEB15'."""
        try:
            parts = ticker.split("-")
            if len(parts) >= 2:
                return parts[1]
        except Exception:
            pass
        return None

    def _save_state(self):
        try:
            with open(config.RISK_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            if os.path.exists(config.RISK_STATE_FILE):
                with open(config.RISK_STATE_FILE) as f:
                    loaded = json.load(f)
                    self.state.update(loaded)
        except Exception:
            pass
