# Local Model Server Lifecycle v0.1

## Mission and scope

`federation/local_model_server_lifecycle.py` governs the start, attestation,
reconciliation, and controlled stop of one allowlisted local `llama.cpp`
server on the Fedora worker. It composes existing RAGHub authority, artifact,
and host-capacity evidence with a fixed process profile and a durable lifecycle
history.

This component is deliberately not a general process manager or inference
API. It answers a narrower operational question: can the exact authorized
worker execution start and retain evidence for the exact verified model
artifact, exact allowlisted server binary, fixed invocation, fixed loopback
endpoint, and observed runtime?

**ATTESTED is not user/task authorization.**

ATTESTED only means the exact governed local server lifecycle passed its
configured artifact, binary, capacity, process, endpoint, and runtime
attestation checks. Permission to use the server for a user, task, prompt, or
inference remains the responsibility of higher governance layers.

## Architecture

`LocalModelServerLifecycleCoordinator` composes these boundaries:

1. `WorkerExecutionCoordinator` supplies authoritative worker-execution
   identity and state.
2. `LocalModelArtifactRegistry` verifies the registered model artifact.
3. `HostResourceCollector` and `HostCapacityGuard` collect host evidence and
   evaluate the request's typed requirement and policy.
4. `SystemLocalModelServerProcessOperations` opens stable files, launches the
   fixed process, observes process and endpoint identity, captures bounded
   output, and sends controlled termination.
5. `LocalModelServerAttestor` checks the allowlisted loopback HTTP routes.
6. `LocalModelServerLifecycleStore` persists every accepted transition in an
   authenticated append-only history.

The coordinator serializes endpoint-affecting work around the single governed
endpoint. Each layer produces typed, fingerprinted evidence that is bound into
later records rather than replaced by an unsourced success flag.

## Accepted contracts reused

v0.1 reuses the repository's accepted contracts instead of defining parallel
authorities:

- Worker execution: `WorkerExecutionCoordinator`,
  `WorkerExecutionStatus`, and `WorkerExecutionResultEnvelope`.
- Artifact trust: `LocalModelArtifactRegistry`, `ArtifactState`, and
  `ArtifactStatus`.
- Host capacity: `HostResourceSnapshot`, `HostResourceRequirement`,
  `HostCapacityPolicy`, `HostCapacityDecision`, `HostCapacityStatus`,
  `HostResourceCollector`, and `HostCapacityGuard`.
- Durable integrity: `require_integrity_key`, `authentication_tag`, and
  `authenticates`.
- Cross-process locking: `federation.file_lock.fcntl`.

These inputs remain separate authorities. For example, a verified artifact
does not authorize launch, an available-capacity decision does not identify a
model, and runtime attestation does not authorize a task.

## Authority binding

A start request binds:

- a lifecycle request ID;
- worker node ID;
- execution attempt ID and upstream execution fingerprint;
- artifact ID, registry fingerprint, and SHA-256;
- the fixed server profile;
- the complete capacity requirement and policy;
- the explicit constrained-capacity choice;
- startup and shutdown timeouts.

Before a new start, the coordinator requires the authoritative execution
attempt to exist in `CLAIMED` state. The worker ID and a real, non-null upstream
execution fingerprint must exactly match the request. The coordinator then
advances that attempt to `RUNNING` immediately before process launch.

Stop requires the same execution authority in `RUNNING` state and binds the
stop request to the current lifecycle record fingerprint, full expected
process identity, worker, execution attempt, and execution fingerprint.
Successful stop completes the worker execution with a result envelope that
references lifecycle evidence.

## Artifact trust boundary

The request must name an artifact that the artifact registry can verify in
`VERIFIED` state. Its registry fingerprint, expected SHA-256, and model alias
must exactly match the request and fixed profile.

The lifecycle then independently opens the artifact as a stable regular file:

- the path must be absolute and canonical;
- symlinks in the path and leaf are forbidden;
- the file is opened with `O_NOFOLLOW` where available;
- SHA-256, size, device, inode, and mode are checked through the open
  descriptor;
