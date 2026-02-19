"""
VOLATILITY ENGINE v1.0 — S&P 500 Bracket Probability Engine
================================================================
Uses VIX-implied volatility to build price distributions for
S&P 500 daily bracket markets on Kalshi (KXINX series).

DATA SOURCES (all free, no API key needed):
  - yfinance: SPY price, VIX level, historical data
  - scipy.stats: Normal distribution CDF for bracket probabilities

CORE LOGIC:
  1. Fetch current SPY price → distribution mean
  2. Fetch VIX level → daily vol = price * (VIX/100) / sqrt(252)
  3. Build normal distribution using scipy.stats.norm
  4. Calculate bracket probabilities via CDF: P(low <= X <= high)
  5. Parse KXINX bracket tickers to extract price ranges + dates

SETTLEMENT:
  Kalshi S&P 500 markets settle on the official S&P 500 index close.
"""

import re
import math
import threading
from datetime import datetime, timezone, timedelta
from scipy.stats import norm

import config

# yfinance calls can hang indefinitely if Yahoo Finance is unresponsive.
# This wrapper runs them in a thread with a hard timeout to prevent
# freezing the main bot loop.
_YFINANCE_TIMEOUT = 30  # seconds


def _yfinance_with_timeout(func, timeout=_YFINANCE_TIMEOUT):
    """Run a yfinance call in a thread with a hard timeout.
    Returns the result or None if it times out or errors."""
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        print(f"  [SP500] yfinance call timed out after {timeout}s — skipping")
        return None
    if error[0]:
        raise error[0]
    return result[0]


