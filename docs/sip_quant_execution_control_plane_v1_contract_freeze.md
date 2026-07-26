# SIP Quantitative Execution Control Plane v1 Contract Freeze

## Freeze identity

- Baseline commit: `925d39361b758294d1e1bea84a924ea4967cf806`
- Baseline subject: `feat(sip): add quantitative execution control plane`
- Branch at freeze: `feature/quant-execution-control-plane-v1`
- Freeze date: 2026-07-25
- Scope: committed Flask APIs, SQLite schema migrations, execution domain
  models, state machines, adapters, and event-chain formats.

This document freezes compatibility against the baseline commit. A frozen
contract is not necessarily safe or approved for production use. Contracts
listed as provisional or unsafe remain observable for compatibility, but MUST
NOT be used as authoritative control-plane gates.

The control plane remains practice/manual only. Live sportsbook execution is
not approved. A thesis, forecast, scanner result, or API payload MUST NOT create
a position without reconciliation, strategy eligibility, risk approval, sizing,
explicit intent creation, and the required confirmation and receipt sequence.

## Contract classifications

- **Authoritative**: the intended v1 control-plane domain contract.
- **Compatibility-only**: persisted or HTTP behavior that existing UI or tests
  may depend on, but which is not an authoritative decision contract.
- **Unsafe/provisional**: an existing bypass. It is documented so it can be
  contained and removed through a versioned migration.
- **Future-disabled**: a shape reserved in code whose activation is explicitly
  outside v1.

## Authoritative probability contract

The canonical in-memory probability object is
`sports.execution.models.ProbabilityRecord`. These values MUST remain distinct:

| Field | Meaning |
| --- | --- |
| `raw_implied_probability` | Probability mechanically implied by a quote. |
| `no_vig_probability` | Vig-removed market probability. |
| `cross_book_consensus_probability` | Consensus probability across books. |
| `raw_sip_probability` | Uncalibrated SIP model output. |
| `calibrated_sip_probability` | SIP output after named calibration version. |
| `reconciled_execution_probability` | Versioned probability eligible for sizing. |
| `confidence_interval` | Lower and upper uncertainty bounds. |
| `break_even_probability` | Price-specific break-even probability. |

Identity and provenance fields are also required: `market_id`, `outcome_id`,
`quote_timestamp`, `forecast_timestamp`, `model_version`,
`calibration_version`, `data_quality_score`, `sportsbook_coverage`,
`reconciliation_method`, `reconciliation_weights`, and `metadata`.

Only `reconciled_execution_probability`, produced by an approved
reconciliation version, may enter position sizing. Callers MUST NOT substitute
`sip_adjusted_probability`, a market probability, raw SIP output, calibrated
SIP output, or an Edge Scanner score.

### Reconciliation algorithm v1

The frozen implementation is `sports.execution.probability.ProbabilityReconciler`.
Its policy fields and defaults are:

- `policy_version`: required
- `reconciliation_version`: required
- `maximum_quote_age_minutes`: `30`
- `minimum_data_quality`: `0.35`
- `minimum_sportsbook_coverage`: `2`
- `consensus_pull_floor`: `0.30`
- `consensus_pull_ceiling`: `0.85`

The last two fields are retained in the frozen data shape but are not consumed
by the baseline algorithm. They MUST NOT be presented as enforced limits.

Inputs are clamped to `[0, 1]` where the implementation explicitly calls
`_clamp`. Quote age is computed in UTC. Naive timestamps are interpreted as
UTC.

If quote age exceeds the policy maximum, the result is:

- status `unavailable`
- probability and both bounds equal to cross-book consensus
- weights `calibrated_sip=0`, `consensus=1`, `historical_prior=0`
- warning `stale quote`

An `unavailable` result MUST block strategy eligibility and sizing even though
it carries a consensus value for explanation.

Otherwise:

```text
coverage_ratio = clamp(book_count / (minimum_book_count * 2))
confidence =
    0.35  * data_quality
  + 0.25  * freshness
  + 0.15  * coverage_ratio
  + 0.125 * strategy_performance
  + 0.125 * model_stability

raw_weight_calibrated = 0.40 * confidence + 0.10
raw_weight_consensus  = 0.35 + 0.20 * (1 - confidence)
raw_weight_prior      = 0.15 + 0.10 * (1 - confidence)

blended = normalized weighted mean of calibrated SIP, consensus, and prior
reconciled = consensus + (blended - consensus) * confidence
uncertainty_width = 0.20 * (1 - confidence)
```

