# Worker Heartbeat, Lease & Intentional Sleep State v0.1

## Architecture and state model

- `federation/heartbeat.py` defines immutable, canonically serialized,
  authenticated heartbeat submissions.
- `federation/heartbeat_registry.py` validates submissions and reconstructs an
  append-only controller-authoritative event history.
- `federation/worker_lease.py` owns the 30-second heartbeat and 90-second lease
  policy constants.
- `federation/worker_liveness.py` defines immutable lease snapshots, power
  capabilities, requested power states, and the `online`, `stale`, `offline`,
  `intentionally_sleeping`, and `waking` states.
- `federation/__init__.py` exports the public heartbeat and liveness contract.

For an active, healthy worker, age through 30 seconds is `online`, age over 30
seconds and under 90 seconds is `stale`, and age at or beyond 90 seconds is
`offline`. Degraded health is `stale`; unhealthy health is `offline`.
Intentional sleep, hibernate, and shutdown requests map to
`intentionally_sleeping`. A waking request maps to `waking`. Only `online` is
routing-eligible. A later normal authenticated heartbeat deterministically
restores `online`.

## Timing policy

The normal heartbeat interval is 30 seconds and the availability lease is 90
seconds. Controller-received UTC time starts and expires each lease. Worker UTC
timestamps are authenticated supporting evidence only and never affect lease
duration. Future worker timestamps, naive timestamps, malformed timestamps,
invalid expiration evidence, and controller-time rollback relative to durable
accepted evidence fail closed.

## Intentional sleep and power-capability model

Workers can advertise display control, sleep, hibernate, shutdown, Wake-on-LAN,
and scheduled-wake support. Evidence can retain the requested power state,
sleep reason, expected wake time, wake method, and whether active work was
checkpointed. Intentional sleep remains distinct from unexpected lease expiry
across restart. Waking remains ineligible until a normal authenticated
heartbeat arrives.

No display, sleep, hibernate, shutdown, wake, timer, or other power command is
executed. Actual power-command execution is explicitly deferred.

## Security and threat model

The implementation reuses the required explicit controller-owned integrity-key
contract. Keys are mandatory byte strings of at least 32 bytes; there are no
generated, optional, hardcoded production, or fallback keys.

Canonical JSON and HMAC-SHA-256 authenticate the complete worker submission.
A second HMAC-SHA-256 authenticates worker and registry identity, heartbeat and
event sequences, boot/session identity, worker and controller times, health,
power capabilities and intent, sleep metadata, checkpoint state, the submitted
tag, predecessor tags, and lease expiration. Verification uses
`hmac.compare_digest` through the shared integrity helpers.

The registry rejects fabricated workers, registry or worker substitution,
wrong-key evidence, altered content, changed duplicate sequences, replay,
out-of-order or skipped sequences, broken predecessor chains, malformed or
duplicate JSON fields, reordered records, invalid timestamps, and rewritten
ordinary SHA-256 evidence. Exact complete duplicates are idempotent and do not
append evidence or extend the original lease.

## Persistence and concurrency behavior

Accepted evidence is newline-delimited, append-only, HMAC-authenticated, and
globally predecessor-chained. Per-worker sequences and authentication
predecessors are also reconstructed and checked. Every write holds an exclusive
`fcntl` file lock, rereads authoritative bytes under that lock, appends one
event, flushes, and calls `fsync`. Concurrent duplicate writers therefore
produce one accepted durable event and one deterministic result.

Restart reconstruction validates every byte before returning state. Corrupt,
truncated, contradictory, reordered, rewritten, wrong-key, or impossible
evidence fails closed. Inspection uses shared locks only when the evidence file
already exists. Inspection of a missing store creates no file, directory,
lock, key, or repair evidence, and failed inspection preserves original bytes.

## Codex optional-enforcement defect

Codex reproduced a fail-open public API: both `TaskRouter` and
`TaskDispatchCoordinator` accepted `heartbeat_registry=None` and skipped
liveness enforcement. A sleeping worker was correctly rejected when a registry
was supplied but received both a routing assignment and a new dispatch offer
when callers omitted it.

