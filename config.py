"""
KALSHI BOT v3.0 — CONFIGURATION
====================================
Weather-focused trading bot with ensemble forecast edge.

QUICK START (local):
  1. Set your API_KEY_ID and PRIVATE_KEY_PATH below
  2. Leave ENVIRONMENT = "demo" until you're confident
  3. Leave DRY_RUN = True to watch without trading
  4. Run: python kalshi_bot.py

RAILWAY DEPLOY:
  Set these env vars in the Railway dashboard:
    KALSHI_API_KEY_ID    — your Kalshi API key
    KALSHI_PRIVATE_KEY   — full PEM key contents (paste the whole key)
    KALSHI_ENVIRONMENT   — "demo" or "production"
    KALSHI_DRY_RUN       — "true" or "false"
"""

import os

# ═══════════════════════════════════════════════════════
# API CREDENTIALS
# ═══════════════════════════════════════════════════════
# Reads from env var first (for Railway), falls back to hardcoded (for local)
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "YOUR_API_KEY_ID_HERE")
PRIVATE_KEY_PATH = "kalshi_private_key.pem"

# If the full PEM key is in an env var (Railway), write it to a file at startup.
# Railway env vars mangle newlines in PEM keys. Handle both formats:
#   - Literal \n characters (Railway pastes them as backslash-n)
#   - Actual newlines (if preserved)
#   - Single-line blob with no newlines at all
_private_key_env = os.environ.get("KALSHI_PRIVATE_KEY", "")
if _private_key_env:
    # Replace literal \n with actual newlines
    _key_content = _private_key_env.replace("\\n", "\n")

    # If no newlines exist at all (fully mangled), try to reconstruct PEM format
    if "\n" not in _key_content.strip():
        # Strip PEM headers/footers if present, then re-add with proper formatting
        _stripped = _key_content
        for _tag in ["-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----",
                      "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----",
                      "-----BEGIN EC PRIVATE KEY-----", "-----END EC PRIVATE KEY-----"]:
            _stripped = _stripped.replace(_tag, "")
        _stripped = _stripped.replace(" ", "")

        # Detect key type from original content
        if "EC PRIVATE" in _key_content:
            _header, _footer = "-----BEGIN EC PRIVATE KEY-----", "-----END EC PRIVATE KEY-----"
        elif "RSA PRIVATE" in _key_content:
            _header, _footer = "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"
        else:
            _header, _footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"

        # Split base64 into 64-char lines (PEM standard)
        _lines = [_stripped[i:i+64] for i in range(0, len(_stripped), 64)]
        _key_content = _header + "\n" + "\n".join(_lines) + "\n" + _footer + "\n"

    with open(PRIVATE_KEY_PATH, "w") as _f:
        _f.write(_key_content)

# ═══════════════════════════════════════════════════════
# ENVIRONMENT
# ═══════════════════════════════════════════════════════
# "demo" = practice with fake money (demo-api.kalshi.co)
# "production" = real money (api.elections.kalshi.com)
ENVIRONMENT = os.environ.get("KALSHI_ENVIRONMENT", "demo")

# True = analyze only, don't place any orders (safest start)
# False = actually place orders
DRY_RUN = os.environ.get("KALSHI_DRY_RUN", "true").lower() == "true"

# ═══════════════════════════════════════════════════════
# SCAN SETTINGS
# ═══════════════════════════════════════════════════════
# How often to scan for trades (seconds)
# 2 minutes catches confirmed outcomes and arbitrage quickly.
# Ensemble data is cached (not re-fetched every scan).
SCAN_INTERVAL = 120  # 2 minutes

# Peak-hour scanning: faster scans during afternoon temperature peaks (12-5 PM ET)
PEAK_SCAN_INTERVAL = 60   # 1 minute during peak hours
PEAK_SCAN_START_ET = 12   # Noon ET
PEAK_SCAN_END_ET = 17     # 5 PM ET

# Which cities to trade weather for
WEATHER_CITIES = [
    "NYC", "CHI", "MIA", "AUS",           # Original 4
    "LAX", "DEN", "PHI", "ATL",           # Expansion wave 1
    "BOS", "DAL", "DC", "HOU",            # Expansion wave 2
    "LV", "MIN", "NOLA", "OKC",           # Expansion wave 3
    "PHX", "SATX", "SEA", "SFO",          # Expansion wave 4
]

