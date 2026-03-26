"""
Run the retrospective settlement-lock backfill and print a compact report.
"""

from settlement_lock import SettlementLockPaper


def main():
    paper = SettlementLockPaper()
    summary = paper.backfill_from_learning_state()

    print()
    print("Retrospective Settlement-Lock Backfill")
    print("=====================================")
    print("Method:", summary.get("analysis_type", ""))
    print("Generated:", summary.get("generated_at", ""))
    print("Scan days:", summary.get("scan_days_considered", 0))
    print("Lockable rows:", summary.get("history_rows", 0))
    print("Unique tickers:", summary.get("unique_lockable_tickers", 0))
    print("Scorable candidates:", summary.get("scorable_candidates", 0))
    print(
        "Estimated gross P&L: %+dc ($%+.2f)"
        % (
            summary.get("estimated_profit_cents", 0),
            summary.get("estimated_profit_cents", 0) / 100.0,
        )
    )
    print("Average price: %.2fc" % (summary.get("average_price_cents", 0) or 0))
    print("Average profit: %.2fc" % (summary.get("average_profit_cents", 0) or 0))
    print("Unscorable lockable rows:", summary.get("unscorable_lockable_candidates", 0))
    print()

    print("Top skip reasons:")
    for reason, count in list(summary.get("by_skip_reason", {}).items())[:8]:
        print("  - %s: %d" % (reason, count))

    print()
    print("Top cities:")
    for city, count in list(summary.get("by_city", {}).items())[:8]:
        print("  - %s: %d" % (city, count))

    print()
    print("Top examples:")
    for row in summary.get("top_examples", [])[:8]:
        print(
            "  - %s | %s | %s @ %sc -> %+dc | actual=%sF"
            % (
                row.get("snapshot_day", ""),
                row.get("ticker", ""),
                row.get("lock_side", "").upper(),
                row.get("price_cents", 0),
                row.get("estimated_profit_cents", 0),
                row.get("actual_high_f", ""),
            )
        )


if __name__ == "__main__":
    main()