- the stable descriptor's path, hash, size, device, and inode must match the
  registry evidence;
- launch uses the inherited `/proc/self/fd/<n>` descriptor path.

This narrows the registry-to-launch time-of-check/time-of-use boundary. The
artifact is not downloaded, rediscovered, or selected by caller-supplied path.

## Server binary identity boundary

The only accepted executable is:

- path:
  `/home/matias/local-ai/llama.cpp/build/bin/llama-server`;
- SHA-256:
  `51101f3ef423200f9096ed07fc186e4670fa7b99ccea2d9a571fdd2ff5851b04`;
- source commit:
  `876a4321163249c43ca4e986818fab5ab081f282`;
- profile ID: `fedora-llama-server-qwen2.5-0.5b-v0.1`.

The path must remain canonical and symlink-free. The opened object must be a
regular executable file whose descriptor hash and physical identity remain
stable. Launch uses the opened executable descriptor as `Popen.executable`,
while the recorded fixed argv retains the allowlisted canonical path as
`argv[0]`.

## Host Resource Capacity Guard composition

The request contains the complete typed
`HostResourceRequirement` and `HostCapacityPolicy`; their fingerprints are
therefore part of lifecycle identity. The collector gathers a snapshot for the
bound worker and, when configured, the requirement's storage root. The guard
evaluates that snapshot, requirement, and policy.

The lifecycle preserves snapshot, requirement, policy, and decision
fingerprints, plus the status and sorted reasons, as
`CapacityPreflightEvidence`.

- `UNSAFE_TO_START` always rejects launch.
- `CONSTRAINED` rejects launch by default.
- `CONSTRAINED` may launch only when the typed request explicitly sets
  `allow_constrained_capacity=True`.
- `AVAILABLE` may proceed to the remaining checks.

A non-`HostResourceSnapshot` collector result is treated as unsafe rather than
accepted as approximate evidence.

## Fixed llama.cpp endpoint and profile

v0.1 supports only the governed loopback endpoint
`127.0.0.1:18080` (`http://127.0.0.1:18080`).

The fixed profile is:

| Setting | Value |
| --- | --- |
| Model alias | `raghub-qwen2.5-0.5b-q4km` |
| Context size | `1024` |
| Threads | `4` |
| Batch threads | `4` |
| Parallel slots | `1` |
| GPU layers | `0` |
| Offline mode | enabled |
| Web UI | disabled |
| Chat capability | enabled |

The exact argv is the allowlisted binary followed by `--model` with the stable
artifact descriptor, `--alias`, `--host`, `--port`, `--ctx-size`,
`--threads`, `--threads-batch`, `--parallel`, `--n-gpu-layers`, `--offline`,
and `--no-webui`.

The child uses `shell=False`, `stdin=DEVNULL`, `cwd="/"`, closed file
descriptors except the two stable inherited descriptors, and a new session. It
does not inherit the caller environment. Its fixed minimal environment contains
only `HOME=/`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, `PATH=/usr/bin:/bin`, and
`TMPDIR=/tmp`; proxy and token names are explicitly represented in the
environment-policy fingerprint as removed.

v0.1 does not accept arbitrary executable, argv, shell, or environment input.

## Lifecycle request identity and fingerprinting

Requests and evidence use deterministic SHA-256 fingerprints over canonical
JSON: keys are sorted, separators are fixed, NaN and infinity are forbidden,
and timestamps are normalized to UTC where present.

The start request fingerprint covers every start field, including nested
profile, capacity requirement, capacity policy, constrained-capacity choice,
and both timeouts. Reusing a lifecycle request ID with any changed field is a
conflict. One execution attempt may bind to only one lifecycle identity.

The stop fingerprint similarly covers the immutable stop identity, expected
current lifecycle fingerprint, expected process identity, authority binding,
and shutdown timeout.

## Lifecycle state machine

The normal start path is:

```text
REQUESTED
  -> PREFLIGHT_PASSED
  -> STARTING
  -> RUNNING_UNATTESTED
  -> ATTESTED
```