# Also scan non-weather markets for arbitrage?
SCAN_ALL_FOR_ARBITRAGE = True

# ═══════════════════════════════════════════════════════
# MARKET TYPES (toggle each independently via env var)
# ═══════════════════════════════════════════════════════
# Each market type can be enabled/disabled independently.
# Weather is ON by default. All others are OFF by default.
MARKET_TYPES = {
    "weather": os.environ.get("ENABLE_WEATHER", "true").lower() == "true",
    "sp500":   os.environ.get("ENABLE_SP500", "false").lower() == "true",
    # Future: "cpi", "oil", "bitcoin", "nfp", "fed"
}

# S&P 500 bracket settings
SP500_SERIES_TICKER = "KXINX"        # Kalshi series ticker for S&P 500 daily brackets
SP500_MIN_EDGE = 0.06                # 6% minimum edge (tighter than weather due to higher liquidity)
SP500_VIX_CACHE_MINUTES = 15         # Cache VIX/SPY data for this many minutes
SP500_MAX_EXPOSURE_CENTS = 800       # $8.00 max exposure for SP500 positions

# ═══════════════════════════════════════════════════════
# RISK PARAMETERS (all in cents, 100 cents = $1.00)
# ═══════════════════════════════════════════════════════
# Max cost of a single auto-trade (no approval needed)
MAX_AUTO_TRADE_CENTS = 2000         # $20.00

# Never invest more than this % of total capital into a single position
MAX_POSITION_PCT = 0.20             # 20% of bankroll

# Confirmed outcome trades (near-guaranteed) can size bigger
CONFIRMED_OUTCOME_POSITION_PCT = 0.25  # 25% of bankroll (CASE 1: NO on exceeded buckets)

# CASE 2: YES on current bucket — riskier, temp can still rise
# Smaller sizing + later time gate + buffer from bucket ceiling
CASE2_POSITION_PCT = 0.10              # 10% of bankroll (vs 25% for CASE 1)
CASE1_MIN_LOCAL_HOUR = 11              # 11 AM local — high can only go up, safe to confirm early
CASE2_MIN_LOCAL_HOUR = 16              # 4 PM local (vs 11 AM for CASE 1)
CASE2_NARROW_MIN_LOCAL_HOUR = 17       # 5 PM local for narrow buckets (≤5°F wide)
CASE2_NARROW_BUCKET_WIDTH = 5          # Buckets ≤5°F wide get stricter rules

# CASE 3: Graduated gap thresholds for "bucket unreachable" detection
# Keys = minimum local hour, Values = minimum temp gap (°F) after rounding buffer
# Earlier detection = larger gap required (more time for temp to rise)
CASE3_GAP_THRESHOLDS = {
    16: 3,   # 4PM+: 3°F (was 2°F — too aggressive)
    15: 5,   # 3PM: 5°F (was 3°F — DC loss)
    14: 6,   # 2PM: 6°F (new tier)
    13: 7,   # 1PM: 7°F (was 5°F)
    12: 8,   # Noon: 8°F (was 7°F)
    11: 10,  # 11AM: 10°F (unchanged)
}
CASE3_ENSEMBLE_VETO_GAP_LATE = 3       # 3PM+: ensemble mean within 3°F of bucket = veto
CASE3_ENSEMBLE_VETO_GAP_DEFAULT = 5    # Before 3PM: ensemble mean within 5°F = veto

# Trades above this require manual approval
APPROVAL_THRESHOLD_CENTS = 2500     # $25.00

# Stop trading if daily losses hit this
DAILY_LOSS_LIMIT_CENTS = 2000       # $20.00

# Max total money at risk across all active positions
MAX_TOTAL_EXPOSURE_PCT = 0.60       # 60% of bankroll (dynamic, percentage-based)
MAX_TOTAL_EXPOSURE_CENTS = 6000     # $60.00 fallback if balance unknown

# Max open positions at once
MAX_OPEN_POSITIONS = 20

