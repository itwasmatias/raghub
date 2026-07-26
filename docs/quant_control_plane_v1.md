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
