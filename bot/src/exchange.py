"""
EXCHANGE INTERFACE ABSTRACTION
==================================
Defines a common interface for market data + order placement.
Two implementations:
  - KalshiExchange: live/demo API (real connectivity)
  - SimExchange: offline simulation (for backtesting)

The strategy, scanner, and bot consume this interface — they never
know whether they're talking to Kalshi or a simulator.
"""

from abc import ABC, abstractmethod
from datetime import datetime
import json
import os
import copy


class ExchangeInterface(ABC):
    """Abstract base class for all exchange implementations."""

    @abstractmethod
    def get_markets(self, series_ticker, status="open", limit=100):
        """Fetch markets for a series. Returns list of market dicts."""
        pass

    @abstractmethod
    def get_market(self, ticker):
        """Fetch a single market by ticker. Returns market dict or None."""
        pass

    @abstractmethod
    def get_orderbook(self, ticker):
        """Fetch orderbook for a ticker. Returns {"yes": [...], "no": [...]}."""
        pass

    @abstractmethod
    def place_order(self, ticker, side, count, order_type="limit",
                    yes_price=None, no_price=None):
        """Place an order. Returns order dict or None."""
        pass

    @abstractmethod
    def cancel_order(self, order_id):
        """Cancel an order. Returns True/False."""
        pass

    @abstractmethod
    def get_positions(self):
        """Get current open positions. Returns list of position dicts."""
        pass

    @abstractmethod
    def get_balance(self):
        """Get account balance in cents. Returns int."""
        pass