# Max exposure per city (weather) — percentage-based with fallback
MAX_PER_CITY_PCT = 0.20             # 20% of bankroll per city
MAX_PER_CITY_CENTS = 1000           # $10.00 fallback if balance unknown

# Cumulative daily spending cap per city — prevents sell-and-rebuy chasing
# Unlike MAX_PER_CITY_CENTS (tracks open exposure), this tracks TOTAL dollars
# spent on a city in one day, even if positions were closed/sold.
MAX_DAILY_CITY_SPEND_CENTS = 3500   # $35.00 cumulative per city per day

# Max positions per city+date combination
MAX_CORRELATED_POSITIONS = 3

# Max total cost per single ticker (prevents over-concentration on one contract)
# e.g., KXHIGHNY-26FEB16-B41.5 can never exceed $15.00 total invested
MAX_PER_TICKER_CENTS = 1500          # $15.00 per ticker
MAX_CONTRACTS_PER_TICKER = 25        # Hard cap on contract count (prevents cheap contract explosion)

# Pause after this many consecutive losses
CONSECUTIVE_LOSS_PAUSE = 5
CONSECUTIVE_LOSS_PAUSE_MINUTES = 30

# Cooldown between trades (seconds)
TRADE_COOLDOWN = 120                # 2 minutes — matches scan interval (was 180)

# ═══════════════════════════════════════════════════════
# RESTING ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════
# Limit orders rest on the book until matched. Auto-cancel
# stale orders to free up capital and avoid phantom positions.
RESTING_ORDER_TIMEOUT = 1500        # 25 min — cancel unfilled buy orders (was 15 min)
RESTING_EXIT_TIMEOUT = 1800         # 30 min — cancel unfilled exit/hedge orders

# ═══════════════════════════════════════════════════════
# ACCOUNT TRACKING
# ═══════════════════════════════════════════════════════
# Total deposited into Kalshi — used to compute true account P&L
# Account P&L = Current Balance - Deposits (most accurate measure)
TOTAL_DEPOSITS_CENTS = 10000        # $100.00

# Settlement proximity: no new positions within N hours of close
SETTLEMENT_PROXIMITY_HOURS = 2
SETTLEMENT_PROXIMITY_EDGE_OVERRIDE = 0.20  # Exceptional edge overrides

# ═══════════════════════════════════════════════════════
# NWS ROUNDING BUFFER
# ═══════════════════════════════════════════════════════
# Per NWS research: 5-minute stations have ±1°F+ error from
# DOS-era °F→°C→°F conversion. Settlement uses raw 1-minute
# readings which can be higher than displayed time series.
ROUNDING_BUFFER_HARD_F = 1              # ±1°F of strike = NO TRADE
ROUNDING_BUFFER_SOFT_F = 2              # ±2°F of strike = 50% size reduction
MIN_FORECAST_STRIKE_SEPARATION_F = 3    # Forecast mean must be ≥3°F from nearest strike

# ═══════════════════════════════════════════════════════
# MODEL DIVERGENCE GATE (tightened from hardcoded 5°F)
# ═══════════════════════════════════════════════════════
MAX_MODEL_DIVERGENCE_F = 4              # >4°F model spread = NO TRADE
MODEL_CONVERGENCE_BOOST_F = 2           # <2°F model spread = 1.2x confidence

# ═══════════════════════════════════════════════════════
# FEE-ADJUSTED EDGE
# ═══════════════════════════════════════════════════════
# Kalshi charges ~7% of profit on taker fills. Limit orders
# (our maker strategy) pay lower/zero fees, but we model
# worst-case taker rate as conservative safety margin.
KALSHI_FEE_PCT = 0.07                   # 7% of expected profit per contract
FEE_ADJUSTED_MIN_EDGE = 0.04            # 4% minimum net edge after fees (was 5%)
FEE_ADJUSTMENT_ENABLED = True

# ═══════════════════════════════════════════════════════
# DAILY TRADE COUNT CAP
# ═══════════════════════════════════════════════════════
# Fewer, higher-conviction trades. Confirmed outcomes and
# arbitrage are exempt (near risk-free).
MAX_DAILY_FORECAST_TRADES = 8   # Was 4 — allow more volume with smaller positions