The normal stop path is:

```text
ATTESTED or RUNNING_UNATTESTED
  -> STOPPING
  -> STOPPED
```

Typed failures may transition pre-terminal states to `FAILED` when absence or
cleanup is deterministic. Ambiguous ownership, process identity, authority, or
cleanup transitions to terminal `RECONCILIATION_REQUIRED`. `STOPPED`, `FAILED`,
and `RECONCILIATION_REQUIRED` are terminal in v0.1.

State records enforce evidence shape. Post-preflight states require capacity,
artifact, and binary evidence; running states require process identity;
`ATTESTED` and later normal states require attestation; stop states require the
stop request and timestamps; terminal failure states require a code and detail.

## Durable authenticated store and history

The lifecycle store is an append-only canonical JSONL log. Each envelope
contains schema version `1`, a contiguous sequence number, the predecessor
authentication tag, the complete record, and an authentication tag calculated
with the repository integrity helpers under the lifecycle domain separator.
Each lifecycle record also points to the prior record fingerprint.

Writes take an exclusive advisory file lock, append, flush, and `fsync`; the
parent directory is `fsync`ed when the file is created. Reads take a shared
lock and replay the full log to project current state.

The store fails closed on a missing trailing newline, malformed JSON, duplicate
keys, invalid schema or sequence, broken predecessor chain, wrong integrity
key or authentication tag, invalid nested fingerprint, forbidden transition,
mutated immutable evidence, conflicting predecessor, or execution-attempt
reuse. The integrity key is required and is not serialized.

## Idempotent start semantics

An exact repeated start for an existing `ATTESTED` lifecycle returns the
existing record without relaunch only while the stored process identity still
matches. If that identity is no longer provable, the lifecycle becomes
reconciliation-required.

An exact repeat observed in `STARTING`, `RUNNING_UNATTESTED`, or `STOPPING`
enters reconciliation under the same endpoint lock. Failed, stopped, or
reconciliation-required identities are not replayable starts. Callers must use
a new lifecycle and execution identity rather than silently retrying a terminal
record.

## Concurrency and cross-process locking

The coordinator uses a thread `RLock` plus an exclusive `fcntl` lock on the
store-specific `.endpoint.lock` file around start, stop, and reconciliation.
The durable store independently locks its JSONL file.

This composition serializes decisions about the one governed endpoint across
threads and processes. Concurrent identical starts observe one authoritative
history and launch at most one process; later contenders return or reconcile
that lifecycle rather than racing a second launch.

Correctness depends on working advisory file locks and all lifecycle writers
using these coordinator and store contracts.

## Process identity and PID reuse defense

The durable process identity binds:

- lifecycle request ID;
- PID;
- Linux kernel process start ticks;
- kernel boot ID;
- executable canonical path, device, inode, mode, size, and SHA-256;
- exact argv and argv fingerprint;
- fixed environment-policy fingerprint;
- governed endpoint;
- creation time.

Observation checks `/proc/<pid>` against boot ID, start ticks, executable
device/inode/path, and exact command line. A missing process is `ABSENT`; any
disagreement or unreadable identity is `IDENTITY_MISMATCH`. A PID alone is
never a stop authority. This prevents signaling an unrelated process after PID
reuse or rebinding.

## Port ownership semantics

Before launch, the coordinator requires the governed port to be unoccupied. It
inspects Linux TCP listener inodes and `/proc/*/fd` to identify an owner PID
when possible, then performs an exclusive bind probe when no listener is
found.

Any occupied or unbindable endpoint before launch becomes
`RECONCILIATION_REQUIRED` with endpoint evidence. The lifecycle does not steal
the port and does not kill an unknown owner.

After HTTP attestation, the endpoint must be occupied by the exact launched
PID. An absent listener or different owner is ambiguous and requires
reconciliation. After a proven governed process exits, a newly observed
unrelated owner is recorded as final endpoint evidence but is not signaled.

## Startup flow

Under the endpoint lock, a new start proceeds in this order:

