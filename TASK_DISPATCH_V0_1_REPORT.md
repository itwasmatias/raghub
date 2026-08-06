# Task Dispatch v0.1 Review Correction Report

## Codex defects corrected

Codex reproduced two independent integrity failures:

1. A fabricated assignment record was accepted after an attacker recomputed the
   public, unkeyed SHA-256 assignment fingerprint.
2. An accepted dispatch event could be rewritten as rejected, its reason changed,
   and the unkeyed event digest or entire digest chain recomputed so restart
   inspection accepted the forged terminal history.

Both defects came from treating attacker-recomputable hashes as authenticity
proof. Deterministic public identities remain SHA-256-based, but persisted
authority is now independently authenticated with a controller-held key.

## Keyed-authenticity design

`federation.integrity` provides the shared standard-library HMAC-SHA256
implementation and enforces an explicit `bytes` key of at least 32 bytes. There
is no default, fallback, environment lookup, key-file read, key-file write, or
secret serialization. Assignment and dispatch records use separate
domain-separation labels. Verification uses `hmac.compare_digest`.

The controller explicitly supplies the same key to
`DurableAssignmentRegistry` and `TaskDispatchCoordinator`; construction rejects
different keys. Restart and multiprocessing fixtures pass the key explicitly.
The assignment registry authenticates the complete canonical record, including
sequence, deterministic identity, routing revision, mission, task, coordinator,
worker, capabilities, authorization, and approval provenance.

Each dispatch `resulting_digest` is now an HMAC authentication tag over the
complete canonical event excluding that tag. `predecessor_digest` carries the
previous event's authentication tag, binding sequence, offer and assignment
identity, mission, task, coordinator, worker, actor, authorization and approval
evidence, transition, reason, timestamps, expiration, and the authenticated
history prefix.

## Authoritative assignment source

`DurableAssignmentRegistry` is the narrow durable provenance source for successful
routing assignments. `TaskRouter` optionally records its existing routing result;
the registry does not select nodes or implement another routing algorithm. Each
record binds the mission, task, coordinator, selected worker, required
capabilities, authorization level, approval requirement, routing revision, and a
deterministic SHA-256 fingerprint plus an HMAC-SHA256 authentication tag.
Dispatch accepts only an assignment ID and resolves the complete authenticated
record from the registry. Caller-created assignment objects are not authority.
Unknown, altered, ambiguous, conflicting, unauthenticated, and
foreign-coordinator records fail closed.

## Durable dispatch-store architecture

The dispatch authority is one append-only UTF-8 JSONL event log. Mutations use an
exclusive `flock`, read and validate the complete log once, reconstruct all
offers, apply one allowed transition, append one canonical JSON record plus one
newline, flush, and `fsync` before success is returned. Inspection uses a shared
lock on an existing log and never creates a directory, lock file, data file, or
event. Assignment and dispatch logs are deliberately separate durable stores;
there is no cross-store atomicity claim.

## Deterministic offer identity

The offer ID is `dispatch-` plus the SHA-256 digest of canonical JSON containing
the authoritative assignment ID, assignment fingerprint, mission ID, and task
ID. One mission/task can have one offer. Identical creation returns the existing
offer without appending; any conflicting recreation is rejected.

## Transition and idempotency rules

Only the assigned worker can accept or reject, and only the exact coordinator can
cancel. Expiration occurs at `now >= expires_at`. Exact accept, reject, cancel,
and expiration replay returns the authoritative terminal offer without another
event. A different terminal decision raises a stable terminal-state conflict
containing the authoritative winner. Acceptance before the deadline is not later
replaced by expiration.

## Process-locking protocol

Every creation and terminal transition acquires the dispatch log's exclusive
file lock before reading state. While holding the lock it validates one snapshot,
resolves the offer, revalidates the actor, checks the injected clock and
expiration boundary, detects exact replay, appends at most one event, flushes,
and calls `fsync`. The lock is then released. Multiprocessing tests synchronize
competing creators and competing terminal decisions.

## Restart reconstruction

