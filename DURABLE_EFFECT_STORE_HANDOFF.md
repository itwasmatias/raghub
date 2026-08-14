# MissionaryX Durable Effect Store v0.1 - Verified Handoff

## Status

The durable effect store is now backed by SQLite with fail-closed schema validation, bounded lock handling, and explicit journal-mode verification for file-backed databases.

Verified by:
- Focused store and effect suites: `159` collected, `159` passed.
- Adjacent control/evidence/delegation suites: `126` passed.

## Verified Behavior

### Storage contract

- File-backed connections require WAL mode.
- Journal mode is queried after `PRAGMA journal_mode=WAL` and must resolve to `wal`.
- WAL establishment retries only bounded lock contention.
- Busy timeout is validated and configurable through `busy_timeout_ms`.
- Store methods fail closed after `close()`.

### Schema contract

- Current-version schema must contain exactly one authoritative version row.
- Partial current-version databases fail closed without repair.
- Contradictory schema versions fail closed.
- Schema validation checks required tables, columns, and indexes before the store is treated as usable.
- Concurrent schema initialization is safe for identical initializers.

### Transactional behavior

- `commit_intent()` and `release_reservation()` are atomic.
- Triggered failures roll back both the intended write and earlier statements in the transaction.
- Reader isolation does not expose uncommitted writer state.
- Lock contention maps to `ConcurrencyConflictError` within a bounded timeout.
- Concurrent process conflict produces exactly one successful write and one semantic conflict.

### Durability and reconstruction

- Intent, dispatch, reservation, and obligation state survive reopen.
- Evidence pointers round-trip through storage.
- Public read paths fail closed on invalid stored enums and timestamps.
- Non-finite authority amounts are rejected at construction, and direct storage corruption with infinities fails closed on read or constraint enforcement.
- Evidence authenticity remains a trust-anchor limitation outside the store.

## Limitations

- Single-host SQLite only.
- Storage integrity is not evidence authenticity.
- The live governed effect gateway remains an unresolved higher-level enforcement concern.

## Requirement To Test Node Matrix

