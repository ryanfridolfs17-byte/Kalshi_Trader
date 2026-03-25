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
from zoneinfo import ZoneInfo
import config


class MakerStrategy:

    def __init__(self, kalshi_client=None, risk_manager=None):
        self.client = kalshi_client
        self.risk = risk_manager
        self.open_orders = self._load_open_orders()
        self._fill_tracking = self._load_fill_tracking()

    @staticmethod
    def _normalize_fill(fill):
        """Normalize Kalshi fill payload to int cents/counts."""
        if not isinstance(fill, dict):
            return fill

        result = dict(fill)

        def _to_int(val):
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                try:
                    return int(float(val))
                except (TypeError, ValueError):
                    return 0
            return 0

        def _dollars_to_cents(val):
            if val is None:
                return 0
            try:
                return int(round(float(val) * 100))
            except (TypeError, ValueError):
                return 0

        result["count"] = _to_int(result.get("count_fp", result.get("count", 0)))
        if "yes_price_dollars" in result or "no_price_dollars" in result:
            result["yes_price"] = _dollars_to_cents(result.get("yes_price_dollars"))
            result["no_price"] = _dollars_to_cents(result.get("no_price_dollars"))
        else:
            result["yes_price"] = _to_int(result.get("yes_price"))
            result["no_price"] = _to_int(result.get("no_price"))
        return result

    def _get_recent_fills_by_order(self, max_pages=3, page_limit=200):
        """Fetch recent fills and group them by order_id."""
        grouped = {}
        if not self.client:
            return grouped

        cursor = None
        for _ in range(max_pages):
            result = self.client.get_fills(limit=page_limit, cursor=cursor)
            if not result:
                break
            fills = result.get("fills", [])
            if not fills:
                break
            for raw_fill in fills:
                fill = self._normalize_fill(raw_fill)
                order_id = fill.get("order_id", "")
                if not order_id:
                    continue
                grouped.setdefault(order_id, []).append(fill)
            cursor = result.get("cursor")
            if not cursor:
                break

        for order_id in grouped:
            grouped[order_id].sort(key=lambda f: f.get("created_time", ""))
        return grouped

    @staticmethod
    def _extract_incremental_fill(fills, already_accounted, side):
        """Return (new_contracts, avg_price_cents) for unseen fills."""
        seen = 0
        new_contracts = 0
        weighted_price = 0
        price_field = "yes_price" if side == "yes" else "no_price"

        for fill in fills:
            count = int(fill.get("count", 0) or 0)
            if count <= 0:
                continue
            fill_start = seen
            fill_end = seen + count
            seen = fill_end
            if fill_end <= already_accounted:
                continue
            unseen = count if already_accounted <= fill_start else (fill_end - already_accounted)
            price_cents = int(fill.get(price_field, 0) or 0)
            if unseen > 0 and price_cents > 0:
                new_contracts += unseen
                weighted_price += unseen * price_cents

        if new_contracts <= 0:
            return 0, 0
        return new_contracts, int(round(float(weighted_price) / float(new_contracts)))

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

        # Dynamic spread buffer based on edge
        if edge > 0.15:
            buffer = 1
        elif edge > 0.10:
            buffer = config.MAKER_SPREAD_BUFFER_CENTS
        else:
            buffer = 3

        # Widen buffer when models disagree (high model_spread = less confident fair value)
        model_spread = signal.get("model_spread")
        if model_spread is not None and model_spread > 0:
            buffer += max(0, int(model_spread / 4))  # +1c per 4F of disagreement

        # Adverse selection adjustment
        spread_adj = self.get_spread_adjustment(side)
        if spread_adj is None:
            print("  [MAKER] Adverse selection: PAUSED on %s side" % side)
            return None
        buffer += spread_adj

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
                    "limit_price_cents": limit_price,
                    "requested_contracts": contracts,
                    "remaining_contracts": contracts,
                    "filled_contracts": 0,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "order_status": order.get("status", "resting"),
                    "action": "buy",
                    "city_code": signal.get("city_code", ""),
                    "is_confirmed": signal.get("is_confirmed", False),
                    "is_arb": signal.get("is_arb", False),
                    "edge": signal.get("edge", 0),
                    "our_prob": signal.get("our_prob", 0),
                    "strategy": signal.get("strategy", ""),
                    "confirmation_verdict": signal.get("confirmation_verdict", ""),
                    "predicted_high": signal.get("predicted_high"),
                    "model_means": signal.get("model_means", {}),
                    "model_spread": signal.get("model_spread"),
                    "target_date": signal.get("target_date", ""),
                    "temp_low": signal.get("temp_low"),
                    "temp_high": signal.get("temp_high"),
                    "bet_description": signal.get("bet_description", ""),
                }
                self._save_open_orders()

                # Track in risk manager
                if self.risk:
                    self.risk.add_pending_order({
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
                self._track_order_side(side)
                return order
        except Exception as e:
            print("  [MAKER] Order failed: %s" % e)

        return None


    def place_market_order(self, signal):
        """Place a MARKET order for confirmed outcomes (taker mode)."""
        if not self.client:
            return None
        ticker = signal.get("ticker", "")
        side = signal.get("side", "yes")
        contracts = signal.get("contracts", 1)

        # NO ceiling cap — same guard as limit order path
        if side == "no":
            price_cents = signal.get("price_cents", 0)
            if price_cents > config.NO_SIDE_MAX_PRICE_CENTS:
                print("  [MAKER] TAKER BLOCKED: NO price %dc exceeds %dc ceiling for %s" % (
                    price_cents, config.NO_SIDE_MAX_PRICE_CENTS, ticker))
                return None

        if config.DRY_RUN:
            order = {
                "order_id": "dry_taker_%s_%d" % (ticker, int(time.time())),
                "ticker": ticker, "side": side,
                "price_cents": signal.get("price_cents", 0),
                "contracts": contracts, "status": "dry_run",
                "placed_at": datetime.now(timezone.utc).isoformat(),
            }
            print("  [MAKER] TAKER MODE (DRY): %s %s x%d" % (side.upper(), ticker, contracts))
            return order
        try:
            result = self.client.create_order(
                ticker=ticker, action="buy", side=side, type="market", count=contracts)
            if result and result.get("order"):
                order = result["order"]
                order_id = order.get("order_id", "")
                price_used = signal.get("price_cents", 0)
                print("  [MAKER] TAKER MODE: %s %s x%d (order %s)" % (
                    side.upper(), ticker, contracts, order_id[:8]))

                # Track in open_orders so check_fills() can detect the fill
                self.open_orders[order_id] = {
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": side,
                    "price_cents": price_used,
                    "limit_price_cents": price_used,
                    "requested_contracts": contracts,
                    "remaining_contracts": contracts,
                    "filled_contracts": 0,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "order_status": order.get("status", "resting"),
                    "action": "buy",
                    "city_code": signal.get("city_code", ""),
                    "is_confirmed": signal.get("is_confirmed", False),
                    "is_arb": signal.get("is_arb", False),
                    "edge": signal.get("edge", 0),
                    "our_prob": signal.get("our_prob", 0),
                    "strategy": signal.get("strategy", ""),
                    "confirmation_verdict": signal.get("confirmation_verdict", ""),
                    "predicted_high": signal.get("predicted_high"),
                    "model_means": signal.get("model_means", {}),
                    "model_spread": signal.get("model_spread"),
                    "target_date": signal.get("target_date", ""),
                    "temp_low": signal.get("temp_low"),
                    "temp_high": signal.get("temp_high"),
                    "bet_description": signal.get("bet_description", ""),
                }
                self._save_open_orders()

                if self.risk:
                    self.risk.add_pending_order({
                        "ticker": ticker, "city_code": signal.get("city_code", ""),
                        "side": side, "price_cents": price_used,
                        "contracts": contracts,
                        "cost_cents": price_used * contracts,
                        "order_id": order_id,
                        "order_status": order.get("status", "resting"),
                        "is_confirmed": signal.get("is_confirmed", False),
                        "is_arb": signal.get("is_arb", False),
                        "our_prob": signal.get("our_prob", 0),
                        "predicted_high": signal.get("predicted_high"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                return order
        except Exception as e:
            print("  [MAKER] Taker order failed: %s" % e)
        return None

    def place_exit_order(self, position, limit_price=1):
        """Place a tracked sell order for an open position."""
        if not self.client:
            return None

        ticker = position.get("ticker", "")
        side = position.get("side", "yes")
        contracts = int(position.get("contracts", 0) or 0)
        if not ticker or contracts <= 0:
            return None

        if self.has_open_exit(ticker):
            return None

        if config.DRY_RUN:
            return {
                "order_id": "dry_exit_%s_%d" % (ticker, int(time.time())),
                "ticker": ticker,
                "side": side,
                "action": "sell",
                "price_cents": limit_price,
                "contracts": contracts,
                "status": "dry_run",
            }

        try:
            if side == "yes":
                result = self.client.sell_order(
                    ticker=ticker, side="yes", count=contracts,
                    type="limit", yes_price=limit_price
                )
            else:
                result = self.client.sell_order(
                    ticker=ticker, side="no", count=contracts,
                    type="limit", no_price=limit_price
                )

            if result and result.get("order"):
                order = result["order"]
                order_id = order.get("order_id", "")
                self.open_orders[order_id] = {
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": side,
                    "price_cents": limit_price,
                    "requested_contracts": contracts,
                    "remaining_contracts": contracts,
                    "filled_contracts": 0,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "order_status": order.get("status", "resting"),
                    "action": "sell",
                }
                self._save_open_orders()
                if self.risk:
                    self.risk.mark_exit_pending(ticker, order_id)
                return order
        except Exception as e:
            print("  [MAKER] Exit order failed: %s" % e)

        return None

    def check_fills(self):
        """
        Check status of all open orders. Returns list of filled orders.
        """
        if not self.client:
            return []

        if self.open_orders:
            self._cleanup_stale_orders()

        filled = []
        to_remove = []
        changed = False
        fills_by_order = self._get_recent_fills_by_order()

        for order_id, info in list(self.open_orders.items()):
            try:
                order = self.client.get_order(order_id)
                if not order:
                    continue

                status = order.get("order_status", order.get("status", ""))
                prev_remaining = info.get("remaining_contracts", info.get("requested_contracts", 1))
                remaining = order.get("remaining_count")
                if remaining is None:
                    remaining = 0 if status in ("executed", "filled") else prev_remaining
                remaining = int(remaining)

                accounted_contracts = int(info.get("filled_contracts", 0) or 0)
                fills_for_order = fills_by_order.get(order_id, [])
                fill_contracts, fill_price_from_fills = self._extract_incremental_fill(
                    fills_for_order, accounted_contracts, info.get("side", "yes")
                )

                if fill_contracts > 0 or remaining < prev_remaining:
                    new_contracts = fill_contracts if fill_contracts > 0 else (prev_remaining - remaining)
                    fill_info = dict(info)
                    fill_info["contracts"] = new_contracts
                    fill_info["limit_price_cents"] = int(
                        info.get("limit_price_cents", info.get("price_cents", 0)) or 0
                    )

                    # Prefer the fills feed because order objects can reflect the
                    # original limit price instead of the true execution price.
                    actual_price = fill_price_from_fills
                    side = info.get("side", "yes")
                    if not actual_price or actual_price <= 0:
                        price_field = "yes_price" if side == "yes" else "no_price"
                        for key in (price_field, price_field + "_cents"):
                            raw = order.get(key)
                            if raw is not None:
                                try:
                                    actual_price = int(raw)
                                except (ValueError, TypeError):
                                    pass
                            if actual_price and actual_price > 0:
                                break
                        if not actual_price or actual_price <= 0:
                            raw = order.get(price_field + "_dollars")
                            if raw is not None:
                                try:
                                    actual_price = int(round(float(raw) * 100))
                                except (ValueError, TypeError):
                                    pass
                    fill_price = actual_price if actual_price and actual_price > 0 else info.get("price_cents", 0)
                    fill_info["price_cents"] = fill_price
                    fill_info["cost_cents"] = fill_price * new_contracts
                    if fill_info["limit_price_cents"] > 0:
                        fill_info["execution_edge_cents"] = fill_info["limit_price_cents"] - fill_price

                    fill_info["order_status"] = status or "partially_filled"
                    filled.append(fill_info)
                    info["filled_contracts"] = accounted_contracts + new_contracts
                    info["remaining_contracts"] = remaining
                    # Update tracked price with actual fill price
                    if actual_price and actual_price > 0:
                        info["price_cents"] = fill_price
                    changed = True
                    ticker = info.get("ticker", "?")
                    print("  [MAKER] FILL: %s %s x%d @ %dc" % (
                        info.get("action", "buy").upper(),
                        ticker,
                        new_contracts,
                        fill_price,
                    ))
                    if info.get("action", "buy") == "buy":
                        self._track_fill_side(info.get("side", "yes"))

                info["order_status"] = status or info.get("order_status", "")

                if status == "executed" or remaining == 0:
                    to_remove.append(order_id)
                    if info.get("action") == "sell" and self.risk:
                        self.risk.clear_exit_pending(info.get("ticker", ""), order_id)
                elif status in ("canceled", "cancelled"):
                    to_remove.append(order_id)
                    if self.risk:
                        if info.get("action") == "sell":
                            self.risk.clear_exit_pending(info.get("ticker", ""), order_id)
                        else:
                            self.risk.clear_pending_order(order_id=order_id)

            except Exception as e:
                print("  [MAKER] Fill check failed for %s: %s" % (order_id[:8] if order_id else "?", e))
                continue

        for oid in to_remove:
            if oid in self.open_orders:
                del self.open_orders[oid]
                changed = True

        if changed:
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
                if info.get("action") == "sell":
                    self.risk.clear_exit_pending(info.get("ticker", ""), order_id)
                else:
                    self.risk.clear_pending_order(order_id=order_id)
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

    def cancel_open_entry_orders(self):
        """Cancel only open buy orders, leaving exit orders intact."""
        cancelled = 0
        for order_id, info in list(self.open_orders.items()):
            if info.get("action", "buy") == "buy":
                if self.cancel_order(order_id):
                    cancelled += 1
        return cancelled

    def _get_stale_threshold_minutes(self):
        """Dynamic stale threshold: tighter near settlement."""
        et_hour = datetime.now(ZoneInfo("America/New_York")).hour
        if et_hour >= 15:
            return 10  # Was 5. Too aggressive — cancels orders before afternoon volatility fills them.
        elif et_hour >= 12:
            return 15
        return config.STALE_ORDER_MINUTES

    def _cleanup_stale_orders(self):
        """Cancel orders older than dynamic stale threshold."""
        now = datetime.now(timezone.utc)
        threshold = self._get_stale_threshold_minutes()
        stale_limit = timedelta(minutes=threshold)
        stale = []

        for order_id, info in self.open_orders.items():
            placed = info.get("placed_at", "")
            try:
                placed_dt = datetime.fromisoformat(placed.replace("Z", "+00:00"))
                if now - placed_dt > stale_limit:
                    stale.append(order_id)
            except Exception:
                print("  [MAKER] Skipping stale check for order %s: malformed timestamp '%s'" % (order_id[:8], placed))

        for oid in stale:
            self.cancel_order(oid)
            print("  [MAKER] Cancelled stale order (%dm threshold): %s" % (threshold, oid[:8]))

    def get_open_order_count(self):
        return len(self.open_orders)

    def has_open_exit(self, ticker):
        return any(
            info.get("ticker") == ticker and info.get("action") == "sell"
            for info in self.open_orders.values()
        )

    def _track_fill_side(self, side):
        """Track which side got filled for adverse selection detection."""
        key = "yes_fills" if side == "yes" else "no_fills"
        self._fill_tracking.setdefault(key, []).append(datetime.now(timezone.utc).isoformat())
        self._save_fill_tracking()

    def _track_order_side(self, side):
        """Track which side we placed orders on."""
        key = "yes_orders" if side == "yes" else "no_orders"
        self._fill_tracking.setdefault(key, []).append(datetime.now(timezone.utc).isoformat())
        self._save_fill_tracking()

    def get_adverse_selection_info(self):
        """Compute 24h rolling fill rate per side. Warn if lopsided."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=72)).isoformat()
        result = {}
        for side in ("yes", "no"):
            fills = [t for t in self._fill_tracking.get("%s_fills" % side, []) if t > cutoff]
            orders = [t for t in self._fill_tracking.get("%s_orders" % side, []) if t > cutoff]
            fill_count = len(fills)
            order_count = len(orders)
            fill_rate = min(1.0, fill_count / order_count) if order_count > 0 else 0.0
            result[side] = {"fills": fill_count, "orders": order_count, "fill_rate": fill_rate}
            if order_count >= 3:
                if fill_rate > 0.85:
                    print("  [MAKER] ADVERSE SELECTION WARNING: %s fill_rate=%.0f%% \u2014 PAUSE maker on %s side" % (
                        side.upper(), fill_rate * 100, side))
                elif fill_rate > 0.70:
                    print("  [MAKER] ADVERSE SELECTION WARNING: %s fill_rate=%.0f%% \u2014 widen spread buffer" % (
                        side.upper(), fill_rate * 100))
        return result

    def get_spread_adjustment(self, side):
        """Returns spread adjustment: 0 normal, 1 widen, None pause.

        Only counts MAKER orders for adverse selection — taker/market orders
        always fill immediately and would create false positive adverse selection.
        Requires 10+ maker orders in 72h window for meaningful signal.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=72)).isoformat()
        # Only count maker (limit) orders, not taker orders
        fills = [t for t in self._fill_tracking.get("%s_fills" % side, []) if t > cutoff]
        orders = [t for t in self._fill_tracking.get("%s_orders" % side, []) if t > cutoff]
        taker_count = len([t for t in self._fill_tracking.get("%s_taker" % side, []) if t > cutoff])
        # Subtract taker orders/fills — they always fill, not adverse selection
        maker_orders = max(0, len(orders) - taker_count)
        maker_fills = max(0, len(fills) - taker_count)
        if maker_orders < 10:  # Need 10+ maker orders for meaningful fill rate
            return 0
        fill_rate = maker_fills / maker_orders
        if fill_rate > 0.85:
            return None  # pause
        elif fill_rate > 0.70:
            return 1  # widen
        return 0

    def _load_fill_tracking(self):
        """Load adverse selection tracking data."""
        path = getattr(config, "FILL_TRACKING_FILE", os.path.join(config.STATE_DIR, "fill_tracking.json"))
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_fill_tracking(self):
        """Save adverse selection tracking data. Prunes entries older than 75h (72h window + buffer)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=75)).isoformat()
        for key in list(self._fill_tracking.keys()):
            entries = self._fill_tracking[key]
            if isinstance(entries, list):
                self._fill_tracking[key] = [t for t in entries if t > cutoff]
        path = getattr(config, "FILL_TRACKING_FILE", os.path.join(config.STATE_DIR, "fill_tracking.json"))
        try:
            config.atomic_json_save(path, self._fill_tracking)
        except Exception as e:
            print("  [MAKER] Error saving fill tracking: %s" % e)

    def _load_open_orders(self):
        try:
            if os.path.exists(config.MAKER_ORDERS_FILE):
                with open(config.MAKER_ORDERS_FILE, "r") as f:
                    data = json.load(f)
                    normalized = {}
                    for order_id, info in data.items():
                        requested = int(info.get("requested_contracts", info.get("contracts", 1)) or 1)
                        remaining = int(info.get("remaining_contracts", requested) or requested)
                        info["requested_contracts"] = requested
                        info["remaining_contracts"] = remaining
                        info["filled_contracts"] = int(info.get("filled_contracts", requested - remaining) or 0)
                        info["action"] = info.get("action", "buy")
                        normalized[order_id] = info
                    return normalized
        except Exception:
            pass
        return {}

    def _save_open_orders(self):
        try:
            config.atomic_json_save(config.MAKER_ORDERS_FILE, self.open_orders)
        except Exception as e:
            print("  [MAKER] Error saving orders: %s" % e)
