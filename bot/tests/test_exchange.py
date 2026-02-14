"""
Unit tests for SimExchange — order placement, settlement, and PnL tracking.

Run with:
    python -m unittest tests/test_exchange.py
    python -m pytest  tests/test_exchange.py
"""

import os
import sys
import tempfile
import unittest

# Allow imports from the src directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange import SimExchange


def _make_market(ticker="KXHIGHNY-26FEB14-T35T39", yes_bid=30, yes_ask=35,
                 no_bid=60, no_ask=65, status="open"):
    """Helper: build a minimal market dict for testing."""
    return {
        "ticker": ticker,
        "title": "High temperature between 35\u00b0F to 39\u00b0F",
        "subtitle": "",
        "event_ticker": "KXHIGHNY-26FEB14",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "status": status,
        "volume": 100,
    }


class TestSimExchangeBalance(unittest.TestCase):
    """Tests that verify balance initialisation and tracking."""

    def _make_exchange(self, balance=10000):
        """Create a deterministic SimExchange with an empty fixtures dir."""
        tmp = tempfile.mkdtemp()
        return SimExchange(
            fixtures_dir=tmp,
            initial_balance_cents=balance,
            fill_probability=1.0,
            slippage_cents=0,
            fees_per_contract_cents=0,
        )

    def test_initial_balance(self):
        """SimExchange should report the initial balance we configured."""
        exch = self._make_exchange(balance=5000)
        self.assertEqual(exch.get_balance(), 5000)

    def test_initial_balance_default(self):
        """Default balance (10 000 cents = $100) should be set when not overridden."""
        exch = self._make_exchange(balance=10000)
        self.assertEqual(exch.get_balance(), 10000)


class TestSimExchangeOrders(unittest.TestCase):
    """Tests for place_order — fills, insufficient funds, deductions."""

    def _make_exchange(self, balance=10000):
        tmp = tempfile.mkdtemp()
        return SimExchange(
            fixtures_dir=tmp,
            initial_balance_cents=balance,
            fill_probability=1.0,
            slippage_cents=0,
            fees_per_contract_cents=0,
        )

    def test_place_order_deducts_balance(self):
        """Placing a YES order should deduct cost (fill_price * count) from balance."""
        exch = self._make_exchange(balance=10000)
        market = _make_market(yes_ask=35)
        exch.load_snapshot([market])

        result = exch.place_order(
            ticker=market["ticker"],
            side="yes",
            count=10,
            yes_price=35,
        )

        self.assertIsNotNone(result)
        # cost = 35 cents * 10 contracts = 350 cents
        self.assertEqual(exch.get_balance(), 10000 - 350)

    def test_place_order_insufficient_funds(self):
        """Order costing more than the available balance should return None."""
        exch = self._make_exchange(balance=100)
        market = _make_market(yes_ask=35)
        exch.load_snapshot([market])

        result = exch.place_order(
            ticker=market["ticker"],
            side="yes",
            count=10,
            yes_price=35,
        )
        # 35 * 10 = 350 > 100 available
        self.assertIsNone(result)
        # Balance unchanged
        self.assertEqual(exch.get_balance(), 100)

    def test_place_order_creates_position(self):
        """A filled order should appear in get_positions()."""
        exch = self._make_exchange()
        market = _make_market()
        exch.load_snapshot([market])

        exch.place_order(ticker=market["ticker"], side="yes", count=5, yes_price=35)

        positions = exch.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["ticker"], market["ticker"])
        self.assertEqual(positions[0]["side"], "yes")
        self.assertEqual(positions[0]["contracts"], 5)

    def test_place_order_unknown_ticker_returns_none(self):
        """Placing an order on a ticker not loaded should return None."""
        exch = self._make_exchange()
        result = exch.place_order(
            ticker="NONEXISTENT-TICKER",
            side="yes",
            count=1,
            yes_price=50,
        )
        self.assertIsNone(result)


