# Rebuild Plan

## Goal

Replace the current monolithic JSON-driven bot with a safer architecture that:

- keeps live and paper execution behavior aligned
- stores all important actions in a durable event store
- evaluates each strategy independently
- requires evidence before any strategy is allowed to trade live

## Principles

- No big-bang rewrite
- Keep paper mode on by default
- Migrate one layer at a time
- Prefer append-only records over mutable state blobs
- Never let "learning" silently change live behavior

## Phases

### Phase 1: Event Store Foundation

- Add SQLite event store
- Dual-write scan cycles, scan decisions, and paper events
- Keep existing JSON files for compatibility
- Move history/reporting reads toward the database

### Phase 2: Shared Order Model

- Define one order object for both paper and live
- Risk approves the final order object only
- Paper and live share fill, expiry, and settlement rules

### Phase 3: Strategy Registry

- Split broad trading logic into named strategies
- Track per-strategy candidate counts, fill rates, P&L, and drawdown
- Start with settlement-lock NO as champion
- Keep challengers paper-only

### Phase 4: Review Engine

- Replace trade-log-centric reviewer with event-store evaluator
- Score every scan decision, paper order, fill, and settlement
- Generate daily strategy reports and promotion decisions

### Phase 5: Live Promotion Gates

- Require minimum resolved paper samples
- Require positive net expectancy after fees/slippage
- Require bounded drawdown and stable fill behavior
- Require deploy SHA parity and healthy telemetry endpoints

## Success Metrics

- Zero live/paper execution drift for the same signal path
- Zero deploy drift between local, Git, and Railway SHA
- Strategy-level profit factor above 1.0 after fees
- Stable daily paper telemetry and history export
- No risk-cap violations from execution-price changes

## Immediate Build Slice

This repository now has the Phase 1 starting point:

- SQLite event store for scan cycles, scan decisions, and paper events
- observation journal dual-write into both JSONL and SQLite
- history endpoints can migrate to the database without breaking current files

## Go-Live Standard

Do not re-enable live trading until the active strategy has:

- enough resolved paper trades to be statistically meaningful
- positive net P&L after fees and slippage assumptions
- acceptable drawdown
- stable fill behavior
- healthy deploy and telemetry checks for multiple days
