# SIP Quantitative Control Plane v1

This document freezes the contract layer for the quantitative control plane.
It does not change runtime behavior and does not introduce new execution engines.

## Purpose

The control plane is the authority boundary that future probability reconciliation,
strategy eligibility, exposure, risk, sizing, execution, settlement, and feedback code must obey.

The current codebase still has direct wager, position, ledger, and forecast paths. Those paths are inventoried here so they can be wrapped or removed in Phase 1.

## Current Entry Points

### Routes
- `POST /api/sip/order-intents`
- `POST /api/sip/execution/orders`
- `POST /api/sip/execution/orders/<order_id>/confirm`
- `POST /api/sip/execution/orders/<order_id>/external-confirmation`
- `GET /api/sip/execution/orders`
- `GET /api/sip/execution/orders/<order_id>`
- `POST /api/sip/practice-wager`
- `GET /api/sip/markets`
- `GET /api/sip/markets/<market_id>`
- `GET /api/sip/markets/<market_id>/probability-history`
- `GET /api/sip/portfolio`
- `GET /api/sip/open-positions`
- `GET /api/sip/settled-positions`
- `GET /api/sip/exposure`
- `GET /api/sip/activity`
- `GET /api/sip/domain-events`
- `GET /api/sip/choices`
- `GET /api/sip/status`
- `POST /api/sip/refresh`
- `GET /api/sip/games`
- `GET /api/sip/games/<canonical_event_id>`
- `GET /api/sip/props`

### Services
- `MoneylineQualificationService.evaluate`
- `BaselineMoneylineModel.predict`
- `PersonalEditionService.refresh`
- `PersonalEditionService.snapshot`
- `PersonalEditionService.resolve_completed_predictions`
- `PersonalEditionScheduler.run_pending`
- `SituationRoomService.get_snapshot` and related forecast/evidence APIs

### Repository Methods
- `save_bankroll_account`
- `append_ledger_entry`
- `save_wager_ticket`
- `save_settlement`
- `save_probability_snapshots`
- `save_forecasts`
- `save_evaluation`
- `save_synthetic_position_valuation`
- `save_exposure_snapshot`
- `record_domain_event`
- `append_domain_event`
- `save_execution_order`
- `append_execution_transition`
- `save_execution_receipt`
- `save_order_intent`
- `save_order_intent_if_absent`
- `save_position`
- `save_ledger_transaction`
- `save_settlement_v2`

### UI Actions
- The SIP market UI submits order intents directly from `static/sip_market_ui.js`.
- The SIP experience calculator submits practice wager estimates from `static/sip_experience.js`.
- The practice wager forms in `templates/sip_personal.html`, `templates/betting_intelligence.html`, `templates/sip_markets.html`, and `templates/sip_market_detail.html` expose direct calculation and recording actions.
- The market cards and filter panels in `static/sip_market_ui.js` can surface qualified opportunities before any control-plane gate exists.

### Scripts and Workers
- `scripts/sip.py migrate`
- `scripts/sip.py refresh`
- `scripts/sip.py evaluate`
- `scripts/sip.py run`
- `scripts/sip.py scheduler`
- `scripts/sip.py train-model`
- `scripts/sip.py prepare-history`
- `PersonalEditionScheduler`

### Test Helpers That Resemble Production Paths
- `_service` in `tests/test_execution_workflow.py`
- `_forecast` in `tests/test_personal_edition.py`
- `_rows` in `tests/test_personal_edition.py`
- `make_client` in `tests/test_sip_experience.py`

## Contract Freeze Summary

The contract package is frozen in `sports/execution/contracts.py` and `sports/execution/statuses.py`.

Contracts are frozen dataclasses with explicit version fields, UTC timestamp fields, and stable JSON serialization rules.

This freeze is amended by **Probability Contract V1 Amendment 0.1**.
See `docs/control_plane_contract_amendment_0_1.md` for added identity fields,
structured reason records, approval statuses, eligibility invariants,
and compatibility behavior.

### Probability Separation Rules

The following values remain distinct by contract:
- Raw American odds
- Raw decimal odds
- Raw implied probability
- No-vig probability
- Cross-book consensus probability
- Raw SIP probability
- Calibrated SIP probability
- Reconciled execution probability
- Break-even probability
- Confidence bounds

Only the reconciled execution probability may flow into sizing or order-intent approval contracts.

### Serialization Rules

- Monetary amounts use `Decimal` and serialize as strings.
- Probabilities use `Decimal` and serialize as strings.
- Timestamps are UTC ISO-8601 strings.
- Contract records are frozen; nested collections are write-once by convention.
- Version fields are explicit in every contract.

## State Transition Contract

Valid lifecycle:

```text
THESIS_DRAFTED
→ FORECAST_CREATED
→ PROBABILITIES_RECONCILED
→ STRATEGY_MATCHED
→ RISK_EVALUATED
→ RISK_APPROVED
→ ORDER_INTENT_CREATED
→ USER_CONFIRMED
→ EXECUTION_SUBMITTED
→ ACCEPTED or REJECTED
→ POSITION_OPEN
→ SETTLEMENT_PENDING
→ SETTLED
→ REVIEWED
```

