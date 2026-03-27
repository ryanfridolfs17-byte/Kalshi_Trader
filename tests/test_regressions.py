import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import dashboard
import config
from kalshi_bot import _prepare_entry_signal
from observation_paper import ObservationPaperTrader
from risk_manager_v2 import RiskManager
from strategy import Strategy


class DummyWeather:

    def __init__(self, parsed, distribution, probability):
        self.parsed = parsed
        self.distribution = distribution
        self.probability = probability
        self.calls = []

    def parse_market_bucket(self, market):
        return dict(self.parsed)

    def get_temperature_distribution(self, city_code, target_date, model_weights=None, city_bias_f=0.0):
        self.calls.append({
            "city_code": city_code,
            "target_date": target_date,
            "model_weights": model_weights,
            "city_bias_f": city_bias_f,
        })
        return dict(self.distribution)

    def calculate_bucket_probability(self, distribution, temp_low, temp_high):
        return self.probability


class DummyConfirmer:

    def __init__(self, verdict="STRONG", size_multiplier=1.0):
        self.verdict = verdict
        self.size_multiplier = size_multiplier

    def confirm_signal(self, **kwargs):
        return {
            "verdict": self.verdict,
            "size_multiplier": self.size_multiplier,
        }


class StrategyRegressionTests(unittest.TestCase):

    def _build_strategy(self, probability, distribution, verdict="STRONG"):
        parsed = {
            "city_code": "NYC",
            "temp_low": 50,
            "temp_high": 51,
            "target_date": "2026-03-27",
        }
        strategy = Strategy()
        strategy.weather = DummyWeather(parsed, distribution, probability)
        strategy.confirmer = DummyConfirmer(verdict=verdict)
        strategy.balance_cents = 4000
        return strategy

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 3, 27, 15, 0, tzinfo=ZoneInfo("America/New_York"))
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    @patch("strategy.datetime", FixedDateTime)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_blocks_yes_side_by_default(self, _mock_threshold):
        strategy = self._build_strategy(
            probability=0.80,
            distribution={
                "forecasted_high_mean": 55.0,
                "raw_forecast_mean": 55.0,
                "model_spread": 1.0,
                "std_dev": 2.0,
                "total_members": 100,
                "model_means": {"gfs_ensemble": 55.0},
            },
        )
        market = {
            "ticker": "KXHIGHNY-26MAR10-B50.5",
            "yes_ask": 20,
            "no_ask": 82,
            "last_price": 20,
            "volume": 100,
            "open_interest": 50,
            "volume_24h": 100,
        }

        signal = strategy.evaluate_market(market)

        self.assertEqual(signal["signal"], "skip")
        self.assertEqual(signal["skip_reason"], "yes_side_blocked")
        self.assertEqual(signal["side"], "yes")

    @patch("strategy.datetime", FixedDateTime)
    @patch.object(config, "ALLOW_YES_SIDE_TRADES", True)
    @patch.object(config, "LONGSHOT_FLOOR_CENTS", 1)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_can_buy_yes_when_enabled(self, _mock_threshold):
        strategy = self._build_strategy(
            probability=0.80,
            distribution={
                "forecasted_high_mean": 55.0,
                "raw_forecast_mean": 55.0,
                "model_spread": 1.0,
                "std_dev": 2.0,
                "total_members": 100,
                "model_means": {"gfs_ensemble": 55.0},
            },
            verdict="CONFIRM",
        )
        market = {
            "ticker": "KXHIGHNY-26MAR27-B50.5",
            "yes_ask": 20,
            "no_ask": 82,
            "last_price": 20,
            "volume": 100,
            "open_interest": 50,
            "volume_24h": 100,
        }

        signal = strategy.evaluate_market(market)

        self.assertEqual(signal["signal"], "buy")
        self.assertEqual(signal["side"], "yes")
        self.assertEqual(signal["price_cents"], 20)
        self.assertGreater(signal["suggested_contracts"], 0)

    @patch("strategy.datetime", FixedDateTime)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_blocks_longshot_no_by_default(self, _mock_threshold):
        strategy = self._build_strategy(
            probability=0.20,
            distribution={
                "forecasted_high_mean": 40.0,
                "raw_forecast_mean": 40.0,
                "model_spread": 1.5,
                "std_dev": 2.0,
                "total_members": 100,
                "model_means": {"gfs_ensemble": 40.0},
            },
        )
        market = {
            "ticker": "KXHIGHNY-26MAR10-B50.5",
            "yes_ask": 85,
            "no_ask": 20,
            "last_price": 85,
            "volume": 100,
            "open_interest": 50,
            "volume_24h": 100,
        }

        signal = strategy.evaluate_market(market)

        self.assertEqual(signal["signal"], "skip")
        self.assertEqual(signal["skip_reason"], "longshot_floor")
        self.assertEqual(signal["side"], "no")

    @patch("strategy.datetime", FixedDateTime)
    @patch.object(config, "LONGSHOT_FLOOR_CENTS", 1)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_uses_no_side_probability_and_kelly_when_longshots_allowed(self, _mock_threshold):
        strategy = self._build_strategy(
            probability=0.20,
            distribution={
                "forecasted_high_mean": 40.0,
                "raw_forecast_mean": 40.0,
                "model_spread": 1.5,
                "std_dev": 2.0,
                "total_members": 100,
                "model_means": {"gfs_ensemble": 40.0},
            },
            verdict="CONFIRM",
        )
        market = {
            "ticker": "KXHIGHNY-26MAR27-B50.5",
            "yes_ask": 85,
            "no_ask": 20,
            "last_price": 85,
            "volume": 100,
            "open_interest": 50,
            "volume_24h": 100,
        }

        signal = strategy.evaluate_market(market)

        self.assertEqual(signal["signal"], "buy")
        self.assertEqual(signal["side"], "no")
        self.assertEqual(signal["price_cents"], 20)
        self.assertGreater(signal["suggested_contracts"], 0)