The correction removes the defaults and optional branches. Both public
constructors now require explicit injection of a concrete, authoritative
`HeartbeatRegistry`. Missing arguments, explicit `None`, unrelated objects, and
fabricated subclasses fail immediately. `TaskRouter` also requires the
heartbeat registry to be bound to the exact `NodeRegistry` it routes.
`TaskDispatchCoordinator` requires the heartbeat registry and dispatch
persistence to share the controller integrity key.

## Routing and dispatch enforcement

`TaskRouter` checks authoritative liveness before capability filtering and
revalidates the deterministically selected worker immediately before producing
an assignment. Missing, stale, offline, intentionally sleeping, and waking
workers cannot receive new routing assignments. Existing capability,
preferred-capability, node-status, staleness, and deterministic ranking rules
remain intact.

`TaskDispatchCoordinator` independently checks the assigned worker's current
lease at `create_offer`, including a final check inside the locked mutation
immediately before appending a new offer. It does not trust the earlier routing
decision. A failed first offer creates no dispatch evidence. An already
persisted identical offer remains idempotent and unchanged if liveness later
changes, and existing offers can still reach normal terminal states.

No optional registry, disabled-liveness flag, no-op registry, empty-registry
fallback, compatibility bypass, alternate constructor, or factory remains.
This correction adds no cancellation, retry, reassignment, recovery, execution,
networking, queue, or background-monitoring behavior.

## Regression tests

The correction adds or extends these exact regressions:

- `test_task_router_requires_authoritative_heartbeat_registry`
- `test_task_router_rejects_heartbeat_registry_for_another_node_registry`
- `test_dispatch_coordinator_requires_authoritative_heartbeat_registry`
- `test_public_constructors_have_no_liveness_bypass`
- `test_liveness_state_controls_new_routing_and_dispatch`, parameterized for
  `online`, `stale`, `offline`, `intentionally_sleeping`, and `waking`
- `test_worker_becoming_ineligible_after_routing_is_rejected_before_offer`
- `test_existing_offer_can_reach_terminal_state_after_worker_sleeps`

The constructor regressions cover omission, explicit `None`, arbitrary objects,
fabricated `HeartbeatRegistry` subclasses, mismatched node registries, and the
absence of constructor defaults. All router, dispatcher, restart,
multiprocessing, wrong-key, and inspection fixtures now inject a real
authoritative registry explicitly.

The pre-existing heartbeat suite continues to cover authenticity, append-only
persistence, clock rollback, concurrent writers, corruption, read-only
inspection, intentional sleep metadata, waking, and online restoration without
weakening those contracts.

## Validation results

- Focused requested suite: `144 passed in 3.26s`
- Full repository suite: `925 passed in 33.98s`
- `PYTHONPYCACHEPREFIX=/tmp/raghub-heartbeat-copilot-fix-pycache python3 -m
  compileall -q federation tests`: passed
- `git diff --check`: passed

## Changed files and final Git status

- `WORKER_HEARTBEAT_RECOVERY_V0_1_REPORT.md`
- `federation/__init__.py`
- `federation/heartbeat.py`
- `federation/heartbeat_registry.py`
- `federation/task_dispatcher.py`
- `federation/task_router.py`
- `federation/worker_lease.py`
- `federation/worker_liveness.py`
- `tests/test_dispatch_inspection.py`
- `tests/test_task_dispatcher.py`
- `tests/test_task_router.py`
- `tests/test_worker_heartbeat.py`

Final status is intentionally unstaged: six tracked files are modified and six
files are untracked.

```text
 M federation/__init__.py
 M federation/task_dispatcher.py
 M federation/task_router.py
 M tests/test_dispatch_inspection.py
 M tests/test_task_dispatcher.py
 M tests/test_task_router.py
?? WORKER_HEARTBEAT_RECOVERY_V0_1_REPORT.md
?? federation/heartbeat.py
?? federation/heartbeat_registry.py
?? federation/worker_lease.py
?? federation/worker_liveness.py
?? tests/test_worker_heartbeat.py
```

`git diff --cached --name-only` is empty. Nothing was staged or committed.
