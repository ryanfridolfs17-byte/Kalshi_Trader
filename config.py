"""
KALSHI BOT v4.0 - CONFIGURATION
====================================
Weather + arbitrage trading bot. Rebuilt from scratch.

QUICK START (local):
  1. Set your API_KEY_ID and PRIVATE_KEY_PATH below
  2. Leave ENVIRONMENT = "demo" until confident
  3. Leave DRY_RUN = True to watch without trading
  4. Run: python kalshi_bot.py

RAILWAY DEPLOY:
  Set env vars: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY,
  KALSHI_ENVIRONMENT, KALSHI_DRY_RUN
"""

import os

# === API CREDENTIALS ===
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = "kalshi_private_key.pem"

# Railway env vars mangle PEM newlines. Handle all formats.
_private_key_env = os.environ.get("KALSHI_PRIVATE_KEY", "")
if _private_key_env:
    _key_content = _private_key_env.replace("\\n", "\n")
    if "\n" not in _key_content.strip():
        _stripped = _key_content
        for _tag in ["-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----",
                      "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----",
                      "-----BEGIN EC PRIVATE KEY-----", "-----END EC PRIVATE KEY-----"]:
            _stripped = _stripped.replace(_tag, "")
        _stripped = _stripped.replace(" ", "")
        if "EC PRIVATE" in _key_content:
            _header, _footer = "-----BEGIN EC PRIVATE KEY-----", "-----END EC PRIVATE KEY-----"
        elif "RSA PRIVATE" in _key_content:
            _header, _footer = "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"
        else:
            _header, _footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
        _lines = [_stripped[i:i+64] for i in range(0, len(_stripped), 64)]
        _key_content = _header + "\n" + "\n".join(_lines) + "\n" + _footer + "\n"
    with open(PRIVATE_KEY_PATH, "w") as _f:
        _f.write(_key_content)

# === ENVIRONMENT ===
ENVIRONMENT = os.environ.get("KALSHI_ENVIRONMENT", "demo")
DRY_RUN = os.environ.get("KALSHI_DRY_RUN", "true").lower() == "true"

# === OPEN-METEO API KEY (optional, removes rate limits) ===
# Free tier: 10,000 requests/day per IP. Railway shared IPs hit this easily.
# $20/month at https://open-meteo.com/en/pricing — set env var OPEN_METEO_API_KEY.
OPEN_METEO_API_KEY = os.environ.get("OPEN_METEO_API_KEY", "")

# === SCAN SETTINGS ===
SCAN_INTERVAL = 120
PEAK_SCAN_INTERVAL = 60
PEAK_SCAN_START_ET = 12
PEAK_SCAN_END_ET = 17

WEATHER_CITIES = [
    "NYC", "CHI", "MIA", "AUS",
    "LAX", "DEN", "PHI", "ATL",
    "BOS", "DAL", "DC", "HOU",
    "LV", "MIN", "NOLA", "OKC",
    "PHX", "SATX", "SEA", "SFO",
]

# === RISK LIMITS (cents, 100 = $1.00) ===
DAILY_LOSS_LIMIT_CENTS = 600
ROLLING_DRAWDOWN_LIMIT_PCT = 0.25   # Block trading if 5-day rolling P&L <= -25% of balance
MAX_PER_REGION_PCT = 0.15   # Max 15% of balance per weather region

CITY_REGIONS = {
    "northeast": ["NYC", "BOS", "PHI", "DC"],
    "southeast": ["MIA", "ATL", "NOLA"],
    "south_central": ["AUS", "DAL", "HOU", "SATX", "OKC"],
    "mountain_west": ["DEN", "LV", "PHX"],
    "west_coast": ["LAX", "SFO", "SEA"],
    "midwest": ["CHI", "MIN"],
}
MAX_TOTAL_EXPOSURE_PCT = 0.50
MAX_POSITION_PCT = 0.05
CONFIRMED_POSITION_PCT = 0.10
ARB_POSITION_PCT = 0.15
MAX_PER_TICKER_CENTS = 400
MAX_CONTRACTS_PER_TICKER = 5
MAX_OPEN_POSITIONS = 6
MAX_PER_CITY_PCT = 0.10
MAX_CORRELATED_POSITIONS = 2
CONSECUTIVE_LOSS_PAUSE = 3
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60
TRADE_COOLDOWN = 120
SETTLEMENT_PROXIMITY_HOURS = 2
SETTLEMENT_PROXIMITY_EDGE_OVERRIDE = 0.20
LIQUIDITY_RESERVE_PCT = 0.20
RESTING_ORDER_TIMEOUT = 1500
RESTING_EXIT_TIMEOUT = 1800
KILL_SWITCH_DAILY_LOSS = True
KILL_SWITCH_CONSEC_LOSSES = 3
KILL_SWITCH_PAUSE_HOURS = 4