Evidence and actor expectations:
- `THESIS_DRAFTED`: research note, operator or analyst
- `FORECAST_CREATED`: model output, model worker
- `PROBABILITIES_RECONCILED`: reconciliation snapshot, control-plane reconciler
- `STRATEGY_MATCHED`: strategy decision, strategy gate
- `RISK_EVALUATED`: exposure and policy inputs, risk engine
- `RISK_APPROVED`: approved or blocked result, risk authority
- `ORDER_INTENT_CREATED`: sizing and decision bundle, order-intent service
- `USER_CONFIRMED`: explicit user confirmation or manual receipt
- `EXECUTION_SUBMITTED`: adapter submission evidence
- `ACCEPTED` / `REJECTED`: acceptance receipt or rejection receipt
- `POSITION_OPEN`: accepted receipt and open-position record
- `SETTLEMENT_PENDING`: unresolved settlement state
- `SETTLED`: settlement result and idempotency key
- `REVIEWED`: post-settlement review and feedback record

Invalid transitions are any jumps that skip an evidence-bearing stage or attempt to move backwards without a correction event.

Reversal and correction are append-only. A correction creates a new event that supersedes the prior decision; the prior record remains intact.

## Recommended Phase 1 Order

1. Wire contract serialization into probability and decision persistence.
2. Replace direct wager and position paths with order-intent-only entry points.
3. Route qualification output through the strategy eligibility contract.
4. Route exposure and risk checks before sizing or execution.
5. Add settlement and feedback contracts after the approval path is sealed.

## Phase 2A Additive Exposure Authority Freeze

Phase 2A introduces additive authoritative exposure contracts under
`sports.execution.exposure_v1`.

Legacy compatibility shapes remain frozen and readable as legacy summary views:

- `sports.execution.exposure.ExposureCalculator`
- `sports.execution.exposure.ExposurePosition`
- `sports.execution.contracts.ExposureSnapshotV1`

Phase 2A does not modify API routes, repository integration, risk sizing behavior,
or persistence migration. It freezes contract semantics only.

### Exposure authority constraints

- No probability values are accepted in exposure input contracts.
- Exposure uses reconciliation references and version lineage only.
- Monetary values use `Decimal`; USD quantization is explicit at `0.01` with
  `ROUND_HALF_EVEN`.
- Contribution-ledger semantics separate top-level monetary totals from
  non-additive dimensional allocation evidence.
- Projection contracts are immutable and cannot mutate bankroll references,
  ledger balances, positions, or reservations.
- Authoritative projections require actual bound snapshot, candidate, and
  bankroll objects; opaque IDs and hashes are insufficient.
- Constructed projections retain those actual objects, and future downstream
  consumers must call `validate_authoritative_projection_integrity` before a
  projection can inform risk, sizing, or execution.
- Projected totals derive exclusively from current snapshot totals plus verified
  candidate economics.
- Candidate parlay dimensions derive from the actual parent and complete leg
  identities and remain non-additive; straight candidates also require their
  complete frozen dimensional identity.
- Record collection validation accepts actual records and resolves corrections
  and reversals through an append-only acyclic single-head lineage graph whose
  edges retire prior heads without rewriting frozen targets and whose
  replacements preserve complete wager and leg identity. Terminal historical
  heads remain hash-bound evidence but are excluded from current aggregation;
  corrected and superseded states require a replacement child.
- Record, candidate, bankroll, contribution, and projection authority boundaries
  reconstruct public fields through the frozen constructors. Semantic/type
  validation therefore does not trust a recomputed hash or process-local seal.
- Canonical sportsbook identity uses the closed V1 provider taxonomy;
  display-name and separator aliases reject before import or manual-ticket
  deduplication.
- Snapshot construction binds actual final-head records, an actual bankroll
  object, and complete monetary and full-identity dimensional contribution
  ledgers. Snapshot totals, counts, aggregates, concentration/correlation
  outputs, and denominator percentages derive from that evidence rather than
  caller values. Positive exposure against a zero bankroll denominator derives
  an unavailable status and explicit reasons; non-available snapshots cannot
  be promoted into authoritative projections. Bound snapshot evidence is
  retained and revalidated at projection assembly, including the original
  availability status and reasons. Full historical lineage has independent
  derived construction evidence, remains scope-checked, and participates in
  candidate collision detection even when it contributes zero current
  economics.
- Keyed seals are process-local tamper evidence only. They are excluded from
  public dataclass hashes, are not durable signatures, and do not defend against
  hostile code invoking module-private helpers. Cross-process payloads must be
  reconstructed through V1 constructors.
- Manual recorded-real records preserve typed immutable receipt and actor
  provenance, point-in-time eligibility, and duplicate-execution identity
  without creating execution authority.
- Reason codes use a closed taxonomy and canonical ordering includes severity
  and deterministic, type-restricted metadata.

Phase 2A does not prove repository completeness or snapshot-ID issuance.
Phase 2B must assemble authoritative history from a repository
manifest/idempotency boundary and run snapshot/projection integrity validation
before any downstream financial decision.

See `docs/exposure_engine_v1_contract_freeze.md` for the complete Phase 2A freeze.
