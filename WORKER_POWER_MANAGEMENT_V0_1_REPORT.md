# Worker Power Management & Wake Coordination v0.1

## Governed-component federation model

Power control is modeled as three separately registered component kinds:
`display`, `system_power`, and `wake_coordinator`. Each immutable component
binds one worker identity and component identity to a closed set of typed
actions, a versioned local policy, controller and integrity authorities, and
one adapter identity. Registration requires an existing `NodeRegistry` worker,
and proposal evaluation consumes authenticated liveness and advertised power
capability evidence through the heartbeat-registry boundary.

Display evidence is deliberately isolated from node, heartbeat, routing,
assignment, task, and dispatch state. Turning a display off never implies that
the worker is asleep or unavailable.

## Power action state machine

The durable workflow separates component registration, proposal, policy
evaluation, approval/refusal, execution authorization, execution attempt,
result, and reconciliation. Supported states are:

`proposed`, `auto_approved`, `awaiting_approval`, `approved`, `refused`,
`execution_authorized`, `executing`, `succeeded`, `failed`, `expired`,
`cancelled`, and `reconciliation_required`.

Exact duplicate proposals, approvals, authorizations, cancellations,
executions, and reconciliations are idempotent. Sequence changes, altered
duplicates, invalid transitions, and contradictory successors fail closed.
The first terminal result wins under concurrent execution.

## Automatic display policy

Only `display_off` and `display_on` can auto-approve. Auto-approval requires:

- the exact registered worker and display component;
- authenticated proposal and worker evidence;
- the advertised `display_control` capability;
- an online worker, not stale, offline, intentionally sleeping, or waking;
- matching policy, controller, integrity, and adapter authorities;
- an explicit local policy allowing automatic display control;
- no interactive-session prohibition or active power transition; and
- the parameter-free, typed, idempotent display action contract.

Conflicting display actions are serialized by refusing a new transition while
the prior transition remains active.

## Explicit approval and local refusal

Sleep, hibernate, shutdown, Wake-on-LAN, and scheduled wake always enter
`awaiting_approval`; no non-display action can auto-approve. Sleep, hibernate,
and shutdown require checkpoint evidence and confirmation that protected work
is safe. Shutdown additionally requires a verified wake path and the exact
policy-authorized coordinator.

An approval is immutable evidence and never executes an adapter. Execution
requires a separate bounded authorization and repeats local validation.
Expired or substituted approvals fail closed. The target can refuse unsupported
capabilities, mismatched identities or authorities, changed policy, unsafe
liveness, missing checkpoint or wake evidence, active transitions, replay,
expiration, and disabled or unsupported adapters. Local final refusals and
adapter refusals are durable audit outcomes.

## Wake coordination

Wake capability is advertised rather than assumed. Wake proposals require the
exact authorized coordinator, authenticated `wake_on_lan` or `scheduled_wake`
capability, explicit approval, replay-resistant sequence evidence, and an
expiration no more than 24 hours away. Scheduled wake also requires a bounded
execution time before expiration. Requests for workers already online or
waking are refused to prevent wake loops. This condition is checked during
policy evaluation and repeated by final local validation before authorization.
No packet is sent in v0.1.

The model recognizes that a powered-off worker cannot wake itself: deployment
must later supply a verified online coordinator, firmware timer, or equivalent
external mechanism with supported hardware, firmware, network, and power
state.

## Persistence and concurrency

Evidence is newline-delimited canonical JSON protected by HMAC-SHA256. Every
record binds its global sequence, predecessor authentication tag, proposal and
component identities, action, capability and policy evidence, checkpoint and
wake evidence, approval details, authorization expiration, adapter attempt and
result, reason, controller time, and both authority identities.

Writers take an exclusive `flock`, reread and authenticate the complete store
while holding the lock, append, flush, and `fsync`. Readers use the existing
file read-only and never create directories, files, lock files, keys, repairs,
or replacement evidence. Correct-key restart rebuilds the state machine.
Wrong keys, malformed UTF-8/JSON, truncation, reordering, broken predecessor
chains, altered records, ordinary SHA-256 reconstruction, and contradictory
history fail closed without changing original bytes.

## Security and threat model

Proposal identities are deterministic SHA-256 digests over closed canonical
contracts, while proposal acceptance and every durable event use the explicit
controller-owned HMAC-SHA256 key. Keys must be bytes of at least 32 bytes and
have no default, generation, fallback, persistence, logging, serialization, or
domain-module environment lookup. Authentication verification uses
`hmac.compare_digest` through the shared federation integrity primitive.

No API accepts command strings, executable paths, shell fragments, arbitrary
parameters, privilege escalation, unrestricted PowerShell, or unrestricted
subprocess execution. Replay, identity substitution, policy substitution,
adapter substitution, stale authorization, and altered evidence are covered
as hostile inputs.

## Adapter boundaries

`RecordingPowerAdapter` is the only executing adapter and records typed
`PowerAction` attempts in memory. Fedora and Windows adapters are narrow typed
boundaries that are disabled by default and always refuse. They contain no
platform command plan or execution mechanism. No real power, display,
firmware, shutdown, or network wake operation was executed.

## Operations Room inspection contract

`OperationsPowerRecord` exposes worker, component, supported actions, current
governed state, pending proposal, approval requirement, refusal reason,
checkpoint status, wake-path status, authorization expiration, latest result,
and audit sequence. Inspection is read-only and suitable for a future iPhone
Operations Room; no mobile UI was added.

## Tests added

`tests/test_worker_power_management.py` adds 62 deterministic test cases covering
safe display auto-approval, unsupported and non-online refusal, local policy,
parameter-free display contracts, idempotency, replay and substitution,
explicit approval, checkpoint and protected-work safety, shutdown wake paths,
authenticated bounded wake
requests, proposal authentication, authorization expiration, durable local
refusal, restart and corruption attacks, physically read-only inspection,
approval and terminal concurrency, cancellation, reconciliation, display
serialization, typed adapters, command-injection rejection, disabled
production adapters, wake-loop prevention at evaluation and authorization,
immutable inspection records, and federation evidence isolation.

Exact results:

- Focused power management:
  `python3 -m pytest -q tests/test_worker_power_management.py`
  completed with `62 passed in 0.94s`.
- Focused power plus affected node, heartbeat, routing, and dispatch:
  `python3 -m pytest -q tests/test_worker_power_management.py tests/test_node_registry.py tests/test_node_capability.py tests/test_node_record.py tests/test_worker_heartbeat.py tests/test_task_router.py tests/test_task_dispatcher.py tests/test_dispatch_inspection.py`
  completed with `222 passed in 3.85s`.
- Full suite: `python3 -m pytest -q` completed with
  `987 passed in 35.83s`.
- Compile: `PYTHONPYCACHEPREFIX=/tmp/raghub-power-management-copilot-pycache python3 -m compileall -q federation tests` passed.
- `git diff --check` passed.

## Deferred features

Real Fedora and Windows implementations, privilege/deployment configuration,
firmware integration, Wake-on-LAN packets, background polling, hardware
discovery, real scheduled wake, and iPhone UI are deferred. Production
adapters must be explicitly injected during a later controlled deployment.

## Changed files and final Git status

- `WORKER_POWER_MANAGEMENT_V0_1_REPORT.md`
- `federation/__init__.py`
- `federation/power_action.py`
- `federation/power_adapter.py`
- `federation/power_coordinator.py`
- `tests/test_worker_power_management.py`

Final repository state: branch `feature/worker-power-management-v0-1`, base
commit `c644e63`, 1 modified tracked file, 5 untracked files, and 0 staged
files. Nothing was committed or pushed.