class ExecutionParityRegressionTests(unittest.TestCase):

    def test_prepare_entry_signal_uses_limit_price_for_maker_risk(self):
        class DummyMaker:
            @staticmethod
            def calculate_limit_price(_signal):
                return 35

        prepared = _prepare_entry_signal({
            "ticker": "KXHIGHNY-TEST",
            "side": "no",
            "price_cents": 20,
            "edge": 0.08,
            "fee_adjusted_edge": 0.05,
            "confirmation_verdict": "CONFIRM",
        }, DummyMaker())

        self.assertEqual(prepared["execution_style"], "maker")
        self.assertEqual(prepared["limit_price"], 35)
        self.assertEqual(prepared["risk_price_cents"], 35)

    def test_observation_paper_queues_unfilled_maker_order(self):
        trader = ObservationPaperTrader(kalshi_client=None)
        trader.state = {
            "active": {},
            "pending_orders": {},
            "history": [],
            "summary": {},
            "last_reconciled_at": "",
            "last_trade_time": None,
            "daily_date": "2026-03-27",
            "trade_count_today": 0,
            "daily_pnl_cents": 0,
            "total_exposure_cents": 0,
            "ticker_entry_dates": {},
            "cycle_log": [],
        }

        result = trader.record_observation_cycle(
            signals=[{
                "ticker": "KXHIGHNY-TEST",
                "city_code": "NYC",
                "side": "no",
                "suggested_contracts": 1,
                "price_cents": 40,
                "current_price_cents": 40,
                "risk_price_cents": 30,
                "limit_price": 30,
                "execution_style": "maker",
                "edge": 0.08,
                "fee_adjusted_edge": 0.05,
                "strategy": "S1-Weather",
                "confirmation_verdict": "CONFIRM",
                "target_date": "2026-03-27",
            }],
            cycle=1,
            balance_cents=10000,
            market_prices={"KXHIGHNY-TEST": {"no": 40}},
            live_risk=None,
            max_per_cycle=3,
        )

        self.assertEqual(result["executed"], [])
        self.assertEqual(len(result["queued"]), 1)
        self.assertEqual(trader.state["summary"]["pending_count"], 1)


class RiskLifecycleRegressionTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.risk_path = os.path.join(self.temp_dir.name, "risk_state.json")

        def _save_json(filepath, data, indent=2):
            import json
            with open(filepath, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=indent)

        self.atomic_patch = patch.object(config, "atomic_json_save", side_effect=_save_json)
        self.file_patch = patch.object(config, "RISK_STATE_FILE", self.risk_path)
        self.atomic_patch.start()
        self.file_patch.start()
        self.addCleanup(self.atomic_patch.stop)
        self.addCleanup(self.file_patch.stop)

    def test_partial_fill_then_cancel_remainder_keeps_filled_exposure(self):
        risk = RiskManager()
        risk.add_pending_order({
            "order_id": "ord-1",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "side": "no",
            "price_cents": 20,
            "contracts": 5,
            "cost_cents": 100,
            "order_status": "resting",
        })

        self.assertEqual(risk.state["total_exposure_cents"], 100)
        self.assertIn("ord-1", risk.get_pending_orders())

        risk.record_fill({
            "order_id": "ord-1",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "side": "no",
            "action": "buy",
            "contracts": 2,
            "price_cents": 20,
            "cost_cents": 40,
        })

        pending = risk.get_pending_orders()["ord-1"]
        self.assertEqual(pending["remaining_contracts"], 3)
        self.assertEqual(risk.get_positions()["KXHIGHNY-TEST"]["contracts"], 2)
        self.assertEqual(risk.state["total_exposure_cents"], 100)

        risk.clear_pending_order(order_id="ord-1")

        self.assertNotIn("ord-1", risk.get_pending_orders())
        self.assertEqual(risk.state["total_exposure_cents"], 40)

        risk.mark_exit_pending("KXHIGHNY-TEST", "exit-1")
        self.assertEqual(
            risk.get_positions()["KXHIGHNY-TEST"]["order_status"],
            "exit_pending",
        )

        risk.record_fill({
            "order_id": "exit-1",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "side": "no",
            "action": "sell",
            "contracts": 2,
            "price_cents": 10,
            "cost_cents": 20,
        })

        self.assertNotIn("KXHIGHNY-TEST", risk.get_positions())
        self.assertEqual(risk.state["total_exposure_cents"], 0)

    def test_pending_order_blocks_opposite_side_exposure(self):
        risk = RiskManager()
        risk.add_pending_order({
            "order_id": "ord-yes",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "side": "yes",
            "price_cents": 20,
            "contracts": 1,
            "cost_cents": 20,
            "order_status": "resting",
        })

        approved, reason = risk.check_trade({
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "side": "no",
            "price_cents": 20,
            "contracts": 1,
        })

        self.assertFalse(approved)
        self.assertIn("Opposite-side exposure", reason)


class DashboardSchemaRegressionTests(unittest.TestCase):

    def test_normalize_risk_state_upgrades_legacy_schema(self):
        legacy = {
            "daily_loss_cents": 250,
            "daily_trade_count": 3,
            "last_reset_date": "2026-03-09",
            "loss_pause_until": "2026-03-10T15:00:00+00:00",
            "positions": [
                {
                    "ticker": "KXHIGHNY-TEST",
                    "side": "no",
                    "contracts": 2,
                    "cost_cents": 40,
                    "price_cents": 20,
                    "city_code": "NYC",
                },
                {
                    "ticker": "KXHIGHNY-TEST",
                    "side": "no",
                    "contracts": 1,
                    "cost_cents": 20,
                    "price_cents": 20,
                    "city_code": "NYC",
                },
            ],
            "pending_orders": {
                "ord-1": {
                    "order_id": "ord-1",
                    "ticker": "KXHIGHNY-TEST",
                    "side": "no",
                    "city_code": "NYC",
                    "price_cents": 25,
                    "requested_contracts": 2,
                    "remaining_contracts": 2,
                    "remaining_cost_cents": 50,
                }
            },
        }

        normalized = dashboard._normalize_risk_state(legacy)

        self.assertEqual(normalized["daily_pnl_cents"], -250)
        self.assertEqual(normalized["trade_count_today"], 3)
        self.assertEqual(normalized["kill_switch_until"], "2026-03-10T15:00:00+00:00")
        self.assertIsInstance(normalized["positions"], dict)
        self.assertEqual(normalized["positions"]["KXHIGHNY-TEST"]["contracts"], 3)
        self.assertEqual(normalized["positions"]["KXHIGHNY-TEST"]["cost_cents"], 60)
        self.assertEqual(normalized["total_exposure_cents"], 110)
        self.assertEqual(normalized["city_exposure"]["NYC"], 110)

    def test_executed_buy_fill_filter_ignores_manual_entries(self):
        self.assertTrue(dashboard._is_executed_buy_fill({"entry_type": "buy_fill"}))
        self.assertFalse(dashboard._is_executed_buy_fill({"entry_type": "manual_close"}))
        self.assertTrue(dashboard._is_executed_buy_fill({"status": "live_filled"}))
        self.assertFalse(dashboard._is_executed_buy_fill({"status": "closed"}))


if __name__ == "__main__":
    unittest.main()