Every coordinator instance reconstructs offers solely from validated durable
events. Offered and terminal states survive restart. Identical creation and exact
terminal replay remain idempotent after reconstruction, and inspection produces
the same immutable values.

## Audit schema and durability

Every authenticated event binds schema version, global monotonic sequence, event
type, deterministic offer ID, routing assignment ID and fingerprint, mission and
task IDs, coordinator and worker IDs, required capabilities, previous and new
state, actor type and node ID, authorization metadata, approval metadata,
timestamp, expiration timestamp, bounded reason, predecessor digest, and
resulting digest. Serialization uses sorted keys, compact separators, UTF-8, and
exactly one final newline per record.

## Corruption handling

Replay preserves bytes and fails closed without rewriting or skipping records.
Tests cover invalid UTF-8, missing final newline, truncated or malformed JSON,
duplicate keys, missing fields, unknown fields, wrong field types, broken
sequence, broken digest chain, impossible transitions, contradictory terminal
events, conflicting creation, foreign-offer events, forged assignments,
single-event rewriting, full-chain rewriting, and wrong-key restart.

## Adversarial regression coverage

New tests added in `tests/test_task_dispatcher.py`:

- `test_fabricated_assignment_with_recomputed_plain_digest_is_rejected`
- `test_altered_assignment_with_recomputed_plain_digest_is_rejected`
- `test_caller_created_assignment_object_cannot_establish_authority`
- `test_integrity_key_is_explicit_bytes_and_shared_by_both_stores`
- `test_rewritten_terminal_event_with_plain_digest_is_rejected`
- `test_fully_rewritten_unkeyed_event_chain_is_rejected`
- `test_wrong_key_restart_fails_closed_and_preserves_original_bytes`

Existing restart, idempotency, and concurrency regressions now inject the same
key explicitly: `test_offered_and_terminal_state_survive_restart`,
`test_exact_terminal_replay_is_idempotent`,
`test_exact_expiration_replay_is_idempotent`, and
`test_first_terminal_transition_wins_across_processes`. Together these cover
correct-key offered and terminal restart, exact-byte preservation after failed
inspection, no duplicate terminal replay event, and one cross-process terminal
outcome/event.

## Read-only inspection

Offer, event, history, and list values are frozen dataclasses and tuples.
Inspection reads one validated snapshot, orders offers and events
deterministically, does not expire or mutate offers, and does not expose
credentials, prompts, private paths, arbitrary internal metadata, or underlying
exception details.

## Threat-model boundary

The keyed records protect against fabricated or modified persisted evidence when
the attacker does not possess the controller integrity key. This milestone does
not claim protection after full compromise of the controller process or its
secret.

## Explicit exclusions

Task Dispatch v0.1 performs no task execution, worker communication, queueing,
retry, reassignment, deployment, provider access, or automatic offer processing.
It adds no node registry and makes no cross-store transaction guarantee.

## Verification and test counts

| Verification | Result |
| --- | ---: |
| Integrated focused | 80 passed |
| Full repository | 888 passed |
| `compileall federation tests` | passed |
| `git diff --check` | passed |

## Exact file inventory

- `TASK_DISPATCH_V0_1_REPORT.md`
- `federation/__init__.py`
- `federation/assignment_registry.py`
- `federation/dispatch_offer.py`
- `federation/integrity.py`
- `federation/routing_decision.py`
- `federation/task_dispatcher.py`
- `federation/task_router.py`
- `tests/test_dispatch_inspection.py`
- `tests/test_dispatch_offer.py`
- `tests/test_task_dispatcher.py`
- `tests/test_task_router.py`

## Final Git status

Nothing is staged.

```text
 M federation/__init__.py
 M federation/routing_decision.py
 M federation/task_router.py
 M tests/test_task_router.py
?? TASK_DISPATCH_V0_1_REPORT.md
?? federation/assignment_registry.py
?? federation/dispatch_offer.py
?? federation/integrity.py
?? federation/task_dispatcher.py
?? tests/test_dispatch_inspection.py
?? tests/test_dispatch_offer.py
?? tests/test_task_dispatcher.py
```