class VolatilityEngine:
    """
    Builds price probability distributions for S&P 500 using
    VIX-implied volatility. Parallel to WeatherEngine.
    """

    def __init__(self):
        self._cache = {}
        self._data_cache = {}  # Raw yfinance data cache
        self._cache_time = None
        self.last_fetch_time = None

    # ═══════════════════════════════════════════════════════
    # MAIN ENTRY: Get price distribution
    # ═══════════════════════════════════════════════════════

    def get_price_distribution(self, target_date=None):
        """
        Fetch SPY/VIX data and build a probability distribution
        of S&P 500 closing prices for the target date.

        Returns:
        {
            "current_price": 6050.25,
            "vix": 15.3,
            "daily_vol": 58.2,
            "mean": 6050.25,
            "std_dev": 58.2,
            "confidence": 0.82,
            "target_date": "2026-02-16",
            "total_members": 1,  # single distribution (not ensemble)
            "raw_prices": [...],  # historical closes for backtest compat
            "hours_to_close": 6.5,  # trading hours remaining
            "intraday_adjustment": True/False,
        }
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check cache
        cache_key = f"sp500_{target_date}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < config.SP500_VIX_CACHE_MINUTES * 60:
                return cached["data"]

        # Fetch market data
        spy_data = self._fetch_spy_data()
        vix_data = self._fetch_vix_data()

        if spy_data is None or vix_data is None:
            print("  [SP500] WARN: Could not fetch SPY/VIX data")
            return None

        current_price = spy_data["current_price"]
        vix_level = vix_data["vix"]
        historical_closes = spy_data.get("historical_closes", [])

        # Calculate daily volatility from VIX
        # VIX represents annualized expected volatility of S&P 500
        # Daily vol = price * (VIX / 100) / sqrt(252)
        daily_vol = current_price * (vix_level / 100.0) / math.sqrt(252)

        # Intraday adjustment: as the trading day progresses,
        # remaining uncertainty shrinks
        hours_to_close = self._hours_to_market_close()
        if 0 < hours_to_close < 6.5:
            # Blend: remaining_fraction of implied vol
            remaining_fraction = hours_to_close / 6.5
            daily_vol *= math.sqrt(remaining_fraction)
            intraday_adj = True
        else:
            intraday_adj = False

        # Days until target date (for multi-day forecasts)
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
            today = datetime.now().date()
            days_ahead = max(1, (target_dt - today).days)
        except ValueError:
            days_ahead = 1

        # Scale volatility for multi-day horizon
        multi_day_vol = daily_vol * math.sqrt(days_ahead)

        # Confidence: lower VIX = tighter distribution = higher confidence
        # VIX < 15 = high confidence, VIX > 30 = low confidence
        confidence = max(0.3, min(0.95, 1.0 - (vix_level / 50.0)))

        result = {
            "current_price": round(current_price, 2),
            "vix": round(vix_level, 2),
            "daily_vol": round(daily_vol, 2),
            "mean": round(current_price, 2),
            "std_dev": round(multi_day_vol, 2),
            "confidence": round(confidence, 3),
            "target_date": target_date,
            "total_members": 1,
            "raw_prices": historical_closes[-90:] if historical_closes else [],
            "hours_to_close": round(hours_to_close, 1),
            "intraday_adjustment": intraday_adj,
            "days_ahead": days_ahead,
        }

        # Cache it
        self._cache[cache_key] = {
            "data": result,
            "fetched_at": datetime.now(),
        }
        self.last_fetch_time = datetime.now()

        return result

    # ═══════════════════════════════════════════════════════
    # BRACKET PROBABILITY
    # ═══════════════════════════════════════════════════════

    def calculate_bracket_probability(self, distribution, price_low, price_high):
        """
        Given a distribution and a price range, calculate the
        probability that the S&P 500 close falls in that range.

        Uses normal CDF: P(low <= X <= high) = CDF(high) - CDF(low)
        """
        if not distribution:
            return None

        mean = distribution["mean"]
        std = distribution["std_dev"]

        if std <= 0:
            return None

        # For "above X" brackets (price_high very large)
        if price_high >= 99999:
            return 1.0 - norm.cdf(price_low, loc=mean, scale=std)

        # For "below X" brackets (price_low very small)
        if price_low <= 0:
            return norm.cdf(price_high, loc=mean, scale=std)

        # Normal range bracket
        prob = norm.cdf(price_high, loc=mean, scale=std) - norm.cdf(price_low, loc=mean, scale=std)
        return round(prob, 6)

    # ═══════════════════════════════════════════════════════
    # TICKER PARSING
    # ═══════════════════════════════════════════════════════

    def parse_market_bracket(self, market):
        """
        Parse a Kalshi S&P 500 bracket market to extract:
        - price range (low, high)
        - target date

        Handles ticker formats like:
          KXINX-26FEB16-B5600    (above 5600)
          KXINX-26FEB16-T5625    (range bracket)
          Also handles title/subtitle patterns like:
          "5,600 to 5,624.99" or "above 5,600" or "below 5,600"

        Returns dict or None if not an S&P 500 market.
        """
        ticker = market.get("ticker", "").upper()
        title = market.get("title", "")
        subtitle = market.get("subtitle", "")
        event_ticker = market.get("event_ticker", "").upper()

        # Must be an KXINX market
        if config.SP500_SERIES_TICKER.upper() not in ticker and \
           config.SP500_SERIES_TICKER.upper() not in event_ticker:
            return None

        combined = f"{title} {subtitle}"

        # Try to extract price range from title/subtitle
        # Patterns: "5,600 to 5,624.99", "5600 to 5625", "above 5,600", "below 5,600"
        price_low = None
        price_high = None

        # Range pattern: "X,XXX to X,XXX" or "XXXX to XXXX"
        range_match = re.search(
            r'([\d,]+(?:\.\d+)?)\s*(?:to|-)\s*([\d,]+(?:\.\d+)?)',
            combined
        )
        if range_match:
            price_low = float(range_match.group(1).replace(",", ""))
            price_high = float(range_match.group(2).replace(",", ""))
        else:
            # Threshold patterns
            above_match = re.search(
                r'(?:above|over|higher than|at least|>=?)\s*([\d,]+(?:\.\d+)?)',
                combined, re.I
            )
            if not above_match:
                above_match = re.search(
                    r'([\d,]+(?:\.\d+)?)\s*or\s*(?:above|higher|more)',
                    combined, re.I
                )
            below_match = re.search(
                r'(?:below|under|lower than|less than|<=?)\s*([\d,]+(?:\.\d+)?)',
                combined, re.I
            )
            if not below_match:
                below_match = re.search(
                    r'([\d,]+(?:\.\d+)?)\s*or\s*(?:below|lower|less)',
                    combined, re.I
                )

            if above_match:
                price_low = float(above_match.group(1).replace(",", ""))
                price_high = 99999  # effectively infinity
            elif below_match:
                price_high = float(below_match.group(1).replace(",", ""))
                price_low = 0
            else:
                # Try to parse from ticker: KXINX-26FEB16-B5600
                parts = ticker.split("-")
                if len(parts) >= 3:
                    bucket = parts[2]
                    bucket_match = re.match(r'[BT](\d+(?:\.\d+)?)', bucket)
                    if bucket_match:
                        threshold = float(bucket_match.group(1))
                        if bucket.startswith("B"):
                            price_low = threshold
                            price_high = threshold + 25  # default 25-point bracket
                        elif bucket.startswith("T"):
                            price_low = threshold
                            price_high = threshold + 25
                    else:
                        return None
                else:
                    return None

        if price_low is None or price_high is None:
            return None

        # Extract date from ticker: KXINX-26FEB16
        target_date = None
        check_str = event_ticker if event_ticker else ticker
        date_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', check_str)
        if date_match:
            year = 2000 + int(date_match.group(1))
            month_str = date_match.group(2)
            day = int(date_match.group(3))
            months = {
                "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
            }
            month = months.get(month_str, 0)
            if month > 0:
                target_date = f"{year}-{month:02d}-{day:02d}"

        return {
            "price_low": price_low,
            "price_high": price_high,
            "target_date": target_date,
        }

    # ═══════════════════════════════════════════════════════
    # DATA FETCHING
    # ═══════════════════════════════════════════════════════

    def _fetch_spy_data(self):
        """Fetch current SPY price and historical closes via yfinance."""
        cache_key = "spy_data"
        if cache_key in self._data_cache:
            cached = self._data_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < config.SP500_VIX_CACHE_MINUTES * 60:
                return cached["data"]

        try:
            import yfinance as yf
            spy = yf.Ticker("SPY")

            # Get current price (with timeout to prevent infinite hang)
            hist = _yfinance_with_timeout(lambda: spy.history(period="3mo"))
            if hist is None or hist.empty:
                return None

            current_price = float(hist["Close"].iloc[-1])
            # SPY trades at ~1/10th of S&P 500 — multiply by ~10 to approximate index
            # Actually, SPY is ~$600 when S&P is ~6000, so ratio is ~10x
            spy_to_spx_ratio = 10.0
            current_spx = current_price * spy_to_spx_ratio

            historical_closes = [float(c) * spy_to_spx_ratio for c in hist["Close"].tolist()]

            result = {
                "current_price": current_spx,
                "spy_price": current_price,
                "historical_closes": historical_closes,
            }

            self._data_cache[cache_key] = {
                "data": result,
                "fetched_at": datetime.now(),
            }
            return result

        except Exception as e:
            print(f"  [SP500] Error fetching SPY data: {e}")
            return None

    def _fetch_vix_data(self):
        """Fetch current VIX level via yfinance."""
        cache_key = "vix_data"
        if cache_key in self._data_cache:
            cached = self._data_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < config.SP500_VIX_CACHE_MINUTES * 60:
                return cached["data"]

        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = _yfinance_with_timeout(lambda: vix.history(period="5d"))
            if hist is None or hist.empty:
                return None

            current_vix = float(hist["Close"].iloc[-1])

            result = {
                "vix": current_vix,
            }

            self._data_cache[cache_key] = {
                "data": result,
                "fetched_at": datetime.now(),
            }
            return result

        except Exception as e:
            print(f"  [SP500] Error fetching VIX data: {e}")
            return None

    def _hours_to_market_close(self):
        """Calculate hours until US market close (4:00 PM ET)."""
        try:
            from datetime import timezone as tz
            now_utc = datetime.now(tz.utc)
            # ET is UTC-5 (EST) or UTC-4 (EDT)
            # Approximate: use UTC-5 for simplicity
            now_et = now_utc - timedelta(hours=5)
            market_close_hour = 16  # 4:00 PM ET
            market_open_hour = 9.5  # 9:30 AM ET

            current_hour = now_et.hour + now_et.minute / 60.0

            if current_hour < market_open_hour:
                return 6.5  # Full trading day ahead
            elif current_hour >= market_close_hour:
                return 0  # Market closed
            else:
                return market_close_hour - current_hour
        except Exception:
            return 6.5  # Default to full day

    # ═══════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════

    def print_status(self):
        """Print current cached state."""
        if not self._cache:
            print("  [SP500] No price distributions cached yet")
            return

        for key, cached in self._cache.items():
            data = cached["data"]
            age_min = (datetime.now() - cached["fetched_at"]).total_seconds() / 60
            print(f"  [SP500] {data['target_date']}: "
                  f"SPX={data['current_price']:.0f}, "
                  f"VIX={data['vix']:.1f}, "
                  f"vol=+/-{data['std_dev']:.0f}pts, "
                  f"cached {age_min:.0f}min ago")
