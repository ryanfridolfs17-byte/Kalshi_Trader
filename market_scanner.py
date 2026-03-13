"""
MARKET SCANNER v4.0
========================
Finds tradeable weather markets and scans for arbitrage.
"""

import requests
from datetime import datetime
import config
from weather_engine import CITIES

# Always use production for public reads (demo orderbooks are empty)
PUBLIC_READ_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _normalize_scanner_market(m):
    """Translate new Kalshi dollar-string fields to integer cents.

    Scanner bypasses kalshi_client, so needs its own normalization.
    """
    if "yes_ask" in m and m["yes_ask"] is not None:
        return
    def _d2c(val):
        try:
            return int(round(float(val) * 100)) if val else 0
        except (ValueError, TypeError):
            return 0
    def _f2i(val):
        try:
            return int(float(val)) if val else 0
        except (ValueError, TypeError):
            return 0
    m["yes_ask"] = _d2c(m.get("yes_ask_dollars"))
    m["no_ask"] = _d2c(m.get("no_ask_dollars"))
    m["yes_bid"] = _d2c(m.get("yes_bid_dollars"))
    m["no_bid"] = _d2c(m.get("no_bid_dollars"))
    m["last_price"] = _d2c(m.get("last_price_dollars"))
    m["volume"] = _f2i(m.get("volume_fp"))
    m["volume_24h"] = _f2i(m.get("volume_24h_fp"))
    m["open_interest"] = _f2i(m.get("open_interest_fp"))


class MarketScanner:

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.base_url = PUBLIC_READ_URL

    def scan_weather_markets(self):
        """Fetch all open weather markets for configured cities."""
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
                    "status": "active",
                    "limit": 200,
                }

                response = requests.get(url, params=params, timeout=15)
                if response.status_code != 200:
                    print(f"  [SCAN] WARN: Failed to fetch {series_ticker}: HTTP {response.status_code}")
                    continue

                data = response.json()
                markets = data.get("markets", [])

                for m in markets:
                    _normalize_scanner_market(m)
                    m["_city_code"] = city_code
                    weather_markets.append(m)

                if markets:
                    print(f"  [SCAN] {city_code}: Found {len(markets)} open weather markets")

            except Exception as e:
                print(f"  [SCAN] Error fetching {series_ticker}: {e}")

        return weather_markets

    def scan_for_arbitrage(self, markets=None):
        """Scan markets for spread arbitrage (YES+NO < 98c)."""
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
                ticker = arb["market"].get("ticker", "?")
                ya = arb["yes_ask"]
                na = arb["no_ask"]
                gap = arb["gap_cents"]
                print(f"    -> {ticker}: YES({ya}c)+NO({na}c)={ya+na}c -> {gap}c free")

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

        print()
        print("  +-- Weather Markets Found ----------------------")
        for city, city_markets in by_city.items():
            events = set(m.get("event_ticker", "") for m in city_markets)
            print(f"  |  {city}: {len(city_markets)} markets across {len(events)} events")
            for m in city_markets[:3]:
                title = m.get("title", "")[:50]
                yes_ask = m.get("yes_ask", 0) or 0
                vol = m.get("volume", 0) or 0
                ticker = m.get("ticker", "?")
                print(f"  |    {ticker}: {title}... YES={yes_ask}c vol={vol}")
        print("  +------------------------------------------------")
        print()
