import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import dashboard
import config
import backfill_observation_db
import sync_local
from bot_db import BotDatabase
from execution_models import build_entry_order
from kalshi_bot import _finalize_observation_decisions, _prepare_entry_signal
from maker_strategy import MakerStrategy
from observation_journal import ObservationJournal
from observation_paper import ObservationPaperTrader
from settlement_lock import SettlementLockPaper
from paper_challengers import PaperChallengerEngine
from risk_manager_v2 import RiskManager
from strategy import Strategy
from strategy_registry import StrategyRegistry, build_strategy_scorecards
from trade_reviewer import TradeReviewer
import weather_engine


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
    @patch.object(config, "ALLOW_YES_SIDE_TRADES", True)
    @patch.object(config, "LONGSHOT_FLOOR_CENTS", 1)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_clamps_bucket_probability_bounds(self, _mock_threshold):
        strategy = self._build_strategy(
            probability=1.01,
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
        self.assertAlmostEqual(signal["our_prob"], 0.9999, places=4)

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

    @patch("strategy.datetime", FixedDateTime)
    @patch.object(config, "LONGSHOT_FLOOR_CENTS", 1)
    @patch.object(config, "ALLOW_NEXT_DAY_DIRECTIONAL_TRADES", False)
    @patch.object(Strategy, "_get_edge_threshold", return_value=0.0)
    def test_weather_strategy_shadow_override_runs_full_next_day_pipeline(self, _mock_threshold):
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
        strategy.weather.parsed["target_date"] = "2026-03-28"
        market = {
            "ticker": "KXHIGHNY-26MAR28-B50.5",
            "yes_ask": 85,
            "no_ask": 20,
            "last_price": 85,
            "volume": 100,
            "open_interest": 50,
            "volume_24h": 100,
        }

        default_signal = strategy.evaluate_market(market)
        shadow_signal = strategy.evaluate_market(
            market,
            allow_next_day_directional_override=True,
        )

        self.assertEqual(default_signal["signal"], "skip")
        self.assertEqual(default_signal["skip_reason"], "next_day_directional_blocked")
        self.assertEqual(shadow_signal["signal"], "buy")
        self.assertEqual(shadow_signal["side"], "no")
        self.assertEqual(shadow_signal["shadow_mode"], "next_day_directional_override")


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

    def test_build_entry_order_preserves_explicit_execution_style(self):
        order = build_entry_order({
            "ticker": "KXHIGHNY-TEST",
            "side": "no",
            "price_cents": 40,
            "current_price_cents": 40,
            "limit_price_cents": 28,
            "execution_style": "maker",
            "edge": 0.30,
            "fee_adjusted_edge": 0.25,
            "confirmation_verdict": "CONFIRMED_OUTCOME",
        })

        self.assertEqual(order["execution_style"], "maker")
        self.assertEqual(order["limit_price_cents"], 28)
        self.assertEqual(order["risk_price_cents"], 28)

    def test_observation_paper_queues_unfilled_maker_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paper_path = os.path.join(temp_dir, "paper_trades.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", recent_events_path), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", recent_decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", recent_cache_path), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path):
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
                trader.journal.close()

    def test_observation_paper_uses_final_order_metadata_for_fill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paper_path = os.path.join(temp_dir, "paper_trades.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", recent_events_path), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", recent_decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", recent_cache_path), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path):
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
                        "price_cents": 35,
                        "current_price_cents": 30,
                        "risk_price_cents": 28,
                        "limit_price_cents": 28,
                        "execution_style": "maker",
                        "edge": 0.08,
                        "fee_adjusted_edge": 0.05,
                        "strategy": "S1-Weather",
                        "confirmation_verdict": "CONFIRM",
                        "target_date": "2026-03-27",
                    }],
                    cycle=2,
                    balance_cents=10000,
                    market_prices={"KXHIGHNY-TEST": {"no": 25}},
                    live_risk=None,
                    max_per_cycle=3,
                )

                self.assertEqual(len(result["executed"]), 1)
                self.assertEqual(result["executed"][0]["limit_price_cents"], 28)
                self.assertEqual(result["executed"][0]["risk_price_cents"], 28)
                self.assertEqual(result["executed"][0]["entry_price_cents"], 25)
                trader.journal.close()

    def test_observation_paper_counts_filled_pending_against_cycle_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            paper_path = os.path.join(temp_dir, "paper_trades.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", recent_events_path), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", recent_decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", recent_cache_path), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path):
                trader = ObservationPaperTrader(kalshi_client=None)
                trader.state = {
                    "active": {},
                    "pending_orders": {
                        "paper_old_1": {
                            "order_id": "paper_old_1",
                            "ticker": "KXHIGHNY-PENDING",
                            "signal": "buy",
                            "side": "no",
                            "contracts": 1,
                            "limit_price_cents": 30,
                            "current_price_cents": 35,
                            "reserved_cost_cents": 30,
                            "city_code": "NYC",
                            "target_date": today,
                            "strategy": "S1-Weather",
                            "confirmation_verdict": "CONFIRM",
                            "placed_at": datetime.now(timezone.utc).isoformat(),
                            "cycle": 1,
                            "execution_style": "maker",
                            "execution_status": "paper_queued",
                            "status": "resting",
                        },
                    },
                    "history": [],
                    "summary": {},
                    "last_reconciled_at": "",
                    "last_trade_time": None,
                    "daily_date": today,
                    "trade_count_today": 0,
                    "daily_pnl_cents": 0,
                    "total_exposure_cents": 0,
                    "ticker_entry_dates": {},
                    "cycle_log": [],
                }

                result = trader.record_observation_cycle(
                    signals=[{
                        "ticker": "KXHIGHNY-NEW",
                        "city_code": "NYC",
                        "side": "no",
                        "suggested_contracts": 1,
                        "price_cents": 35,
                        "current_price_cents": 35,
                        "risk_price_cents": 30,
                        "limit_price_cents": 30,
                        "execution_style": "maker",
                        "edge": 0.08,
                        "fee_adjusted_edge": 0.05,
                        "strategy": "S1-Weather",
                        "confirmation_verdict": "CONFIRM",
                        "target_date": today,
                    }],
                    cycle=2,
                    balance_cents=10000,
                    market_prices={
                        "KXHIGHNY-PENDING": {"no": 25},
                        "KXHIGHNY-NEW": {"no": 25},
                    },
                    live_risk=None,
                    max_per_cycle=1,
                )

                self.assertEqual(len(result["filled_pending"]), 1)
                self.assertEqual(result["executed"], [])
                self.assertEqual(result["queued"], [])
                self.assertEqual(result["blocked_reasons"]["per_cycle_limit"], 1)
                trader.journal.close()

    def test_observation_paper_shadow_risk_merges_live_exposure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paper_path = os.path.join(temp_dir, "paper_trades.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            risk_path = os.path.join(temp_dir, "risk_state.json")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", recent_events_path), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", recent_decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", recent_cache_path), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path), \
                    patch.object(config, "RISK_STATE_FILE", risk_path):
                trader = ObservationPaperTrader(kalshi_client=None)
                trader.state["active"] = {
                    "KXHIGHCHI-TEST": {
                        "ticker": "KXHIGHCHI-TEST",
                        "city_code": "CHI",
                        "side": "no",
                        "contracts": 1,
                        "entry_price_cents": 25,
                        "cost_cents": 25,
                        "strategy": "S1-Weather",
                        "confirmation_verdict": "CONFIRM",
                    }
                }

                live_risk = RiskManager()
                live_risk.state["positions"] = {
                    "KXHIGHNY-TEST": {
                        "ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "side": "no",
                        "contracts": 2,
                        "price_cents": 20,
                        "cost_cents": 40,
                        "order_status": "executed",
                    }
                }
                live_risk._refresh_exposure()

                shadow = trader._build_shadow_risk(10000, live_risk=live_risk)

                self.assertEqual(shadow.state["total_exposure_cents"], 65)
                self.assertIn("KXHIGHNY-TEST", shadow.state["positions"])
                self.assertIn("KXHIGHCHI-TEST", shadow.state["positions"])
                trader.journal.close()