# === EDGE & PRICING ===
MIN_EDGE = 0.06
CONFIRMED_MIN_EDGE = 0.05
CASE1_MIN_EDGE = 0.02       # CASE 1 only: obs already exceeded bucket, near-guaranteed
ARB_MIN_SPREAD_CENTS = 7
ENABLE_ARBITRAGE_STRATEGY = False
KALSHI_FEE_PCT = 0.07
FEE_ADJUSTED_MIN_EDGE = 0.03
CASE1_FEE_ADJUSTED_MIN_EDGE = 0.005  # Was 0.01. Confirmed outcomes: near-guaranteed, accept thinner edge.
LONGSHOT_FLOOR_CENTS = 30  # Was 3. YES <30c: 4W/19L (-$14.71). Favourite-longshot bias confirmed by Whelan 2025.
NEAR_CERTAINTY_CAP_CENTS = 80  # Was 93. Buying YES >80c = terrible risk/reward with 5-8F model MAE.
NO_SIDE_MAX_PRICE_CENTS = 75       # Was 35. NO at 75c+: 29W/0L (+$20.52) — our most profitable segment, was blocked!
NO_SIDE_SIZING_MULTIPLIER = 0.60   # Was 0.30. 29-0 track record at high NO prices — increase sizing.
NEXT_DAY_EDGE_MULTIPLIER = 1.5
EARLY_MORNING_EDGE_MULTIPLIER = 2.0  # 6-9 AM local: 14% min edge (stale 00Z forecasts)
NEXT_DAY_SIZING_MULTIPLIER = 0.50
MIN_PAYOUT_DOLLARS = 0.10

# === CITY-SPECIFIC EDGE MULTIPLIERS ===
# Data-driven: cities with negative P&L get higher edge thresholds.
# 1.0 = no change, 1.5 = 50% higher min edge for that city.
CITY_EDGE_MULTIPLIERS = {
    "PHI": 2.0,   # Worst bias: -7.86F ensemble miscalibration
    "DAL": 1.5,   # -$20.81
    "AUS": 1.5,   # -$19.49
    "HOU": 1.5,   # -5.5F bias
    "PHX": 1.3,   # -$16.13
    "DEN": 1.3,   # -$15.08
    "DC":  1.3,   # -$14.39
    "CHI": 1.2,   # -$8.61
}

# === LEARNING AUTO-APPLICATION ===
LEARNING_AUTO_APPLY = True           # Kill switch: False disables all auto-applied learning
LEARNING_MIN_DATA_POINTS = 5         # Min data points before applying learned bias (was 3 — too noisy)
MAX_BIAS_CORRECTION_F = 3.0          # Cap learned bias at ±3F (backtest: no city has >2.2F true bias)

# === BACKTEST-DERIVED MODEL WEIGHTS (365 days × 20 cities, March 2026) ===
# GFS: 1.8F MAE, 0.0 bias. ECMWF: 3.0F MAE. GEM: 2.8F MAE. ICON: no backtest data.
# Used when no per-city learned weights available. Learned weights override when sufficient data.
DEFAULT_MODEL_WEIGHTS = {
    "gfs_ensemble": 0.375,   # 1.8x weight of others (best model for all 20 cities)
    "ecmwf_ifs": 0.208,
    "icon_eps": 0.208,       # No backtest data; assume ECMWF-equivalent
    "gem_ensemble": 0.208,
}

# === SEASONAL EDGE MULTIPLIERS (backtest: March 2.7F MAE vs Oct 2.0F) ===
# Spring is structurally harder to forecast (frontal systems).
SEASONAL_EDGE_MULTIPLIERS = {
    3: 1.2,   # March: hardest month
    4: 1.1,   # April
    5: 1.1,   # May
}

