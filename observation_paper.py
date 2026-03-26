"""
Observation-mode paper trading ledger.

This simulates entries while the live bot is paused so we can keep learning
from the exact signals the production code would otherwise try to trade.
"""

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config


class ObservationPaperTrader:
    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.state = self._load_state()
        self._ensure_defaults()

    def _ensure_defaults(self):
        defaults = {
            "active": {},
            "history": [],
            "summary": {},
            "last_reconciled_at": "",
            "last_trade_time": None,
            "daily_date": "",
            "trade_count_today": 0,
            "daily_pnl_cents": 0,
            "total_exposure_cents": 0,
            "ticker_entry_dates": {},
            "cycle_log": [],
        }
        for key, value in defaults.items():
            if key not in self.state:
                if isinstance(value, dict):
                    self.state[key] = dict(value)
                elif isinstance(value, list):
                    self.state[key] = list(value)
                else:
                    self.state[key] = value

    def record_observation_cycle(self, signals, cycle, balance_cents, limit_price_fn, max_per_cycle=3):
        """Simulate paper entries for a scan cycle."""
        self._check_daily_reset()
        now_iso = datetime.now(timezone.utc).isoformat()
        executed = []
        blocked_reasons = Counter()

        for signal in signals:
            if len(executed) >= max_per_cycle:
                blocked_reasons["per_cycle_limit"] += 1
                continue

            limit_price = limit_price_fn(signal)
            if not limit_price or limit_price <= 0:
                blocked_reasons["invalid_limit_price"] += 1
                continue

            approved, result = self._fit_trade(
                signal,
                balance_cents=balance_cents,
                limit_price=limit_price,
                same_cycle=bool(executed),
            )
            if not approved:
                blocked_reasons[result] += 1
                continue

            contracts = result
            position = self._build_position(signal, cycle, limit_price, contracts, now_iso)
            self.state["active"][position["ticker"]] = position
            self.state["last_trade_time"] = time.time()
            self.state["trade_count_today"] = int(self.state.get("trade_count_today", 0) or 0) + 1
            self.state["total_exposure_cents"] = int(self.state.get("total_exposure_cents", 0) or 0) + position["cost_cents"]
            self.state.setdefault("ticker_entry_dates", {})[position["ticker"]] = self._today_et()
            executed.append(position)

        self.state.setdefault("cycle_log", []).append({
            "cycle": cycle,
            "timestamp": now_iso,
            "signals_seen": len(signals),
            "paper_entries": len(executed),
            "blocked_reasons": dict(blocked_reasons),
            "top_signals": [
                {
                    "ticker": s.get("ticker", ""),
                    "side": s.get("side", ""),
                    "edge": round(s.get("edge", 0) or 0, 4),
                    "strategy": s.get("strategy", ""),
                }
                for s in signals[:5]
            ],
        })
        self.state["cycle_log"] = self.state["cycle_log"][-50:]
        self._refresh_summary()
        self._save_state()
        return {
            "executed": executed,
            "blocked_reasons": dict(blocked_reasons),
        }

    def reconcile_settlements(self):
        if not self.client:
            return

        changed = False
        active = self.state.get("active", {})
        history = self.state.get("history", [])
        now = datetime.now(timezone.utc)
        today_et = self._today_et()

        for ticker, pos in list(active.items()):
            result = self._get_market_result(ticker)
            if result in ("yes", "no"):
                gross = self._gross_profit_cents(
                    side=pos.get("side", ""),
                    result=result,
                    price_cents=int(pos.get("entry_price_cents", 0) or 0),
                    contracts=int(pos.get("contracts", 0) or 0),
                )
                net = gross - int(pos.get("estimated_entry_fee_cents", 0) or 0)
                resolved = dict(pos)
                resolved.update({
                    "status": "win" if gross > 0 else "loss",
                    "market_result": result,
                    "resolved_at": now.isoformat(),
                    "gross_profit_cents": gross,
                    "net_profit_cents": net,
                })
                history.append(resolved)
                del active[ticker]
                self.state["daily_pnl_cents"] = int(self.state.get("daily_pnl_cents", 0) or 0) + net
                changed = True
                continue

            target_date = pos.get("target_date", "")
            if target_date and target_date < today_et:
                stale_cutoff = datetime.fromisoformat(pos.get("opened_at", now.isoformat()).replace("Z", "+00:00"))
                if now - stale_cutoff > timedelta(days=2):
                    expired = dict(pos)
                    expired.update({
                        "status": "expired_unknown",
                        "market_result": "",
                        "resolved_at": now.isoformat(),
                        "gross_profit_cents": 0,
                        "net_profit_cents": 0,
                    })
                    history.append(expired)
                    del active[ticker]
                    changed = True

        if changed:
            self.state["history"] = history[-500:]
        self.state["last_reconciled_at"] = now.isoformat()
        self._refresh_summary()
        self._save_state()

    def get_public_summary(self):
        self._refresh_summary()
        summary = dict(self.state.get("summary", {}))
        return {
            "summary": summary,
            "active": [
                {
                    "ticker": row.get("ticker", ""),
                    "side": row.get("side", ""),
                    "contracts": row.get("contracts", 0),
                    "entry_price_cents": row.get("entry_price_cents", 0),
                    "strategy": row.get("strategy", ""),
                    "target_date": row.get("target_date", ""),
                    "opened_at": row.get("opened_at", ""),
                }
                for row in list(self.state.get("active", {}).values())[:10]
            ],
            "recent_history": [
                {
                    "ticker": row.get("ticker", ""),
                    "side": row.get("side", ""),
                    "status": row.get("status", ""),
                    "net_profit_cents": row.get("net_profit_cents", 0),
                    "resolved_at": row.get("resolved_at", ""),
                }
                for row in self.state.get("history", [])[-10:]
            ],
            "recent_cycles": self.state.get("cycle_log", [])[-10:],
        }

    def _fit_trade(self, signal, balance_cents, limit_price, same_cycle=False):
        ticker = signal.get("ticker", "")
        city = signal.get("city_code", "")
        contracts = int(signal.get("suggested_contracts", signal.get("contracts", 1)) or 0)
        if not ticker or not city or contracts <= 0 or limit_price <= 0:
            return False, "invalid_signal"

        if ticker in self.state.get("active", {}):
            return False, "paper_position_exists"

        today = self._today_et()
        if self.state.get("ticker_entry_dates", {}).get(ticker) == today:
            return False, "paper_no_reentry_same_day"

        last_trade = self.state.get("last_trade_time")
        if last_trade and not same_cycle:
            elapsed = time.time() - float(last_trade)
            if elapsed < config.TRADE_COOLDOWN:
                return False, "paper_cooldown"

        if len(self.state.get("active", {})) >= config.MAX_OPEN_POSITIONS:
            return False, "paper_max_open_positions"

        if self._city_position_count(city) >= config.MAX_CORRELATED_POSITIONS:
            return False, "paper_correlated_limit"

        reserve_balance = int(balance_cents * (1.0 - config.LIQUIDITY_RESERVE_PCT))
        max_contracts = contracts

        total_room = int(reserve_balance * config.MAX_TOTAL_EXPOSURE_PCT) - int(self.state.get("total_exposure_cents", 0) or 0)
        if total_room <= 0:
            return False, "paper_total_exposure_limit"
        max_contracts = min(max_contracts, total_room // limit_price)

        ticker_room = config.MAX_PER_TICKER_CENTS
        max_contracts = min(max_contracts, ticker_room // limit_price)

        city_room = int(reserve_balance * config.MAX_PER_CITY_PCT) - self._city_exposure(city)
        if city_room <= 0:
            return False, "paper_city_limit"
        max_contracts = min(max_contracts, city_room // limit_price)

        region_room = self._region_room(city, reserve_balance)
        if region_room <= 0:
            return False, "paper_region_limit"
        max_contracts = min(max_contracts, region_room // limit_price)

        max_contracts = min(max_contracts, config.MAX_CONTRACTS_PER_TICKER)
        if max_contracts < 1:
            return False, "paper_sized_to_zero"
        return True, max_contracts

    def _build_position(self, signal, cycle, limit_price, contracts, opened_at):
        ticker = signal.get("ticker", "")
        estimated_fee = self._estimate_entry_fee_cents(limit_price, contracts)
        return {
            "ticker": ticker,
            "side": signal.get("side", ""),
            "contracts": contracts,
            "entry_price_cents": limit_price,
            "cost_cents": limit_price * contracts,
            "estimated_entry_fee_cents": estimated_fee,
            "city_code": signal.get("city_code", ""),
            "target_date": signal.get("target_date", ""),
            "strategy": signal.get("strategy", ""),
            "confirmation_verdict": signal.get("confirmation_verdict", ""),
            "edge": round(signal.get("edge", 0) or 0, 4),
            "fee_adjusted_edge": round(signal.get("fee_adjusted_edge", 0) or 0, 4),
            "our_prob": round(signal.get("our_prob", 0) or 0, 4),
            "market_prob": round(signal.get("market_prob", 0) or 0, 4),
            "reasoning": signal.get("reasoning", ""),
            "opened_at": opened_at,
            "cycle": cycle,
            "status": "active",
        }

    def _refresh_summary(self):
        active_positions = list(self.state.get("active", {}).values())
        resolved = [h for h in self.state.get("history", []) if h.get("status") in ("win", "loss")]
        wins = [h for h in resolved if h.get("status") == "win"]
        losses = [h for h in resolved if h.get("status") == "loss"]
        total_exposure_cents = int(sum(int(p.get("cost_cents", 0) or 0) for p in active_positions))
        self.state["total_exposure_cents"] = total_exposure_cents
        self.state["summary"] = {
            "active_count": len(active_positions),
            "resolved_count": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "trade_count_today": int(self.state.get("trade_count_today", 0) or 0),
            "daily_pnl_cents": int(self.state.get("daily_pnl_cents", 0) or 0),
            "total_exposure_cents": total_exposure_cents,
            "gross_profit_cents": int(sum(h.get("gross_profit_cents", 0) or 0 for h in resolved)),
            "net_profit_cents": int(sum(h.get("net_profit_cents", 0) or 0 for h in resolved)),
            "last_reconciled_at": self.state.get("last_reconciled_at", ""),
            "last_trade_time": self.state.get("last_trade_time"),
        }

    def _check_daily_reset(self):
        today = self._today_et()
        if self.state.get("daily_date") == today:
            return
        self.state["daily_date"] = today
        self.state["trade_count_today"] = 0
        self.state["daily_pnl_cents"] = 0
        self.state["ticker_entry_dates"] = {
            ticker: dt for ticker, dt in self.state.get("ticker_entry_dates", {}).items()
            if dt == today
        }

    def _city_exposure(self, city_code):
        total = 0
        for pos in self.state.get("active", {}).values():
            if pos.get("city_code", "") == city_code:
                total += int(pos.get("cost_cents", 0) or 0)
        return total

    def _city_position_count(self, city_code):
        return sum(1 for pos in self.state.get("active", {}).values() if pos.get("city_code", "") == city_code)

    def _region_room(self, city_code, reserve_balance):
        region = self._get_region(city_code)
        if not region:
            return reserve_balance
        limit = int(reserve_balance * config.MAX_PER_REGION_PCT)
        used = 0
        for pos in self.state.get("active", {}).values():
            if self._get_region(pos.get("city_code", "")) == region:
                used += int(pos.get("cost_cents", 0) or 0)
        return limit - used

    @staticmethod
    def _estimate_entry_fee_cents(price_cents, contracts):
        fee_per_contract = config.KALSHI_FEE_PCT * min(price_cents, 100 - price_cents)
        return int(round(fee_per_contract * contracts))

    @staticmethod
    def _gross_profit_cents(side, result, price_cents, contracts):
        if side == result:
            return (100 - price_cents) * contracts
        return -price_cents * contracts

    @staticmethod
    def _get_region(city_code):
        for region, cities in config.CITY_REGIONS.items():
            if city_code in cities:
                return region
        return ""

    @staticmethod
    def _today_et():
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

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

    def _load_state(self):
        try:
            if os.path.exists(config.PAPER_TRADES_FILE):
                with open(config.PAPER_TRADES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            config.atomic_json_save(config.PAPER_TRADES_FILE, self.state)
        except Exception:
            pass