The historical prior defaults to `no_vig_probability`. Result statuses are
exactly `reconciled` and `unavailable`.

## Authoritative strategy and risk contracts

`StrategyEligibilityPolicy` is immutable and versioned by `strategy_id` and
`strategy_version`. The engine blocks on any failed rule:

- minimum sportsbook count
- maximum quote age
- minimum data quality
- approved market type
- approved model version
- minimum reconciled edge
- expected value after vig strictly greater than the configured minimum
- maximum uncertainty width
- open market, when required
- no unresolved injury or lineup blocker, when required

`RiskPolicy` is immutable and versioned by `policy_version`. The engine blocks
on:

- maximum stake per position
- maximum percentage of available bankroll
- event, team, player, league, strategy, or correlated-cluster exposure limit
- daily loss or weekly drawdown limit
- maximum simultaneous positions
- minimum available reserve
- configured model-status restriction

`override_policy` does not make a violating decision approved in the baseline
implementation. It adds a warning only. Any future override must be a separate,
audited transition with actor, reason, and policy version.

Experimental status applies `experimental_model_multiplier` to approved stake.
The default sizing policy independently uses an experimental multiplier of
zero. Both cap attributions MUST remain visible in audit output.

## Authoritative exposure and sizing contracts

`ExposurePosition` identifies league, team, player, event, market, market type,
outcome, sportsbook, strategy, model version, settlement horizon, and correlated
exposure group. It carries open stake, maximum loss, maximum profit, realized
profit/loss, estimated open value, and optional parlay legs.

The frozen exposure calculator:

- counts each top-level position's open stake once in portfolio open stake;
- allocates that stake to every dimension of the top-level position;
- allocates the full parent stake to every parlay leg dimension;
- therefore treats each leg as fully exposed for concentration purposes;
- reduces available bankroll and increases reserved bankroll by proposed stake;
- uses the maximum aggregate absolute dimension total as maximum possible loss;
- uses the same realized P/L for daily and weekly drawdown.

These conservative parlay allocations are authoritative for v1. The
maximum-loss, maximum-profit, open-value, and drawdown formulas are provisional
approximations and MUST NOT be represented as sportsbook settlement simulation.

`KellySizer` uses:

```text
kelly_fraction = (profit_multiple * reconciled_probability
                  - (1 - reconciled_probability)) / profit_multiple
kelly_amount = bankroll * kelly_fraction * fractional_kelly_multiplier
```

The default fractional multiplier is `0.25`. A non-positive profit multiple or
non-positive Kelly fraction produces stake zero. Otherwise the result is capped
by per-position, event, team, player, correlated, strategy, daily-risk, and
available-bankroll limits. Stake and Kelly amount are quantized to cents. The
winning cap name or `kelly` MUST be retained as `limiting_factor`.

## Authoritative OrderIntentV2 state machine

The authoritative state enum is
`sports.execution.models.OrderIntentState`. Allowed transitions are exactly:

```text
thesis_drafted -> forecast_created
forecast_created -> probabilities_reconciled
probabilities_reconciled -> strategy_matched
strategy_matched -> risk_evaluated
risk_evaluated -> risk_approved
risk_approved -> order_intent_created
order_intent_created -> user_confirmed
user_confirmed -> execution_submitted
execution_submitted -> accepted | rejected
accepted -> position_open
position_open -> settlement_pending
settlement_pending -> settled
settled -> reviewed
```

`rejected` and `reviewed` are terminal. Repeating the current state is
idempotent. Submission is idempotent for a previously recorded execution ID.
Opening a position requires both `accepted` state and an acceptance receipt ID.
Price movement above the confirmation tolerance sets `requires_revalidation`
and MUST NOT advance to `accepted`.

There is no transition from thesis, forecast, reconciliation, or strategy
matching directly to order intent, execution, or position. Edge Scanner ingress
may create a candidate only; it may not advance this state machine.

## Execution adapter contract

The authoritative protocol in `sports.execution.models.ExecutionAdapter`
contains:

- `request_quote(order_intent)`
- `validate_availability(order_intent)`
- `submit(order_intent, confirmation)`
- `get_status(execution_id)`
- `cancel(execution_id)`
- `fetch_receipt(execution_id)`

Approved v1 adapters:

- `PracticeExecutionAdapter`
- `ManualRecordingAdapter`

`FutureDeepLinkAdapter` is future-disabled. It may return a link but is not an
approved v1 execution path.