# === CITY BIAS SAFETY GATE ===
CITY_BIAS_BLOCK_THRESHOLD_F = 5.0   # Block city if |learned bias| > 5F
CITY_BIAS_BLOCK_MIN_COUNT = 5       # Require >= 5 data points before blocking

# === NWS & FORECAST GUARDS ===
ROUNDING_BUFFER_HARD_F = 1
ROUNDING_BUFFER_SOFT_F = 2
NO_SEPARATION_FLOOR_F = 2.0
NO_SEPARATION_STD_DEV_MULT = 0.6
MAX_MODEL_DIVERGENCE_YES_F = 8
MAX_MODEL_DIVERGENCE_NO_F = 10
MODEL_CONVERGENCE_BOOST_F = 2
CONFIRM_NO_SEPARATION_PENALTY = 1.25

# === OPEN-METEO RATE LIMITING ===
# Free tier: 10K requests/day per IP. Railway shared IPs exhaust this.
# Window: only fetch ensemble data during active trading hours.
# Outside window: bot still runs (exits use METAR/NWS, not Open-Meteo).
OPEN_METEO_FETCH_START_ET = 7    # Was 8. Catch early West Coast CASE 1 opportunities.
OPEN_METEO_FETCH_END_ET = 19     # Was 18. Late East Coast convergence trades.
ENSEMBLE_CACHE_TTL = 3600         # 60 min (models update every 6h — 15 min was 9x too aggressive)
DISTRIBUTION_CACHE_TTL = 3600     # 60 min (matches ensemble)
CLOUD_COVER_CACHE_TTL = 7200      # 120 min (daily data, changes once/day)

# === METAR OBSERVATION SOURCE ===
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
METAR_CACHE_TTL_SEC = 90           # Slightly under 2-min cycle for fresh data each cycle
METAR_REQUEST_TIMEOUT = 10         # Seconds
METAR_HOURS_LOOKBACK = 18          # Hours of METAR history to fetch (covers full day)
METAR_ENABLED = True               # Kill switch to fall back to NWS-only

# Confirmed outcome settings
CASE1_MIN_LOCAL_HOUR = 9  # Was 10. Gain 1h of CASE 1 detection for early southern city spikes.
CASE3_GAP_THRESHOLDS = {
    16: 2, 15: 4, 14: 6, 13: 7, 12: 8, 11: 12, 10: 15,
}
CASE3_COOLING_REQUIRED_BEFORE_HOUR = 14
CASE3_ENSEMBLE_VETO_GAP_LATE = 3
CASE3_ENSEMBLE_VETO_GAP_DEFAULT = 5

# === MAKER STRATEGY ===
MAKER_SPREAD_BUFFER_CENTS = 2
STALE_ORDER_MINUTES = 30
MAX_OPEN_ORDERS = 5
ADVERSE_SELECTION_PAUSE_MINUTES = 60
# === CONVERGENCE CONFIDENCE ===
CONVERGENCE_SCORE_THRESHOLD = 0.7  # Was 0.5. No performance data; require tighter obs tracking.
CONVERGENCE_MIN_LOCAL_HOUR = 12  # Was 14. Start at noon city-local time when METAR obs available.
CONVERGENCE_SIZING_BOOST = 0.5  # Was 0.3 (1.3x max). Now 1.5x max. Reward convergence confidence.
# Convergence RE-ENABLES edge threshold lowering (5% instead of 6%+) when score > threshold.
AFTERNOON_CONFIRM_MIN_EDGE = 0.04  # 4% edge: afternoon + CONFIRM verdict only

# === CLOUD COVER BIAS ===
CLOUD_COVER_THRESHOLD_PCT = 70
CLOUD_COVER_TEMP_BIAS_F = -1.5
PRECIP_THRESHOLD_MM = 0.5
PRECIP_TEMP_BIAS_F = -1.0
WIND_SPEED_THRESHOLD_KMH = 32    # ~20 mph; above this, boundary layer mixing suppresses highs
WIND_SPEED_TEMP_BIAS_F = -1.0    # Applied when wind exceeds threshold
WEATHER_BIAS_CAP_F = -2.0        # Max total weather adjustment (cloud+precip+wind). Prevents -3.5F stacking.
CASE1_NO_PRICE_CAP = 70          # Was 60. At 70c: pay 70c win 30c on confirmed = 40% return.

