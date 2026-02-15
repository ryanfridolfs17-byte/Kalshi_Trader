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

# If the full PEM key is in an env var (Railway), write it to a file at startup
_private_key_env = os.environ.get("KALSHI_PRIVATE_KEY", "")
if _private_key_env:
    with open(PRIVATE_KEY_PATH, "w") as _f:
        _f.write(_private_key_env)

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
# Weather markets move fast — 5 minutes is ideal
SCAN_INTERVAL = 300  # 5 minutes

# Which cities to trade weather for
# Options: "NYC", "CHI", "MIA", "AUS"
WEATHER_CITIES = ["NYC", "CHI", "MIA", "AUS"]

# Also scan non-weather markets for arbitrage?
SCAN_ALL_FOR_ARBITRAGE = True

# ═══════════════════════════════════════════════════════
# RISK PARAMETERS (all in cents, 100 cents = $1.00)
# ═══════════════════════════════════════════════════════
# Max cost of a single auto-trade (no approval needed)
MAX_AUTO_TRADE_CENTS = 300          # $3.00

# Trades above this require manual approval
APPROVAL_THRESHOLD_CENTS = 500      # $5.00

# Stop trading if daily losses hit this
DAILY_LOSS_LIMIT_CENTS = 1000       # $10.00

# Max total money at risk across all positions
MAX_TOTAL_EXPOSURE_CENTS = 2000     # $20.00

# Max open positions at once
MAX_OPEN_POSITIONS = 15

# Max exposure per city (weather)
MAX_PER_CITY_CENTS = 800            # $8.00 per city

# Max positions per city+date combination
MAX_CORRELATED_POSITIONS = 3

# Pause after this many consecutive losses
CONSECUTIVE_LOSS_PAUSE = 5
CONSECUTIVE_LOSS_PAUSE_MINUTES = 30

# Max trades per day
MAX_DAILY_TRADES = 20

# Cooldown between trades (seconds)
TRADE_COOLDOWN = 180                # 3 minutes (faster for weather)

# Settlement proximity: no new positions within N hours of close
SETTLEMENT_PROXIMITY_HOURS = 2
SETTLEMENT_PROXIMITY_EDGE_OVERRIDE = 0.20  # Exceptional edge overrides

# Liquidity tiers for position sizing (by open_interest)
LIQUIDITY_TIER_1_OI = 50    # open_interest < 50 → max 1 contract
LIQUIDITY_TIER_2_OI = 200   # open_interest < 200 → max 2 contracts
LIQUIDITY_FULL_OI = 500     # open_interest >= 500 → full sizing

# ═══════════════════════════════════════════════════════
# STRATEGY SETTINGS
# ═══════════════════════════════════════════════════════
# Minimum edge to consider a trade (8% for weather)
MIN_EDGE = 0.08

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
PENDING_TRADES_FILE = "pending_trades.json"

# ═══════════════════════════════════════════════════════
# KILL SWITCH / OBSERVATION MODE
# ═══════════════════════════════════════════════════════
OBSERVATION_MODE = False  # Manual override; also auto-set by kill switch
KILL_SWITCH_CONSECUTIVE_LOSSES = 3
KILL_SWITCH_MIN_SHARPE_7D = 0.0

# ═══════════════════════════════════════════════════════
# LOGGING & STATE FILES
# ═══════════════════════════════════════════════════════
EDGE_ATTRIBUTION_FILE = "edge_attribution.json"
TRADE_LOG_FILE = "trade_history.json"
RISK_STATE_FILE = "risk_state.json"
DAILY_REPORTS_FILE = "daily_reports.json"
LOG_LEVEL = "DEBUG"  # "DEBUG" for verbose output

# ═══════════════════════════════════════════════════════
# API BASE URLS (used by kalshi_client.py)
# ═══════════════════════════════════════════════════════
DEMO_API_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