`FutureAuthorizedTransactionalAdapter` is future-disabled and MUST always be
constructed with `authorized=False` in v1. The existence of its constructor
flag is not authorization. No environment variable, request field, or runtime
configuration may activate it.

## Authoritative event-chain contract

The authoritative in-memory event shape is
`sports.execution.models.ExecutionEvent`:

- aggregate: `aggregate_id`, `event_type`, `version`
- causality: `timestamp`, `actor`, `correlation_id`, `causation_id`
- evidence: `payload`, `payload_hash`
- chain: `previous_event_hash`, `event_hash`
- versions: `model_version`, `strategy_version`, `risk_policy_version`

Payload hashes use SHA-256 over canonical JSON with sorted keys and compact
separators. Event hashes use SHA-256 over:

```text
aggregate_id | event_type | version | payload_hash | previous_event_hash
```

The chain is global in list order: every appended event points to the immediately
preceding event, including across aggregate IDs. Validation recomputes payload
and event hashes and verifies every previous-hash pointer.

Timestamp, actor, correlation, causation, and policy/model version metadata are
stored but are not covered by the baseline event hash. This is a documented
integrity gap, not permission to mutate those values. A corrected v2 hash
envelope must be introduced with an explicit hash/schema version; existing
hashes must never be rewritten.

## Persisted schema freeze

The canonical DDL is `PersonalEditionRepository.MIGRATIONS` versions 1 through
6 at the baseline commit. Existing columns, enum strings, JSON payloads, unique
constraints, and index names MUST remain readable.

The tables are:

- v1: `sip_events`, `sip_quotes`, `sip_forecasts`, `sip_evaluations`,
  `sip_feed_state`, `sip_job_runs`
- v2: `sip_resolved_predictions`
- v3: `sip_bankroll_accounts`, `sip_ledger_entries`, `sip_wagers`,
  `sip_wager_legs`, `sip_settlements`, `sip_audit_log`
- v4: `sip_markets`, `sip_outcomes`, `sip_market_quotes`,
  `sip_order_intents`, `sip_bets_v2`, `sip_bet_legs`, `sip_parlays`,
  `sip_positions`, `sip_ledger_transactions`, `sip_settlements_v2`
- v5: `sip_event_catalog`, `sip_probability_snapshots`,
  `sip_model_forecasts_v2`, `sip_synthetic_position_valuations`,
  `sip_exposure_snapshots`, `sip_domain_events`, `sip_aggregate_versions`,
  plus additive market, outcome, quote, and position columns
- v6: `sip_execution_orders`, `sip_execution_state_transitions`,
  `sip_execution_receipts`
- migration ledger: `sip_schema_migrations`

All stored money and probability decimals remain text-encoded. JSON columns
remain JSON text. Future changes MUST use a new numbered migration and MUST be
additive until a separately approved data migration exists.

The v5 `sip_probability_snapshots.sip_adjusted_probability` column is
compatibility-only. It does not represent the authoritative raw, calibrated, or
reconciled SIP probabilities. New control-plane persistence must use separate
columns or a versioned table rather than changing this field's meaning.

The v5 `sip_domain_events` table is compatibility-only. Its unique key is
`(aggregate_type, aggregate_id, event_version)`. Its repository hash format is
not the authoritative global chain described above. A future persistent global
chain requires a new schema version/table or additive hash-version fields.

## HTTP API freeze

The following committed Flask paths and method bindings remain addressable for
compatibility. Existing response keys must not be silently renamed or assigned
new meanings.

### Read APIs

- `GET /api/sip/dashboard`
- `GET /api/sip/wnba`
- `GET /api/sip/games`
- `GET /api/sip/games/<canonical_event_id>`
- `GET /api/sip/choices`
- `GET /api/sip/status`
- `GET /api/sip/markets`
- `GET /api/sip/events`
- `GET /api/sip/events/<event_id>`
- `GET /api/sip/props`
- `GET /api/sip/markets/<market_id>`
- `GET /api/sip/markets/<market_id>/probability-history`
- `GET /api/sip/execution/orders`
- `GET /api/sip/execution/orders/<order_id>`
- `GET /api/sip/portfolio`
- `GET /api/sip/open-positions`
- `GET /api/sip/settled-positions`
- `GET /api/sip/exposure`
- `GET /api/sip/activity`
- `GET /api/sip/domain-events`

### Existing mutation APIs