class TestSimExchangeSettlement(unittest.TestCase):
    """Tests for settle_market — wins, losses, and PnL."""

    def _make_exchange(self, balance=10000):
        tmp = tempfile.mkdtemp()
        return SimExchange(
            fixtures_dir=tmp,
            initial_balance_cents=balance,
            fill_probability=1.0,
            slippage_cents=0,
            fees_per_contract_cents=0,
        )

    def test_settle_market_win(self):
        """Buying YES at 35c, settling YES: payout = 100c/contract, profit = 65c/contract."""
        exch = self._make_exchange(balance=10000)
        market = _make_market(yes_ask=35)
        exch.load_snapshot([market])

        exch.place_order(ticker=market["ticker"], side="yes", count=2, yes_price=35)
        # Balance after order: 10000 - (35 * 2) = 9930
        self.assertEqual(exch.get_balance(), 9930)

        settled = exch.settle_market(market["ticker"], "yes")

        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["result"], "win")
        # Payout: 2 contracts * 100c = 200c added to balance
        # Final: 9930 + 200 = 10130
        self.assertEqual(exch.get_balance(), 10130)
        self.assertEqual(settled[0]["profit_cents"], 200 - 70)  # 130

    def test_settle_market_loss(self):
        """Buying YES at 35c, settling NO: total loss of cost."""
        exch = self._make_exchange(balance=10000)
        market = _make_market(yes_ask=35)
        exch.load_snapshot([market])

        exch.place_order(ticker=market["ticker"], side="yes", count=2, yes_price=35)
        self.assertEqual(exch.get_balance(), 9930)

        settled = exch.settle_market(market["ticker"], "no")

        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["result"], "loss")
        # No payout — balance stays at 9930
        self.assertEqual(exch.get_balance(), 9930)
        self.assertEqual(settled[0]["profit_cents"], -70)  # lost the 70c cost

    def test_settle_no_side_win(self):
        """Buying NO at 65c, settling NO: payout = 100c/contract."""
        exch = self._make_exchange(balance=10000)
        market = _make_market(no_ask=65)
        exch.load_snapshot([market])

        exch.place_order(ticker=market["ticker"], side="no", count=1, no_price=65)
        self.assertEqual(exch.get_balance(), 10000 - 65)

        settled = exch.settle_market(market["ticker"], "no")
        self.assertEqual(settled[0]["result"], "win")
        # 9935 + 100 = 10035
        self.assertEqual(exch.get_balance(), 10035)

    def test_get_pnl(self):
        """PnL should aggregate across multiple settled trades."""
        exch = self._make_exchange(balance=10000)

        market_a = _make_market(ticker="KXHIGHNY-26FEB14-T35T39", yes_ask=40)
        market_b = _make_market(ticker="KXHIGHNY-26FEB14-T40T44", yes_ask=30)
        exch.load_snapshot([market_a, market_b])

        # Trade A: buy YES at 40c * 2 = 80c cost
        exch.place_order(ticker=market_a["ticker"], side="yes", count=2, yes_price=40)
        # Trade B: buy YES at 30c * 3 = 90c cost
        exch.place_order(ticker=market_b["ticker"], side="yes", count=3, yes_price=30)

        # Settle A as win, B as loss
        exch.settle_market(market_a["ticker"], "yes")
        exch.settle_market(market_b["ticker"], "no")

        pnl = exch.get_pnl()

        self.assertEqual(pnl["wins"], 1)
        self.assertEqual(pnl["losses"], 1)
        self.assertEqual(pnl["total_trades"], 2)
        # Trade A returned 200c, trade B returned 0c
        self.assertEqual(pnl["total_returned_cents"], 200)
        # Total invested: 80 + 90 = 170
        self.assertEqual(pnl["total_invested_cents"], 170)
        # Profit: 200 - 170 = 30
        self.assertEqual(pnl["profit_cents"], 30)
        self.assertAlmostEqual(pnl["win_rate"], 0.5)

    def test_get_pnl_no_trades(self):
        """PnL with zero trades should be all zeros."""
        exch = self._make_exchange()
        pnl = exch.get_pnl()

        self.assertEqual(pnl["total_trades"], 0)
        self.assertEqual(pnl["profit_cents"], 0)
        self.assertEqual(pnl["wins"], 0)
        self.assertEqual(pnl["losses"], 0)


if __name__ == "__main__":
    unittest.main()