1. Reject conflicting lifecycle or execution-attempt reuse.
2. Validate the authoritative execution attempt in `CLAIMED` state.
3. Persist `REQUESTED`.
4. Verify the artifact registry identity.
5. Collect host resources, evaluate capacity, and retain capacity evidence.
6. Require the fixed endpoint to be free.
7. Open and verify the allowlisted binary and stable artifact descriptor.
8. Persist `PREFLIGHT_PASSED`, then `STARTING`.
9. Advance worker execution authority to `RUNNING`.
10. Launch the fixed process and capture its birth identity.
11. Persist `RUNNING_UNATTESTED`.
12. Poll the three allowlisted attestations within the startup deadline.
13. Prove endpoint ownership by the launched PID.
14. Persist `ATTESTED` with bound attestation and bounded output evidence.

## Runtime attestation

Attestation uses a direct `http.client.HTTPConnection` to the fixed loopback
host and port. It does not use proxies. Only `/health`, `/v1/models`, and
`/props` are allowlisted; response bodies are limited to 1 MiB. Redirect
headers and all 3xx responses are rejected.

Every accepted route produces evidence containing route, status, response byte
length, response SHA-256, and a fingerprint of the accepted semantic content.
The final attestation binds those route records to the request, execution,
artifact, binary, capacity, process identity, endpoint, invocation, model
alias, and attestation time.

### `/health` attestation

The response must be HTTP 200, valid UTF-8 JSON, a JSON object, and contain
exactly the accepted health meaning `status == "ok"`. Connection failures,
timeouts, and non-200 readiness responses remain pending until the startup
deadline. Malformed JSON, redirects, and a wrong semantic status are typed
health-attestation failures.

### `/v1/models` attestation

The response must be HTTP 200 and a JSON object whose `data` field is a list
containing exactly one model object. That object's `id` must exactly equal
`raghub-qwen2.5-0.5b-q4km`. Empty, multiple, malformed, or differently named
models fail attestation.

### `/props` and chat-template attestation

Because the fixed profile enables chat capability, `/props` is mandatory. It
must return HTTP 200 and a JSON object with a non-empty string
`chat_template`. If `chat_template_caps` is present and non-null, it must be a
JSON object. Evidence retains the chat-template SHA-256 and accepted capability
value, not an interpretation that the template is suitable for every task.

## `RUNNING_UNATTESTED` versus `ATTESTED`

`RUNNING_UNATTESTED` means the exact process was launched and its durable
identity was captured, but the complete runtime and endpoint checks have not
yet succeeded. It is not a usable-ready state.

`ATTESTED` means the stored process still matched while `/health`,
`/v1/models`, `/props`, and endpoint ownership passed and their evidence was
bound to all preflight identities. It is an operational lifecycle result only.
Again, **ATTESTED is not user/task authorization.**

## Startup timeout behavior

Startup attestation uses a monotonic deadline from the request, with accepted
timeouts in `(0, 300]` seconds. Individual HTTP calls receive at most two
seconds or the smaller remaining deadline. Pending health readiness is retried
at bounded intervals.

On timeout or another typed startup failure after launch, the coordinator tries
one safe normal cleanup: it re-observes the exact identity, sends `SIGTERM`
only if it still matches, and waits for no longer than the request's shutdown
timeout. Proven process absence allows a durable `FAILED` transition. If exact
shutdown cannot be proved, the result is `RECONCILIATION_REQUIRED` with
`startup_cleanup_ambiguous`. v0.1 does not escalate to `SIGKILL`.

## Controlled stop and shutdown timeout

Stop is accepted only for `ATTESTED` or `RUNNING_UNATTESTED` and only through a
fully bound `LocalModelServerStopRequest`. Before signaling, the coordinator
revalidates worker authority, exact stored process identity, and compatible
endpoint ownership. It then persists `STOPPING` before sending `SIGTERM`.

The wait loop continuously rechecks exact process identity. Proven absence
allows `STOPPED`, records completion time and final endpoint evidence, and
marks worker execution succeeded. Identity mismatch requires reconciliation.