- `POST /api/sip/refresh`
- `POST /api/sip/practice-wager`
- `POST /api/sip/order-intents`
- `POST /api/sip/execution/orders`
- `POST /api/sip/execution/orders/<order_id>/confirm`
- `POST /api/sip/execution/orders/<order_id>/external-confirmation`

The execution mutation APIs are compatibility-only and unsafe/provisional.
They are not an HTTP binding of the authoritative `sports.execution` gates.
New control-plane APIs MUST be introduced under a versioned namespace and MUST
persist the versions and decisions that justify every transition.

## Known unsafe bypass register

### B-001: order-intent route creates positions directly

- Location: `app.py`, `POST /api/sip/order-intents`
- Behavior: after basic order validation, the route writes a bet, open position,
  synthetic valuation, and stake-reservation ledger transaction.
- Missing gates: probability reconciliation, strategy eligibility, portfolio
  projection, risk approval, Kelly sizing, authoritative state-machine
  transitions, and acceptance receipt.
- Containment: practice/manual compatibility only. MUST NOT be treated as
  control-plane order creation.
- Required remediation: versioned candidate-to-intent orchestration followed by
  explicit confirmation and adapter receipt before position creation.

### B-002: execution-order route fabricates successful validation

- Location: `app.py`, `POST /api/sip/execution/orders`
- Behavior: when no order intent exists, the route creates one with
  `OrderValidationResult(is_valid=True, issues=())` and stake-only market,
  event, and league exposure.
- Missing gates: all quantitative control-plane gates and correlation-aware
  exposure.
- Containment: do not expose as an approved execution ingress.
- Required remediation: require an existing authoritative intent ID and verify
  its immutable decision/version references before any state transition.

### B-003: legacy orchestrator validates without a validation engine

- Location: `sports/personal/execution.py`, `ExecutionOrchestrator.new_order`
- Behavior: every new order transitions from `draft` to `validated`; its receipt
  contains mode only.
- Missing evidence: reconciliation, eligibility, risk, sizing, policy versions,
  input hashes, and actor/correlation lineage.
- Containment: its state machine is compatibility-only and MUST NOT be confused
  with `OrderIntentState`.

### B-004: direct and prefilled execution modes exist

- Locations: `sports/personal/execution.py`, `app.py`
- Behavior: `ExecutionMode` exposes `prefilled_link` and `direct_auto`.
  `SIP_TRANSACTIONAL_SPORTSBOOK_API_AUTHORIZED` can construct the direct adapter
  as authorized, and execution responses report that environment-derived flag.
- Risk: live execution can be activated without an approved control-plane
  release.
- Containment: keep the environment setting false/unset; reject `direct_auto`
  and `prefilled_link` at any externally reachable boundary for v1.
- Required remediation: remove environment-only authorization and require a
  separately reviewed capability system in a future version.

### B-005: synthetic SIP probability is persisted

- Location: `app.py`, order-intent handling
- Behavior: `current_sip_probability` is generated as implied market
  probability plus `0.02`, capped at `0.98`.
- Risk: a fabricated model value can appear as decision or performance data.
- Containment: `sip_synthetic_position_valuations` and all derived values are
  display/demo compatibility data only. They MUST NOT enter reconciliation,
  qualification, sizing, calibration, P/L attribution, or model evaluation.
- Required remediation: replace with nullable real forecast/reconciliation
  references in a versioned schema.

### B-006: GET portfolio mutates persistence

- Location: `app.py`, `GET /api/sip/portfolio`
- Behavior: every read creates and stores a new exposure snapshot.
- Risk: read traffic changes audit and analytical history.
- Containment: consumers must not infer that stored snapshot frequency equals
  decision frequency.
- Required remediation: make the read endpoint side-effect free and persist
  snapshots only from explicit versioned calculation events.

### B-007: two incompatible probability models coexist

- Locations: `sports/personal/wagering.py`,
  `sports/execution/models.py`, `sip_probability_snapshots`
- Behavior: the compatibility model uses `sip_adjusted_probability`; the
  authoritative model separates raw, calibrated, and reconciled values.
- Risk: field mapping can silently collapse probability provenance.
- Containment: no implicit mapping from `sip_adjusted_probability` to any
  authoritative SIP probability.

### B-008: two incompatible state machines coexist

- Locations: `sports/personal/execution.py`,
  `sports/execution/state_machine.py`
- Behavior: legacy execution orders begin at `draft` and can reach submission
  without the thesis-to-risk chain.
- Risk: a legacy `validated` state can be mistaken for control-plane approval.
- Containment: state names must be namespace-qualified in code, APIs, events,
  and UI. Legacy order states are not OrderIntentV2 states.

