"""
KALSHI API CLIENT
==================
Handles all communication with the Kalshi API.
Supports both demo and production environments.

Kalshi uses RSA signature-based authentication:
  - You have an API Key ID (public)
  - You have a Private Key file (secret)
  - Each request is signed with your private key
"""

import base64
import datetime
import json
import time
import requests
import config


class KalshiClient:
    """Handles authenticated communication with Kalshi's API."""

    def __init__(self):
        if config.ENVIRONMENT == "demo":
            self.base_url = config.DEMO_API_URL
            print("  [API] Connected to Kalshi DEMO environment (fake money)")
        else:
            self.base_url = config.PROD_API_URL
            print("  [API] Connected to Kalshi PRODUCTION environment (real money!)")

        self.api_key_id = config.API_KEY_ID
        self.private_key = None
        self._load_private_key()

    def _load_private_key(self):
        """Load RSA private key from file."""
        if config.API_KEY_ID == "YOUR_API_KEY_ID_HERE":
            print("  [API] WARNING: API key not configured - will use public endpoints only")
            return

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend

            with open(config.PRIVATE_KEY_PATH, "rb") as key_file:
                key_data = key_file.read()
                self.private_key = serialization.load_pem_private_key(
                    key_data,
                    password=None,
                    backend=default_backend()
                )
            # Diagnostic: show key format without exposing the key itself
            key_text = key_data.decode("utf-8", errors="replace")
            key_lines = key_text.strip().split("\n")
            print(f"  [API] Private key loaded successfully")
            print(f"  [API] Key format: {len(key_lines)} lines, "
                  f"first='{key_lines[0][:30]}...', "
                  f"last='{key_lines[-1][:30]}...'")
            print(f"  [API] Key ID: {self.api_key_id[:8]}...{self.api_key_id[-4:]}")

            # Immediate auth test
            test = self._request("GET", "/portfolio/balance", authenticated=True)
            if test:
                print(f"  [API] OK Auth test PASSED -- balance: ${test.get('balance', 0)/100:.2f}")
            else:
                print(f"  [API] FAIL Auth test FAILED -- check API key + private key pairing")
        except FileNotFoundError:
            print(f"  [API] WARNING: Private key file not found: {config.PRIVATE_KEY_PATH}")
            print("  [API] Bot will scan markets but cannot trade")
        except ImportError:
            print("  [API] WARNING: 'cryptography' package not installed")
            print("  [API] Run: pip install cryptography")
        except Exception as e:
            print(f"  [API] WARNING: Error loading private key: {e}")

    def _sign_request(self, method, path):
        """Create RSA-PSS signature for an authenticated request."""
        if not self.private_key:
            return {}

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        # Kalshi requires the FULL path including /trade-api/v2 prefix for signing
        # Our paths are relative (e.g., /portfolio/balance), so prepend the API prefix
        full_path = "/trade-api/v2" + path
        path_for_signing = full_path.split("?")[0]
        message = f"{timestamp}{method}{path_for_signing}"

        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, data=None, authenticated=False):
        """Make an API request to Kalshi with rate-limit retry."""
        url = f"{self.base_url}{path}"

        for attempt in range(3):
            headers = {}
            if authenticated:
                headers = self._sign_request(method, path)
                if not headers:
                    print("  [API] Cannot make authenticated request - no API key")
                    return None

            try:
                if method == "GET":
                    response = requests.get(url, headers=headers, timeout=30)
                elif method == "POST":
                    response = requests.post(url, headers=headers, json=data, timeout=30)
                elif method == "DELETE":
                    response = requests.delete(url, headers=headers, timeout=30)
                else:
                    return None

                if response.status_code in (200, 201):
                    return response.json()
                elif response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", 2))
                    print(f"  [API] Rate limited — retry {attempt+1}/2 in {wait}s")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  [API] Error {response.status_code}: {response.text[:200]}")
                    return None

            except requests.exceptions.RequestException as e:
                print(f"  [API] Request failed: {e}")
                return None

        print(f"  [API] Rate limit retries exhausted for {method} {path}")
        return None

    # =========================================================
    # API RESPONSE NORMALIZATION
    # =========================================================

    @staticmethod
    def _normalize_market(m):
        """Translate new Kalshi API field names to internal format.

        New API uses dollar strings (yes_ask_dollars: "0.0500") and
        float-point strings (volume_fp: "136.00"). Internal code uses
        integer cents (yes_ask: 5) and integer counts (volume: 136).
        """
        if not isinstance(m, dict):
            return m
        # Skip if already normalized (old fields present)
        if "yes_ask" in m and m["yes_ask"] is not None:
            return m

        def _dollars_to_cents(val):
            if val is None:
                return 0
            try:
                return int(round(float(val) * 100))
            except (ValueError, TypeError):
                return 0

        def _fp_to_int(val):
            if val is None:
                return 0
            try:
                return int(float(val))
            except (ValueError, TypeError):
                return 0

        m["yes_ask"] = _dollars_to_cents(m.get("yes_ask_dollars"))
        m["no_ask"] = _dollars_to_cents(m.get("no_ask_dollars"))
        m["yes_bid"] = _dollars_to_cents(m.get("yes_bid_dollars"))
        m["no_bid"] = _dollars_to_cents(m.get("no_bid_dollars"))
        m["last_price"] = _dollars_to_cents(m.get("last_price_dollars"))
        m["volume"] = _fp_to_int(m.get("volume_fp"))
        m["volume_24h"] = _fp_to_int(m.get("volume_24h_fp"))
        m["open_interest"] = _fp_to_int(m.get("open_interest_fp"))
        return m

    # =========================================================
    # PUBLIC ENDPOINTS (no authentication needed)
    # =========================================================

    def get_exchange_status(self):
        """Check if the exchange is currently open."""
        return self._request("GET", "/exchange/status")

    def get_events(self, limit=100, cursor=None, status="open",
                   series_ticker=None, with_nested_markets=True):
        """
        Get events (groups of related markets).
        Example: "NFL Week 12" is an event containing markets for each game.
        """
        params = [f"limit={limit}", f"status={status}"]
        if cursor:
            params.append(f"cursor={cursor}")
        if series_ticker:
            params.append(f"series_ticker={series_ticker}")
        if with_nested_markets:
            params.append("with_nested_markets=true")

        query = "&".join(params)
        return self._request("GET", f"/events?{query}")

    def get_markets(self, limit=100, cursor=None, event_ticker=None,
                    series_ticker=None, status="open"):
        """
        Get individual markets.
        Each market is a yes/no question you can trade on.
        """
        params = [f"limit={limit}", f"status={status}"]
        if cursor:
            params.append(f"cursor={cursor}")
        if event_ticker:
            params.append(f"event_ticker={event_ticker}")
        if series_ticker:
            params.append(f"series_ticker={series_ticker}")

        query = "&".join(params)
        result = self._request("GET", f"/markets?{query}")
        if result and "markets" in result:
            result["markets"] = [self._normalize_market(m) for m in result["markets"]]
        return result

    def get_market(self, ticker):
        """Get detailed info about a specific market."""
        result = self._request("GET", f"/markets/{ticker}")
        if result and "market" in result:
            result["market"] = self._normalize_market(result["market"])
        elif result and "ticker" in result:
            result = self._normalize_market(result)
        return result

    def get_orderbook(self, ticker):
        """Get the order book (all buy/sell offers) for a market."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

    def get_book_imbalance(self, ticker):
        """Compute order book imbalance (OBI).
        OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        Returns float in [-1, 1] or 0.0 on error.
        OBI > 0.5 = buy pressure, OBI < -0.5 = sell pressure."""
        try:
            book = self.get_orderbook(ticker)
            if not book:
                return 0.0
            yes_bids = book.get("yes", [])
            no_bids = book.get("no", [])
            # For yes side: yes bids are buy orders, calculate depth
            bid_depth = sum(level[1] for level in yes_bids if len(level) >= 2) if yes_bids else 0
            ask_depth = sum(level[1] for level in no_bids if len(level) >= 2) if no_bids else 0
            total = bid_depth + ask_depth
            if total == 0:
                return 0.0
            return (bid_depth - ask_depth) / total
        except Exception:
            return 0.0

    # =========================================================
    # AUTHENTICATED ENDPOINTS (need API key)
    # =========================================================

    def get_balance(self):
        """Get your account balance (in cents)."""
        result = self._request("GET", "/portfolio/balance", authenticated=True)
        if result:
            balance_cents = result.get("balance", 0)
            print(f"  [API] Account balance: ${balance_cents / 100:.2f}")
        return result

    def get_positions(self, limit=200, cursor=None):
        """Get your current open positions (non-zero only)."""
        params = [f"limit={limit}", "count_filter=position"]
        if cursor:
            params.append(f"cursor={cursor}")
        query = "&".join(params)
        return self._request("GET", f"/portfolio/positions?{query}", authenticated=True)

    def create_order(self, ticker, side, count, action="buy", type="market",
                     yes_price=None, no_price=None):
        """
        Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g., "HIGHNY-26FEB12-T40")
            side: "yes" or "no"
            count: Number of contracts
            action: "buy" or "sell"
            type: "market" or "limit"
            yes_price: Price in cents for limit orders (1-99)
            no_price: Price in cents for limit orders (1-99)
        """
        order_data = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": type,
        }
        if type == "limit":
            if yes_price is not None:
                order_data["yes_price"] = yes_price
            if no_price is not None:
                order_data["no_price"] = no_price

        return self._request("POST", "/portfolio/orders", data=order_data, authenticated=True)

    def sell_order(self, ticker, side, count, type="market", yes_price=None, no_price=None):
        """
        Sell (exit) a position on Kalshi.
        Same as create_order but with action="sell".
        """
        order_data = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "count": count,
            "type": type,
        }
        if type == "limit":
            if yes_price is not None:
                order_data["yes_price"] = yes_price
            if no_price is not None:
                order_data["no_price"] = no_price

        return self._request("POST", "/portfolio/orders", data=order_data, authenticated=True)

    def cancel_order(self, order_id):
        """Cancel a pending order."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}", authenticated=True)

    def get_order(self, order_id):
        """Get details of a specific order by ID."""
        result = self._request("GET", "/portfolio/orders/%s" % order_id, authenticated=True)
        if result and "order" in result:
            order = result["order"]
            self._normalize_order(order)
            return order
        return result

    @staticmethod
    def _normalize_order(o):
        """Normalize order response fields from dollar strings to int cents.

        Kalshi API (March 2026) returns yes_price_dollars/no_price_dollars
        as string dollars on order responses. Convert to yes_price/no_price
        int cents so downstream code (check_fills) works correctly.
        """
        if not isinstance(o, dict):
            return o
        for field in ("yes_price", "no_price"):
            dollar_field = field + "_dollars"
            if dollar_field in o:
                try:
                    o[field] = int(round(float(o[dollar_field]) * 100))
                except (ValueError, TypeError):
                    pass
        return o

    def get_orders(self, ticker=None, status=None):
        """Get your orders."""
        params = []
        if ticker:
            params.append(f"ticker={ticker}")
        if status:
            params.append(f"status={status}")
        query = "&".join(params) if params else ""
        path = f"/portfolio/orders?{query}" if query else "/portfolio/orders"
        return self._request("GET", path, authenticated=True)

    def get_fills(self, limit=200, cursor=None, min_ts=None):
        """Get executed trade fills (buys and sells)."""
        params = [f"limit={limit}"]
        if cursor:
            params.append(f"cursor={cursor}")
        if min_ts:
            params.append(f"min_ts={min_ts}")
        query = "&".join(params)
        return self._request("GET", f"/portfolio/fills?{query}", authenticated=True)

    def get_settlements(self, limit=200, cursor=None, min_ts=None):
        """Get settlement results for resolved markets."""
        params = [f"limit={limit}"]
        if cursor:
            params.append(f"cursor={cursor}")
        if min_ts:
            params.append(f"min_ts={min_ts}")
        query = "&".join(params)
        return self._request("GET", f"/portfolio/settlements?{query}", authenticated=True)
