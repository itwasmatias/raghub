# SIP Betting Intelligence Layer

This package turns timestamped sportsbook quotes and calibrated SIP predictions
into an explicit `qualified` or `no_bet` decision. It is intentionally
feed-agnostic: provider adapters should normalize their payloads into
`OddsQuote` records before calling the engine.

## Decision flow

1. Match quotes by event, market, selection, and line.
2. Keep the latest snapshot per book and selection.
3. Remove each complete book's vig using proportional, power, or Shin.
4. Exclude stale quotes from consensus and line shopping.
5. Compare SIP's calibrated probability with the multi-book consensus.
6. Calculate expected return at the best fresh price.
7. Apply uncertainty, model agreement, sample size, and calibration gates.
8. Return a qualified opportunity or the exact reasons for abstaining.

```python
engine = BettingIntelligenceEngine(
    BettingPolicy(
        no_vig_method=NoVigMethod.POWER,
        minimum_edge=0.03,
        minimum_expected_return=0.02,
    )
)
assessment = engine.assess(prediction, quotes, as_of=decision_time)
```

Only qualified assessments can be persisted as recommendations. The SQLite
repository stores raw snapshots, the contemporaneous model/market decision,
and later closing prices and outcomes. Passing `as_of` makes historical
evaluation point-in-time safe: future snapshots and already-started events are
ignored.

Provider ingestion, injury/lineup scenario projection, correlated-parlay
pricing, bankroll sizing, and UI presentation remain separate concerns. They
can build on this package without weakening its abstention and audit rules.
