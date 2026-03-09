"""
RISK MANAGER v4.0
====================
10 safety checks (down from 19). Simplified kill switch.
Daily loss = stop for day (auto-resume). 5 consecutive = 4h pause.
No Sharpe-based shutdown.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
import config


class RiskManager:

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.state = self._load_state()
        self._ensure_state_defaults()

    def _ensure_state_defaults(self):
        defaults = {
            "positions": {},
            "daily_pnl_cents": 0,
            "daily_date": "",
            "consecutive_losses": 0,
            "kill_switch_until": None,
            "last_trade_time": None,
            "trade_count_today": 0,
            "total_exposure_cents": 0,
        }
        for k, v in defaults.items():
            if k not in self.state:
                self.state[k] = v

    def check_trade(self, signal):
        """
        Run 10 safety checks. Returns (approved, reason).
        signal dict keys: ticker, city_code, side, price_cents, contracts,
                         cost_cents, edge, is_confirmed, is_arb
        """
        ticker = signal.get("ticker", "")
        city = signal.get("city_code", "")
        cost = signal.get("cost_cents", 0)
        is_confirmed = signal.get("is_confirmed", False)
        is_arb = signal.get("is_arb", False)

        self._check_daily_reset()

        # 1. Kill switch
        if self.state.get("kill_switch_until"):
            until = self.state["kill_switch_until"]
            if datetime.now(timezone.utc).isoformat() < until:
                return False, "Kill switch active until " + until
            else:
                self.state["kill_switch_until"] = None
                self._save_state()

        # 2. Daily loss limit
        dpnl = self.state["daily_pnl_cents"]
        if dpnl <= -config.DAILY_LOSS_LIMIT_CENTS:
            return False, "Daily loss limit hit: %dc" % dpnl

        # 3. Consecutive loss pause
        if self.state["consecutive_losses"] >= config.KILL_SWITCH_CONSEC_LOSSES:
            pause_until = datetime.now(timezone.utc) + timedelta(
                hours=config.KILL_SWITCH_PAUSE_HOURS
            )
            self.state["kill_switch_until"] = pause_until.isoformat()
            self._save_state()
            return False, "%d consecutive losses -> %dh pause" % (
                config.KILL_SWITCH_CONSEC_LOSSES, config.KILL_SWITCH_PAUSE_HOURS)

        # 4. Trade cooldown (same-cycle trades exempt)
        last = self.state.get("last_trade_time")
        if last and not signal.get("same_cycle", False):
            if isinstance(last, str):
                last = 0  # Old format, skip cooldown
            elapsed = time.time() - last
            if elapsed < config.TRADE_COOLDOWN:
                return False, "Cooldown: %ds remaining" % int(config.TRADE_COOLDOWN - elapsed)

        # 5. Max open positions (confirmed/arb bypass)
        if not is_confirmed and not is_arb:
            open_count = len(self.state["positions"])
            if open_count >= config.MAX_OPEN_POSITIONS:
                return False, "Max %d open positions (%d active)" % (
                    config.MAX_OPEN_POSITIONS, open_count)

        # 6. Total exposure (confirmed bypass)
        balance = self._get_balance_cents()
        max_exposure = int(balance * config.MAX_TOTAL_EXPOSURE_PCT)
        if not is_confirmed:
            cur_exp = self.state["total_exposure_cents"]
            if cur_exp + cost > max_exposure:
                return False, "Exposure limit: %dc + %dc > %dc" % (cur_exp, cost, max_exposure)

        # 7. Per-ticker limit
        ticker_exposure = self._ticker_exposure(ticker)
        if ticker_exposure + cost > config.MAX_PER_TICKER_CENTS:
            return False, "Per-ticker limit: %dc + %dc > %dc" % (
                ticker_exposure, cost, config.MAX_PER_TICKER_CENTS)

        # 8. Per-city concentration (confirmed bypass)
        if not is_confirmed:
            max_city = int(balance * config.MAX_PER_CITY_PCT)
            city_exp = self._city_exposure(city)
            if city_exp + cost > max_city:
                return False, "City %s limit: %dc + %dc > %dc" % (city, city_exp, cost, max_city)

        # 9. Correlated positions
        max_corr = config.MAX_CORRELATED_POSITIONS
        if is_confirmed:
            max_corr += 1
        city_positions = sum(1 for p in self.state["positions"].values()
                           if p.get("city_code") == city)
        if city_positions >= max_corr:
            return False, "Correlated limit: %d positions in %s (max %d)" % (
                city_positions, city, max_corr)

        # 10. Max contracts per ticker
        existing = self.state["positions"].get(ticker, {})
        existing_contracts = existing.get("contracts", 0)
        new_contracts = signal.get("contracts", 1)
        if existing_contracts + new_contracts > config.MAX_CONTRACTS_PER_TICKER:
            return False, "Contract limit: %d + %d > %d" % (
                existing_contracts, new_contracts, config.MAX_CONTRACTS_PER_TICKER)

        return True, "Approved"

    def record_trade(self, trade_info):
        """Record a new trade in risk state."""
        ticker = trade_info.get("ticker", "")
        self.state["positions"][ticker] = {
            "ticker": ticker,
            "city_code": trade_info.get("city_code", ""),
            "side": trade_info.get("side", "yes"),
            "price_cents": trade_info.get("price_cents", 0),
            "contracts": trade_info.get("contracts", 1),
            "cost_cents": trade_info.get("cost_cents", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "order_id": trade_info.get("order_id", ""),
            "order_status": trade_info.get("order_status", "resting"),
            "is_confirmed": trade_info.get("is_confirmed", False),
            "is_arb": trade_info.get("is_arb", False),
        }
        self.state["total_exposure_cents"] += trade_info.get("cost_cents", 0)
        self.state["last_trade_time"] = time.time()
        self.state["trade_count_today"] += 1
        self._save_state()

    def record_settlement(self, ticker, pnl_cents):
        """Record settlement result."""
        if ticker in self.state["positions"]:
            del self.state["positions"][ticker]
        self.state["daily_pnl_cents"] += pnl_cents
        self.state["total_exposure_cents"] = max(
            0, self.state["total_exposure_cents"] - abs(pnl_cents)
        )
        if pnl_cents < 0:
            self.state["consecutive_losses"] += 1
        else:
            self.state["consecutive_losses"] = 0
        self._save_state()

    def close_position(self, ticker):
        """Remove a position from tracking."""
        if ticker in self.state["positions"]:
            cost = self.state["positions"][ticker].get("cost_cents", 0)
            del self.state["positions"][ticker]
            self.state["total_exposure_cents"] = max(
                0, self.state["total_exposure_cents"] - cost
            )
            self._save_state()

    def add_pending_order(self, ticker, order_info):
        """Track a pending/resting order."""
        self.state["positions"][ticker] = order_info
        self._save_state()

    def clear_pending_order(self, ticker):
        """Remove a pending order that was cancelled."""
        if ticker in self.state["positions"]:
            cost = self.state["positions"][ticker].get("cost_cents", 0)
            del self.state["positions"][ticker]
            self.state["total_exposure_cents"] = max(
                0, self.state["total_exposure_cents"] - cost
            )
            self._save_state()

    def get_positions(self):
        return dict(self.state.get("positions", {}))

    def get_state_summary(self):
        return {
            "open_positions": len(self.state["positions"]),
            "daily_pnl_cents": self.state["daily_pnl_cents"],
            "consecutive_losses": self.state["consecutive_losses"],
            "total_exposure_cents": self.state["total_exposure_cents"],
            "kill_switch_until": self.state.get("kill_switch_until"),
            "trade_count_today": self.state["trade_count_today"],
        }

    def _ticker_exposure(self, ticker):
        pos = self.state["positions"].get(ticker, {})
        return pos.get("cost_cents", 0)

    def _city_exposure(self, city):
        total = 0
        for p in self.state["positions"].values():
            if p.get("city_code") == city:
                total += p.get("cost_cents", 0)
        return total

    def _get_balance_cents(self):
        if self.client:
            try:
                bal = self.client.get_balance()
                if bal and "balance" in bal:
                    return bal["balance"]
            except Exception:
                pass
        return config.TOTAL_DEPOSITS_CENTS

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state["daily_date"] != today:
            self.state["daily_date"] = today
            self.state["daily_pnl_cents"] = 0
            self.state["trade_count_today"] = 0
            self._save_state()

    def _load_state(self):
        try:
            if os.path.exists(config.RISK_STATE_FILE):
                with open(config.RISK_STATE_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            config.atomic_json_save(config.RISK_STATE_FILE, self.state)
        except Exception as e:
            print("  [RISK] Error saving state: %s" % e)
