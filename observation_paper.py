"""
Observation-mode paper trading ledger.

This simulates entries while the live bot is paused so we can keep learning
from the exact signals the production code would otherwise try to trade.
"""

import copy
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from observation_journal import ObservationJournal
from risk_manager_v2 import RiskManager


class ObservationPaperTrader:
    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.journal = ObservationJournal()
        self.state = self._load_state()
        self._ensure_defaults()

    def _ensure_defaults(self):
        defaults = {
            "active": {},
            "pending_orders": {},
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

    def record_observation_cycle(
        self,
        signals,
        cycle,
        balance_cents,
        market_prices=None,
        live_risk=None,
        max_per_cycle=3,
    ):
        """Simulate a full scan cycle with realistic resting-order behavior."""
        self._check_daily_reset()
        market_prices = market_prices or {}
        now_iso = datetime.now(timezone.utc).isoformat()
        blocked_reasons = Counter()

        filled_pending, expired_pending = self._sync_pending_orders(
            market_prices=market_prices,
            cycle=cycle,
            opened_at=now_iso,
        )
        placed_this_cycle = []
        executed = []
        queued = []

        for signal in signals:
            if len(placed_this_cycle) >= max_per_cycle:
                blocked_reasons["per_cycle_limit"] += 1
                continue

            limit_price = int(signal.get("limit_price", 0) or 0)
            if limit_price <= 0:
                blocked_reasons["invalid_limit_price"] += 1
                continue

            approved, result = self._fit_trade(
                signal,
                balance_cents=balance_cents,
                limit_price=limit_price,
                live_risk=live_risk,
                same_cycle=bool(placed_this_cycle or filled_pending),
            )
            if not approved:
                blocked_reasons[result] += 1
                continue

            contracts = int(result or 0)
            current_price = self._market_price_for_signal(signal, market_prices)
            execution_style = signal.get("execution_style", "maker")
            fills_now = (
                execution_style == "taker"
                or (current_price > 0 and current_price <= limit_price)
            )

            if fills_now:
                fill_price = current_price if current_price > 0 else limit_price
                position = self._build_position(signal, cycle, fill_price, contracts, now_iso)
                self._store_active_position(position)
                self.state.setdefault("ticker_entry_dates", {})[position["ticker"]] = self._today_et()
                self.state["last_trade_time"] = time.time()
                self.state["trade_count_today"] = int(self.state.get("trade_count_today", 0) or 0) + 1
                executed.append(position)
                self.journal.log_paper_event("paper_order_filled", position)
                placed_this_cycle.append(position)
                continue

            order = self._build_pending_order(
                signal=signal,
                cycle=cycle,
                limit_price=limit_price,
                contracts=contracts,
                opened_at=now_iso,
                current_price=current_price,
            )
            self.state.setdefault("pending_orders", {})[order["order_id"]] = order
            self.state.setdefault("ticker_entry_dates", {})[order["ticker"]] = self._today_et()
            self.journal.log_paper_event("paper_order_queued", order)
            queued.append(order)
            placed_this_cycle.append(order)

        self.state.setdefault("cycle_log", []).append({
            "cycle": cycle,
            "timestamp": now_iso,
            "signals_seen": len(signals),
            "paper_entries": len(executed),
            "filled_pending": len(filled_pending),
            "resting_orders": len(queued),
            "expired_pending": len(expired_pending),
            "blocked_reasons": dict(blocked_reasons),
            "top_signals": [
                {
                    "ticker": s.get("ticker", ""),
                    "side": s.get("side", ""),
                    "edge": round(s.get("edge", 0) or 0, 4),
                    "strategy": s.get("strategy", ""),
                    "execution_style": s.get("execution_style", ""),
                    "limit_price_cents": int(s.get("limit_price", 0) or 0),
                }
                for s in signals[:5]
            ],
        })
        self.state["cycle_log"] = self.state["cycle_log"][-50:]
        self._refresh_summary()
        self._save_state()
        return {
            "executed": executed,
            "queued": queued,
            "filled_pending": filled_pending,
            "expired_pending": expired_pending,
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
                self.journal.log_paper_event("paper_trade_resolved", resolved)
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
                    self.journal.log_paper_event("paper_trade_expired_unknown", expired)
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
            "pending": [
                {
                    "ticker": row.get("ticker", ""),
                    "side": row.get("side", ""),
                    "contracts": row.get("contracts", 0),
                    "limit_price_cents": row.get("limit_price_cents", 0),
                    "current_price_cents": row.get("current_price_cents", 0),
                    "strategy": row.get("strategy", ""),
                    "placed_at": row.get("placed_at", ""),
                }
                for row in list(self.state.get("pending_orders", {}).values())[:10]
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

    def _fit_trade(self, signal, balance_cents, limit_price, live_risk=None, same_cycle=False):
        ticker = signal.get("ticker", "")
        city = signal.get("city_code", "")
        contracts = int(signal.get("suggested_contracts", signal.get("contracts", 1)) or 0)
        if not ticker or not city or contracts <= 0 or limit_price <= 0:
            return False, "invalid_signal"

        if ticker in self.state.get("active", {}):
            return False, "paper_position_exists"
        if self._has_pending_ticker(ticker):
            return False, "paper_pending_exists"

        shadow = self._build_shadow_risk(balance_cents, live_risk)
        risk_price_cents = int(signal.get("risk_price_cents", limit_price) or limit_price)
        risk_signal = {
            "ticker": ticker,
            "city_code": city,
            "side": signal.get("side", ""),
            "price_cents": risk_price_cents,
            "contracts": contracts,
            "cost_cents": risk_price_cents * contracts,
            "edge": signal.get("edge", 0),
            "is_confirmed": signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME",
            "is_arb": signal.get("strategy", "") == "S2-Arbitrage",
            "same_cycle": same_cycle,
        }
        approved, reason = shadow.check_trade(risk_signal)
        if not approved:
            return False, self._normalize_block_reason(reason)
        return True, int(risk_signal.get("contracts", contracts) or contracts)

    def _build_position(self, signal, cycle, entry_price_cents, contracts, opened_at):
        ticker = signal.get("ticker", "")
        estimated_fee = self._estimate_entry_fee_cents(entry_price_cents, contracts)
        return {
            "ticker": ticker,
            "side": signal.get("side", ""),
            "contracts": contracts,
            "entry_price_cents": entry_price_cents,
            "cost_cents": entry_price_cents * contracts,
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

    def _build_pending_order(self, signal, cycle, limit_price, contracts, opened_at, current_price):
        ticker = signal.get("ticker", "")
        return {
            "order_id": "paper_%s_%d_%d" % (ticker, cycle, int(time.time() * 1000)),
            "ticker": ticker,
            "side": signal.get("side", ""),
            "contracts": contracts,
            "limit_price_cents": limit_price,
            "current_price_cents": int(current_price or 0),
            "reserved_cost_cents": limit_price * contracts,
            "city_code": signal.get("city_code", ""),
            "target_date": signal.get("target_date", ""),
            "strategy": signal.get("strategy", ""),
            "confirmation_verdict": signal.get("confirmation_verdict", ""),
            "edge": round(signal.get("edge", 0) or 0, 4),
            "fee_adjusted_edge": round(signal.get("fee_adjusted_edge", 0) or 0, 4),
            "our_prob": round(signal.get("our_prob", 0) or 0, 4),
            "market_prob": round(signal.get("market_prob", 0) or 0, 4),
            "reasoning": signal.get("reasoning", ""),
            "placed_at": opened_at,
            "cycle": cycle,
            "execution_style": signal.get("execution_style", "maker"),
            "status": "resting",
        }

    def _sync_pending_orders(self, market_prices, cycle, opened_at):
        pending_orders = self.state.get("pending_orders", {})
        filled = []
        expired = []
        history = self.state.get("history", [])
        now = datetime.now(timezone.utc)

        for order_id, order in list(pending_orders.items()):
            if self._pending_expired(order, now):
                expired_row = dict(order)
                expired_row.update({
                    "status": "expired_unfilled",
                    "resolved_at": now.isoformat(),
                    "net_profit_cents": 0,
                })
                history.append(expired_row)
                self.journal.log_paper_event("paper_order_expired_unfilled", expired_row)
                expired.append(expired_row)
                del pending_orders[order_id]
                continue

            current_price = self._market_price(order.get("ticker", ""), order.get("side", ""), market_prices)
            if current_price <= 0:
                continue
            order["current_price_cents"] = current_price
            limit_price = int(order.get("limit_price_cents", 0) or 0)
            if current_price > limit_price:
                continue

            fill_signal = {
                "ticker": order.get("ticker", ""),
                "side": order.get("side", ""),
                "city_code": order.get("city_code", ""),
                "target_date": order.get("target_date", ""),
                "strategy": order.get("strategy", ""),
                "confirmation_verdict": order.get("confirmation_verdict", ""),
                "edge": order.get("edge", 0),
                "fee_adjusted_edge": order.get("fee_adjusted_edge", 0),
                "our_prob": order.get("our_prob", 0),
                "market_prob": order.get("market_prob", 0),
                "reasoning": order.get("reasoning", ""),
            }
            position = self._build_position(
                signal=fill_signal,
                cycle=order.get("cycle", cycle),
                entry_price_cents=current_price,
                contracts=int(order.get("contracts", 0) or 0),
                opened_at=opened_at,
            )
            self._store_active_position(position)
            self.state["last_trade_time"] = time.time()
            self.state["trade_count_today"] = int(self.state.get("trade_count_today", 0) or 0) + 1
            filled.append(position)
            self.journal.log_paper_event("paper_order_filled", position)
            del pending_orders[order_id]

        if history:
            self.state["history"] = history[-500:]
        return filled, expired

    def _pending_expired(self, order, now):
        placed_at = order.get("placed_at", "")
        if placed_at:
            try:
                placed_dt = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
                timeout = int(getattr(config, "RESTING_ORDER_TIMEOUT", 1500) or 1500)
                if now - placed_dt >= timedelta(seconds=timeout):
                    return True
            except ValueError:
                pass

        target_date = order.get("target_date", "")
        today_et = self._today_et()
        return bool(target_date and target_date < today_et)

    def _build_shadow_risk(self, balance_cents, live_risk=None):
        shadow = RiskManager.__new__(RiskManager)
        shadow.client = None
        shadow._cached_balance = int(balance_cents or 0)
        shadow._balance_cache_time = time.time()
        shadow._pnl_cache = copy.deepcopy(getattr(live_risk, "_pnl_cache", None))
        shadow._pnl_cache_time = getattr(live_risk, "_pnl_cache_time", 0)
        live_state = copy.deepcopy(getattr(live_risk, "state", {}) or {})
        shadow.state = self._merge_shadow_state(live_state)
        shadow._save_state = lambda: None
        shadow._refresh_exposure()
        return shadow

    def _merge_shadow_state(self, live_state):
        state = dict(live_state) if isinstance(live_state, dict) else {}
        defaults = {
            "positions": {},
            "pending_orders": {},
            "daily_pnl_cents": 0,
            "daily_date": self._today_et(),
            "consecutive_losses": 0,
            "kill_switch_until": None,
            "last_trade_time": None,
            "trade_count_today": 0,
            "total_exposure_cents": 0,
            "win_amounts": [],
            "loss_amounts": [],
            "observation_mode": False,
            "observation_reason": "",
            "ticker_entry_dates": {},
        }
        for key, value in defaults.items():
            if key not in state:
                state[key] = copy.deepcopy(value)

        state["positions"] = {
            ticker: {
                "ticker": row.get("ticker", ""),
                "city_code": row.get("city_code", ""),
                "side": row.get("side", ""),
                "price_cents": int(row.get("entry_price_cents", row.get("price_cents", 0)) or 0),
                "contracts": int(row.get("contracts", 0) or 0),
                "cost_cents": int(row.get("cost_cents", 0) or 0),
                "order_status": "executed",
                "is_confirmed": row.get("confirmation_verdict") == "CONFIRMED_OUTCOME",
                "is_arb": row.get("strategy", "") == "S2-Arbitrage",
            }
            for ticker, row in self.state.get("active", {}).items()
        }
        state["pending_orders"] = {
            row.get("order_id", ""): {
                "order_id": row.get("order_id", ""),
                "ticker": row.get("ticker", ""),
                "city_code": row.get("city_code", ""),
                "side": row.get("side", ""),
                "price_cents": int(row.get("limit_price_cents", 0) or 0),
                "requested_contracts": int(row.get("contracts", 0) or 0),
                "remaining_contracts": int(row.get("contracts", 0) or 0),
                "filled_contracts": 0,
                "remaining_cost_cents": int(row.get("reserved_cost_cents", 0) or 0),
                "placed_at": row.get("placed_at", ""),
                "order_status": "resting",
                "action": "buy",
                "is_confirmed": row.get("confirmation_verdict") == "CONFIRMED_OUTCOME",
                "is_arb": row.get("strategy", "") == "S2-Arbitrage",
            }
            for row in self.state.get("pending_orders", {}).values()
            if row.get("order_id")
        }

        merged_entry_dates = dict(state.get("ticker_entry_dates", {}) or {})
        merged_entry_dates.update(self.state.get("ticker_entry_dates", {}) or {})
        state["ticker_entry_dates"] = merged_entry_dates

        live_last_trade = state.get("last_trade_time")
        paper_last_trade = self.state.get("last_trade_time")
        if live_last_trade is None:
            state["last_trade_time"] = paper_last_trade
        elif paper_last_trade is not None:
            state["last_trade_time"] = max(float(live_last_trade), float(paper_last_trade))

        state["daily_pnl_cents"] = int(state.get("daily_pnl_cents", 0) or 0) + int(self.state.get("daily_pnl_cents", 0) or 0)
        state["trade_count_today"] = int(state.get("trade_count_today", 0) or 0) + int(self.state.get("trade_count_today", 0) or 0)

        paper_wins = [int(h.get("net_profit_cents", 0) or 0) for h in self.state.get("history", []) if int(h.get("net_profit_cents", 0) or 0) > 0]
        paper_losses = [abs(int(h.get("net_profit_cents", 0) or 0)) for h in self.state.get("history", []) if int(h.get("net_profit_cents", 0) or 0) < 0]
        state["win_amounts"] = (list(state.get("win_amounts", []) or []) + paper_wins)[-50:]
        state["loss_amounts"] = (list(state.get("loss_amounts", []) or []) + paper_losses)[-50:]
        state["consecutive_losses"] = max(
            int(state.get("consecutive_losses", 0) or 0),
            self._paper_consecutive_losses(),
        )
        state["observation_mode"] = False
        state["observation_reason"] = ""
        return state

    def _paper_consecutive_losses(self):
        count = 0
        for row in reversed(self.state.get("history", [])):
            if row.get("status") == "loss":
                count += 1
                continue
            if row.get("status") == "win":
                break
        return count

    def _store_active_position(self, position):
        ticker = position.get("ticker", "")
        if not ticker:
            return
        existing = self.state.setdefault("active", {}).get(ticker)
        if not existing or existing.get("side") != position.get("side"):
            self.state["active"][ticker] = position
            return

        existing_contracts = int(existing.get("contracts", 0) or 0)
        new_contracts = int(position.get("contracts", 0) or 0)
        total_contracts = existing_contracts + new_contracts
        total_cost = int(existing.get("cost_cents", 0) or 0) + int(position.get("cost_cents", 0) or 0)
        avg_price = int(round(float(total_cost) / float(total_contracts))) if total_contracts else 0
        merged = dict(existing)
        merged.update(position)
        merged["contracts"] = total_contracts
        merged["cost_cents"] = total_cost
        merged["entry_price_cents"] = avg_price
        merged["estimated_entry_fee_cents"] = (
            int(existing.get("estimated_entry_fee_cents", 0) or 0)
            + int(position.get("estimated_entry_fee_cents", 0) or 0)
        )
        self.state["active"][ticker] = merged

    def _has_pending_ticker(self, ticker):
        return any(order.get("ticker", "") == ticker for order in self.state.get("pending_orders", {}).values())

    def _refresh_summary(self):
        active_positions = list(self.state.get("active", {}).values())
        pending_orders = list(self.state.get("pending_orders", {}).values())
        resolved = [h for h in self.state.get("history", []) if h.get("status") in ("win", "loss")]
        wins = [h for h in resolved if h.get("status") == "win"]
        losses = [h for h in resolved if h.get("status") == "loss"]
        active_exposure_cents = int(sum(int(p.get("cost_cents", 0) or 0) for p in active_positions))
        pending_exposure_cents = int(sum(int(p.get("reserved_cost_cents", 0) or 0) for p in pending_orders))
        total_exposure_cents = active_exposure_cents + pending_exposure_cents
        self.state["total_exposure_cents"] = total_exposure_cents
        self.state["summary"] = {
            "active_count": len(active_positions),
            "pending_count": len(pending_orders),
            "resolved_count": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "trade_count_today": int(self.state.get("trade_count_today", 0) or 0),
            "daily_pnl_cents": int(self.state.get("daily_pnl_cents", 0) or 0),
            "active_exposure_cents": active_exposure_cents,
            "pending_exposure_cents": pending_exposure_cents,
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
    def _today_et():
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    @staticmethod
    def _normalize_block_reason(reason):
        if not reason:
            return "paper_blocked"
        text = str(reason).strip().lower().replace(" ", "_")
        text = text.replace(":", "").replace("%", "pct").replace("/", "_")
        return text[:80]

    def _market_price_for_signal(self, signal, market_prices):
        ticker = signal.get("ticker", "")
        side = signal.get("side", "")
        price = self._market_price(ticker, side, market_prices)
        if price > 0:
            return price
        return int(signal.get("current_price_cents", 0) or signal.get("price_cents", 0) or 0)

    @staticmethod
    def _market_price(ticker, side, market_prices):
        market = market_prices.get(ticker, {}) or {}
        return int(market.get(side, 0) or 0)

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