| Req | Requirement | Node(s) |
|---|---|---|
| 1 | Bool `AuthorityReservation.amount` is rejected | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_boolean_authority_amount_is_rejected` |
| 2 | Finite integer and float amounts round-trip | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_finite_integer_and_float_amounts_round_trip` |
| 3 | NaN/non-finite amounts are rejected | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_nonfinite_authority_amount_is_rejected` |
| 4 | Positive infinity in storage fails closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_infinite_amount_in_storage_fails_closed_on_read` |
| 5 | Negative infinity injection is blocked | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_infinite_amount_in_storage_fails_closed_on_read` |
| 6 | Terminal reservation state cannot regress | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_reservation_state_cannot_regress_after_terminal_transition` |
| 7 | Terminal obligation state cannot regress | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_obligation_state_cannot_regress_after_terminal_transition` |
| 8 | Store close blocks follow-on reads | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_store_close_refuses_follow_on_operations` |
| 9 | WAL mode is verified on file-backed init | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_wal_initialization_requires_verified_mode_and_bounded_timeout` |
| 10 | Busy timeout validation rejects invalid values | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_busy_timeout_validation_rejects_invalid_values` |
| 11 | Partial current-version schema fails closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_partial_current_version_schema_fails_closed_without_mutation` |
| 12 | Contradictory schema versions fail closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_contradictory_schema_versions_fail_closed` |
| 13 | Concurrent schema init is safe | `tests/test_durable_effect_store_durability.py::TestConcurrentOperations::test_concurrent_schema_initialization_is_safe` |
| 14 | Commit rollback on second write failure | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_commit_intent_rolls_back_when_second_write_fails` |
| 15 | Release rollback on insert failure | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_release_reservation_rolls_back_when_release_insert_fails` |
| 16 | Reader isolation hides uncommitted writes | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_reader_sees_only_committed_state_during_uncommitted_writer_transaction` |
| 17 | Lock contention maps to bounded domain error | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_lock_contention_maps_to_concurrency_conflict_quickly` |
| 18 | Release-versus-consume concurrent race | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_release_vs_consume_concurrent_race_yields_exactly_one_terminal_result` |
| 19 | Contradictory terminal-decision race | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_contradictory_terminal_dispositions_do_not_both_land` |
| 20 | Terminal-decision evidence round-trip without bypass | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_contradictory_terminal_dispositions_do_not_both_land` |
| 21 | Wrong-domain evidence rejection | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_terminal_evidence_pointer_control_domain_mismatch_fails_closed` |
| 22 | Fingerprint corruption fails closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_terminal_evidence_pointer_reference_fingerprint_corruption_fails_closed`, `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_terminal_evidence_pointer_record_fingerprint_corruption_fails_closed` |
| 23 | ControlDomain corruption fails closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_terminal_evidence_pointer_control_domain_mismatch_fails_closed`, `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_invalid_stored_control_domain_fails_closed_on_open` |
| 24 | Nonfinite stored-number corruption fails closed | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_nonfinite_storage_corruption_fails_closed_on_read[positive-infinity-inf]`, `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_nonfinite_storage_corruption_fails_closed_on_read[negative-infinity--inf]`, `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_nonfinite_storage_corruption_fails_closed_on_read[nan-or-null-nan]` |
| 25 | Partial-schema rejection | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_partial_current_version_schema_fails_closed_without_mutation` |
| 26 | Conflicting process commits | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_two_processes_conflicting_payloads_produce_one_winner` |
| 27 | Canonical state after process contention | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_two_processes_conflicting_payloads_produce_one_winner` |
| 28 | Process restart preserves committed intent state | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_process_restart_persists_exact_committed_state` |
| 29 | Identical process commits are idempotent | `tests/test_durable_effect_store_attacks.py::TestAdditionalDurabilityAttacks::test_two_processes_commit_identical_semantic_intent` |
| 30 | Reservation survives reopen | `tests/test_durable_effect_store_durability.py::TestPersistenceAcrossRestarts::test_authority_reservation_survives_close_reopen` |
| 31 | Obligation survives reopen | `tests/test_durable_effect_store_durability.py::TestPersistenceAcrossRestarts::test_reconciliation_obligation_survives_close_reopen` |
| 32 | Indeterminate posture survives reopen | `tests/test_durable_effect_store_durability.py::TestPersistenceAcrossRestarts::test_indeterminate_posture_with_reserved_authority_survives` |
| 33 | Evidence pointer round-trips exactly | `tests/test_durable_effect_store_durability.py::TestPersistenceAcrossRestarts::test_evidence_pointer_round_trip` |
| 34 | Terminal/evidence authenticity boundary remains enforced | `tests/test_effect_safety.py::TestEvidenceAuthenticityEnforcement::test_1_fabricated_string_evidence_rejected`, `tests/test_effect_safety.py::TestEvidenceAuthenticityEnforcement::test_4_wrong_correlation_key_rejected`, `tests/test_effect_safety.py::TestEvidenceAuthenticityEnforcement::test_14_verified_nothing_landed_releases_exact_reservation`, `tests/test_effect_safety.py::TestTerminalDispositionSecurityBoundary::test_1_well_formed_pointer_not_in_spine_rejected`, `tests/test_effect_safety.py::TestTerminalDispositionSecurityBoundary::test_10_valid_semantically_bound_evidence_succeeds`, `tests/test_effect_safety.py::TestControlDomainBinding::test_cross_domain_provider_evidence_is_rejected` |

## Notes For Claude Verification

- `EffectIntentRegistry` still delegates to `DurableEffectStore`.
- Storage integrity and evidence authenticity remain separate guarantees.
- The unresolved high-level gap is the live governed effect gateway.
- The unresolved medium gap is evidence authenticity not being trust-anchored by the store itself.