If the process still matches at the shutdown deadline, the lifecycle records
`RECONCILIATION_REQUIRED` with `shutdown_timeout` and raises
`LifecycleShutdownTimeoutError`. It does not claim `STOPPED`, does not send a
second arbitrary signal, and does not use `SIGKILL`.

An exact repeated stop request returns or reconciles the existing stop. A
different stop identity conflicts.

## Reconciliation semantics

Reconciliation observes durable evidence; it never guesses and never launches
a replacement process.

- `STOPPED` and `FAILED` return unchanged.
- `RECONCILIATION_REQUIRED` remains terminal and requires operator handling.
- Without durable process identity, an occupied endpoint is ambiguous; a
  closed endpoint becomes deterministic `FAILED`, closing a still-running
  worker execution as failed.
- A stored identity mismatch indicates PID reuse or changed process identity
  and requires reconciliation.
- An interrupted stop with the exact process still running requires
  reconciliation; the signal is not replayed.
- An interrupted stop with proven absence can complete `STOPPED`.
- A running lifecycle with proven process absence becomes `FAILED`.
- An already `ATTESTED` lifecycle with a matching process remains attested.
- A matching `RUNNING_UNATTESTED` process may be attested only when worker
  execution authority is still `RUNNING`; otherwise the mismatch requires
  reconciliation.

## Partial and pre-preflight reconciliation evidence

Failure records preserve only evidence that was actually established. They do
not fabricate a complete preflight.

For example, endpoint occupancy is checked after capacity evaluation but
before binary and stable-artifact opening. That result remains based on a
`REQUESTED` record and carries capacity evidence plus the endpoint observation,
while artifact preflight evidence and binary identity remain absent. Failures
earlier than capacity may contain neither. This partial shape is intentional:
operators can distinguish a pre-preflight ambiguity from a process that
actually reached launch.

Once preflight passes, state validation requires the complete capacity,
artifact, and binary evidence. Once launch identity is durable,
`RUNNING_UNATTESTED` records that identity even if later startup cleanup becomes
ambiguous.

## stdout and stderr bounding

The real process implementation drains stdout and stderr concurrently so pipe
backpressure does not block the child. Each stream retains only its newest
512 KiB, for a maximum combined retained output of 1 MiB. Older bytes are
dropped and `output_truncated` records that fact. Invalid UTF-8 is decoded with
replacement characters.

Output is evidence for diagnosis, not authority. It is captured into attested,
failed, and stopped records when available, and record construction rejects
output beyond the retained bound.

## Fail-closed behavior

v0.1 rejects rather than approximates when it cannot prove authority, artifact
identity, binary identity, capacity safety, process birth, process continuity,
endpoint ownership, endpoint semantics, durable history integrity, cleanup, or
stop identity. Unexpected errors at the launch boundary are converted to a
typed process-launch failure and recorded through the same cleanup rules.

Ambiguity is not converted into success. In particular, no endpoint response
alone proves process ownership, no PID alone proves process identity, no
registry name alone proves artifact bytes, and no existing process is adopted
without its durable lifecycle identity.

## Security boundaries

- Loopback-only binding limits network exposure but is not an authorization
  mechanism.
- Worker execution authority, artifact trust, host capacity, process identity,
  endpoint ownership, and HTTP semantics are independently checked.
- Stable descriptors and no-symlink rules narrow file replacement attacks.
- Exact boot ID and start ticks defend against PID reuse.
- A minimal non-inherited environment prevents caller-controlled process
  configuration and proxy/token inheritance.
- `shell=False`, fixed argv, fixed executable, and fixed cwd remove a
  caller-supplied command surface.
- Authenticated chained history detects tampering but does not encrypt stored
  records or process output.
- Advisory locks coordinate conforming local processes; they do not prevent an
  unrelated privileged process from modifying the host.
- Local root or an attacker controlling the trusted model root, binary path,
  integrity key, kernel process data, or lifecycle process can violate
  assumptions outside this component's boundary.

## Real Fedora smoke workflow