# === PORTFOLIO REBALANCING ===
REBALANCE_INTERVAL_CYCLES = 15
REBALANCE_MIN_NEW_EDGE = 0.15
REBALANCE_MAX_OLD_EDGE = 0.03

# === PROFIT PROTECTION EXITS ===
PROFIT_EXIT_PROB_DROP = 0.15        # Exit if current_prob drops 15%+ below entry_prob
PROFIT_EXIT_MIN_PROFIT_PCT = 0.50   # Position must be up >= 50% from entry to trigger
PROFIT_EXIT_PEAK_DROP_PCT = 0.20    # Safety net: exit if price drops 20% from peak
PROFIT_EXIT_MIN_PEAK_CENTS = 20     # Peak must reach 20c+ (ignore penny noise)

# === PROFIT-TAKING EXIT ===
# Rule 8: Sell when up big AND forecast says we're on the wrong side.
PROFIT_TAKE_MIN_GAIN_PCT = 1.0    # 100% gain minimum to consider profit-taking
PROFIT_TAKE_PROB_TIER1 = 0.50     # Up 100%+: sell if forecast prob < 50% (wrong side)
PROFIT_TAKE_PROB_TIER2 = 0.65     # Up 200%+: sell if forecast prob < 65% (marginal side)

# === TAKER MODE ===
TAKER_MODE_MIN_EDGE = 0.15
STRONG_TAKER_MIN_FEE_ADJ_EDGE = 0.20  # Was 0.12 hardcoded. STRONG verdict 7% win rate — not reliable for taker.

# === BUCKET INCONSISTENCY DETECTION ===
BUCKET_SUM_DEVIATION_CENTS = 8   # Min deviation from 100c to flag
BUCKET_SUM_MIN_MARKETS = 5      # Min buckets in event to analyze


# === ACCOUNT ===
TOTAL_DEPOSITS_CENTS = 10000
BALANCE_FALLBACK_CENTS = 4800  # Conservative fallback when API unavailable (~$48 bankroll)
SETTLEMENT_HOUR_ET = 10

# === STATE PERSISTENCE ===
STATE_DIR = os.environ.get("STATE_DIR", ".")
os.makedirs(STATE_DIR, exist_ok=True)

LEARNING_HISTORY_FILE = os.path.join(STATE_DIR, "learning_history.json")
SCAN_SNAPSHOT_RETENTION_DAYS = 30
SCAN_SNAPSHOT_SAVE_EVERY_CYCLES = int(
    os.environ.get("SCAN_SNAPSHOT_SAVE_EVERY_CYCLES", "1")
)
LEARNING_INCREMENTAL_INTERVAL_MINUTES = int(
    os.environ.get("LEARNING_INCREMENTAL_INTERVAL_MINUTES", "15")
)

