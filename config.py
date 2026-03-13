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
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "7bac3bd2-e6b5-4859-93ff-26ee15f2c249")
PRIVATE_KEY_PATH = "kalshi_private_key.pem"

# Railway env vars mangle PEM newlines. Handle all formats.
_private_key_env = os.environ.get("KALSHI_PRIVATE_KEY", "")
if _private_key_env:
    _key_content = _private_key_env.replace("\n", "\n")
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
MAX_TOTAL_EXPOSURE_PCT = 0.40
MAX_POSITION_PCT = 0.05
CONFIRMED_POSITION_PCT = 0.10
ARB_POSITION_PCT = 0.15
MAX_PER_TICKER_CENTS = 400
MAX_CONTRACTS_PER_TICKER = 5
MAX_OPEN_POSITIONS = 3
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
MIN_EDGE = 0.07
CONFIRMED_MIN_EDGE = 0.05
ARB_MIN_SPREAD_CENTS = 7
ENABLE_ARBITRAGE_STRATEGY = False
KALSHI_FEE_PCT = 0.07
FEE_ADJUSTED_MIN_EDGE = 0.03
LONGSHOT_FLOOR_CENTS = 5
NEAR_CERTAINTY_CAP_CENTS = 88
NO_SIDE_MAX_PRICE_CENTS = 60
NO_SIDE_SIZING_MULTIPLIER = 0.40
NEXT_DAY_EDGE_MULTIPLIER = 1.5
NEXT_DAY_SIZING_MULTIPLIER = 0.50
MIN_PAYOUT_DOLLARS = 0.25

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
OPEN_METEO_FETCH_START_ET = 8    # 8 AM ET — covers 6 AM Central/Eastern
OPEN_METEO_FETCH_END_ET = 18     # 6 PM ET — after this, exits only
ENSEMBLE_CACHE_TTL = 900          # 15 min (models update every 6h)
DISTRIBUTION_CACHE_TTL = 900      # 15 min (matches ensemble)
CLOUD_COVER_CACHE_TTL = 1800      # 30 min (daily data, changes slowly)

# === METAR OBSERVATION SOURCE ===
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
METAR_CACHE_TTL_SEC = 90           # Slightly under 2-min cycle for fresh data each cycle
METAR_REQUEST_TIMEOUT = 10         # Seconds
METAR_HOURS_LOOKBACK = 18          # Hours of METAR history to fetch (covers full day)
METAR_ENABLED = True               # Kill switch to fall back to NWS-only

# Confirmed outcome settings
CASE1_MIN_LOCAL_HOUR = 10
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
CONVERGENCE_SCORE_THRESHOLD = 0.7
CONVERGENCE_MIN_LOCAL_HOUR = 14
CONVERGENCE_SIZING_BOOST = 0.5  # up to 1.5x sizing

# === CLOUD COVER BIAS ===
CLOUD_COVER_THRESHOLD_PCT = 70
CLOUD_COVER_TEMP_BIAS_F = -1.5
PRECIP_THRESHOLD_MM = 0.5
PRECIP_TEMP_BIAS_F = -1.0

# === PORTFOLIO REBALANCING ===
REBALANCE_INTERVAL_CYCLES = 15
REBALANCE_MIN_NEW_EDGE = 0.15
REBALANCE_MAX_OLD_EDGE = 0.03

# === TAKER MODE ===
TAKER_MODE_MIN_EDGE = 0.15

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

TRADE_LOG_FILE = os.path.join(STATE_DIR, "trade_history.json")
RISK_STATE_FILE = os.path.join(STATE_DIR, "risk_state.json")
PNL_HISTORY_FILE = os.path.join(STATE_DIR, "pnl_history.json")
BOT_STATUS_FILE = os.path.join(STATE_DIR, "bot_status.json")
MAKER_ORDERS_FILE = os.path.join(STATE_DIR, "maker_orders.json")
SCAN_LOG_FILE = os.path.join(STATE_DIR, "scan_log.json")
LEARNING_STATE_FILE = os.path.join(STATE_DIR, "learning_state.json")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

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