class ObservationJournalRegressionTests(unittest.TestCase):

    def test_maker_strategy_tracks_taker_orders_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fill_tracking_path = os.path.join(temp_dir, "fill_tracking.json")
            maker_orders_path = os.path.join(temp_dir, "maker_orders.json")
            with patch.object(config, "FILL_TRACKING_FILE", fill_tracking_path), \
                    patch.object(config, "MAKER_ORDERS_FILE", maker_orders_path):
                maker = MakerStrategy(kalshi_client=None, risk_manager=None)
                maker._track_order_side("no", is_taker=True)
                maker._track_order_side("no", is_taker=False)
                maker._track_fill_side("no")
                maker._track_fill_side("no")

                info = maker.get_adverse_selection_info()

                self.assertEqual(len(maker._fill_tracking["no_taker"]), 1)
                self.assertEqual(info["no"]["orders"], 1)
                self.assertEqual(info["no"]["fills"], 1)

    def test_observation_journal_writes_recent_history_and_daily_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            resolved_at = datetime.now(timezone.utc).isoformat()
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=42,
                markets_scanned=120,
                decisions=[
                    {
                        "ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "target_date": today,
                        "signal": "buy",
                        "side": "no",
                        "edge": 0.12,
                        "price_cents": 33,
                        "limit_price": 30,
                        "risk_price_cents": 30,
                        "strategy": "S1-Weather",
                    },
                    {
                        "ticker": "KXHIGHBOS-TEST",
                        "city_code": "BOS",
                        "target_date": today,
                        "signal": "skip",
                        "side": "yes",
                        "skip_reason": "yes_side_blocked",
                    },
                ],
                signals_found=1,
                trades_placed=0,
                skip_counts={"yes_side_blocked": 3, "no_edge": 7},
                null_count=5,
                evaluated_count=90,
                weather_error="",
                observation_mode=True,
                top_signals=[{"ticker": "KXHIGHNY-TEST", "side": "no", "edge": 0.12}],
                paper_result={
                    "executed": [{"ticker": "KXHIGHNY-TEST"}],
                    "filled_pending": [],
                    "queued": [],
                    "expired_pending": [],
                    "blocked_reasons": {"per_cycle_limit": 2},
                },
                settlement_lock_candidates=1,
            )
            journal.log_paper_event(
                "paper_trade_resolved",
                {
                    "ticker": "KXHIGHNY-TEST",
                    "side": "no",
                    "contracts": 1,
                    "strategy": "S1-Weather",
                    "status": "win",
                    "net_profit_cents": 68,
                    "gross_profit_cents": 70,
                    "market_result": "no",
                    "resolved_at": resolved_at,
                },
            )

            history = journal.get_recent_history(hours=72, include_decisions=True)

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 2)
            self.assertEqual(history["summary"]["buy_decisions"], 1)
            self.assertEqual(history["summary"]["paper_resolved"], 1)
            self.assertEqual(history["summary"]["paper_wins"], 1)
            self.assertEqual(history["summary"]["paper_net_profit_cents"], 68)
            by_date = {row["date"]: row for row in history["daily_summary"]}
            scan_day = max(by_date)
            self.assertEqual(by_date[scan_day]["decision_rows"], 2)
            self.assertEqual(by_date[scan_day]["buy_decisions"], 1)
            self.assertEqual(by_date[scan_day]["skip_decisions"], 1)
            self.assertEqual(by_date[scan_day]["paper_entries"], 1)
            self.assertEqual(by_date[scan_day]["skip_reasons"]["yes_side_blocked"], 3)
            self.assertTrue(any(row["paper_resolved"] == 1 for row in history["daily_summary"]))
            self.assertEqual(history["recent_decisions"][0]["execution_status"], "")
            journal.close()

    def test_observation_journal_history_reads_recent_tail_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            with open(events_path, "w", encoding="utf-8") as handle:
                for idx in range(4000):
                    handle.write(
                        '{"timestamp":"%s","event_type":"scan_cycle","cycle":%d,"markets_scanned":1,"signals_found":0,"trades_placed":0,"diag_null":0,"diag_evaluated":1,"diag_skips":{},"paper_entries":0,"paper_filled_pending":0,"paper_resting_orders":0,"paper_expired_pending":0,"paper_blocked_reasons":{},"settlement_lock_candidates":0,"top_signals":[]}\n'
                        % (old_ts, idx)
                    )
                for idx in range(3):
                    handle.write(
                        '{"timestamp":"%s","event_type":"scan_cycle","cycle":%d,"markets_scanned":2,"signals_found":1,"trades_placed":0,"diag_null":0,"diag_evaluated":2,"diag_skips":{"no_edge":1},"paper_entries":0,"paper_filled_pending":0,"paper_resting_orders":0,"paper_expired_pending":0,"paper_blocked_reasons":{},"settlement_lock_candidates":0,"top_signals":[]}\n'
                        % (recent_ts, 5000 + idx)
                    )

            history = journal.get_recent_history(hours=24, event_limit=10, include_decisions=False)

            self.assertEqual(history["summary"]["scan_cycles"], 3)
            self.assertEqual(len(history["recent_events"]), 3)
            self.assertEqual(history["summary"]["skip_reasons"]["no_edge"], 3)
            journal.close()

    def test_observation_journal_fast_mode_uses_jsonl_without_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=decisions_path,
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            now_ts = datetime.now(timezone.utc).isoformat()
            with open(events_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"timestamp":"%s","event_type":"scan_cycle","cycle":1,"markets_scanned":5,"signals_found":1,"trades_placed":0,"diag_null":0,"diag_evaluated":5,"diag_skips":{"no_edge":4},"paper_entries":0,"paper_filled_pending":0,"paper_resting_orders":0,"paper_expired_pending":0,"paper_blocked_reasons":{},"settlement_lock_candidates":0,"top_signals":[]}\n'
                    % now_ts
                )
            with open(decisions_path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"timestamp":"%s","cycle":1,"observation_mode":true,"ticker":"KXHIGHNY-TEST","event_ticker":"KXHIGHNY-TEST","city_code":"NYC","target_date":"2026-03-30","signal":"buy","execution_status":"paper_blocked","side":"no","skip_reason":null,"strategy":"S1-Weather","execution_style":"maker","edge":0.11,"fee_adjusted_edge":0.08,"our_prob":0.75,"market_prob":0.3,"price_cents":30,"entry_price_cents":30,"limit_price_cents":28,"risk_price_cents":28,"yes_price_cents":70,"no_price_cents":30,"predicted_high":48,"todays_high_snapshot":45,"confirmation_verdict":"CONFIRM","market_title":"NYC 47-48","market_subtitle":"47 to 48","strike_type":"between","floor_strike":47,"cap_strike":48}\n'
                    % now_ts
                )

            journal.db.close()
            history = journal.get_recent_history(
                hours=24,
                event_limit=10,
                decision_limit=10,
                include_decisions=True,
                prefer_db=False,
                fast_mode=True,
            )

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 1)
            self.assertEqual(history["recent_decisions"][0]["ticker"], "KXHIGHNY-TEST")
            journal.close()

    def test_observation_journal_cached_history_reads_recent_cache_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=recent_events_path,
                recent_decisions_file=recent_decisions_path,
                recent_cache_file=recent_cache_path,
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=9,
                markets_scanned=12,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "buy",
                    "side": "no",
                    "strategy": "S1-Weather",
                    "execution_status": "paper_queued",
                }],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )
            journal.log_paper_event("paper_trade_resolved", {
                "ticker": "KXHIGHNY-TEST",
                "side": "no",
                "contracts": 1,
                "strategy": "S1-Weather",
                "status": "win",
                "net_profit_cents": 70,
            })

            os.remove(journal.events_file)
            os.remove(journal.decisions_file)
            journal.db.close()

            history = journal.get_recent_history(
                hours=24,
                event_limit=10,
                decision_limit=10,
                include_decisions=True,
                prefer_db=False,
                fast_mode=True,
                cached_only=True,
            )

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["paper_resolved"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 1)
            self.assertEqual(history["recent_decisions"][0]["execution_status"], "paper_queued")
            journal.close()

    def test_observation_journal_get_cached_history_uses_json_cache_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=recent_cache_path,
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=10,
                markets_scanned=4,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "skip",
                    "side": "no",
                    "strategy": "S1-Weather",
                    "skip_reason": "no_edge",
                }],
                signals_found=0,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            os.remove(journal.events_file)
            os.remove(journal.decisions_file)
            os.remove(journal.recent_events_file)
            os.remove(journal.recent_decisions_file)
            journal.db.close()

            history = journal.get_cached_history(
                hours=24,
                event_limit=10,
                decision_limit=10,
                include_decisions=True,
            )

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 1)
            self.assertEqual(history["recent_events"][0]["cycle"], 10)
            journal.close()

    def test_observation_journal_cached_history_summarizes_decisions_without_exposing_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=12,
                markets_scanned=6,
                decisions=[
                    {
                        "ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "target_date": "2026-03-30",
                        "signal": "buy",
                        "side": "no",
                        "strategy": "S4-NextDayNoPaper",
                    },
                    {
                        "ticker": "KXHIGHSEA-TEST",
                        "city_code": "SEA",
                        "target_date": "2026-03-30",
                        "signal": "skip",
                        "side": "no",
                        "strategy": "S1-Weather",
                        "skip_reason": "no_edge",
                    },
                ],
                signals_found=1,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            history = journal.get_cached_history(
                hours=24,
                event_limit=10,
                decision_limit=10,
                include_decisions=False,
            )

            self.assertEqual(history["summary"]["decision_rows"], 2)
            self.assertEqual(history["summary"]["buy_decisions"], 1)
            self.assertEqual(history["summary"]["skip_decisions"], 1)
            self.assertEqual(history["recent_decisions"], [])
            journal.close()

    def test_observation_journal_upgrades_legacy_daily_summary_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            with open(daily_summary_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "updated_at": "2026-03-31T18:00:00+00:00",
                    "days": {
                        "2026-03-31": {
                            "date": "2026-03-31",
                            "scan_cycles": 1,
                            "markets_scanned": 10,
                            "signals_found": 0,
                            "trades_placed_live": 0,
                            "diag_null": 0,
                            "diag_evaluated": 10,
                            "weather_errors": 0,
                            "paper_entries": 0,
                            "paper_filled_pending": 0,
                            "paper_resting_orders": 0,
                            "paper_expired_pending": 0,
                            "paper_resolved": 0,
                            "paper_wins": 0,
                            "paper_losses": 0,
                            "paper_net_profit_cents": 0,
                            "settlement_lock_candidates": 0,
                            "skip_reasons": {"no_edge": 2},
                            "paper_blocked_reasons": {},
                        }
                    },
                }, handle)

            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=daily_summary_path,
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=13,
                markets_scanned=6,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-31",
                    "signal": "buy",
                    "side": "no",
                    "strategy": "S4-NextDayNoPaper",
                }],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )

            rows = journal.load_daily_summary(days=2)
            rows_by_date = {row["date"]: row for row in rows}
            self.assertEqual(rows_by_date["2026-03-31"]["decision_rows"], 0)
            self.assertEqual(rows_by_date["2026-03-31"]["buy_decisions"], 0)
            self.assertEqual(rows_by_date["2026-03-31"]["skip_decisions"], 0)
            today_key = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            self.assertEqual(rows_by_date[today_key]["decision_rows"], 1)
            self.assertEqual(rows_by_date[today_key]["buy_decisions"], 1)
            self.assertEqual(rows_by_date[today_key]["skip_decisions"], 0)
            self.assertEqual(rows_by_date[today_key]["scan_cycles"], 1)
            journal.close()

    def test_observation_journal_skips_recent_mirror_writes_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(config, "OBSERVATION_ENABLE_RECENT_MIRRORS", False):
            recent_events = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache = os.path.join(temp_dir, "observation_recent_cache.json")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=recent_events,
                recent_decisions_file=recent_decisions,
                recent_cache_file=recent_cache,
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=14,
                markets_scanned=4,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-31",
                    "signal": "skip",
                    "side": "no",
                    "strategy": "S1-Weather",
                    "skip_reason": "no_edge",
                }],
                signals_found=0,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            self.assertFalse(os.path.exists(recent_events))
            self.assertFalse(os.path.exists(recent_decisions))
            self.assertFalse(os.path.exists(recent_cache))

            history = journal.get_cached_history(hours=24, include_decisions=True)
            self.assertEqual(history["summary"]["decision_rows"], 1)
            self.assertEqual(history["recent_decisions"][0]["ticker"], "KXHIGHNY-TEST")
            journal.close()

    def test_observation_journal_history_summary_keeps_decision_counts_when_mirrors_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(config, "OBSERVATION_ENABLE_RECENT_MIRRORS", False):
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=16,
                markets_scanned=8,
                decisions=[
                    {
                        "ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "target_date": "2026-03-31",
                        "signal": "buy",
                        "side": "no",
                        "strategy": "S4-NextDayNoPaper",
                    },
                    {
                        "ticker": "KXHIGHSEA-TEST",
                        "city_code": "SEA",
                        "target_date": "2026-03-31",
                        "signal": "skip",
                        "side": "no",
                        "strategy": "S1-Weather",
                        "skip_reason": "no_edge",
                    },
                ],
                signals_found=1,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            history = journal.get_cached_history(
                hours=24,
                event_limit=10,
                decision_limit=10,
                include_decisions=False,
            )

            self.assertEqual(history["summary"]["decision_rows"], 2)
            self.assertEqual(history["summary"]["buy_decisions"], 1)
            self.assertEqual(history["summary"]["skip_decisions"], 1)
            self.assertEqual(history["recent_decisions"], [])
            journal.close()

    def test_observation_journal_startup_housekeeping_removes_disabled_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(config, "OBSERVATION_ENABLE_RECENT_MIRRORS", False), \
                patch.object(config, "OBSERVATION_ENABLE_SQLITE", False):
            recent_events = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache = os.path.join(temp_dir, "observation_recent_cache.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            for path in (recent_events, recent_decisions, recent_cache, db_path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("stale")

            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=recent_events,
                recent_decisions_file=recent_decisions,
                recent_cache_file=recent_cache,
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=db_path,
            )

            self.assertFalse(os.path.exists(recent_events))
            self.assertFalse(os.path.exists(recent_decisions))
            self.assertFalse(os.path.exists(recent_cache))
            self.assertFalse(os.path.exists(db_path))
            journal.close()

    def test_observation_journal_uses_database_when_jsonl_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=decisions_path,
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )

            journal.log_scan_cycle(
                cycle=7,
                markets_scanned=12,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "skip",
                    "side": "no",
                    "skip_reason": "no_edge",
                    "strategy": "S1-Weather",
                }],
                signals_found=0,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            os.remove(events_path)
            os.remove(decisions_path)

            history = journal.get_recent_history(hours=24, include_decisions=True)

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 1)
            self.assertEqual(history["recent_events"][0]["cycle"], 7)
            journal.close()


