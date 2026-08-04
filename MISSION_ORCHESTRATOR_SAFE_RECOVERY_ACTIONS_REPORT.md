# Mission Orchestrator Safe Recovery Actions v0.1

## Worktree and final status

- Worktree: `/home/matias/raghub-controller-safe-recovery-actions-v0-1`
- Branch: `feature/controller-safe-recovery-actions-v0-1`
- Required HEAD: `5d8b761`
- Fourth Codex verdict received: `REVISE`
- Final correction status: both remaining fourth-REVISE findings corrected with fixture-based offline tests.
- Changes remain unstaged and uncommitted. No push, merge, deployment, live provider, network service, SSH, credential, Codex, Claude, Ollama, or other-worktree access was used.

## Second Codex REVISE findings

### 1. Strict completion evidence

`MissionScheduler.completion_report_error` is the shared completion authority for ordinary reconciliation and repair. A successful completion now requires:

- exact mission, mission-task, and accepted queue identity
- a nonempty successful provider result
- nonempty verifier evidence
- an executed nonempty `argv`, integer zero `exit_code`, and `timed_out == false` for every verifier record
- evidence for every test command required by the mission task
- nonempty changed-result evidence
- valid timezone-aware `started_at` and `finished_at` datetimes
- `finished_at >= started_at`

Empty tests, empty verifier records, missing commands/results, provider-only success, naive/date-only/reversed/malformed timestamps, and identity mismatches cannot plan or apply `COMPLETE_FROM_DURABLE_SUCCESS`.

### 2. Immutable report snapshot and report lock

The authoritative report lock is `reports_root/.reports.lock`. `ReportStore`, the direct CLI report writer, scheduler reconciliation, and repair use it. Repair lock order is:

1. Mission store
2. Durable queue
3. Report evidence
4. Mission event evidence

Completion discovery reads the selected report through one open file, hashes and parses those exact bytes, compares pre/post file-descriptor identity, revalidates directory membership, and detects conflicting reports. Repair passes the validated payload directly into scheduler completion logic rather than allowing an unlocked reread. It revalidates report identity and directory conflicts before mutation. Replacement, disappearance, inode change, changed contents, or a new conflict returns `STALE` or `HUMAN_REVIEW_REQUIRED` without mission completion.

### 3. Mutation-boundary budget checks

Runtime is recomputed during planning, under authoritative apply locks, after `repair_started`, after potentially blocking validation, immediately before queue creation, and immediately before completion persistence. Expiration cannot create a queue record or complete a task. If intent was already durable, one idempotent `repair_expired` event makes the stopped action discoverable and retry classification remains nonduplicating.

Completion accounting preserves the accepted behavior where the successful attempt itself can move the mission to `budget_exhausted`; the pre-completion boundary uses the budget state before applying that successful result.

### 4. Corrupt or partial event tails

`MissionEventLog` rejects:

- a final JSON record without a newline
- truncated JSON
- valid records followed by a partial record
- invalid UTF-8
- partial tails left across restart

Every append validates the complete existing snapshot first, then writes, flushes, and `fsync`s. Corrupt bytes are preserved unchanged and produce explicit corruption/human-review handling. Malformed records are never skipped. Lock-held append requires the internal `MissionEventLog.locked()` token state, preventing an unlocked durability bypass. Scheduler stops before state or dependent queue mutation when event evidence cannot be parsed.

### 5. Legacy queue-ID event evidence

Repair computes the exact current and legacy deterministic IDs for each selected mission task. The complete accepted set is included in action evidence and therefore action identity. Success, failure, loss, cancellation, and blocking events for either exact ID are recognized. Unrelated IDs are rejected. Legacy terminal evidence prevents current-ID rematerialization, and pre-existing legacy success is not ignored.

### 6. Scheduler and repair causal locking

Ordinary scheduler reconciliation and repair share the same store-to-queue-to-report-to-event lock order. `FileLock` now supports safe same-thread reentrancy so existing authoritative store and queue operations can run inside that protocol without lock inversion.

