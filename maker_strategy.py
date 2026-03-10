"""
MAKER STRATEGY v4.0
=====================
Limit order pricing + resting order management.
Places orders at fair_value - spread_buffer.
Dynamic: high edge -> 1c buffer, low edge -> 3c buffer.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
import config


class MakerStrategy:

    def __init__(self, kalshi_client=None, risk_manager=None):
        self.client = kalshi_client
        self.risk = risk_manager
        self.open_orders = self._load_open_orders()

    def calculate_limit_price(self, signal):
        """
        Calculate limit order price for a signal.
        Returns price in cents, or None if no valid price.
        """
        side = signal.get("side", "yes")
        edge = signal.get("edge", 0)
        our_prob = signal.get("our_prob", 0.5)

        # Fair value in cents
        if side == "yes":
            fair_value = int(our_prob * 100)
        elif side == "no":
            fair_value = int((1 - our_prob) * 100)
        else:
            return signal.get("price_cents", 0)  # arb

        # Dynamic spread buffer
        if edge > 0.15:
            buffer = 1
        elif edge > 0.10:
            buffer = config.MAKER_SPREAD_BUFFER_CENTS
        else:
            buffer = 3

        # Limit price = fair_value - buffer (we want to buy cheaper)
        limit_price = fair_value - buffer
        limit_price = max(config.LONGSHOT_FLOOR_CENTS, limit_price)
        limit_price = min(config.NEAR_CERTAINTY_CAP_CENTS, limit_price)

        if side == "no":
            limit_price = min(limit_price, config.NO_SIDE_MAX_PRICE_CENTS)

        return limit_price

    def place_order(self, signal, limit_price=None):
        """
        Place a limit order via Kalshi API.
        Returns order dict or None on failure.
        """
        if not self.client:
            return None

        if limit_price is None:
            limit_price = self.calculate_limit_price(signal)

        if limit_price is None or limit_price <= 0:
            return None

        ticker = signal.get("ticker", "")
        side = signal.get("side", "yes")
        contracts = signal.get("contracts", 1)

        # Clean up stale orders every cycle (not just when maxed out)
        if self.open_orders:
            self._cleanup_stale_orders()

        # Check open order count
        if len(self.open_orders) >= config.MAX_OPEN_ORDERS:
            print("  [MAKER] Max open orders (%d) reached" % config.MAX_OPEN_ORDERS)
            return None

        if config.DRY_RUN:
            order = {
                "order_id": "dry_%s_%d" % (ticker, int(time.time())),
                "ticker": ticker,
                "side": side,
                "price_cents": limit_price,
                "contracts": contracts,
                "status": "dry_run",
                "placed_at": datetime.now(timezone.utc).isoformat(),
            }
            print("  [MAKER] DRY RUN: %s %s %dc x%d @ %dc" % (
                side.upper(), ticker, limit_price, contracts, limit_price))
            return order

        try:
            if side == "yes":
                order_params = {
                    "ticker": ticker,
                    "action": "buy",
                    "side": "yes",
                    "type": "limit",
                    "yes_price": limit_price,
                    "count": contracts,
                }
            elif side == "no":
                order_params = {
                    "ticker": ticker,
                    "action": "buy",
                    "side": "no",
                    "type": "limit",
                    "no_price": limit_price,
                    "count": contracts,
                }
            else:
                return None

            result = self.client.create_order(**order_params)
            if result and result.get("order"):
                order = result["order"]
                order_id = order.get("order_id", "")
                self.open_orders[order_id] = {
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": side,
                    "price_cents": limit_price,
                    "contracts": contracts,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "order_status": order.get("status", "resting"),
                }
                self._save_open_orders()

                # Track in risk manager
                if self.risk:
                    self.risk.add_pending_order(ticker, {
                        "ticker": ticker,
                        "city_code": signal.get("city_code", ""),
                        "side": side,
                        "price_cents": limit_price,
                        "contracts": contracts,
                        "cost_cents": limit_price * contracts,
                        "order_id": order_id,
                        "order_status": order.get("status", "resting"),
                        "is_confirmed": signal.get("is_confirmed", False),
                        "is_arb": signal.get("is_arb", False),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                print("  [MAKER] Placed: %s %s %dc x%d (order %s)" % (
                    side.upper(), ticker, limit_price, contracts, order_id[:8]))
                return order
        except Exception as e:
            print("  [MAKER] Order failed: %s" % e)

        return None

    def check_fills(self):
        """
        Check status of all open orders. Returns list of filled orders.
        """
        if not self.client:
            return []

        filled = []
        to_remove = []

        for order_id, info in list(self.open_orders.items()):
            try:
                order = self.client.get_order(order_id)
                if not order:
                    continue

                status = order.get("order_status", order.get("status", ""))
                remaining = order.get("remaining_count", info.get("contracts", 1))

                if status == "executed" or remaining == 0:
                    filled.append(info)
                    to_remove.append(order_id)
                    ticker = info.get("ticker", "?")
                    print("  [MAKER] FILLED: %s @ %dc" % (ticker, info.get("price_cents", 0)))
                elif status in ("canceled", "cancelled"):
                    to_remove.append(order_id)
                    if self.risk:
                        self.risk.clear_pending_order(info.get("ticker", ""))

            except Exception:
                continue

        for oid in to_remove:
            if oid in self.open_orders:
                del self.open_orders[oid]

        if to_remove:
            self._save_open_orders()

        return filled

    def cancel_order(self, order_id):
        """Cancel a specific order."""
        if not self.client:
            return False
        try:
            self.client.cancel_order(order_id)
            info = self.open_orders.pop(order_id, {})
            if info and self.risk:
                self.risk.clear_pending_order(info.get("ticker", ""))
            self._save_open_orders()
            return True
        except Exception as e:
            print("  [MAKER] Cancel failed: %s" % e)
            return False

    def cancel_all(self):
        """Cancel all open orders."""
        cancelled = 0
        for order_id in list(self.open_orders.keys()):
            if self.cancel_order(order_id):
                cancelled += 1
        return cancelled

    def _cleanup_stale_orders(self):
        """Cancel orders older than STALE_ORDER_MINUTES."""
        now = datetime.now(timezone.utc)
        stale_limit = timedelta(minutes=config.STALE_ORDER_MINUTES)
        stale = []

        for order_id, info in self.open_orders.items():
            placed = info.get("placed_at", "")
            try:
                placed_dt = datetime.fromisoformat(placed.replace("Z", "+00:00"))
                if now - placed_dt > stale_limit:
                    stale.append(order_id)
            except Exception:
                stale.append(order_id)

        for oid in stale:
            self.cancel_order(oid)
            print("  [MAKER] Cancelled stale order: %s" % oid[:8])

    def get_open_order_count(self):
        return len(self.open_orders)

    def _load_open_orders(self):
        try:
            if os.path.exists(config.MAKER_ORDERS_FILE):
                with open(config.MAKER_ORDERS_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_open_orders(self):
        try:
            config.atomic_json_save(config.MAKER_ORDERS_FILE, self.open_orders)
        except Exception as e:
            print("  [MAKER] Error saving orders: %s" % e)
