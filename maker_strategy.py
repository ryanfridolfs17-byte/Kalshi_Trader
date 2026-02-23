"""
MAKER EXECUTION STRATEGY v4.0
====================================
Converts trade signals into maker (limit) orders posted below fair value.

Instead of: "Buy YES at market" (taker, pays fees, crosses spread)
Does:       "Post YES limit at fair_value - spread_buffer and wait" (maker)

Becker dataset shows makers earn +0.77% to +1.25% excess returns on Kalshi
across 80 of 99 price levels. Takers lose at 80 of 99 price levels.

Order lifecycle: place → monitor → cancel/replace if stale → fill tracking

This module manages:
  1. Limit order price calculation (fair value - buffer)
  2. Open order tracking and stale order cancellation
  3. Fill detection and adverse selection monitoring
  4. Order replacement when forecasts update
"""

import json
import os
from datetime import datetime, timezone, timedelta
import config


class MakerStrategy:
    """
    Converts trade signals into limit orders and manages their lifecycle.
    Posts orders below fair value (maker side) and waits for fills.
    """

    def __init__(self, kalshi_client, risk_manager=None):
        self.client = kalshi_client
        self.risk = risk_manager
        self.open_orders = []  # Tracked maker orders
        self._load_open_orders()

        # Config
        self.spread_buffer_cents = getattr(config, "MAKER_SPREAD_BUFFER_CENTS", 2)
        self.stale_order_minutes = getattr(config, "STALE_ORDER_MINUTES", 30)
        self.max_open_orders = getattr(config, "MAX_OPEN_ORDERS", 10)
        self.adverse_selection_pause_minutes = getattr(config, "ADVERSE_SELECTION_PAUSE_MINUTES", 60)

    # ═══════════════════════════════════════════════════════
    # ORDER PLACEMENT
    # ═══════════════════════════════════════════════════════

    def calculate_maker_price(self, signal):
        """
        Calculate the optimal limit order price for a maker order.

        For buying YES:  limit_price = fair_value - spread_buffer
                         (post below fair value — filled when someone sells into us)
        For buying NO:   limit_price = (100 - fair_value) - spread_buffer
                         (same logic on NO side)

        Never posts at or above fair value (that's taker behavior).
        Minimum edge after spread buffer must still exceed fee + minimum threshold.

        Returns:
            {
                "limit_price_cents": int,
                "original_price_cents": int,
                "buffer_applied": int,
                "effective_edge": float,
                "viable": bool,
                "reason": str,
            }
        """
        side = signal.get("side", "yes")
        edge = signal.get("edge", 0)
        price_cents = signal.get("price_cents", 0)

        if price_cents <= 0:
            return {"viable": False, "reason": "No price available",
                    "limit_price_cents": 0, "original_price_cents": 0,
                    "buffer_applied": 0, "effective_edge": 0}

        # Calculate maker price — step back from the aggressive price
        # For YES: we're buying, so lower our bid
        # For NO: we're buying NO, so lower our NO bid
        buffer = self.spread_buffer_cents

        # Dynamic buffer: wider for low-edge trades (protect margin),
        # tighter for high-edge trades (maximize fill probability)
        if edge > 0.15:
            buffer = max(1, buffer - 1)  # Tighter buffer for high edge
        elif edge < 0.08:
            buffer = buffer + 1  # Wider buffer for marginal edge

        limit_price = price_cents - buffer

        # Guard: price must stay within valid range (1-99)
        limit_price = max(1, min(99, limit_price))

        # Calculate effective edge after buffer
        # Buffer improves our entry by buffer cents, so edge increases
        effective_edge = edge + (buffer / 100.0)

        # Verify edge still exceeds minimum after fees (expected fee, matching strategy.py)
        if config.FEE_ADJUSTMENT_ENABLED:
            win_prob = min(0.95, (limit_price / 100.0) + effective_edge)
            fee_drag = config.KALSHI_FEE_PCT * (100 - limit_price) / 100.0 * win_prob
            net_edge = effective_edge - fee_drag
        else:
            net_edge = effective_edge

        viable = (limit_price >= 1 and limit_price <= 99
                  and net_edge >= config.FEE_ADJUSTED_MIN_EDGE)

        return {
            "limit_price_cents": limit_price,
            "original_price_cents": price_cents,
            "buffer_applied": buffer,
            "effective_edge": round(effective_edge, 4),
            "net_edge_after_fees": round(net_edge, 4) if config.FEE_ADJUSTMENT_ENABLED else round(effective_edge, 4),
            "viable": viable,
            "reason": "OK" if viable else f"Net edge {net_edge:.1%} below threshold after buffer",
        }

    def place_maker_order(self, signal, dry_run=False):
        """
        Convert a trade signal into a maker (limit) order.

        Args:
            signal: Output from strategy.py / scorecard evaluation
            dry_run: If True, calculate but don't place the order

        Returns:
            {
                "order_id": str or None,
                "ticker": str,
                "side": str,
                "limit_price_cents": int,
                "contracts": int,
                "placed_at": str (ISO),
                "status": "open" | "dry_run" | "failed",
                "maker_pricing": dict,
            }
        """
        ticker = signal.get("ticker", "")
        side = signal.get("side", "yes")
        contracts = signal.get("suggested_contracts", 0)

        if not ticker or contracts <= 0:
            return {"status": "failed", "reason": "Missing ticker or zero contracts"}

        # Check open order limit
        active_orders = [o for o in self.open_orders if o.get("status") == "open"]
        if len(active_orders) >= self.max_open_orders:
            return {"status": "failed",
                    "reason": f"Max {self.max_open_orders} open maker orders reached"}

        # Calculate maker price
        pricing = self.calculate_maker_price(signal)
        if not pricing["viable"]:
            # Fall back to original price (taker-like) if maker price not viable
            print(f"    [MAKER] Buffer not viable ({pricing['reason']}), using original price")
            pricing["limit_price_cents"] = signal.get("price_cents", 0)
            pricing["buffer_applied"] = 0

        limit_price = pricing["limit_price_cents"]

        result = {
            "ticker": ticker,
            "side": side,
            "limit_price_cents": limit_price,
            "contracts": contracts,
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "status": "dry_run" if dry_run else "pending",
            "maker_pricing": pricing,
            "signal_edge": signal.get("edge", 0),
            "signal_strategy": signal.get("strategy", ""),
            "city_code": signal.get("city_code", ""),
        }

        if dry_run:
            print(f"    [MAKER] DRY RUN: Would post {side.upper()} limit @ {limit_price}¢ "
                  f"x{contracts} on {ticker} (buffer: {pricing['buffer_applied']}¢)")
            return result

        # Place the actual limit order
        try:
            if side == "yes":
                order_result = self.client.create_order(
                    ticker=ticker, side="yes", count=contracts,
                    type="limit", yes_price=limit_price
                )
            else:
                order_result = self.client.create_order(
                    ticker=ticker, side="no", count=contracts,
                    type="limit", no_price=limit_price
                )

            if order_result:
                order = order_result.get("order", order_result)
                order_id = order.get("order_id", "")
                status = order.get("status", "resting")

                result["order_id"] = order_id
                result["status"] = "filled" if status == "executed" else "open"
                result["remaining"] = order.get("remaining_count", contracts)

                # Track if not immediately filled
                if result["status"] == "open":
                    self.open_orders.append(result)
                    self._save_open_orders()

                buffer_str = f" (buffer: {pricing['buffer_applied']}¢)" if pricing["buffer_applied"] > 0 else ""
                print(f"    [MAKER] Posted {side.upper()} limit @ {limit_price}¢ "
                      f"x{contracts} on {ticker}{buffer_str} → {result['status']}")
            else:
                result["status"] = "failed"
                result["reason"] = "API returned None"
                print(f"    [MAKER] FAILED to place order on {ticker}")

        except Exception as e:
            result["status"] = "failed"
            result["reason"] = str(e)
            print(f"    [MAKER] ERROR placing order on {ticker}: {e}")

        return result

    # ═══════════════════════════════════════════════════════
    # ORDER MANAGEMENT
    # ═══════════════════════════════════════════════════════

    def manage_open_orders(self):
        """
        Check all tracked open orders. For each:
        1. If filled → update status, log
        2. If stale (open > stale_order_minutes) → cancel
        3. If cancelled externally → clean up tracking

        Returns list of status updates.
        """
        if not self.open_orders:
            return []

        updates = []
        now = datetime.now(timezone.utc)
        still_open = []

        # Get all resting orders from Kalshi
        resting_order_ids = set()
        try:
            resting_resp = self.client.get_orders(status="resting")
            if resting_resp:
                orders = resting_resp.get("orders", [])
                for o in orders:
                    resting_order_ids.add(o.get("order_id", ""))
        except Exception as e:
            print(f"  [MAKER] Error fetching resting orders: {e}")
            # Keep all orders as-is if we can't check
            return []

        for order in self.open_orders:
            if order.get("status") != "open":
                continue

            order_id = order.get("order_id", "")
            ticker = order.get("ticker", "")
            placed_at_str = order.get("placed_at", "")

            # Parse placement time
            try:
                placed_at = datetime.fromisoformat(placed_at_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                placed_at = now  # Fallback

            age_minutes = (now - placed_at).total_seconds() / 60

            # Check if still resting on Kalshi
            if order_id and order_id not in resting_order_ids:
                # Order is no longer resting — either filled or cancelled
                order["status"] = "filled_or_cancelled"
                updates.append({
                    "ticker": ticker, "order_id": order_id,
                    "action": "resolved", "age_minutes": round(age_minutes, 1),
                })
                print(f"  [MAKER] Order {order_id[:8]}... on {ticker} resolved (filled or cancelled)")
                continue

            # Check if stale
            if age_minutes > self.stale_order_minutes:
                # Cancel stale order
                try:
                    self.client.cancel_order(order_id)
                    order["status"] = "cancelled_stale"
                    updates.append({
                        "ticker": ticker, "order_id": order_id,
                        "action": "cancelled_stale", "age_minutes": round(age_minutes, 1),
                    })
                    print(f"  [MAKER] Cancelled stale order {order_id[:8]}... on {ticker} "
                          f"({age_minutes:.0f}min old)")
                except Exception as e:
                    print(f"  [MAKER] Error cancelling {order_id[:8]}...: {e}")
                    still_open.append(order)
                continue

            # Order still active and not stale
            still_open.append(order)

        self.open_orders = still_open
        self._save_open_orders()

        if updates:
            print(f"  [MAKER] Processed {len(updates)} order updates, "
                  f"{len(still_open)} still open")

        return updates

    def check_adverse_selection(self, recent_fills=None):
        """
        After fills, check for signs of adverse selection (informed taker flow).

        Red flags:
          - Fill followed by immediate price movement against us
          - Multiple fills in same direction within short window

        Returns:
            {
                "level": "normal" | "caution" | "pause",
                "reason": str,
                "recommendation": str,
            }
        """
        if not recent_fills:
            return {"level": "normal", "reason": "No recent fills to analyze",
                    "recommendation": "Continue normally"}

        # Check for rapid same-direction fills (potential informed flow)
        if len(recent_fills) >= 3:
            # If 3+ fills in same ticker within 10 minutes, flag caution
            fill_times = []
            for fill in recent_fills:
                try:
                    t = datetime.fromisoformat(fill.get("created_time", "").replace("Z", "+00:00"))
                    fill_times.append(t)
                except (ValueError, TypeError):
                    pass

            if len(fill_times) >= 3:
                fill_times.sort()
                window = (fill_times[-1] - fill_times[0]).total_seconds() / 60
                if window < 10:
                    return {
                        "level": "caution",
                        "reason": f"{len(fill_times)} fills in {window:.0f}min window",
                        "recommendation": "Reduce new order sizes by 50% for this market",
                    }

        return {"level": "normal", "reason": "Fill pattern appears normal",
                "recommendation": "Continue normally"}

    def get_open_order_count(self):
        """Return count of currently tracked open orders."""
        return len([o for o in self.open_orders if o.get("status") == "open"])

    def get_open_orders_summary(self):
        """Return summary of open maker orders for dashboard/logging."""
        active = [o for o in self.open_orders if o.get("status") == "open"]
        if not active:
            return "No open maker orders"

        lines = [f"  [MAKER] {len(active)} open orders:"]
        for o in active:
            age = ""
            try:
                placed = datetime.fromisoformat(o["placed_at"].replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - placed).total_seconds() / 60
                age = f"{age_min:.0f}min"
            except Exception:
                pass
            lines.append(
                f"    {o['side'].upper()} {o['ticker']} @ {o['limit_price_cents']}¢ "
                f"x{o['contracts']} ({age})"
            )
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════

    def _load_open_orders(self):
        """Load tracked open orders from state file."""
        filepath = os.path.join(config.STATE_DIR, "maker_orders.json")
        try:
            if os.path.exists(filepath):
                with open(filepath) as f:
                    self.open_orders = json.load(f)
            else:
                self.open_orders = []
        except Exception:
            self.open_orders = []

    def _save_open_orders(self):
        """Save tracked open orders to state file."""
        filepath = os.path.join(config.STATE_DIR, "maker_orders.json")
        try:
            config.atomic_json_save(filepath, self.open_orders)
        except Exception as e:
            print(f"  [MAKER] Error saving orders: {e}")