Durable `task_succeeded` precedes mission-state completion and dependent enqueue. Ready dependents additionally require durable matching prerequisite-success evidence. Scheduler-versus-repair races cannot duplicate success, and a success-event write failure leaves mission state nonterminal. Restart recovery supports:

- success event durable before state persistence
- state durable before `repair_applied`
- `repair_started` durable without success

Retries recover missing audit evidence without duplicating `task_succeeded` or `repair_applied`. This is ordered, retryable cross-store recovery and does not claim cross-store atomicity.

### 7. Fully bound `repair_applied`

`NO_LONGER_NEEDED` is no longer based on `action_id` alone. A valid `repair_applied` event binds:

- action ID
- mission ID
- mission task ID
- proposed repair
- expected queue ID
- mission revision
- action evidence fingerprint
- durable effect fingerprint

The effect fingerprint includes the current task-state payload and authoritative queue-record hash. Missing or contradictory metadata fails closed. A historical event cannot suppress repair when the queue/state effect is absent, preserving recurrence after later queue loss.

### 8. Single-read event snapshots

`MissionEventLog.read_snapshot` reads event bytes once and returns immutable parsed events, the exact raw bytes, and their SHA-256 revision. Hashing and classification therefore always describe the same bytes. Planning uses that snapshot directly; apply performs the same operation while holding the event lock. Changed evidence is deterministically replanned or rejected.

### 9. Pre-existing success ordering

Planning classifies valid pre-existing current/legacy `task_succeeded` evidence with nonterminal mission state as `PREEXISTING_SUCCESS_REQUIRES_RECONCILIATION`, not a new ordered repair. It does not append `repair_started` after an older success event. Ordinary scheduler restart reconciliation consumes the existing evidence, persists state without duplicating success, and only then permits dependents.

## Third Codex REVISE findings

### 1. FileLock owner and depth accounting

`FileLock` now tracks the owning thread and reentrant depth independently for each instance while retaining path-wide process coordination. Only the `0 -> 1` transition acquires the operating-system file lock, and only the `1 -> 0` transition releases it. Inner exits decrement depth without deactivating the outer scope, exceptions unwind correctly, and foreign-thread release raises explicitly.

Focused tests cover same-instance nesting, inner and outer exceptions, foreign release, immediate acquisition by a second thread, repeated scheduler cycles, and nested store, queue, report, and event operations. Every tested authoritative path leaves the path owner/depth registry empty.

### 2. Thread-bound event-lock authorization

`MissionEventLog.locked()` now yields an opaque scope token bound to the exact event-log instance, current thread, and active context. `append_locked()` requires that exact token. Tokens from another instance, another thread, a completed scope, or fabricated objects are rejected before any bytes are written. Nested same-thread scopes remain valid independently and each token is invalidated on its own exit.

Lock-held appends still validate the complete existing tail before writing and retain append-only binary writes, `flush`, and `fsync`.

### 3. State-before-dependent-enqueue ordering

Scheduler completion now follows this causal sequence under the shared store, queue, report, and event lock order:

1. Append and fsync nonduplicate `task_succeeded` evidence.
2. Persist the prerequisite task completion state.
3. Reload the mission state and event snapshot and verify their exact task and queue identities agree.
4. Compute readiness only from that verified durable state and evidence.
5. Create a dependent queue record and then persist its mission-state linkage.

Failure-injection tests cover interruption after success append, after prerequisite state persistence, before and after dependent queue creation, restart at each boundary, and repeated reconciliation. Restarts create one dependent queue record, one success event, and one dependent enqueue transition without authorizing dependents from event/state disagreement.

This is ordered, idempotent cross-store recovery; it is not a claim of cross-store atomicity.

### 4. Post-success-event runtime boundary

Repair recomputes mission and task runtime budgets during planning, after authoritative locks are held, after `repair_started`, before success evidence, immediately after `task_succeeded` append/fsync, and immediately before mission-state persistence. Both the mission `max_runtime_seconds` deadline and task `timeout_seconds` deadline are enforced.

If a deadline is crossed after repair intent, one idempotent `repair_expired` event records the action ID, violated budgets, and whether success evidence was already durable. Scheduler and repair treat that marker as a fail-closed boundary: they do not complete the mission task or enqueue dependents from the associated success event, and retries do not duplicate `repair_started`, `task_succeeded`, or `repair_expired`.

