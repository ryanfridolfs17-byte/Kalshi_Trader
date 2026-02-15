"""
MARKET SCANNER v3.1
========================
Finds tradeable weather + S&P 500 markets and scans for arbitrage.

Weather scanning: Fetches markets from KXHIGHNY, KXHIGHCHI,
KXHIGHMIA, KXHIGHAUS series.

S&P 500 scanning: Fetches KXINX daily bracket markets.

Arbitrage scanning: Checks all open markets for YES+NO < 98¢.
"""

import requests
from datetime import datetime
import config
from weather_engine import CITIES

# Kalshi API base URLs
API_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}

# Always use production for public reads (demo orderbooks are empty)
PUBLIC_READ_URL = "https://api.elections.kalshi.com/trade-api/v2"


class MarketScanner:

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        # Use production URL for public reads — demo orderbooks are empty
        self.base_url = PUBLIC_READ_URL

    def scan_weather_markets(self):
        """
        Fetch all open weather markets for configured cities.
        Returns list of market dicts grouped by city.
        """
        weather_markets = []

        for city_code in config.WEATHER_CITIES:
            city_info = CITIES.get(city_code)
            if not city_info:
                continue

            series_ticker = city_info["series_ticker"]

            try:
                url = f"{self.base_url}/markets"
                params = {
                    "series_ticker": series_ticker,
                    "status": "open",
                    "limit": 200,
                }

                response = requests.get(url, params=params, timeout=15)
                if response.status_code != 200:
                    print(f"  [SCAN] WARN: Failed to fetch {series_ticker}: HTTP {response.status_code}")
                    continue

                data = response.json()
                markets = data.get("markets", [])

                for m in markets:
                    m["_city_code"] = city_code
                    weather_markets.append(m)

                if markets:
                    print(f"  [SCAN] {city_code}: Found {len(markets)} open weather markets")
                else:
                    print(f"  [SCAN] {city_code}: No open markets")

            except Exception as e:
                print(f"  [SCAN] Error fetching {series_ticker}: {e}")

        return weather_markets

    def scan_for_arbitrage(self, markets=None):
        """
        Scan markets for spread arbitrage (YES+NO < 98¢).
        If no markets provided, fetches open markets.
        """
        if markets is None:
            markets = self._fetch_all_open_markets()

        arb_opportunities = []

        for m in markets:
            yes_ask = m.get("yes_ask", 0) or 0
            no_ask = m.get("no_ask", 0) or 0

            if yes_ask > 0 and no_ask > 0:
                total = yes_ask + no_ask
                if total < 98:
                    gap = 100 - total
                    arb_opportunities.append({
                        "market": m,
                        "gap_cents": gap,
                        "yes_ask": yes_ask,
                        "no_ask": no_ask,
                    })

        if arb_opportunities:
            arb_opportunities.sort(key=lambda x: x["gap_cents"], reverse=True)
            print(f"  [SCAN] Found {len(arb_opportunities)} arbitrage opportunities!")
            for arb in arb_opportunities[:5]:
                m = arb["market"]
                print(f"    → {m['ticker']}: YES({arb['yes_ask']}¢)+NO({arb['no_ask']}¢)"
                      f"={arb['yes_ask']+arb['no_ask']}¢ → {arb['gap_cents']}¢ free")

        return arb_opportunities

    def _fetch_all_open_markets(self):
        """Fetch a batch of open markets for arbitrage scanning."""
        try:
            url = f"{self.base_url}/markets"
            params = {"status": "open", "limit": 200}
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                return response.json().get("markets", [])
        except Exception:
            pass
        return []

    def scan_sp500_markets(self):
        """
        Fetch all open S&P 500 bracket markets (KXINX series).
        Only runs if SP500 market type is enabled.
        Returns list of market dicts.
        """
        if not config.MARKET_TYPES.get("sp500"):
            return []

        sp500_markets = []
        series_ticker = config.SP500_SERIES_TICKER

        try:
            url = f"{self.base_url}/markets"
            params = {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 200,
            }

            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                print(f"  [SCAN] WARN: Failed to fetch {series_ticker}: HTTP {response.status_code}")
                return []

            data = response.json()
            markets = data.get("markets", [])

            for m in markets:
                m["_market_type"] = "sp500"
                sp500_markets.append(m)

            if markets:
                print(f"  [SCAN] SP500: Found {len(markets)} open bracket markets")
            else:
                print(f"  [SCAN] SP500: No open markets")

        except Exception as e:
            print(f"  [SCAN] Error fetching {series_ticker}: {e}")

        return sp500_markets

    def scan_all_enabled_markets(self):
        """Scan all enabled market types and return combined list."""
        all_markets = []

        if config.MARKET_TYPES.get("weather"):
            all_markets.extend(self.scan_weather_markets())

        if config.MARKET_TYPES.get("sp500"):
            all_markets.extend(self.scan_sp500_markets())

        return all_markets

    def print_sp500_summary(self, markets):
        """Print a summary of S&P 500 bracket markets found."""
        if not markets:
            print("  [SCAN] No S&P 500 markets found")
            return

        events = set(m.get("event_ticker", "") for m in markets)
        print(f"\n  ┌─ S&P 500 Bracket Markets Found ──────────────")
        print(f"  │  {len(markets)} markets across {len(events)} events")
        for m in markets[:5]:
            title = m.get("title", "")[:50]
            yes_ask = m.get("yes_ask", 0) or 0
            vol = m.get("volume", 0) or 0
            print(f"  │    {m['ticker']}: {title}... "
                  f"YES={yes_ask}c vol={vol}")
        print(f"  └────────────────────────────────────────────────\n")

    def print_weather_summary(self, markets):
        """Print a summary of weather markets found."""
        if not markets:
            print("  [SCAN] No weather markets found")
            return

        by_city = {}
        for m in markets:
            city = m.get("_city_code", "?")
            if city not in by_city:
                by_city[city] = []
            by_city[city].append(m)

        print(f"\n  ┌─ Weather Markets Found ───────────────────────")
        for city, city_markets in by_city.items():
            events = set(m.get("event_ticker", "") for m in city_markets)
            print(f"  │  {city}: {len(city_markets)} markets across {len(events)} events")
            # Show a few examples
            for m in city_markets[:3]:
                title = m.get("title", "")[:50]
                yes_ask = m.get("yes_ask", 0) or 0
                vol = m.get("volume", 0) or 0
                print(f"  │    {m['ticker']}: {title}... "
                      f"YES={yes_ask}¢ vol={vol}")
        print(f"  └────────────────────────────────────────────────\n")