# ═══════════════════════════════════════════════════════
# SETTLEMENT-AWARE CAPITAL MANAGEMENT
# ═══════════════════════════════════════════════════════
# Kalshi weather markets settle at 10 AM ET the day after the market date.
# Capital is locked between market close and settlement. These settings
# ensure the bot has cash available for the overnight/morning edge window.
#
# NEXT-DAY TRADING WINDOWS (ET, ranked by edge):
#   00Z cascade: 12:30 AM - 6:30 AM  (all 143 ensemble members fresh, market asleep)
#   06Z pulse:   6:00 AM - 9:00 AM   (supplementary GFS/ECMWF runs)
#   12Z cascade: 2:30 PM - 6:30 PM   (second full refresh, new listings from 10 AM)
SETTLEMENT_HOUR_ET = 10             # Hour (ET) when settlements process
LIQUIDITY_RESERVE_PCT = 0.30        # 30% of bankroll — reserve cash for confirmed outcomes (was 50%)
PRE_SETTLEMENT_SIZING_MULT = 0.75   # 75% sizing before settlements clear (was 60%)

# ═══════════════════════════════════════════════════════
# PORTFOLIO REVIEW SETTINGS
# ═══════════════════════════════════════════════════════
# Continuously re-evaluate open positions and take action
# when model forecasts change throughout the day.
PORTFOLIO_REVIEW_ENABLED = True

# Pare when edge drops below this fraction of entry edge
EDGE_DECAY_PARE_THRESHOLD = 0.50    # 50% decay → start paring

# Full exit or hedge when edge reverses past this point
EDGE_REVERSAL_THRESHOLD = -0.05     # -5% = edge flipped

# Take profit when unrealized gain exceeds this
TAKE_PROFIT_PCT = 0.30              # 30%

# Don't review positions younger than this (avoid whipsaw on fresh trades)
MIN_REVIEW_AGE_MINUTES = 10

# Liquidity tiers (no longer used for position sizing caps — sizing is
# governed by Kelly + 20% bankroll cap. Kept for market_quality filter.)
LIQUIDITY_TIER_1_OI = 50
LIQUIDITY_TIER_2_OI = 200
LIQUIDITY_FULL_OI = 500

# ═══════════════════════════════════════════════════════
# SEASONAL SIZING
# ═══════════════════════════════════════════════════════
SEASONAL_SIZING_ENABLED = True
SEASONAL_MULTIPLIER_MIN = 0.5
SEASONAL_MULTIPLIER_MAX = 1.3
REGIME_DETECTION_ENABLED = True
ANOMALY_THRESHOLD_F = 15             # °F departure from normal to trigger regime
ADAPTIVE_LEARNING_MIN_TRADES = 30    # Min trades per city-month before adjusting
ADAPTIVE_LEARNING_BLEND = 0.3        # Weight given to empirical data vs theoretical

# ═══════════════════════════════════════════════════════
# STRATEGY SETTINGS
# ═══════════════════════════════════════════════════════
# Minimum edge to consider a trade (8% for weather)
MIN_EDGE = 0.08

# Skip trades where total payout (contracts × payout_per_contract) is below this
MIN_PAYOUT_DOLLARS = 1.50      # Was $5 — scaled for $40 bankroll (3% of bankroll)

# Minimum volume on a market to trade it
# Note: Kalshi weather markets are low-volume. 1+ is realistic.
MIN_VOLUME = 1

# ═══════════════════════════════════════════════════════
# TRADE APPROVAL
# ═══════════════════════════════════════════════════════
# Trades only require dashboard approval when ALL of these are true:
#   1. Adding the trade would exceed MAX_OPEN_POSITIONS
#   2. Edge > 28% (20% above the 8% minimum)
#   3. Confirmation is STRONG
# All other trades auto-execute.
HIGH_EDGE_APPROVAL_THRESHOLD = 0.28

# ═══════════════════════════════════════════════════════
# KILL SWITCH / OBSERVATION MODE
# ═══════════════════════════════════════════════════════
OBSERVATION_MODE = False  # Manual override; also auto-set by kill switch
KILL_SWITCH_CONSECUTIVE_LOSSES = 3
KILL_SWITCH_MIN_SHARPE_7D = 0.0