The exact partial state after post-success expiration is:

- the original provider report and queue success record remain unchanged
- one durable `repair_started` event remains
- one durable `task_succeeded` event may remain
- one durable `repair_expired` event has `success_evidence_durable: true`
- mission and task state remain at their pre-completion values
- dependent queue records and dependent mission-state linkage remain absent

Event history is never deleted or rewritten.

### 5. Removal of unbound `NO_LONGER_NEEDED`

An exact queue record appearing after planning no longer produces `NO_LONGER_NEEDED`. Without a matching active `repair_started` recovery path and complete bound `repair_applied` evidence, the result is `STALE` or `HUMAN_REVIEW_REQUIRED`; externally appearing work is not silently adopted.

Interrupted controller-owned repairs may resume only from their exact durable intent and are reported as `APPLIED` when missing linkage or audit evidence is explicitly recovered. `NO_LONGER_NEEDED` remains available only when the complete repair binding matches the current mission/task state and exact durable effect fingerprint. Forged action-only audit records, changed payloads, cross-mission records, missing effects, and historical effects that later disappear cannot suppress recurrence. Ordinary concurrent-applicant idempotency remains intact.

## Fourth Codex REVISE findings

### 1. Exact prerequisite-success identity validation

Dependent readiness now requires one explicit `task_succeeded` event whose
mission ID, mission-task ID, and queue-task ID exactly agree with the current
mission state. The queue identity must be the exact deterministic current or
legacy ID for that prerequisite; a missing queue ID is never a wildcard.

Wrong, missing, unrelated, malformed, conflicting, cross-file, and
wrong-event-type evidence fails closed before dependent queue creation or
mission-state linkage. Exact current and legacy identities still advance
dependents, and restart recovery retains one success event and one dependent
record.

### 2. Fully bound `repair_started` recovery authorization

One shared exact repair-intent validator now authorizes `repair_started`
deduplication, retry, and interrupted recovery. It binds:

- action, mission, mission-task, and expected queue identities
- finding and proposed repair type
- mission revision and action evidence fingerprint
- expected canonical queue payload fingerprint
- the current mission/task state and, for recovery, the exact durable queue
  record, queue state, and payload

Incomplete, cross-mission, cross-task, stale-recurrence, conflicting, or
payload-mismatched evidence cannot adopt external work, link the task, mutate
the mission, write `repair_applied`, or return `APPLIED` or
`NO_LONGER_NEEDED`. An exact controller-owned interrupted repair may resume
and returns `APPLIED`. Fully bound `repair_applied` plus its exact durable
effect remains the only basis for `NO_LONGER_NEEDED`; concurrent-applicant
idempotency and recurrence after later queue loss remain intact.

## Preserved accepted behavior

- Reserved metadata protection
- Split-state detection
- Presentation-only `created_at`
- Physically read-only CLI dry-run
- Concurrent applicant idempotency
- Recurrence after later queue loss
- Current and legacy deterministic-ID migration
- Existing queue, scheduler, retry, and provider authorities
- No fix-all or periodic repair execution

## Final verification

- Focused recovery:
  `python3 -m pytest -q tools/ai_controller/tests/test_mission_safe_recovery.py tools/ai_controller/tests/test_mission_repair_cli.py tools/ai_controller/tests/test_mission_reconciliation_events.py`
  - **174 passed**
- Mission suite:
  `python3 -m pytest -q tools/ai_controller/tests/test_mission*.py`
  - **275 passed**
- Full controller suite:
  `python3 -m pytest -q tools/ai_controller/tests`
  - **405 passed**
- Compilation:
  `PYTHONPYCACHEPREFIX=/tmp/raghub-safe-recovery-pycache python3 -m compileall -q tools/ai_controller`
  - **passed**
- `git diff --check`
  - **passed**

## Final result

The fourth Codex REVISE corrections are complete. Prerequisite success must
carry an exact current or legacy identity, and interrupted repair recovery
requires a fully bound controller-owned `repair_started` intent and exact
durable effect.
