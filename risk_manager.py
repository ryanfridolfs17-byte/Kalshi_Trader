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
  6. Cooldown between trades
  7. Manual approval threshold
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
            "daily_city_spend": {},  # Cumulative daily spending per city (not reduced by sells)
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

        # 2. Exposure checks (settlement-aware)
        classified = self.classify_positions()
        active_exposure = classified["active_exposure_cents"]
        pending_exposure = classified["pending_exposure_cents"]

        # 2a. Active exposure cap: dynamic percentage-based (60% of bankroll)
        balance = self.state.get("balance_cents", config.MAX_TOTAL_EXPOSURE_CENTS)
        exposure_cap = max(int(balance * config.MAX_TOTAL_EXPOSURE_PCT), config.MAX_TOTAL_EXPOSURE_CENTS)
        if active_exposure + cost > exposure_cap:
            return False, (f"Active exposure cap: ${active_exposure/100:.2f} + ${cost/100:.2f} "
                           f"> ${exposure_cap/100:.2f} ({config.MAX_TOTAL_EXPOSURE_PCT:.0%} of ${balance/100:.2f})"
                           f" (+${pending_exposure/100:.2f} pending settlement)")

        # 2b. Hard ceiling: active + pending can't grow unbounded
        hard_ceiling = exposure_cap * 2
        if active_exposure + pending_exposure + cost > hard_ceiling:
            return False, (f"Total capital guard: ${(active_exposure + pending_exposure)/100:.2f} "
                           f"+ ${cost/100:.2f} > ${hard_ceiling/100:.2f}")

        # 2c. Dynamic per-position cap: never more than 20% of capital in one trade
        # If over cap, reduce contracts to fit instead of rejecting outright
        balance = self.state.get("balance_cents", config.MAX_TOTAL_EXPOSURE_CENTS)
        max_per_position = int(balance * config.MAX_POSITION_PCT)
        if cost > max_per_position and price > 0:
            capped_contracts = max(1, max_per_position // price)
            capped_cost = price * capped_contracts
            if capped_cost > max_per_position:
                return False, f"Per-position cap: even 1 contract @ {price}c > ${max_per_position/100:.2f}"
            print(f"    [RISK] Position cap: {contracts} → {capped_contracts} contracts "
                  f"(${cost/100:.2f} → ${capped_cost/100:.2f}, cap ${max_per_position/100:.2f})")
            signal["suggested_contracts"] = capped_contracts
            contracts = capped_contracts
            cost = capped_cost

        # 2d. Liquidity reserve: keep % of bankroll liquid for next-day overnight edge window
        # Confirmed outcomes bypass: near-guaranteed wins shouldn't be blocked by reserve
        reserve_cents = int(balance * config.LIQUIDITY_RESERVE_PCT)
        available = exposure_cap - active_exposure
        is_confirmed = signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME"
        if available - cost < reserve_cents:
            if not is_confirmed and edge < 0.20:  # Override for exceptional edge or confirmed outcomes
                return False, (f"Liquidity reserve: ${available/100:.2f} available, "
                               f"trade costs ${cost/100:.2f}, need ${reserve_cents/100:.2f} reserve (25% of bankroll)")

        # 3. Max positions (count only active, pending are settling out)
        if len(classified["active"]) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max {config.MAX_OPEN_POSITIONS} active positions reached"

        # 4. Per-city exposure (weather strategy) — dynamic percentage-based (30% of bankroll)
        city = signal.get("city_code", "")
        if city:
            city_cap = max(int(balance * config.MAX_PER_CITY_PCT), config.MAX_PER_CITY_CENTS)
            city_exp = self.state["city_exposure"].get(city, 0)
            if city_exp + cost > city_cap:
                return False, (f"{city} exposure: ${city_exp/100:.2f} + ${cost/100:.2f} "
                               f"> ${city_cap/100:.2f} ({config.MAX_PER_CITY_PCT:.0%} of bankroll)")

        # 4a2. Cumulative daily city spending cap (prevents sell-and-rebuy chasing)
        if city:
            daily_spend = self.state.get("daily_city_spend", {}).get(city, 0)
            if daily_spend + cost > config.MAX_DAILY_CITY_SPEND_CENTS:
                return False, (f"{city} daily spend cap: ${daily_spend/100:.2f} + ${cost/100:.2f} "
                               f"> ${config.MAX_DAILY_CITY_SPEND_CENTS/100:.2f} cumulative")

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

        # 4c. Per-ticker cost cap (prevents runaway scaling into one contract)
        ticker = signal.get("ticker", "")
        if ticker:
            existing_ticker_cost = sum(
                p.get("cost_cents", 0) for p in self.state["positions"]
                if p.get("ticker") == ticker
            )
            if existing_ticker_cost + cost > config.MAX_PER_TICKER_CENTS:
                return False, (f"Ticker {ticker} capped: ${existing_ticker_cost/100:.2f} + ${cost/100:.2f} "
                               f"> ${config.MAX_PER_TICKER_CENTS/100:.2f}")

            # 4d. Per-ticker contract count cap (prevents cheap contract explosion)
            existing_contracts = sum(
                p.get("contracts", 0) for p in self.state["positions"]
                if p.get("ticker") == ticker
            )
            new_contracts = signal.get("suggested_contracts", 0)
            if existing_contracts + new_contracts > config.MAX_CONTRACTS_PER_TICKER:
                return False, (f"Contract cap: {existing_contracts} + {new_contracts} "
                               f"> {config.MAX_CONTRACTS_PER_TICKER}")

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

        # 6. Cooldown
        if self.state["last_trade_time"]:
            elapsed = (datetime.now() - datetime.fromisoformat(self.state["last_trade_time"])).total_seconds()
            if elapsed < config.TRADE_COOLDOWN:
                remaining = config.TRADE_COOLDOWN - elapsed
                return False, f"Cooldown: {remaining:.0f}s remaining"

        # 6b. Daily forecast trade count cap (confirmed outcomes + arbitrage exempt)
        is_confirmed = signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME"
        is_arbitrage = signal.get("strategy") == "S2-Arbitrage"
        if not is_confirmed and not is_arbitrage:
            forecast_trades_today = self.state.get("daily_forecast_trades", 0)
            if forecast_trades_today >= config.MAX_DAILY_FORECAST_TRADES:
                return False, (f"Daily forecast trade cap: {forecast_trades_today} trades "
                               f"(max {config.MAX_DAILY_FORECAST_TRADES}/day)")

        # 7. Manual approval
        if cost > config.APPROVAL_THRESHOLD_CENTS:
            return "NEEDS_APPROVAL", f"Trade costs ${cost/100:.2f} > ${config.APPROVAL_THRESHOLD_CENTS/100:.2f} — needs approval"

        # 8. Settlement proximity — no new positions within N hours of close
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
        """Record a new position, or merge into existing if same ticker (scale-in)."""
        # Check if we already hold this ticker — merge if so
        existing = None
        for p in self.state["positions"]:
            if p.get("ticker") == ticker:
                existing = p
                break

        if existing:
            existing["contracts"] += contracts
            existing["cost_cents"] += cost_cents
            existing["edge"] = edge  # Update to latest edge
            existing["expected_profit_cents"] += expected_profit_cents
        else:
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
        # Track forecast trades for daily cap (initialized on first use)
        if "daily_forecast_trades" not in self.state:
            self.state["daily_forecast_trades"] = 0
        self.state["daily_forecast_trades"] += 1

        if city_code:
            self.state["city_exposure"][city_code] = self.state["city_exposure"].get(city_code, 0) + cost_cents
            # Track cumulative daily spending (never reduced by sells — resets at midnight)
            if "daily_city_spend" not in self.state:
                self.state["daily_city_spend"] = {}
            self.state["daily_city_spend"][city_code] = self.state["daily_city_spend"].get(city_code, 0) + cost_cents

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

    def reduce_position(self, ticker, sell_contracts, released_cost_cents):
        """Reduce a position's contract count and cost after a partial sell.
        If contracts reach 0, removes the position entirely."""
        for pos in self.state["positions"]:
            if pos.get("ticker") == ticker:
                pos["contracts"] = max(0, pos["contracts"] - sell_contracts)
                pos["cost_cents"] = max(0, pos["cost_cents"] - released_cost_cents)

                if pos["contracts"] <= 0:
                    self.remove_position(ticker)
                else:
                    # Update city exposure
                    city = pos.get("city_code", "")
                    if city and city in self.state.get("city_exposure", {}):
                        self.state["city_exposure"][city] = max(
                            0, self.state["city_exposure"][city] - released_cost_cents
                        )
                    self._save_state()
                return
        # Position not found — nothing to reduce

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
        # Reload observation_mode from disk — the dashboard's /api/resume
        # endpoint writes directly to risk_state.json, so we need to pick
        # up external changes each cycle.
        try:
            if os.path.exists(config.RISK_STATE_FILE):
                with open(config.RISK_STATE_FILE) as f:
                    disk_state = json.load(f)
                if not disk_state.get("observation_mode") and self.state.get("observation_mode"):
                    # Dashboard resumed trading — apply to in-memory state
                    self.state["observation_mode"] = False
                    self.state["observation_reason"] = ""
                    self.state["consecutive_losses"] = disk_state.get("consecutive_losses", 0)
                    self.state["loss_pause_until"] = disk_state.get("loss_pause_until")
                    print("  [RISK] Observation mode cleared via dashboard")
        except Exception:
            pass

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
    # SETTLEMENT-AWARE CAPITAL MANAGEMENT
    # ═══════════════════════════════════════════════════════

    def _parse_market_date(self, ticker):
        """Parse market date from ticker. Returns date object or None.
        Ticker format: KXHIGHNY-26FEB14-B46.5 → Feb 14, 2026."""
        parts = ticker.split("-") if ticker else []
        if len(parts) >= 2:
            try:
                return datetime.strptime(parts[1], "%y%b%d").date()
            except ValueError:
                pass
        return None

    def classify_positions(self):
        """Split positions into active (tradeable) vs pending settlement (locked).

        Active: market date is today or future.
        Pending settlement: market date has passed, awaiting 10 AM ET settlement.
        """
        today = datetime.now().date()
        active = []
        pending = []

        for p in self.state["positions"]:
            market_date = self._parse_market_date(p.get("ticker", ""))
            if market_date is not None and market_date < today:
                pending.append(p)
            else:
                active.append(p)

        return {
            "active": active,
            "pending_settlement": pending,
            "active_exposure_cents": sum(p.get("cost_cents", 0) for p in active),
            "pending_exposure_cents": sum(p.get("cost_cents", 0) for p in pending),
        }

    def is_pre_settlement_window(self):
        """Check if pending settlements exist and it's before settlement hour."""
        classified = self.classify_positions()
        if not classified["pending_settlement"]:
            return False
        # Approximate ET: UTC - 5 (matches existing codebase pattern)
        utc_now = datetime.now(timezone.utc)
        et_hour = (utc_now.hour - 5) % 24
        return et_hour < config.SETTLEMENT_HOUR_ET

    def get_exposure_breakdown(self):
        """Return structured exposure data for the dashboard."""
        classified = self.classify_positions()
        return {
            "active_exposure_cents": classified["active_exposure_cents"],
            "pending_settlement_cents": classified["pending_exposure_cents"],
            "active_positions": len(classified["active"]),
            "pending_positions": len(classified["pending_settlement"]),
            "is_pre_settlement": self.is_pre_settlement_window(),
        }

    def update_balance(self, balance_cents):
        """Store the current Kalshi account balance for dynamic sizing."""
        if balance_cents is not None:
            self.state["balance_cents"] = balance_cents
            self._save_state()

    # ═══════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════

    def print_status(self):
        """Print current risk state."""
        self._maybe_reset_daily()
        classified = self.classify_positions()
        active_exp = classified["active_exposure_cents"]
        pending_exp = classified["pending_exposure_cents"]
        n_active = len(classified["active"])
        n_pending = len(classified["pending_settlement"])
        balance = self.state.get("balance_cents")

        print(f"\n  ┌─ Risk Status ─────────────────────────────────")
        if balance is not None:
            print(f"  │  Balance:        ${balance/100:.2f}")
        print(f"  │  Daily loss:     ${self.state['daily_loss_cents']/100:.2f} / ${config.DAILY_LOSS_LIMIT_CENTS/100:.2f}")
        print(f"  │  Active exposure: ${active_exp/100:.2f} / ${config.MAX_TOTAL_EXPOSURE_CENTS/100:.2f}")
        if pending_exp > 0:
            print(f"  │  Pending settle:  ${pending_exp/100:.2f} (locked until {config.SETTLEMENT_HOUR_ET} AM ET)")
            print(f"  │  Total committed: ${(active_exp + pending_exp)/100:.2f}")
        pending_note = f" (+{n_pending} settling)" if n_pending > 0 else ""
        print(f"  │  Positions:      {n_active}{pending_note} / {config.MAX_OPEN_POSITIONS}")
        print(f"  │  Trades today:   {self.state['daily_trade_count']}")
        print(f"  │  Loss streak:    {self.state['consecutive_losses']}")

        daily_spend = self.state.get("daily_city_spend", {})
        if daily_spend:
            spend_str = ", ".join(f"{c}: ${v/100:.2f}" for c, v in daily_spend.items())
            print(f"  │  Daily spend:    {spend_str} (cap: ${config.MAX_DAILY_CITY_SPEND_CENTS/100:.2f})")

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
            self.state["daily_forecast_trades"] = 0  # Reset forecast trade counter
            self.state["last_reset_date"] = today
            self.state["consecutive_losses"] = 0
            self.state["loss_pause_until"] = None
            self.state["daily_city_spend"] = {}  # Reset cumulative city spending
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
