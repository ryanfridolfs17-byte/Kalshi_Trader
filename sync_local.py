"""
Sync Railway state files to local machine.
Usage: python sync_local.py [--url URL] [--token TOKEN]

Defaults:
  URL:   RAILWAY_URL env var or https://kalshitrader-production.up.railway.app
  Token: DASHBOARD_TOKEN env var
"""

import json
import os
import sys
import requests

# File mapping: API key -> local config path
FILE_MAP = {
    "trades": "trade_history.json",
    "risk": "risk_state.json",
    "pnl": "pnl_history.json",
    "bot_status": "bot_status.json",
    "scan_log": "scan_log.json",
    "maker": "maker_orders.json",
    "learning": "learning_state.json",
    "fill_tracking": "fill_tracking.json",
}


def sync(url=None, token=None, state_dir="."):
    url = url or os.environ.get("RAILWAY_URL", "https://kalshitrader-production.up.railway.app")
    token = token or os.environ.get("DASHBOARD_TOKEN", "")

    if not token:
        print("ERROR: No token. Set DASHBOARD_TOKEN env var or pass --token")
        sys.exit(1)

    endpoint = f"{url.rstrip('/')}/api/sync"
    print(f"Fetching {endpoint} ...")

    try:
        r = requests.get(endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except requests.RequestException as e:
        print(f"ERROR: Request failed: {e}")
        sys.exit(1)

    if r.status_code == 401:
        print("ERROR: Unauthorized — check your DASHBOARD_TOKEN")
        sys.exit(1)
    if r.status_code != 200:
        print(f"ERROR: HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)

    data = r.json()
    synced_at = data.pop("synced_at", "unknown")
    written = 0

    for key, filename in FILE_MAP.items():
        if key not in data:
            continue
        filepath = os.path.join(state_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data[key], f, indent=2)
        size = os.path.getsize(filepath)
        print(f"  {filename:30s} {size:>8,} bytes")
        written += 1

    print(f"\nSynced {written} files (Railway snapshot: {synced_at})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sync Railway state to local")
    parser.add_argument("--url", help="Railway app URL")
    parser.add_argument("--token", help="Dashboard bearer token")
    parser.add_argument("--dir", default=".", help="Local state directory")
    args = parser.parse_args()
    sync(url=args.url, token=args.token, state_dir=args.dir)