class SimExchange(ExchangeInterface):
    """
    Simulated exchange for offline backtesting.
    Reads market snapshots from fixture files and simulates fills.

    Features:
      - Configurable fill probability and slippage
      - Tracks positions, balance, and P&L
      - Supports market settlement based on outcomes
      - Deterministic when seeded
    """

    def __init__(self, fixtures_dir="data/fixtures", initial_balance_cents=10000,
                 fill_probability=0.85, slippage_cents=1, fees_per_contract_cents=0):
        self.fixtures_dir = fixtures_dir
        self.balance_cents = initial_balance_cents
        self.initial_balance = initial_balance_cents
        self.fill_probability = fill_probability
        self.slippage_cents = slippage_cents
        self.fees_cents = fees_per_contract_cents

        # State
        self._markets = {}        # ticker -> market dict
        self._positions = []      # list of position dicts
        self._orders = []         # order history
        self._settlements = {}    # ticker -> "yes" or "no"
        self._trade_counter = 0
        self._current_step = 0    # For stepping through time

        # Load fixtures
        self._load_fixtures()

    def _load_fixtures(self):
        """Load market snapshot fixtures from JSON files."""
        if not os.path.exists(self.fixtures_dir):
            return

        for filename in sorted(os.listdir(self.fixtures_dir)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(self.fixtures_dir, filename)
            try:
                with open(filepath) as f:
                    data = json.load(f)

                if "markets" in data:
                    for m in data["markets"]:
                        self._markets[m["ticker"]] = m
                if "settlements" in data:
                    self._settlements.update(data["settlements"])
            except Exception:
                pass

    def load_snapshot(self, markets, settlements=None):
        """Load markets directly (for programmatic test setup)."""
        for m in markets:
            self._markets[m["ticker"]] = m
        if settlements:
            self._settlements.update(settlements)

    def get_markets(self, series_ticker, status="open", limit=100):
        """Return markets matching the series ticker."""
        results = []
        for ticker, market in self._markets.items():
            if series_ticker.upper() in ticker.upper():
                if status == "open" and market.get("status", "open") != "open":
                    continue
                results.append(copy.deepcopy(market))
                if len(results) >= limit:
                    break
        return results

    def get_market(self, ticker):
        """Return a single market by ticker."""
        market = self._markets.get(ticker)
        if market:
            return copy.deepcopy(market)
        return None

    def get_orderbook(self, ticker):
        """Return simulated orderbook (derived from bid/ask)."""
        market = self._markets.get(ticker)
        if not market:
            return {"yes": [], "no": []}

        yes_bid = market.get("yes_bid", 0) or 0
        yes_ask = market.get("yes_ask", 0) or 0
        no_bid = market.get("no_bid", 0) or 0
        no_ask = market.get("no_ask", 0) or 0

        return {
            "yes": [{"price": yes_bid, "quantity": 50}, {"price": yes_ask, "quantity": 50}],
            "no": [{"price": no_bid, "quantity": 50}, {"price": no_ask, "quantity": 50}],
        }

    def place_order(self, ticker, side, count, order_type="limit",
                    yes_price=None, no_price=None):
        """
        Simulate order placement with configurable fill behavior.
        Applies slippage and fees. Deducts from balance.
        """
        market = self._markets.get(ticker)
        if not market:
            return None

        # Determine fill price
        if side == "yes":
            ask = market.get("yes_ask", 0) or 0
            fill_price = (yes_price or ask) + self.slippage_cents
        else:
            ask = market.get("no_ask", 0) or 0
            fill_price = (no_price or ask) + self.slippage_cents

        fill_price = min(fill_price, 99)  # Cap at 99c

        # Simulate fill probability
        import random
        if random.random() > self.fill_probability:
            return None  # Order didn't fill

        # Calculate cost
        cost = fill_price * count
        fees = self.fees_cents * count
        total_cost = cost + fees

        if total_cost > self.balance_cents:
            return None  # Insufficient funds

        # Execute
        self.balance_cents -= total_cost
        self._trade_counter += 1

        order = {
            "order_id": f"sim_{self._trade_counter}",
            "ticker": ticker,
            "side": side,
            "count": count,
            "fill_price": fill_price,
            "cost_cents": cost,
            "fees_cents": fees,
            "status": "filled",
            "timestamp": datetime.now().isoformat(),
        }
        self._orders.append(order)

        # Add to positions
        self._positions.append({
            "ticker": ticker,
            "side": side,
            "contracts": count,
            "cost_cents": cost,
            "fill_price": fill_price,
            "city_code": self._extract_city(ticker),
            "timestamp": order["timestamp"],
        })

        return {"order": order}

    def cancel_order(self, order_id):
        return True

    def get_positions(self):
        return copy.deepcopy(self._positions)

    def get_balance(self):
        return self.balance_cents

    # ─── Settlement ───

    def settle_market(self, ticker, result):
        """
        Settle a market. result is "yes" or "no".
        Updates positions and balance accordingly.
        """
        self._settlements[ticker] = result
        if ticker in self._markets:
            self._markets[ticker]["status"] = "settled"
            self._markets[ticker]["result"] = result

        settled_positions = []
        remaining_positions = []

        for pos in self._positions:
            if pos["ticker"] != ticker:
                remaining_positions.append(pos)
                continue

            side = pos["side"]
            contracts = pos["contracts"]
            cost = pos["cost_cents"]

            if (side == "yes" and result == "yes") or \
               (side == "no" and result == "no"):
                # WIN: get $1 per contract
                payout = contracts * 100
                profit = payout - cost
                self.balance_cents += payout
                pos["settled"] = True
                pos["result"] = "win"
                pos["profit_cents"] = profit
            else:
                # LOSS: lose cost
                pos["settled"] = True
                pos["result"] = "loss"
                pos["profit_cents"] = -cost

            settled_positions.append(pos)

        self._positions = remaining_positions
        return settled_positions

    def settle_all(self):
        """Settle all markets that have outcomes defined."""
        all_settled = []
        for ticker, result in self._settlements.items():
            settled = self.settle_market(ticker, result)
            all_settled.extend(settled)
        return all_settled

    # ─── Metrics ───

    def get_pnl(self):
        """Calculate total P&L from all settled orders."""
        total_cost = 0
        total_payout = 0
        wins = 0
        losses = 0

        for order in self._orders:
            ticker = order["ticker"]
            side = order["side"]
            cost = order["cost_cents"]
            total_cost += cost

            result = self._settlements.get(ticker)
            if result:
                if (side == "yes" and result == "yes") or \
                   (side == "no" and result == "no"):
                    total_payout += order["count"] * 100
                    wins += 1
                else:
                    losses += 1

        return {
            "total_invested_cents": total_cost,
            "total_returned_cents": total_payout,
            "profit_cents": total_payout - total_cost,
            "roi_pct": ((total_payout - total_cost) / total_cost * 100) if total_cost > 0 else 0,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / (wins + losses) if (wins + losses) > 0 else 0,
            "total_trades": len(self._orders),
            "balance_cents": self.balance_cents,
            "balance_change_cents": self.balance_cents - self.initial_balance,
        }

    def get_trade_history(self):
        """Return all orders placed."""
        return copy.deepcopy(self._orders)

    # ─── Helpers ───

    @staticmethod
    def _extract_city(ticker):
        """Extract city code from ticker like KXHIGHNY-26FEB14-B44.5."""
        ticker_upper = ticker.upper()
        city_map = {
            "KXHIGHNY": "NYC",
            "KXHIGHCHI": "CHI",
            "KXHIGHMIA": "MIA",
            "KXHIGHAUS": "AUS",
        }
        for prefix, code in city_map.items():
            if prefix in ticker_upper:
                return code
        return ""


class KalshiExchange(ExchangeInterface):
    """
    Live Kalshi exchange implementation.
    Wraps the existing KalshiClient for the ExchangeInterface.
    """

    def __init__(self, kalshi_client):
        self.client = kalshi_client

    def get_markets(self, series_ticker, status="open", limit=100):
        """Fetch markets from Kalshi API."""
        import requests
        prod_url = "https://api.elections.kalshi.com/trade-api/v2"
        try:
            params = {
                "series_ticker": series_ticker,
                "status": status,
                "limit": limit,
            }
            response = requests.get(f"{prod_url}/markets", params=params, timeout=15)
            if response.status_code == 200:
                return response.json().get("markets", [])
        except Exception:
            pass
        return []

    def get_market(self, ticker):
        """Fetch single market from Kalshi API."""
        import requests
        prod_url = "https://api.elections.kalshi.com/trade-api/v2"
        try:
            response = requests.get(f"{prod_url}/markets/{ticker}", timeout=15)
            if response.status_code == 200:
                return response.json().get("market", {})
        except Exception:
            pass
        return None

    def get_orderbook(self, ticker):
        if self.client:
            return self.client.get_orderbook(ticker) or {"yes": [], "no": []}
        return {"yes": [], "no": []}

    def place_order(self, ticker, side, count, order_type="limit",
                    yes_price=None, no_price=None):
        if not self.client:
            return None
        return self.client.create_order(
            ticker=ticker, side=side, count=count,
            type=order_type, yes_price=yes_price, no_price=no_price,
        )

    def cancel_order(self, order_id):
        if not self.client:
            return False
        try:
            self.client.cancel_order(order_id)
            return True
        except Exception:
            return False

    def get_positions(self):
        if not self.client:
            return []
        try:
            return self.client.get_positions() or []
        except Exception:
            return []

    def get_balance(self):
        if not self.client:
            return 0
        try:
            return self.client.get_balance() or 0
        except Exception:
            return 0
