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
            print("  [API] ⚠️  API key not configured - will use public endpoints only")
            return

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend

            with open(config.PRIVATE_KEY_PATH, "rb") as key_file:
                self.private_key = serialization.load_pem_private_key(
                    key_file.read(),
                    password=None,
                    backend=default_backend()
                )
            print("  [API] Private key loaded successfully")
        except FileNotFoundError:
            print(f"  [API] ⚠️  Private key file not found: {config.PRIVATE_KEY_PATH}")
            print("  [API] Bot will scan markets but cannot trade")
        except ImportError:
            print("  [API] ⚠️  'cryptography' package not installed")
            print("  [API] Run: pip install cryptography")
        except Exception as e:
            print(f"  [API] ⚠️  Error loading private key: {e}")

    def _sign_request(self, method, path):
        """Create RSA-PSS signature for an authenticated request."""
        if not self.private_key:
            return {}

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        # Strip query params from path for signing
        path_for_signing = path.split("?")[0]
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
        """Make an API request to Kalshi."""
        url = f"{self.base_url}{path}"
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

            if response.status_code == 200 or response.status_code == 201:
                return response.json()
            else:
                print(f"  [API] Error {response.status_code}: {response.text[:200]}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"  [API] Request failed: {e}")
            return None

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
        return self._request("GET", f"/markets?{query}")

    def get_market(self, ticker):
        """Get detailed info about a specific market."""
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker):
        """Get the order book (all buy/sell offers) for a market."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

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

    def get_positions(self):
        """Get your current open positions."""
        return self._request("GET", "/portfolio/positions", authenticated=True)

    def create_order(self, ticker, side, count, type="market",
                     yes_price=None, no_price=None):
        """
        Place an order on Kalshi.

        Args:
            ticker: Market ticker (e.g., "HIGHNY-26FEB12-T40")
            side: "yes" or "no"
            count: Number of contracts
            type: "market" or "limit"
            yes_price: Price in cents for limit orders (1-99)
            no_price: Price in cents for limit orders (1-99)
        """
        order_data = {
            "ticker": ticker,
            "action": "buy",
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
