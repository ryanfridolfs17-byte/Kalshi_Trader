import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import dashboard
import config
import backfill_observation_db
from bot_db import BotDatabase
from execution_models import build_entry_order
from kalshi_bot import _prepare_entry_signal
from observation_journal import ObservationJournal
from observation_paper import ObservationPaperTrader
from risk_manager_v2 import RiskManager
from strategy import Strategy
from strategy_registry import build_strategy_scorecards
from trade_reviewer import TradeReviewer


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
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
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
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
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

    def test_observation_paper_shadow_risk_merges_live_exposure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paper_path = os.path.join(temp_dir, "paper_trades.json")
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            summary_path = os.path.join(temp_dir, "observation_daily_summary.json")
            db_path = os.path.join(temp_dir, "bot_data.sqlite3")
            risk_path = os.path.join(temp_dir, "risk_state.json")
            with patch.object(config, "PAPER_TRADES_FILE", paper_path), \
                    patch.object(config, "OBSERVATION_EVENTS_FILE", events_path), \
                    patch.object(config, "SCAN_DECISIONS_FILE", decisions_path), \
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

    def test_observation_journal_writes_recent_history_and_daily_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal = ObservationJournal(
                events_file=os.path.join(temp_dir, "observation_events.jsonl"),
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
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
                        "target_date": "2026-03-30",
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
                        "target_date": "2026-03-30",
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
                    "resolved_at": "2026-03-30T18:00:00+00:00",
                },
            )

            history = journal.get_recent_history(hours=72, include_decisions=True)

            self.assertEqual(history["summary"]["scan_cycles"], 1)
            self.assertEqual(history["summary"]["decision_rows"], 2)
            self.assertEqual(history["summary"]["buy_decisions"], 1)
            self.assertEqual(history["summary"]["paper_resolved"], 1)
            self.assertEqual(history["summary"]["paper_wins"], 1)
            self.assertEqual(history["summary"]["paper_net_profit_cents"], 68)
            self.assertEqual(history["daily_summary"][-1]["paper_entries"], 1)
            self.assertEqual(history["daily_summary"][-1]["paper_resolved"], 1)
            self.assertEqual(history["daily_summary"][-1]["skip_reasons"]["yes_side_blocked"], 3)
            self.assertEqual(history["recent_decisions"][0]["execution_status"], "")
            journal.close()

    def test_observation_journal_history_reads_recent_tail_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=os.path.join(temp_dir, "scan_decisions.jsonl"),
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

    def test_observation_journal_uses_database_when_jsonl_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = os.path.join(temp_dir, "observation_events.jsonl")
            decisions_path = os.path.join(temp_dir, "scan_decisions.jsonl")
            journal = ObservationJournal(
                events_file=events_path,
                decisions_file=decisions_path,
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


class ObservationDashboardRegressionTests(unittest.TestCase):

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
                daily_summary_file=daily_summary,
                db_path=db_path,
            )
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
            }, clear=False), patch.object(dashboard, "_observation_journal", journal):
                response = dashboard._build_observation_response()

            self.assertIn("strategy_scorecards", response)
            self.assertTrue(any(card["strategy"] == "S1-Weather" for card in response["strategy_scorecards"]))
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