# ═══════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════
# All JSON state files live here. On Railway, mount a Volume at /data
# so state survives redeploys. Locally defaults to current directory.
STATE_DIR = os.environ.get("STATE_DIR", ".")
os.makedirs(STATE_DIR, exist_ok=True)

def atomic_json_save(filepath, data, indent=2):
    """Write JSON atomically: write to temp file, then os.replace().

    Prevents 0-byte corruption if the process is killed mid-write.
    """
    import json as _json
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            _json.dump(data, f, indent=indent)
        os.replace(tmp_path, filepath)
    except Exception:
        # Clean up temp file if replace failed
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


EDGE_ATTRIBUTION_FILE = os.path.join(STATE_DIR, "edge_attribution.json")
TRADE_ANALYSIS_FILE = os.path.join(STATE_DIR, "trade_analysis.json")
ANALYSIS_HOUR_ET = 11  # Run analysis 1hr after 10AM settlements
TRADE_LOG_FILE = os.path.join(STATE_DIR, "trade_history.json")
RISK_STATE_FILE = os.path.join(STATE_DIR, "risk_state.json")
DAILY_REPORTS_FILE = os.path.join(STATE_DIR, "daily_reports.json")
PENDING_TRADES_FILE = os.path.join(STATE_DIR, "pending_trades.json")
BOT_STATUS_FILE = os.path.join(STATE_DIR, "bot_status.json")
PNL_HISTORY_FILE = os.path.join(STATE_DIR, "pnl_history.json")
BACKTEST_RESULTS_FILE = os.path.join(STATE_DIR, "backtest_results.json")
SCAN_LOG_FILE = os.path.join(STATE_DIR, "scan_log.json")
MAKER_ORDERS_FILE = os.path.join(STATE_DIR, "maker_orders.json")
LOG_LEVEL = "DEBUG"  # "DEBUG" for verbose output

# ═══════════════════════════════════════════════════════
# TRADE SCORECARD THRESHOLDS (v4.0)
# ═══════════════════════════════════════════════════════
# Recursive evaluation loop: every trade must pass all 8 criteria.
# Confirmed outcomes and arbitrage bypass the scorecard.
SCORECARD_ENABLED = True
SCORECARD_MAX_ITERATIONS = 3              # Max diagnose-fix-retry loops
MIN_FORECAST_CONVERGENCE = 0.60           # 60% of ensemble must agree within 2°F
MIN_EDGE_AFTER_FEES = 0.04               # 4% minimum edge (same as FEE_ADJUSTED_MIN_EDGE)
MIN_TIMING_MULTIPLIER = 0.50             # Don't trade in bad timing windows
MIN_LIQUIDITY_CONTRACTS = 3              # Need 3+ contracts at target price
MAX_CORRELATION_EXPOSURE = 0.40          # 40% max in correlated positions

# ═══════════════════════════════════════════════════════
# MAKER EXECUTION STRATEGY (v4.0)
# ═══════════════════════════════════════════════════════
# Post limit orders below fair value (maker side) and wait for fills.
# Makers earn +0.77%-1.25% excess returns on Kalshi per Becker dataset.
MAKER_STRATEGY_ENABLED = True
MAKER_SPREAD_BUFFER_CENTS = 2            # Post limit orders 2¢ below fair value
STALE_ORDER_MINUTES = 30                 # Cancel and re-evaluate after 30 min
MAX_OPEN_ORDERS = 10                     # Cap on simultaneous open maker orders
ADVERSE_SELECTION_PAUSE_MINUTES = 60     # Pause market after adverse selection detected

# ═══════════════════════════════════════════════════════
# HEALTH MONITORING & EMAIL ALERTS
# ═══════════════════════════════════════════════════════
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")  # sender email
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # app password
HEALTH_STALE_MINUTES = 15    # scan older than this = stalled
HEALTH_WARN_MINUTES = 10     # yellow status threshold
PENDING_ALERT_MINUTES = 30   # alert if pending trade unapproved this long

# ═══════════════════════════════════════════════════════
# API BASE URLS (used by kalshi_client.py)
# ═══════════════════════════════════════════════════════
DEMO_API_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