class StrategyScorecardRegressionTests(unittest.TestCase):

    def test_strategy_scorecards_use_event_counts_without_double_counting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=db_path,
            )
            journal.log_scan_cycle(
                cycle=11,
                markets_scanned=20,
                decisions=[
                    {
                        "ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "target_date": "2026-03-30",
                        "signal": "buy",
                        "side": "no",
                        "strategy": "S1-Weather",
                        "execution_status": "paper_blocked",
                    },
                    {
                        "ticker": "KXHIGHSEA-TEST",
                        "city_code": "SEA",
                        "target_date": "2026-03-30",
                        "signal": "skip",
                        "side": "no",
                        "strategy": "S3-SettlementLock",
                        "skip_reason": "no_edge",
                    },
                ],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )
            journal.log_paper_event("paper_order_queued", {
                "ticker": "KXHIGHNY-TEST",
                "side": "no",
                "contracts": 1,
                "strategy": "S1-Weather",
                "target_date": "2026-03-30",
                "cycle": 11,
                "status": "resting",
                "limit_price_cents": 30,
            })
            journal.log_paper_event("paper_order_filled", {
                "ticker": "KXHIGHNY-TEST",
                "side": "no",
                "contracts": 1,
                "strategy": "S1-Weather",
                "target_date": "2026-03-30",
                "cycle": 11,
                "status": "active",
                "limit_price_cents": 30,
                "entry_price_cents": 29,
            })
            journal.log_paper_event("paper_trade_resolved", {
                "ticker": "KXHIGHNY-TEST",
                "side": "no",
                "contracts": 1,
                "strategy": "S1-Weather",
                "target_date": "2026-03-30",
                "cycle": 11,
                "status": "win",
                "net_profit_cents": 69,
                "gross_profit_cents": 70,
            })

            with patch.object(config, "ALLOW_LIVE_WEATHER_STRATEGY", False), \
                    patch.object(config, "ENABLE_SETTLEMENT_LOCK_STRATEGY", True), \
                    patch.object(config, "ALLOW_LIVE_SETTLEMENT_LOCK_TRADES", False):
                db = BotDatabase(db_path=db_path)
                cards = build_strategy_scorecards(hours=24, db=db)
                db.close()

            weather = next(card for card in cards if card["strategy"] == "S1-Weather")
            self.assertEqual(weather["buy_decisions"], 1)
            self.assertEqual(weather["paper_blocked"], 1)
            self.assertEqual(weather["paper_queued"], 1)
            self.assertEqual(weather["paper_filled"], 1)
            self.assertEqual(weather["paper_resolved"], 1)
            self.assertEqual(weather["fill_rate"], 1.0)
            self.assertIn("live_disabled_by_config", weather["promotion_blockers"])
            journal.close()

    def test_strategy_scorecards_fall_back_to_jsonl_when_sqlite_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(config, "OBSERVATION_ENABLE_SQLITE", False):
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            recent_events_path = os.path.join(temp_dir, "observation_recent_events.jsonl")
            recent_decisions_path = os.path.join(temp_dir, "observation_recent_decisions.jsonl")
            recent_cache_path = os.path.join(temp_dir, "observation_recent_cache.json")
            daily_summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=decisions_path,
                recent_events_file=recent_events_path,
                recent_decisions_file=recent_decisions_path,
                recent_cache_file=recent_cache_path,
                daily_summary_file=daily_summary_path,
                db_path=db_path,
            )
            journal.log_scan_cycle(
                cycle=15,
                markets_scanned=12,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-31",
                    "signal": "buy",
                    "side": "no",
                    "strategy": "S4-NextDayNoPaper",
                    "execution_status": "paper_filled",
                }],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )
            journal.log_paper_event("paper_order_filled", {
                "ticker": "KXHIGHNY-TEST",
                "side": "no",
                "contracts": 1,
                "strategy": "S4-NextDayNoPaper",
            })

            with patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", recent_events_path), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", recent_decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", recent_cache_path), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", daily_summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path):
                cards = build_strategy_scorecards(hours=24)
            s4 = next(card for card in cards if card["strategy"] == "S4-NextDayNoPaper")
            self.assertEqual(s4["buy_decisions"], 1)
            self.assertEqual(s4["paper_filled"], 1)
            journal.close()

    def test_strategy_scorecards_auto_kill_negative_paper_challenger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=db_path,
            )
            journal.log_scan_cycle(
                cycle=21,
                markets_scanned=6,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-31",
                    "signal": "buy",
                    "side": "no",
                    "strategy": "S4-NextDayNoPaper",
                    "execution_status": "paper_filled",
                }],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )
            for idx, pnl in enumerate((-55, -46, -54, 43, 42), start=1):
                journal.log_paper_event("paper_trade_resolved", {
                    "ticker": f"KXHIGHNY-TEST-{idx}",
                    "side": "no",
                    "contracts": 1,
                    "strategy": "S4-NextDayNoPaper",
                    "target_date": "2026-03-31",
                    "net_profit_cents": pnl,
                })

            with patch.object(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", True), \
                    patch.object(config, "PAPER_STRATEGY_AUTOKILL_ENABLED", True), \
                    patch.object(config, "PAPER_STRATEGY_AUTOKILL_MIN_RESOLVED", 5), \
                    patch.object(config, "PAPER_STRATEGY_AUTOKILL_MIN_PROFIT_FACTOR", 1.0), \
                    patch.object(config, "PAPER_STRATEGY_AUTOKILL_MIN_EXPECTANCY_CENTS", 0.0):
                db = BotDatabase(db_path=db_path)
                cards = build_strategy_scorecards(hours=24, db=db)
                db.close()

            s4 = next(card for card in cards if card["strategy"] == "S4-NextDayNoPaper")
            self.assertIn("auto_killed_negative_paper_performance", s4["paper_entry_blockers"])
            self.assertFalse(s4["eligible_for_paper_entries"])
            journal.close()


class ObservationChallengerRegressionTests(unittest.TestCase):

    class DummyWeatherStrategy:
        def __init__(self, signal, shadow_signal=None):
            self.signal = signal
            self.shadow_signal = shadow_signal or signal

        def evaluate_market(self, market, todays_high=None, allow_next_day_directional_override=False):
            row = dict(self.shadow_signal if allow_next_day_directional_override else self.signal)
            row.setdefault("ticker", market.get("ticker", ""))
            row.setdefault("city_code", market.get("_city_code", ""))
            return row

    class DummySettlementLock:
        @staticmethod
        def evaluate_market(market, todays_high=None):
            return None

        @staticmethod
        def build_trade_signal(candidate, market, balance_cents):
            return None

    def test_registry_adds_next_day_no_paper_challenger_in_observation_mode(self):
        signal = {
            "ticker": "KXHIGHDEN-26MAR31-B60.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "skip_reason": "next_day_directional_blocked",
            "price_cents": 67,
            "yes_price_cents": 34,
            "no_price_cents": 67,
            "edge": 0.2239,
            "fee_adjusted_edge": 0.2008,
            "our_prob": 0.1061,
            "market_prob": 0.67,
            "target_date": "2026-03-31",
            "city_code": "DEN",
            "predicted_high": 58.1,
            "strike_type": "between",
            "floor_strike": 60,
            "cap_strike": 61,
        }
        shadow_signal = dict(signal)
        shadow_signal.update({
            "signal": "buy",
            "skip_reason": None,
            "strategy": "S1-Weather",
            "shadow_mode": "next_day_directional_override",
            "confirmation_verdict": "CONFIRM",
            "suggested_contracts": 2,
        })
        registry = StrategyRegistry(
            weather_strategy=self.DummyWeatherStrategy(signal, shadow_signal=shadow_signal),
            settlement_lock=self.DummySettlementLock(),
        )
        market = {"ticker": signal["ticker"], "_city_code": "DEN"}

        with patch.object(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_TIGHT_NEXT_DAY_NO", False), \
                patch.object(config, "PAPER_CHALLENGER_MIN_FEE_ADJ_EDGE", 0.05), \
                patch.object(config, "PAPER_CHALLENGER_MIN_PRICE_CENTS", 35), \
                patch.object(config, "PAPER_CHALLENGER_MAX_PRICE_CENTS", 80), \
                patch.object(config, "PAPER_STRATEGY_STATUS_CACHE_SECONDS", 0):
            result = registry.evaluate_markets(
                [market],
                observed_highs={},
                balance_cents=10000,
                observation_mode=True,
            )
            self.assertEqual(len(result["buy_signals"]), 1)
            self.assertEqual(result["buy_signals"][0]["strategy"], "S4-NextDayNoPaper")
            self.assertEqual(result["buy_signals"][0]["execution_style"], "taker")
            self.assertEqual(result["buy_signals"][0]["paper_shadow_mode"], "next_day_directional_override")
            self.assertEqual(len(result["all_decisions"]), 2)

            live_result = registry.evaluate_markets(
                [market],
                observed_highs={},
                balance_cents=10000,
                observation_mode=False,
            )
            self.assertEqual(live_result["buy_signals"], [])

    def test_registry_adds_tight_next_day_no_paper_challenger_in_observation_mode(self):
        signal = {
            "ticker": "KXHIGHNY-26APR04-B56.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "skip_reason": "next_day_directional_blocked",
            "price_cents": 58,
            "yes_price_cents": 42,
            "no_price_cents": 58,
            "edge": 0.20,
            "fee_adjusted_edge": 0.15,
            "our_prob": 0.22,
            "market_prob": 0.58,
            "target_date": "2026-04-04",
            "city_code": "NYC",
            "predicted_high": 54.1,
            "strike_type": "between",
            "floor_strike": 56,
            "cap_strike": 57,
        }
        shadow_signal = dict(signal)
        shadow_signal.update({
            "signal": "buy",
            "skip_reason": None,
            "strategy": "S1-Weather",
            "shadow_mode": "next_day_directional_override",
            "confirmation_verdict": "CONFIRM",
            "suggested_contracts": 2,
        })
        registry = StrategyRegistry(
            weather_strategy=self.DummyWeatherStrategy(signal, shadow_signal=shadow_signal),
            settlement_lock=self.DummySettlementLock(),
        )
        market = {"ticker": signal["ticker"], "_city_code": "NYC"}

        with patch.object(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", False), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_TIGHT_NEXT_DAY_NO", True), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_PRICE_CENTS", 48), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MAX_PRICE_CENTS", 66), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_EDGE", 0.12), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_FEE_ADJ_EDGE", 0.08), \
                patch.object(config, "PAPER_STRATEGY_STATUS_CACHE_SECONDS", 0):
            result = registry.evaluate_markets(
                [market],
                observed_highs={},
                balance_cents=10000,
                observation_mode=True,
            )

        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["buy_signals"][0]["strategy"], "S6-TightNextDayNoPaper")
        self.assertEqual(result["buy_signals"][0]["execution_style"], "taker")

    def test_tight_next_day_challenger_respects_price_band(self):
        engine = PaperChallengerEngine()
        signal = {
            "ticker": "KXHIGHNY-26APR04-B56.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "skip_reason": "next_day_directional_blocked",
            "strike_type": "between",
        }
        shadow_signal = {
            "ticker": "KXHIGHNY-26APR04-B56.5",
            "strategy": "S1-Weather",
            "signal": "buy",
            "side": "no",
            "strike_type": "between",
            "price_cents": 72,
            "edge": 0.25,
            "fee_adjusted_edge": 0.20,
            "confirmation_verdict": "CONFIRM",
            "shadow_mode": "next_day_directional_override",
        }
        with patch.object(config, "PAPER_CHALLENGER_ALLOW_TIGHT_NEXT_DAY_NO", True), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_PRICE_CENTS", 48), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MAX_PRICE_CENTS", 66), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_EDGE", 0.12), \
                patch.object(config, "PAPER_CHALLENGER_TIGHT_NEXT_DAY_MIN_FEE_ADJ_EDGE", 0.08):
            challenger = engine._build_tight_next_day_no_challenger(
                signal,
                shadow_signal=shadow_signal,
                strategy_statuses=None,
            )
        self.assertIsNone(challenger)

    def test_registry_does_not_add_next_day_challenger_without_shadow_buy(self):
        signal = {
            "ticker": "KXHIGHDEN-26MAR31-B60.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "skip_reason": "next_day_directional_blocked",
            "price_cents": 67,
            "fee_adjusted_edge": 0.20,
            "target_date": "2026-03-31",
            "city_code": "DEN",
            "predicted_high": 58.1,
            "strike_type": "between",
            "floor_strike": 60,
            "cap_strike": 61,
        }
        shadow_skip = dict(signal)
        shadow_skip.update({
            "skip_reason": "confirmation_reject",
            "shadow_mode": "next_day_directional_override",
        })
        registry = StrategyRegistry(
            weather_strategy=self.DummyWeatherStrategy(signal, shadow_signal=shadow_skip),
            settlement_lock=self.DummySettlementLock(),
        )
        market = {"ticker": signal["ticker"], "_city_code": "DEN"}

        with patch.object(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", True), \
                patch.object(config, "PAPER_STRATEGY_STATUS_CACHE_SECONDS", 0):
            result = registry.evaluate_markets(
                [market],
                observed_highs={},
                balance_cents=10000,
                observation_mode=True,
            )
        self.assertEqual(result["buy_signals"], [])
        self.assertEqual(len(result["all_decisions"]), 1)

    def test_finalize_observation_decisions_keeps_same_ticker_different_strategies(self):
        decisions = [
            {
                "ticker": "KXHIGHDEN-26MAR31-B60.5",
                "strategy": "S1-Weather",
                "signal": "skip",
                "side": "no",
                "skip_reason": "next_day_directional_blocked",
                "target_date": "2026-03-31",
            },
            {
                "ticker": "KXHIGHDEN-26MAR31-B60.5",
                "strategy": "S4-NextDayNoPaper",
                "signal": "buy",
                "side": "no",
                "target_date": "2026-03-31",
            },
        ]
        paper_summary = {
            "queued": [{
                "ticker": "KXHIGHDEN-26MAR31-B60.5",
                "strategy": "S4-NextDayNoPaper",
                "side": "no",
                "target_date": "2026-03-31",
                "execution_status": "paper_queued",
            }]
        }

        finalized = _finalize_observation_decisions(decisions, paper_summary)
        self.assertEqual(len(finalized), 2)
        s1 = next(row for row in finalized if row["strategy"] == "S1-Weather")
        s4 = next(row for row in finalized if row["strategy"] == "S4-NextDayNoPaper")
        self.assertEqual(s1["skip_reason"], "next_day_directional_blocked")
        self.assertEqual(s4["execution_status"], "paper_queued")

    def test_soft_settlement_lock_challenger_builds_signal(self):
        engine = PaperChallengerEngine()
        class FixedSoftLockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 4, 3, 15, 0, tzinfo=ZoneInfo("America/New_York"))
                if tz is None:
                    return base.replace(tzinfo=None)
                return base.astimezone(tz)

        today = "2026-04-03"
        signal = {
            "ticker": "KXHIGHTSEA-26MAR30-B52.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "price_cents": 74,
            "yes_price_cents": 27,
            "no_price_cents": 74,
            "target_date": today,
            "city_code": "SEA",
            "strike_type": "between",
            "floor_strike": 52,
            "cap_strike": 53,
        }
        with patch.object(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_SOFT_SETTLEMENT_LOCK", True), \
                patch.object(config, "PAPER_CHALLENGER_SOFT_LOCK_MIN_LOCAL_HOUR", 1), \
                patch.object(config, "PAPER_CHALLENGER_SOFT_LOCK_MAX_PRICE_CENTS", 85), \
                patch("paper_challengers.datetime", FixedSoftLockDateTime):
            challengers = engine.generate({}, signal, todays_high=54, observation_mode=True)
        self.assertEqual(len(challengers), 1)
        self.assertEqual(challengers[0]["strategy"], "S5-SoftSettlementLockPaper")

    def test_registry_blocks_auto_killed_next_day_challenger(self):
        signal = {
            "ticker": "KXHIGHDEN-26MAR31-B60.5",
            "strategy": "S1-Weather",
            "signal": "skip",
            "side": "no",
            "skip_reason": "next_day_directional_blocked",
            "price_cents": 67,
            "yes_price_cents": 34,
            "no_price_cents": 67,
            "edge": 0.2239,
            "fee_adjusted_edge": 0.2008,
            "our_prob": 0.1061,
            "market_prob": 0.67,
            "target_date": "2026-03-31",
            "city_code": "DEN",
            "predicted_high": 58.1,
            "strike_type": "between",
            "floor_strike": 60,
            "cap_strike": 61,
        }
        shadow_signal = dict(signal)
        shadow_signal.update({
            "signal": "buy",
            "skip_reason": None,
            "strategy": "S1-Weather",
            "shadow_mode": "next_day_directional_override",
            "confirmation_verdict": "CONFIRM",
            "suggested_contracts": 2,
        })
        registry = StrategyRegistry(
            weather_strategy=self.DummyWeatherStrategy(signal, shadow_signal=shadow_signal),
            settlement_lock=self.DummySettlementLock(),
        )
        registry._scorecard_cache = {
            "S4-NextDayNoPaper": {
                "strategy": "S4-NextDayNoPaper",
                "paper_entry_enabled": True,
                "paper_entry_blockers": ["auto_killed_negative_paper_performance"],
            }
        }
        registry._scorecard_cache_at = datetime.now(timezone.utc)
        market = {"ticker": signal["ticker"], "_city_code": "DEN"}

        with patch.object(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", True), \
                patch.object(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", True), \
                patch.object(config, "PAPER_STRATEGY_STATUS_CACHE_SECONDS", 3600):
            result = registry.evaluate_markets(
                [market],
                observed_highs={},
                balance_cents=10000,
                observation_mode=True,
            )

        self.assertEqual(result["buy_signals"], [])
        self.assertEqual(len(result["all_decisions"]), 1)


class WeatherFetchWindowRegressionTests(unittest.TestCase):

    class EveningDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 3, 30, 19, 30, tzinfo=ZoneInfo("America/New_York"))
            if tz is None:
                return base.replace(tzinfo=None)
            return base.astimezone(tz)

    @patch("weather_engine.datetime", EveningDateTime)
    def test_off_hours_fetch_override_keeps_weather_engine_live(self):
        with patch.object(config, "ALLOW_OFF_HOURS_FORECAST_FETCH", True):
            self.assertTrue(weather_engine._in_fetch_window())
        with patch.object(config, "ALLOW_OFF_HOURS_FORECAST_FETCH", False):
            self.assertFalse(weather_engine._in_fetch_window())


