"""
Sync Railway state and observation history to the local machine.

Usage:
  python sync_local.py [--url URL] [--token TOKEN]

Defaults:
  URL:   RAILWAY_URL env var or https://kalshitrader-production.up.railway.app
  Token: DASHBOARD_TOKEN env var
"""

import json
import os
import sys

import requests

from backfill_observation_db import import_observation_export, run_backfill


FILE_MAP = {
    "trades": "trade_history.json",
    "risk": "risk_state.json",
    "pnl": "pnl_history.json",
    "bot_status": "bot_status.json",
    "scan_log": "scan_log.json",
    "maker": "maker_orders.json",
    "learning": "learning_state.json",
    "paper_locks": "paper_locks.json",
    "paper_trades": "paper_trades.json",
    "observation_daily_summary": "observation_daily_summary.json",
}


def _request_json(endpoint, token, timeout=30):
    try:
        response = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print(f"ERROR: Request failed: {exc}")
        sys.exit(1)

    if response.status_code == 401:
        print("ERROR: Unauthorized - check your DASHBOARD_TOKEN")
        sys.exit(1)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def sync(url=None, token=None, state_dir=".", observation_hours=24 * 14):
    url = url or os.environ.get("RAILWAY_URL", "https://kalshitrader-production.up.railway.app")
    token = token or os.environ.get("DASHBOARD_TOKEN", "")

    if not token:
        print("ERROR: No token. Set DASHBOARD_TOKEN env var or pass --token")
        sys.exit(1)

    sync_endpoint = (
        f"{url.rstrip('/')}/api/sync"
        f"?include_observation=1&hours={int(observation_hours)}&events=1000&decisions=5000"
    )
    print(f"Fetching {sync_endpoint} ...")
    try:
        data = _request_json(sync_endpoint, token=token, timeout=60)
        observation_export = data.pop("observation_export", None)
        state_source = "sync"
    except RuntimeError as exc:
        print(f"WARNING: {exc}")
        state_endpoint = f"{url.rstrip('/')}/api/state"
        print(f"Falling back to {state_endpoint} ...")
        try:
            data = _request_json(state_endpoint, token=token, timeout=30)
        except RuntimeError as state_exc:
            print(f"ERROR: {state_exc}")
            sys.exit(1)
        observation_export = None
        state_source = "state"
    written = 0

    file_map = dict(FILE_MAP)
    if "fill_tracking" in data:
        file_map["fill_tracking"] = "fill_tracking.json"

    for key, filename in file_map.items():
        if key not in data:
            continue
        filepath = os.path.join(state_dir, filename)
        with open(filepath, "w", encoding="utf-8") as handle:
            json.dump(data[key], handle, indent=2)
        size = os.path.getsize(filepath)
        print(f"  {filename:30s} {size:>8,} bytes")
        written += 1

    if observation_export is not None:
        import_summary = import_observation_export(
            observation_export,
            replace=True,
            state_dir=state_dir,
        )
        import_mode = f"Railway sync snapshot export ({state_source})"
    else:
        export_endpoint = (
            f"{url.rstrip('/')}/api/observation/export"
            f"?hours={int(observation_hours)}&events=1000&decisions=5000"
        )
        print(f"\nFetching {export_endpoint} ...")
        try:
            export_data = _request_json(export_endpoint, token=token, timeout=60)
            import_summary = import_observation_export(
                export_data,
                replace=True,
                state_dir=state_dir,
            )
            import_mode = "Railway observation export"
        except RuntimeError as exc:
            print(f"WARNING: {exc}")
            print("Falling back to local DB backfill from synced Railway state files ...")
            import_summary = run_backfill(
                replace=True,
                include_live_trades=True,
                include_retro_locks=True,
                state_dir=state_dir,
            )
            import_mode = "legacy state backfill"

    print(f"\nSynced {written} state files")
    print(
        f"Observation import ({import_mode}): "
        f"{json.dumps(import_summary, separators=(',', ':'))}"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Railway state to local")
    parser.add_argument("--url", help="Railway app URL")
    parser.add_argument("--token", help="Dashboard bearer token")
    parser.add_argument("--dir", default=".", help="Local state directory")
    parser.add_argument(
        "--observation-hours",
        type=int,
        default=24 * 14,
        help="Hours of observation history to import",
    )
    args = parser.parse_args()
    sync(
        url=args.url,
        token=args.token,
        state_dir=args.dir,
        observation_hours=args.observation_hours,
    )