TRADE_LOG_FILE = os.path.join(STATE_DIR, "trade_history.json")
RISK_STATE_FILE = os.path.join(STATE_DIR, "risk_state.json")
PNL_HISTORY_FILE = os.path.join(STATE_DIR, "pnl_history.json")
BOT_STATUS_FILE = os.path.join(STATE_DIR, "bot_status.json")
MAKER_ORDERS_FILE = os.path.join(STATE_DIR, "maker_orders.json")
SCAN_LOG_FILE = os.path.join(STATE_DIR, "scan_log.json")
LEARNING_STATE_FILE = os.path.join(STATE_DIR, "learning_state.json")
FILL_TRACKING_FILE = os.path.join(STATE_DIR, "fill_tracking.json")
PAPER_LOCKS_FILE = os.path.join(STATE_DIR, "paper_locks.json")
PAPER_TRADES_FILE = os.path.join(STATE_DIR, "paper_trades.json")
OBSERVATION_EVENTS_FILE = os.path.join(STATE_DIR, "observation_events.jsonl")
SCAN_DECISIONS_FILE = os.path.join(STATE_DIR, "scan_decisions.jsonl")
OBSERVATION_RECENT_EVENTS_FILE = os.path.join(STATE_DIR, "observation_recent_events.jsonl")
OBSERVATION_RECENT_DECISIONS_FILE = os.path.join(STATE_DIR, "observation_recent_decisions.jsonl")
OBSERVATION_RECENT_CACHE_FILE = os.path.join(STATE_DIR, "observation_recent_cache.json")
OBSERVATION_DAILY_SUMMARY_FILE = os.path.join(STATE_DIR, "observation_daily_summary.json")
BOT_DB_FILE = os.path.join(STATE_DIR, "bot_data.sqlite3")
OBSERVATION_SUMMARY_RETENTION_DAYS = int(
    os.environ.get("OBSERVATION_SUMMARY_RETENTION_DAYS", "30")
)
OBSERVATION_RECENT_EVENT_LIMIT = int(
    os.environ.get("OBSERVATION_RECENT_EVENT_LIMIT", "1500")
)
OBSERVATION_RECENT_DECISION_LIMIT = int(
    os.environ.get("OBSERVATION_RECENT_DECISION_LIMIT", "6000")
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
BOT_VERSION = os.environ.get("BOT_VERSION", "4.1-fill-audit")
STRATEGY_SCORECARD_WINDOW_HOURS = int(
    os.environ.get("STRATEGY_SCORECARD_WINDOW_HOURS", str(24 * 14))
)
ALLOW_LIVE_WEATHER_STRATEGY = os.environ.get(
    "ALLOW_LIVE_WEATHER_STRATEGY",
    "false"
).lower() == "true"

# Explicit production kill switches for historically losing paths.
ALLOW_YES_SIDE_TRADES = os.environ.get("ALLOW_YES_SIDE_TRADES", "false").lower() == "true"
ALLOW_STRONG_VERDICTS = os.environ.get("ALLOW_STRONG_VERDICTS", "false").lower() == "true"
ALLOW_NEXT_DAY_DIRECTIONAL_TRADES = os.environ.get("ALLOW_NEXT_DAY_DIRECTIONAL_TRADES", "false").lower() == "true"
ALLOW_THRESHOLD_DIRECTIONAL_TRADES = os.environ.get("ALLOW_THRESHOLD_DIRECTIONAL_TRADES", "false").lower() == "true"
AUTO_OBSERVATION_MODE = os.environ.get(
    "AUTO_OBSERVATION_MODE",
    "true" if ENVIRONMENT == "production" else "false"
).lower() == "true"
AUTO_OBSERVATION_REASON = os.environ.get(
    "AUTO_OBSERVATION_REASON",
    "Safety lock enabled pending manual review/deploy verification."
)
SETTLEMENT_LOCK_MIN_PAYOUT_CENTS = int(os.environ.get("SETTLEMENT_LOCK_MIN_PAYOUT_CENTS", "8"))
ENABLE_SETTLEMENT_LOCK_STRATEGY = os.environ.get("ENABLE_SETTLEMENT_LOCK_STRATEGY", "true").lower() == "true"
ALLOW_LIVE_SETTLEMENT_LOCK_TRADES = os.environ.get("ALLOW_LIVE_SETTLEMENT_LOCK_TRADES", "false").lower() == "true"
ALLOW_SETTLEMENT_LOCK_YES = os.environ.get("ALLOW_SETTLEMENT_LOCK_YES", "false").lower() == "true"
SETTLEMENT_LOCK_MIN_LOCAL_HOUR = int(os.environ.get("SETTLEMENT_LOCK_MIN_LOCAL_HOUR", "10"))
SETTLEMENT_LOCK_MAX_PRICE_CENTS = int(os.environ.get("SETTLEMENT_LOCK_MAX_PRICE_CENTS", "80"))
SETTLEMENT_LOCK_MAX_POSITION_PCT = float(os.environ.get("SETTLEMENT_LOCK_MAX_POSITION_PCT", "0.03"))

# === API URLS ===
DEMO_API_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_API_URL = "https://api.elections.kalshi.com/trade-api/v2"

# === HEALTH MONITORING ===
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
HEALTH_STALE_MINUTES = 15
HEALTH_WARN_MINUTES = 10

# === DASHBOARD AUTH ===
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")  # Bearer token; empty = no auth (local dev)


def atomic_json_save(filepath, data, indent=2):
    """Write JSON atomically: write to temp, then os.replace()."""
    import json as _json
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            _json.dump(data, f, indent=indent)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