class ObservationDashboardRegressionTests(unittest.TestCase):

    def test_observation_dashboard_route_serves_html(self):
        server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/observation",
                timeout=10,
            ) as response:
                body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Observation Dashboard", body)
            self.assertIn("window.__OBS_BOOTSTRAP__", body)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        finally:
            server.server_close()
            thread.join(timeout=2)

    def test_observation_strategies_response_includes_daily_pnl_without_sqlite(self):
        with patch.object(config, "OBSERVATION_ENABLE_SQLITE", False), \
                patch.object(dashboard, "build_strategy_scorecards", return_value=[
                    {"strategy": "S1-Weather", "label": "Weather", "net_profit_cents": 0}
                ]):
            payload = dashboard._build_observation_strategies_response(hours=24)

        self.assertIn("generated_at", payload)
        self.assertEqual(len(payload["scorecards"]), 1)
        self.assertEqual(payload["scorecards"][0]["strategy"], "S1-Weather")
        self.assertEqual(payload["scorecards"][0]["daily_pnl"], [])

    def test_root_redirects_to_observation_dashboard(self):
        server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
            try:
                opener.open(f"http://127.0.0.1:{server.server_port}/", timeout=10)
                self.fail("Expected redirect response")
            except urllib.error.HTTPError as response:
                self.assertEqual(response.code, 302)
                self.assertEqual(response.headers.get("Location"), "/observation")
        finally:
            server.server_close()
            thread.join(timeout=2)

    def test_observation_scan_detail_response_handles_sqlite_disabled(self):
        with patch.object(config, "OBSERVATION_ENABLE_SQLITE", False), \
                patch.object(dashboard._observation_journal, "fetch_decisions_by_cycle", return_value=[]):
            payload = dashboard._build_observation_scan_detail_response(cycle=7)

        self.assertEqual(payload["cycle"], 7)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["source"], "jsonl")

    def test_observation_journal_fetch_decisions_by_cycle_reads_recent_jsonl_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=decisions_path,
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )
            now_ts = datetime.now(timezone.utc).isoformat()
            with open(decisions_path, "w", encoding="utf-8") as handle:
                for cycle in (10, 11, 12):
                    for idx in range(3):
                        handle.write(json.dumps({
                            "timestamp": now_ts,
                            "cycle": cycle,
                            "ticker": f"KXHIGHNY-{cycle}-{idx}",
                            "signal": "skip",
                            "city_code": "NYC",
                        }))
                        handle.write("\n")

            rows = journal.fetch_decisions_by_cycle(cycle=11, limit=5)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["cycle"] == 11 for row in rows))
            self.assertEqual(rows[0]["ticker"], "KXHIGHNY-11-0")
            journal.close()

    def test_observation_scan_detail_response_falls_back_to_jsonl_when_sqlite_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_journal = dashboard._observation_journal
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )
            dashboard._observation_journal = journal
            journal.log_scan_cycle(
                cycle=77,
                markets_scanned=3,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-04-04",
                    "signal": "buy",
                    "side": "no",
                    "strategy": "S7-AfternoonNOSweetSpot",
                    "edge": 0.18,
                    "fee_adjusted_edge": 0.14,
                    "our_prob": 0.81,
                    "market_prob": 0.63,
                    "price_cents": 63,
                    "confirmation_verdict": "CONFIRM",
                    "execution_status": "paper_filled",
                }],
                signals_found=1,
                trades_placed=0,
                observation_mode=True,
            )
            try:
                with patch.object(config, "OBSERVATION_ENABLE_SQLITE", False):
                    payload = dashboard._build_observation_scan_detail_response(cycle=77)
                self.assertEqual(payload["source"], "jsonl")
                self.assertEqual(payload["count"], 1)
                self.assertEqual(payload["decisions"][0]["ticker"], "KXHIGHNY-TEST")
            finally:
                dashboard._observation_journal = old_journal
                journal.close()

    def test_settlement_lock_tracks_no_observation_by_et_hour(self):
        paper = SettlementLockPaper(weather_engine=DummyWeather({}, {}, 0.5))
        observed_at = datetime(2026, 4, 4, 18, 0, tzinfo=timezone.utc)

        result = paper.evaluate_market_snapshot(
            market={"ticker": "KXHIGHNY-TEST"},
            todays_high=None,
            observed_at=observed_at,
        )

        self.assertIsNone(result)
        stats = paper.get_eval_stats()
        self.assertEqual(stats["no_observation"], 1)
        self.assertEqual(stats["no_observation_h14"], 1)

    def test_observation_response_includes_strategy_scorecards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bot_status = os.path.join(temp_dir, "bot_status.json")
            risk_state = os.path.join(temp_dir, "risk_state.json")
            scan_log = os.path.join(temp_dir, "scan_log.json")
            paper_trades = os.path.join(temp_dir, "paper_trades.json")
            paper_locks = os.path.join(temp_dir, "paper_locks.json")
            daily_summary = os.path.join(temp_dir, "observation_daily_summary.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")

            for path, payload in (
                (bot_status, {"observation_mode": True, "timestamp": "2026-03-30T18:00:00+00:00"}),
                (risk_state, {"observation_mode": True, "observation_reason": "testing"}),
                (scan_log, {"markets_scanned": 10, "signals_found": 1, "trades_placed": 0}),
                (paper_trades, {"summary": {}, "active": {}, "pending_orders": {}, "history": [], "cycle_log": []}),
                (paper_locks, {"summary": {}, "active": {}}),
                (daily_summary, {"updated_at": "", "days": {}}),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    import json
                    json.dump(payload, handle)

            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=decisions_path,
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=daily_summary,
                db_path=db_path,
            )
            reviewer = TradeReviewer()
            reviewer.state["last_review_date"] = "2026-03-30"
            reviewer.state["last_incremental_review_at"] = "2026-03-30T18:15:00+00:00"
            journal.log_scan_cycle(
                cycle=1,
                markets_scanned=10,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "skip",
                    "side": "no",
                    "strategy": "S1-Weather",
                    "skip_reason": "no_edge",
                }],
                signals_found=0,
                trades_placed=0,
                observation_mode=True,
            )

            with patch.dict(dashboard.STATE_FILES, {
                "bot_status": bot_status,
                "risk": risk_state,
                "scan_log": scan_log,
                "paper_trades": paper_trades,
                "paper_locks": paper_locks,
                "observation_daily_summary": daily_summary,
            }, clear=False), patch.object(dashboard, "_observation_journal", journal), patch.object(dashboard, "_trade_reviewer", reviewer):
                response = dashboard._build_observation_response()

            self.assertIn("strategy_scorecards", response)
            self.assertIn("learning_status", response)
            self.assertEqual(response["learning_status"]["last_review_date"], "2026-03-30")
            self.assertEqual(response["learning_status"]["last_incremental_review_at"], "2026-03-30T18:15:00+00:00")
            self.assertTrue(any(card["strategy"] == "S1-Weather" for card in response["strategy_scorecards"]))
            journal.close()

    def test_observation_history_endpoint_returns_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )
            journal.log_scan_cycle(
                cycle=3,
                markets_scanned=8,
                decisions=[{
                    "ticker": "KXHIGHNY-TEST",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "skip",
                    "side": "no",
                    "strategy": "S1-Weather",
                    "skip_reason": "no_edge",
                }],
                signals_found=0,
                trades_placed=0,
                skip_counts={"no_edge": 1},
                observation_mode=True,
            )

            old_journal = dashboard._observation_journal
            dashboard._observation_journal = journal
            server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/observation/history?hours=24&events=10",
                    timeout=10,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["summary"]["scan_cycles"], 1)
                self.assertEqual(payload["summary"]["decision_rows"], 1)
                self.assertEqual(payload["summary"]["buy_decisions"], 0)
                self.assertEqual(payload["summary"]["skip_decisions"], 1)
                self.assertEqual(len(payload["recent_events"]), 1)
            finally:
                server.server_close()
                thread.join(timeout=2)
                dashboard._observation_journal = old_journal
                journal.close()

    def test_observation_history_endpoint_clamps_query_bounds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_journal = dashboard._observation_journal
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )
            dashboard._observation_journal = journal
            captured = {}

            def fake_history(**kwargs):
                captured.update(kwargs)
                return {"summary": {}, "recent_events": []}

            with patch.object(dashboard._observation_journal, "get_cached_history", side_effect=fake_history):
                server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
                thread = threading.Thread(target=server.handle_request, daemon=True)
                thread.start()
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/api/observation/history?hours=9999&events=9999",
                        timeout=10,
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["summary"], {})
                    self.assertEqual(captured["hours"], 168)
                    self.assertEqual(captured["event_limit"], 500)
                    self.assertFalse(captured["include_decisions"])
                finally:
                    server.server_close()
                    thread.join(timeout=2)
                    dashboard._observation_journal = old_journal
                    journal.close()

    def test_observation_journal_tracks_city_pnl_for_resolved_paper_trades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
                recent_events_file=os.path.join(temp_dir, "observation_recent_events.jsonl"),
                recent_decisions_file=os.path.join(temp_dir, "observation_recent_decisions.jsonl"),
                recent_cache_file=os.path.join(temp_dir, "observation_recent_cache.json"),
                daily_summary_file=os.path.join(temp_dir, "observation_daily_summary.json"),
                db_path=os.path.join(temp_dir, "bot_data.sqlite3"),
            )
            journal.log_paper_event("paper_trade_resolved", {
                "ticker": "KXHIGHNY-TEST",
                "city_code": "NYC",
                "strategy": "S7-AfternoonNOSweetSpot",
                "status": "win",
                "net_profit_cents": 55,
                "target_date": "2026-04-04",
                "resolved_at": "2026-04-04T18:00:00+00:00",
            })
            day = journal.load_daily_summary(days=1)[0]
            self.assertEqual(day["paper_net_profit_cents"], 55)
            self.assertEqual(day["city_pnl"]["NYC"]["wins"], 1)
            self.assertEqual(day["city_pnl"]["NYC"]["pnl_cents"], 55)
            self.assertEqual(day["strategy_city_pnl"]["NYC"]["S7-AfternoonNOSweetSpot"]["wins"], 1)
            journal.close()


