# Probability Contract V1 Amendment 0.1

Date: 2026-07-25
Branch: feature/control-plane-contract-amendment-v1
Baseline checkpoint: dfac9fb

## Why this amendment exists before Phase 1

Phase 1 probability reconciliation implementation depends on strict, explicit, and testable evidence contracts.
The original v1 freeze documented intent but left ambiguity around identity fields, approval status semantics,
reconciliation result eligibility, and structured machine-readable reasons.

This amendment is applied before engine implementation so reconciliation behavior is built on an unambiguous
contract and does not require backward-incompatible repurposing later.

## Added fields and evidence scope

### ProbabilitySnapshotV1 additions

- Scope identity: league, event_id, market_type, period, selection_id, outcome_schema, event_start_time, as_of
- Historical prior: historical_prior_probability, historical_prior_source, historical_prior_version,
  historical_prior_timestamp, historical_prior_sample_scope, historical_prior_missing
- Quote and consensus evidence: execution_quote_id, execution_sportsbook_id, execution_quote_timestamp,
  consensus_constituent_quote_ids, constituent_sportsbook_ids, oldest_constituent_timestamp,
  consensus_calculated_at, complete_fresh_sportsbook_count, market_dispersion
- Version and approval evidence: reconciliation_policy_version, reconciliation_method_version,
  confidence_interval_method_version, model_status, calibration_status, model_approval_status,
  calibration_approval_status, policy_approval_status, reconciliation_approval_status
- Canonical lineage evidence: canonical_input_hash, source_quote_set_id, trust_factors

### ProbabilityReconciliationResultV1 additions

- Authoritative probability field: reconciled_execution_probability
- Compatibility alias field: probability (must equal reconciled_execution_probability)
- Explicit edge and break-even tracking: break_even_probability, reconciled_edge
- Structured decision records: blocking_violations, downgrades, warning_reasons, informational_adjustments
- Version and approval evidence: calibration_version, reconciliation_policy_version,
  reconciliation_method_version, confidence_interval_method_version,
  model_status, calibration_status, model_approval_status, calibration_approval_status,
  policy_approval_status, reconciliation_approval_status
- Data and freshness evidence: required_input_evidence_present, is_stale, quote_age_seconds,
  sportsbook_coverage, complete_fresh_sportsbook_count, quote_freshness_as_of, as_of
- Canonical lineage evidence: canonical_input_hash, source_quote_set_id
- Effective weight tracking: effective_component_weights, trust_factors

## Validation invariants added

- All contract timestamps are timezone-aware UTC
- Naive timestamps are rejected
- Quote and evidence timestamps cannot be later than as_of
- Event and market identity fields must be non-empty
- Probability values must be finite Decimal values in [0,1]
- Confidence bounds must satisfy lower <= probability <= upper
- American odds, decimal odds, implied probability, and break-even probability must be internally consistent
- Weight maps must be nonnegative and sum to one at contract precision where required
- Coverage counts must be nonnegative
- Historical prior missing state is explicit and cannot be confused with zero prior
- Mutable input mappings are copied into immutable contract state

## Execution eligibility rule

ProbabilityReconciliationResultV1 exposes a deterministic execution_eligible property.
A result is execution eligible only when all are true:

- status is reconciled
- blocking_violations is empty
- policy_approval_status is approved
- reconciliation_approval_status is approved
- model_approval_status is approved
- calibration_approval_status is approved
- model_status is approved
- calibration_status is approved
- required_input_evidence_present is true
- is_stale is false

Numeric fallback values in unavailable, stale, rejected, or blocked records remain non-eligible.

## Structured reason taxonomy

DecisionReasonV1 now carries machine-stable fields:

- code
- severity
- category
- message
- observed_value
- threshold
- source_reference

Severity values: info, warning, error
Category values: data_quality, coverage, freshness, model, calibration, policy, reconciliation, validation

## Compatibility behavior

- Existing serialization format remains JSON with deterministic key ordering
- Existing probability alias field (probability) remains present for compatibility
- probability is treated as an alias and must match reconciled_execution_probability
- Existing v1 fields are not silently repurposed

## Deprecated ambiguous semantics

- Generic probability without authority context is deprecated as standalone authority
- Any interpretation of probability that differs from reconciled_execution_probability is invalid
- Historical prior defaults are no longer implicit; missing prior must be explicit

## Migration expectations

- Existing persisted records are still readable
- New producers should populate all added identity, approval, and evidence fields
- Consumers should switch to reconciled_execution_probability and execution_eligible checks
- Structured reason codes should replace free-form string matching in downstream gates