### B-009: persisted domain events are not the global execution chain

- Locations: `sports/personal/repository.py`, `sports/execution/events.py`
- Behavior: the repository maintains aggregate-version chains; the
  authoritative in-memory helper chains globally in list order. Neither
  persistence nor a transactional global sequence binds the latter yet.
- Risk: audit completeness and cross-aggregate ordering cannot be proven from
  the current database alone.
- Containment: do not claim the persisted log is a verified global immutable
  chain.
- Required remediation: append-only global sequence, transactional predecessor
  selection, hash/schema version, uniqueness constraints, and full-chain
  verification.

### B-010: event hashes omit material metadata

- Location: `sports/execution/events.py`
- Behavior: event hash omits timestamp, actor, correlation ID, causation ID,
  model version, strategy version, and risk-policy version.
- Risk: those audit fields can change without breaking hash validation.
- Containment: treat the baseline hash as payload/order tamper evidence only.
- Required remediation: introduce a v2 canonical event envelope and preserve
  v1 verification for historical events.

### B-011: future transactional adapter can be authorized by constructor

- Location: `sports/execution/adapters.py`
- Behavior: `FutureAuthorizedTransactionalAdapter(authorized=True)` submits an
  accepted result.
- Risk: a future placeholder can be used as live authorization.
- Containment: v1 composition roots and tests MUST assert `authorized=False`;
  the adapter MUST NOT be registered in a Flask blueprint.

### B-012: no committed orchestration binds all authoritative gates

- Location: control-plane package boundary
- Behavior: reconciliation, strategy, exposure, risk, sizing, state machine,
  adapters, and event chain are separately implemented and tested.
- Risk: callers can invoke later stages without evidence from earlier stages.
- Containment: no package function currently constitutes approved end-to-end
  execution.
- Required remediation: add one application service that accepts candidates
  only, persists immutable decisions and versions, advances the state machine,
  and emits events transactionally.

## Change-control rules

1. Do not change the meaning of a frozen field, enum value, state, event type,
   hash, or response key in place.
2. Add new behavior through an explicit versioned contract and additive
   migration.
3. Preserve readers and validators for v1 data after v2 exists.
4. Every authoritative decision must store its input references, result,
   policy/model versions, timestamp, actor, correlation ID, and causation ID.
5. A rejected, unavailable, stale, or zero-size result is a durable decision,
   not an exception to skip.
6. HTTP mutation routes must reject unknown fields that could alter execution
   semantics and require idempotency keys.
7. Practice/manual operation remains the only allowed execution capability.
8. Edge Scanner integration may create candidates only.

## Required regression shield

Before modifying a frozen contract, retain or extend these focused suites:

- `tests/test_probability_reconciliation.py`
- `tests/test_strategy_gate.py`
- `tests/test_exposure_engine.py`
- `tests/test_risk_engine.py`
- `tests/test_position_sizing.py`
- `tests/test_order_intent_state_machine.py`
- `tests/test_execution_event_log.py`
- `tests/test_execution_workflow.py`
- `tests/test_personal_wagering_foundation.py`
- `tests/test_sip_market_experience.py`

Additional tests required before exposing a control-plane blueprint:

- legacy mutation routes cannot manufacture authoritative approval;
- a thesis and Edge Scanner candidate cannot create an intent or position;
- only reconciled probability reaches Kelly sizing;
- unavailable reconciliation blocks all downstream stages;
- policy and model versions are immutable and persisted;
- every invalid state transition fails;
- position opening requires an acceptance receipt;
- practice and manual are the only registered adapters;
- live, transactional, and deep-link modes are rejected regardless of
  environment;
- global event appends are atomic and verify after restart;
- event metadata tampering fails under the new hash version;
- GET read APIs perform no writes.

## Exit criteria for lifting the containment freeze

The unsafe/provisional paths remain contained until all of the following are
true:

- one orchestration service enforces the complete gate sequence;
- authoritative probability, policy, decision, intent, receipt, and event
  records have additive persisted schemas;
- the global event chain is transactional and restart-verifiable;
- legacy mutation routes are disabled, versioned, or routed through the
  authoritative orchestrator;
- only practice/manual adapters are registered;
- shadow/practice operation demonstrates reconciliation, exposure, risk,
  sizing, state, event, settlement, and calibration integrity;
- focused and full test suites pass on the lifting commit;
- a separate review explicitly approves any contract version change.