The real smoke workflow is an explicit operator exercise and is not part of
deterministic tests:

1. Confirm the repository and Fedora host are the intended isolated
   environment and retain the lifecycle integrity key securely.
2. Confirm the allowlisted binary exists at the exact fixed path and matches
   the fixed SHA-256 and source build identity.
3. Place the intended GGUF inside an explicitly trusted artifact-registry root,
   register it with alias `raghub-qwen2.5-0.5b-q4km`, and obtain a fresh
   `VERIFIED` state. Do not download a model through this lifecycle.
4. Create and claim the authoritative worker execution attempt with a real
   upstream execution fingerprint for the Fedora worker.
5. Define the host resource requirement and policy for that artifact and host.
   Keep constrained launch disabled unless the operator deliberately accepts
   the typed `CONSTRAINED` decision.
6. Confirm `127.0.0.1:18080` is intended for this test and is not owned by
   another service. Do not kill an existing owner to make the test pass.
7. Construct the lifecycle store, authoritative coordinator, artifact
   registry, Linux host collector and guard, system process operations, and
   loopback HTTP transport. Submit the exact start request once.
8. Require the resulting durable record to be `ATTESTED`; inspect the bound
   artifact, binary, capacity, process, endpoint, `/health`, `/v1/models`, and
   `/props` evidence. Do not treat that state as permission to perform
   inference.
9. Construct the stop request from the latest lifecycle record fingerprint and
   exact stored process identity. Submit it once and require a durable
   `STOPPED` record with proven process absence.
10. If any step returns `RECONCILIATION_REQUIRED`, stop automation and inspect
    the stored history and endpoint/process evidence. Do not relaunch, adopt,
    kill, or overwrite evidence manually.

The smoke workflow starts a real local server and must be run only when
explicitly authorized. It must not be inferred from deterministic unit-test
success.

## Limitations

- v0.1 supports only Linux/Fedora process observation through `/proc`.
- It supports only the governed loopback endpoint `127.0.0.1:18080`.
- The binary path, binary digest, source commit, model alias, resource profile,
  and invocation are compile-time constants.
- HTTP attestation verifies a small accepted semantic surface, not the model's
  mathematical quality, safety, provenance beyond artifact evidence, or
  inference correctness.
- `/props` verifies that a non-empty chat template is advertised; it does not
  prove every prompt will be formatted correctly.
- Port owner discovery may be unavailable because of permissions or races; an
  unprovable owner fails closed.
- Process output is tail-only and may be truncated.
- Authenticated storage provides integrity and ordering, not confidentiality.
- `RECONCILIATION_REQUIRED` is terminal in v0.1 and has no automated repair.
- The component assumes cooperating writers honor its advisory locks.

## Non-goals

v0.1:

- does not accept arbitrary executable, argv, shell, or environment;
- does not download models;
- does not use cloud fallback;
- does not perform inference;
- does not expose or invoke completion/chat inference routes;
- does not provide user or task authorization;
- does not select among endpoints, ports, models, binaries, or profiles;
- does not `SIGKILL` on shutdown timeout;
- does not kill unknown port owners;
- does not automatically relaunch during reconciliation;
- does not adopt an untracked process;
- does not replace artifact-registry, capacity, worker-execution, or higher
  governance contracts.

## Future relationship with Governed Local Model Runtime

The Governed Local Model Runtime can compose this lifecycle as its narrow
server-control and attestation layer. A future runtime may add governed
inference requests, user/task authorization, budgets, approvals, scheduling,
prompt and response policy, and result evidence. Those capabilities must
remain above this component rather than weakening its fixed process and trust
boundaries.

The intended composition is:

```text
higher user/task governance
  -> Governed Local Model Runtime request authorization
  -> authoritative worker execution
  -> verified artifact and host-capacity evidence
  -> Local Model Server Lifecycle ATTESTED
  -> separately governed inference
```

The lifecycle's `ATTESTED` record may be required input to that future runtime,
but it must never be interpreted as sufficient authorization to serve a user
or execute a task.