class ObservationBackfillRegressionTests(unittest.TestCase):

    def test_event_ticker_backfill_helper_strips_bucket_suffix(self):
        self.assertEqual(
            backfill_observation_db._event_ticker_from_ticker("KXHIGHNY-26MAR16-B54.5"),
            "KXHIGHNY-26MAR16",
        )
        self.assertEqual(
            backfill_observation_db._event_ticker_from_ticker("KXHIGHNY-26MAR16-T55"),
            "KXHIGHNY-26MAR16",
        )

    def test_backfill_creates_db_rows_from_legacy_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            learning_state = os.path.join(temp_dir, "learning_state.json")
            trade_log = os.path.join(temp_dir, "trade_history.json")
            paper_locks = os.path.join(temp_dir, "paper_locks.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")

            with open(learning_state, "w", encoding="utf-8") as handle:
                import json
                json.dump({
                    "scan_snapshots": {
                        "2026-03-20": {
                            "KXHIGHNY-26MAR20-B54.5": {
                                "timestamp": "2026-03-20T15:00:00+00:00",
                                "ticker": "KXHIGHNY-26MAR20-B54.5",
                                "city_code": "NYC",
                                "target_date": "2026-03-20",
                                "signal": "buy",
                                "side": "no",
                                "edge": 0.12,
                                "our_prob": 0.8,
                                "market_prob": 0.3,
                                "price_cents": 30,
                            }
                        }
                    }
                }, handle)

            with open(trade_log, "w", encoding="utf-8") as handle:
                import json
                json.dump([
                    {
                        "timestamp": "2026-03-20T15:05:00+00:00",
                        "settled_at": "2026-03-20T22:00:00+00:00",
                        "ticker": "KXHIGHNY-26MAR20-B54.5",
                        "strategy": "S1-Weather",
                        "entry_type": "buy_fill",
                        "side": "no",
                        "price_cents": 30,
                        "contracts": 1,
                        "cost_cents": 30,
                        "result": "win",
                        "profit_cents": 70,
                        "target_date": "2026-03-20",
                    }
                ], handle)

            with open(paper_locks, "w", encoding="utf-8") as handle:
                import json
                json.dump({
                    "retrospective": {
                        "history": [
                            {
                                "ticker": "KXHIGHNY-26MAR20-B54.5",
                                "snapshot_day": "2026-03-20",
                                "timestamp": "2026-03-20T16:00:00+00:00",
                                "city_code": "NYC",
                                "target_date": "2026-03-20",
                                "title": "NYC 54-55",
                                "subtitle": "54 to 55",
                                "strike_type": "between",
                                "floor_strike": 54,
                                "cap_strike": 55,
                                "actual_high_f": 60,
                                "lock_side": "no",
                                "lock_type": "upper_bound_breached",
                                "signal": "buy",
                                "skip_reason": "",
                                "price_cents": 35,
                                "can_score": True,
                                "estimated_profit_cents": 65,
                            }
                        ]
                    }
                }, handle)

            with patch.object(config, "LEARNING_STATE_FILE", learning_state), \
                    patch.object(config, "TRADE_LOG_FILE", trade_log), \
                    patch.object(config, "PAPER_LOCKS_FILE", paper_locks), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
                    patch.object(config, "OBSERVATION_RECENT_EVENTS_FILE", os.path.join(temp_dir, "observation_recent_events.jsonl")), \
                    patch.object(config, "OBSERVATION_RECENT_DECISIONS_FILE", os.path.join(temp_dir, "observation_recent_decisions.jsonl")), \
                    patch.object(config, "OBSERVATION_RECENT_CACHE_FILE", os.path.join(temp_dir, "observation_recent_cache.json")), \
                    patch.object(config, "OBSERVATION_DAILY_SUMMARY_FILE", summary_path), \
                    patch.object(config, "BOT_DB_FILE", db_path):
                summary = backfill_observation_db.run_backfill(replace=True)

            self.assertEqual(summary["scan_days_backfilled"], 1)
            self.assertEqual(summary["scan_decisions_backfilled"], 1)
            self.assertEqual(summary["legacy_live_fill_events"], 1)
            self.assertEqual(summary["legacy_live_resolved_events"], 1)
            self.assertEqual(summary["retrospective_lock_resolved_events"], 1)

            db = BotDatabase(db_path=db_path)
            self.assertEqual(len(db.fetch_recent_decisions(hours=24 * 365, max_rows=50)), 2)
            events = db.fetch_recent_events(hours=24 * 365, max_rows=50)
            self.assertTrue(any(row.get("event_type") == "paper_trade_resolved" for row in events))
            db.close()

    def test_import_observation_export_rebuilds_local_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = {
                "generated_at": "2026-03-30T18:00:00+00:00",
                "daily_summary": [
                    {
                        "date": "2026-03-30",
                        "scan_cycles": 1,
                        "markets_scanned": 10,
                        "signals_found": 1,
                        "trades_placed_live": 0,
                        "diag_null": 0,
                        "diag_evaluated": 10,
                        "weather_errors": 0,
                        "paper_entries": 0,
                        "paper_filled_pending": 0,
                        "paper_resting_orders": 0,
                        "paper_expired_pending": 0,
                        "paper_resolved": 1,
                        "paper_wins": 1,
                        "paper_losses": 0,
                        "paper_net_profit_cents": 70,
                        "settlement_lock_candidates": 0,
                        "skip_reasons": {"no_edge": 9},
                        "paper_blocked_reasons": {},
                    }
                ],
                "recent_events": [
                    {
                        "timestamp": "2026-03-30T15:00:00+00:00",
                        "event_type": "scan_cycle",
                        "cycle": 9,
                        "observation_mode": True,
                        "markets_scanned": 10,
                        "signals_found": 1,
                        "trades_placed": 0,
                        "diag_null": 0,
                        "diag_evaluated": 10,
                        "diag_skips": {"no_edge": 9},
                        "weather_api_error": "",
                        "paper_entries": 0,
                        "paper_filled_pending": 0,
                        "paper_resting_orders": 0,
                        "paper_expired_pending": 0,
                        "paper_blocked_reasons": {},
                        "settlement_lock_candidates": 0,
                        "top_signals": [{"ticker": "KXHIGHNY-TEST", "side": "no", "edge": 0.11, "strategy": "S1-Weather"}],
                    },
                    {
                        "timestamp": "2026-03-30T22:00:00+00:00",
                        "event_type": "paper_trade_resolved",
                        "ticker": "KXHIGHNY-TEST",
                        "side": "no",
                        "contracts": 1,
                        "strategy": "S1-Weather",
                        "target_date": "2026-03-30",
                        "cycle": 9,
                        "status": "win",
                        "limit_price_cents": 30,
                        "current_price_cents": 0,
                        "entry_price_cents": 30,
                        "reserved_cost_cents": 30,
                        "cost_cents": 30,
                        "gross_profit_cents": 70,
                        "net_profit_cents": 70,
                        "market_result": "no",
                        "confirmation_verdict": "CONFIRM",
                    },
                ],
                "recent_decisions": [
                    {
                        "timestamp": "2026-03-30T15:00:00+00:00",
                        "cycle": 9,
                        "observation_mode": True,
                        "ticker": "KXHIGHNY-TEST",
                        "event_ticker": "KXHIGHNY-TEST",
                        "city_code": "NYC",
                        "target_date": "2026-03-30",
                        "signal": "buy",
                        "execution_status": "paper_blocked",
                        "side": "no",
                        "skip_reason": None,
                        "strategy": "S1-Weather",
                        "execution_style": "maker",
                        "edge": 0.11,
                        "fee_adjusted_edge": 0.08,
                        "our_prob": 0.75,
                        "market_prob": 0.3,
                        "price_cents": 30,
                        "entry_price_cents": 30,
                        "limit_price_cents": 28,
                        "risk_price_cents": 28,
                        "yes_price_cents": 70,
                        "no_price_cents": 30,
                        "predicted_high": 48,
                        "todays_high_snapshot": 45,
                        "confirmation_verdict": "CONFIRM",
                        "market_title": "NYC 47-48",
                        "market_subtitle": "47 to 48",
                        "strike_type": "between",
                        "floor_strike": 47,
                        "cap_strike": 48,
                    }
                ],
            }

            summary = backfill_observation_db.import_observation_export(
                payload,
                replace=True,
                state_dir=temp_dir,
            )

            self.assertEqual(summary["scan_cycles_imported"], 1)
            self.assertEqual(summary["scan_decisions_imported"], 1)
            self.assertEqual(summary["paper_events_imported"], 1)

            db = BotDatabase(db_path=os.path.join(temp_dir, "bot_data.sqlite3"))
            self.assertEqual(len(db.fetch_recent_decisions(hours=24 * 365, max_rows=20)), 1)
            events = db.fetch_recent_events(hours=24 * 365, max_rows=20)
            self.assertTrue(any(row.get("event_type") == "paper_trade_resolved" for row in events))
            db.close()


