"""
KALSHI ACCOUNT DIAGNOSTIC
=========================
Pull full trade history, fills, settlements, and compute real P&L.
Run this locally with your API credentials configured in config.py.

Usage:
    KALSHI_ENVIRONMENT=production python kalshi_diagnostic.py
"""

import sys
import json
from datetime import datetime, timedelta, timezone
from kalshi_client import KalshiClient
import config

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    print("=" * 70)
    print("  KALSHI ACCOUNT DIAGNOSTIC")
    print("=" * 70)
    print()

    client = KalshiClient()

    # --- BALANCE ---
    balance = client.get_balance()
    if balance:
        bal_cents = balance.get("balance", 0)
        deposits = config.TOTAL_DEPOSITS_CENTS
        account_pnl = bal_cents - deposits
        print(f"\n  Current Balance:  ${bal_cents / 100:.2f}")
        print(f"  Total Deposited:  ${deposits / 100:.2f}")
        print(f"  Account P&L:      ${account_pnl / 100:+.2f} "
              f"({'profit' if account_pnl >= 0 else 'LOSS'})")
    else:
        print("  ERROR: Could not fetch balance. Check API credentials.")
        return

    # --- CURRENT POSITIONS ---
    print(f"\n{'-' * 70}")
    print("  OPEN POSITIONS")
    print(f"{'-' * 70}")
    positions = client.get_positions()
    if positions and positions.get("market_positions"):
        total_exposure = 0
        for pos in positions["market_positions"]:
            ticker = pos.get("ticker", "?")
            qty = pos.get("position", 0)
            if qty == 0:
                continue
            side = "YES" if qty > 0 else "NO"
            abs_qty = abs(qty)
            # Market value and cost basis
            market_val = pos.get("market_exposure", 0)
            total_cost = pos.get("total_traded", 0)
            realized = pos.get("realized_pnl", 0)
            print(f"  {ticker}")
            print(f"    {side} x{abs_qty}  |  exposure: ${market_val/100:.2f}  |  "
                  f"cost: ${total_cost/100:.2f}  |  realized P&L: ${realized/100:+.2f}")
            total_exposure += abs(market_val)
        print(f"\n  Total exposure: ${total_exposure/100:.2f}")
    else:
        print("  No open positions")

    # --- ALL FILLS (Trade History) ---
    print(f"\n{'-' * 70}")
    print("  ALL FILLS (Complete Trade History)")
    print(f"{'-' * 70}")
    all_fills = []
    cursor = None
    for page in range(20):  # Max 20 pages = 4000 fills
        result = client.get_fills(limit=200, cursor=cursor)
        if not result or not result.get("fills"):
            break
        all_fills.extend(result["fills"])
        cursor = result.get("cursor")
        if not cursor:
            break

    if all_fills:
        # Sort by timestamp
        all_fills.sort(key=lambda f: f.get("created_time", ""))

        total_bought = 0
        total_sold = 0
        total_fees = 0
        fills_by_ticker = {}

        for fill in all_fills:
            ts = fill.get("created_time", "?")[:19]
            ticker = fill.get("ticker", "?")
            side = fill.get("side", "?").upper()
            action = fill.get("action", "?").upper()
            count = fill.get("count", 0)
            price = fill.get("yes_price", 0) if side == "YES" else fill.get("no_price", 0)
            # Use whichever price field is available
            if price == 0:
                price = fill.get("yes_price", 0) or fill.get("no_price", 0)
            cost = count * price
            fee = fill.get("fee", 0) or 0

            if action == "BUY":
                total_bought += cost
            else:
                total_sold += cost
            total_fees += fee

            if ticker not in fills_by_ticker:
                fills_by_ticker[ticker] = []
            fills_by_ticker[ticker].append(fill)

            print(f"  {ts}  {action:4s} {side:3s} x{count:2d} @ {price:3d}c  "
                  f"= ${cost/100:6.2f}  fee: ${fee/100:.2f}  {ticker}")

        print(f"\n  Total fills: {len(all_fills)}")
        print(f"  Total bought: ${total_bought/100:.2f}")
        print(f"  Total sold:   ${total_sold/100:.2f}")
        print(f"  Total fees:   ${total_fees/100:.2f}")
    else:
        print("  No fills found")

    # --- SETTLEMENTS ---
    print(f"\n{'-' * 70}")
    print("  SETTLEMENTS (Resolved Markets)")
    print(f"{'-' * 70}")
    all_settlements = []
    cursor = None
    for page in range(20):
        result = client.get_settlements(limit=200, cursor=cursor)
        if not result or not result.get("settlements"):
            break
        all_settlements.extend(result["settlements"])
        cursor = result.get("cursor")
        if not cursor:
            break

    total_settlement_pnl = 0
    if all_settlements:
        all_settlements.sort(key=lambda s: s.get("settled_time", ""))
        for s in all_settlements:
            ts = s.get("settled_time", "?")[:19]
            ticker = s.get("ticker", "?")
            result_val = s.get("settlement_result", "?")
            pnl = s.get("revenue", 0)  # Revenue from settlement
            total_settlement_pnl += pnl
            won = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "PUSH")
            print(f"  {ts}  {ticker:40s}  result={result_val}  "
                  f"P&L: ${pnl/100:+6.2f}  [{won}]")

        print(f"\n  Total settlements: {len(all_settlements)}")
        print(f"  Settlement P&L:   ${total_settlement_pnl/100:+.2f}")
    else:
        print("  No settlements found")

    # --- RESTING ORDERS ---
    print(f"\n{'-' * 70}")
    print("  RESTING ORDERS (Unfilled)")
    print(f"{'-' * 70}")
    orders = client.get_orders(status="resting")
    if orders and orders.get("orders"):
        for o in orders["orders"]:
            ts = o.get("created_time", "?")[:19]
            ticker = o.get("ticker", "?")
            side = o.get("side", "?").upper()
            action = o.get("action", "?").upper()
            remaining = o.get("remaining_count", 0)
            price = o.get("yes_price", 0) or o.get("no_price", 0)
            print(f"  {ts}  {action:4s} {side:3s} x{remaining:2d} @ {price:3d}c  {ticker}")
    else:
        print("  No resting orders")

    # --- SUMMARY ---
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Deposited:       ${deposits / 100:.2f}")
    print(f"  Current Balance: ${bal_cents / 100:.2f}")
    print(f"  Account P&L:     ${account_pnl / 100:+.2f}")
    if all_settlements:
        wins = sum(1 for s in all_settlements if s.get("revenue", 0) > 0)
        losses = sum(1 for s in all_settlements if s.get("revenue", 0) < 0)
        if wins + losses > 0:
            print(f"  Win/Loss:        {wins}W / {losses}L "
                  f"({wins/(wins+losses)*100:.0f}% WR)")
    print(f"{'=' * 70}")

    # Save raw data for analysis
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "balance_cents": bal_cents,
        "deposits_cents": deposits,
        "account_pnl_cents": account_pnl,
        "fills": all_fills,
        "settlements": all_settlements,
        "positions": positions.get("market_positions", []) if positions else [],
    }
    outfile = "diagnostic_output.json"
    with open(outfile, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Raw data saved to {outfile}")


if __name__ == "__main__":
    main()
