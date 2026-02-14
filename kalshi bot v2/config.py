"""
KALSHI BOT v3.0 — CONFIGURATION
====================================
Weather-focused trading bot with ensemble forecast edge.

QUICK START:
  1. Set your API_KEY_ID and PRIVATE_KEY_PATH below
  2. Leave ENVIRONMENT = "demo" until you're confident
  3. Leave DRY_RUN = True to watch without trading
  4. Run: python kalshi_bot.py
"""

# ═══════════════════════════════════════════════════════
# API CREDENTIALS
# ═══════════════════════════════════════════════════════
# Get these from: kalshi.com → Settings → API Keys
API_KEY_ID = "YOUR_API_KEY_ID_HERE"
PRIVATE_KEY_PATH = "kalshi_private_key.pem"

# ═══════════════════════════════════════════════════════
# ENVIRONMENT
# ═══════════════════════════════════════════════════════
# "demo" = practice with fake money (demo-api.kalshi.co)
# "production" = real money (api.elections.kalshi.com)
ENVIRONMENT = "demo"

# True = analyze only, don't place any orders (safest start)
# False = actually place orders
DRY_RUN = True

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
MAX_TOTAL_EXPOSURE_CENTS = 1500     # $15.00

# Max open positions at once
MAX_OPEN_POSITIONS = 8

# Max exposure per city (weather)
MAX_PER_CITY_CENTS = 500            # $5.00 per city

# Pause after this many consecutive losses
CONSECUTIVE_LOSS_PAUSE = 5
CONSECUTIVE_LOSS_PAUSE_MINUTES = 30

# Max trades per day
MAX_DAILY_TRADES = 20

# Cooldown between trades (seconds)
TRADE_COOLDOWN = 180                # 3 minutes (faster for weather)

# ═══════════════════════════════════════════════════════
# STRATEGY SETTINGS
# ═══════════════════════════════════════════════════════
# Minimum edge to consider a trade (8% for weather)
MIN_EDGE = 0.08

# Minimum volume on a market to trade it
# Note: Kalshi weather markets are low-volume. 1+ is realistic.
MIN_VOLUME = 1

# ═══════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════
TRADE_LOG_FILE = "trade_history.json"
RISK_STATE_FILE = "risk_state.json"
LOG_LEVEL = "DEBUG"  # "DEBUG" for verbose output

# ═══════════════════════════════════════════════════════
# API BASE URLS (used by kalshi_client.py)
# ═══════════════════════════════════════════════════════
DEMO_API_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