class SyncLocalRegressionTests(unittest.TestCase):

    def test_sync_local_falls_back_when_export_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = []

            def fake_request(endpoint, token, timeout=30):
                calls.append(endpoint)
                if endpoint.endswith("/api/state"):
                    return {
                        "trades": [],
                        "risk": {},
                        "pnl": {},
                        "bot_status": {},
                        "scan_log": {},
                        "maker": {},
                        "learning": {
                            "scan_snapshots": {
                                "2026-03-20": {
                                    "KXHIGHNY-26MAR20-B54.5": {
                                        "timestamp": "2026-03-20T15:00:00+00:00",
                                        "ticker": "KXHIGHNY-26MAR20-B54.5",
                                        "city_code": "NYC",
                                        "target_date": "2026-03-20",
                                        "signal": "buy",
                                        "side": "no",
                                        "edge": 0.12,
                                        "our_prob": 0.8,
                                        "market_prob": 0.3,
                                        "price_cents": 30,
                                    }
                                }
                            }
                        },
                        "paper_locks": {"retrospective": {"history": []}},
                        "paper_trades": {"history": [], "active": {}, "pending_orders": {}, "cycle_log": [], "summary": {}},
                        "observation_daily_summary": {"updated_at": "", "days": {}},
                    }
                raise RuntimeError("HTTP 502: export unavailable")

            with patch.object(sync_local, "_request_json", side_effect=fake_request):
                sync_local.sync(
                    url="https://example.com",
                    token="token",
                    state_dir=temp_dir,
                    observation_hours=24,
                )

            db = BotDatabase(db_path=os.path.join(temp_dir, "bot_data.sqlite3"))
            self.assertEqual(len(db.fetch_recent_decisions(hours=24 * 365, max_rows=20)), 1)
            db.close()


class TradeReviewerRegressionTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.learning_state = os.path.join(self.temp_dir.name, "learning_state.json")
        self.learning_history = os.path.join(self.temp_dir.name, "learning_history.json")
        self.trade_log = os.path.join(self.temp_dir.name, "trade_history.json")
        self.paper_log = os.path.join(self.temp_dir.name, "paper_trades.json")

        self.patches = [
            patch.object(config, "LEARNING_STATE_FILE", self.learning_state),
            patch.object(config, "LEARNING_HISTORY_FILE", self.learning_history),
            patch.object(config, "TRADE_LOG_FILE", self.trade_log),
            patch.object(config, "PAPER_TRADES_FILE", self.paper_log),
            patch.object(config, "SCAN_SNAPSHOT_SAVE_EVERY_CYCLES", 1),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_load_trade_log_merges_resolved_paper_trades(self):
        with open(self.trade_log, "w", encoding="utf-8") as handle:
            handle.write("[]")
        with open(self.paper_log, "w", encoding="utf-8") as handle:
            handle.write(
                """{
  "history": [
    {
      "ticker": "KXHIGHNY-TEST",
      "side": "no",
      "contracts": 1,
      "entry_price_cents": 32,
      "cost_cents": 32,
      "city_code": "NYC",
      "target_date": "2026-03-30",
      "strategy": "S1-Weather",
      "confirmation_verdict": "CONFIRM",
      "edge": 0.11,
      "predicted_high": 44.5,
      "status": "win",
      "net_profit_cents": 66,
      "gross_profit_cents": 68,
      "estimated_entry_fee_cents": 2,
      "opened_at": "2026-03-30T15:00:00+00:00",
      "resolved_at": "2026-03-30T22:00:00+00:00"
    },
    {
      "ticker": "KXHIGHBOS-TEST",
      "status": "expired_unfilled"
    }
  ]
}"""
            )

        reviewer = TradeReviewer()
        merged = reviewer._load_trade_log(include_paper=True)

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["paper_trade"])
        self.assertEqual(merged[0]["entry_type"], "buy_fill")
        self.assertEqual(merged[0]["profit_cents"], 66)

    def test_capture_forecast_snapshot_replaces_duplicate_ticker(self):
        reviewer = TradeReviewer()
        reviewer.capture_forecast_snapshot({
            "signal": "buy",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "predicted_high": 55.0,
            "model_means": {"gfs": 55.0},
            "price_cents": 40,
            "side": "no",
            "target_date": "2026-03-30",
        })
        reviewer.capture_forecast_snapshot({
            "signal": "buy",
            "ticker": "KXHIGHNY-TEST",
            "city_code": "NYC",
            "predicted_high": 54.0,
            "model_means": {"gfs": 54.0},
            "entry_price_cents": 28,
            "side": "no",
            "target_date": "2026-03-30",
            "execution_status": "paper_filled",
        })

        snapshots = reviewer.state["forecast_snapshots"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["market_price_cents"], 28)
        self.assertEqual(snapshots[0]["execution_status"], "paper_filled")

    def test_reconcile_scans_treats_unfilled_paper_buy_as_missed_not_trade(self):
        reviewer = TradeReviewer()
        reviewer.state["actual_temps"] = {"NYC_2026-03-30": 40}
        reviewer.state["scan_snapshots"] = {
            "2026-03-30": {
                "KXHIGHNY-26MAR30-B50.5": {
                    "ticker": "KXHIGHNY-26MAR30-B50.5",
                    "city_code": "NYC",
                    "target_date": "2026-03-30",
                    "signal": "buy",
                    "execution_status": "paper_queued",
                    "side": "no",
                    "edge": 0.11,
                    "our_prob": 0.8,
                    "market_prob": 0.3,
                    "price_cents": 30,
                    "predicted_high": 44.0,
                    "model_means": {"gfs": 44.0},
                    "skip_reason": None,
                    "confirmation_verdict": "CONFIRM",
                    "timestamp": "2026-03-30T18:00:00+00:00",
                }
            }
        }

        reviewer._reconcile_scans("2026-03-30")

        recon = reviewer.state["scan_reconciliation"][-1]
        self.assertEqual(recon["correct_trades"], 0)
        self.assertEqual(recon["bad_trades"], 0)
        self.assertEqual(recon["missed_opportunities"], 1)
        self.assertEqual(recon["missed_by_guard"]["paper_queued"], 1)


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
