# Windows Display Adapter v0.1 Report

## Baseline

- Branch: `feature/windows-display-adapter-v0-1`
- Exact starting HEAD/base commit:
  `be27fcdd3f7ab3413e1a66669b5736b8e64e7ef9`
- Starting worktree: existing implementation uncommitted, with nothing staged
- Development platform: Fedora

## Architecture

The adapter is a per-user helper boundary intended to run in the active
console user's visible desktop session. It is not an interactive Windows
service: services run in an isolated session and must not manipulate another
user's desktop. v0.1 never starts a process in another session.

`WindowsDisplayAdapter` binds one immutable deployment to one
`GovernedPowerComponent`: adapter, worker, display component, policy version,
controller authority, integrity authority, enabled state, local action policy,
locked-session policy, explicit minimum idle duration, and fixed native
timeout. Construction rejects missing or untyped deployment configuration,
invalid bindings, non-display components, invalid thresholds/timeouts, and
untyped native/session/worker probes. Real execution is disabled unless
`enabled=True` is explicitly supplied.

The helper entry point accepts only typed `display_off` and `display_on`
choices. It defaults to disabled, refuses non-Windows execution, and has safe
help, version, and dry-run paths. Real flags return the stable
`governed_composition_required` refusal. The helper accepts no adapter,
authorization, native API, native `invoke` method, or arbitrary callback, and
cannot reach native execution. It has no listener, socket, HTTP endpoint,
named pipe, SSH handling, or controller transport.

## Governed-Component Integration

The accepted sequence remains proposal, policy evaluation, approval,
execution authorization, final coordinator validation, adapter attempt, and
durable result/audit evidence. `PowerCoordinator` still performs final worker
liveness and advertised-capability validation immediately before invocation.
For adapters that require enforcement, it uses an explicit
`PowerExecutionAuthorizationAuthority` to sign an immutable
`PowerExecutionAuthorization` with domain-separated HMAC-SHA256 over canonical
JSON. The payload binds proposal, worker, component, adapter, typed action,
policy version, controller authority, integrity authority, authorization
sequence, issuance time, and expiration. The key is explicit bytes of at least
32 bytes; there is no generated, default, fallback, environment, or persisted
key path.

`WindowsDisplayAdapter` requires an exact concrete authorization verifier and
authenticates the record independently before local validation or native
invocation. Missing or malformed tags, wrong keys, expired records, and any
changed bound field fail closed without a native call. Ordinary constructor or
dictionary reconstruction without a valid tag is not executable, and a stale
tag copied with `dataclasses.replace` does not authenticate changed content.
Copy and deepcopy of an unchanged immutable record remain valid because they
carry the same authenticated evidence. Recording and disabled adapters keep
their existing contract.

## Native API and Platform Isolation

`NativeDisplayApi` is the fakeable narrow abstraction. The real
`WindowsNativeDisplayApi` loads the fixed `user32` DLL only after a typed
platform probe confirms Windows. Construction on Fedora fails closed with
`WindowsPlatformError` and `unsupported_platform`; no Windows DLL or native
function is touched. Federation's POSIX file locks are behind an import-safe
facade so the per-user Windows modules can import without loading a missing
`fcntl` module; controller registry use still requires POSIX locks.

The real backend is a trusted low-level primitive and does not perform
authorization. It must be owned by `WindowsDisplayAdapter` in trusted
composition and must never be passed directly to the CLI, transport, user
input, or an arbitrary callback. Direct native invocation is outside the
external-input API, and no convenience factory exposes it.

The real backend exposes only `invoke(PowerAction)`. Its internal mapping is:

| Action | API | Message | Command | `lParam` |
|---|---|---:|---:|---:|
| `DISPLAY_OFF` | `SendMessageTimeoutW` | `WM_SYSCOMMAND` (`0x0112`) | `SC_MONITORPOWER` (`0xF170`) | `2` |
| `DISPLAY_ON` | `SendMessageTimeoutW` | `WM_SYSCOMMAND` (`0x0112`) | `SC_MONITORPOWER` (`0xF170`) | `-1` |

The target is the fixed internal desktop broadcast constant. The flags are the
fixed internal `SMTO_ABORTIFHUNG` value. Deployment supplies one validated
timeout from 1 through 5000 milliseconds; it is immutable after construction
and cannot be overridden per call. A zero return, timeout, or native error is
a failure. There is no retry or alternate native method.

## Active Session, Idle, and Local Refusal Policy

`WindowsSessionSnapshot` records the process session, active console session,
whether they match, visible interactive window-station availability, measured
idle seconds, locked state, other local prohibition, and UTC observation time.
The production standard-library probe uses documented Win32 session, window
station, input-idle, and input-desktop APIs. Tests inject deterministic probes.

Both actions require an active console session, the helper in that exact
session, a visible interactive window station, no local prohibition, and no
conflicting transition. Locked-session behavior is an explicit
`allow_when_locked` deployment choice. Display-off additionally requires local
display-off permission and the explicitly configured minimum idle duration.
Display-on requires local display-on permission but no idle threshold.

## Typed Results and Failure Behavior

`WindowsDisplayResult` contains only the adapter, worker and component IDs;
typed action; attempted and native-success booleans; stable code and safe
reason; session ID; observed idle duration; controller timestamp; and fixed
timeout. It does not claim the display state changed.

Stable codes cover disabled deployment, unsupported platform/action, missing
authorization, identity/policy/authority mismatch, unavailable worker or
capability, absent/wrong/noninteractive session, local prohibition, idle
threshold, conflicting transition, and native failure. Native failure is
audited durably as terminal `FAILED`; native success is terminal `SUCCEEDED`.
Neither path retries.

## Idempotency and Concurrency

The adapter has no parallel unauthenticated state store, polling thread, or
background worker. A local non-blocking transition lock rejects overlapping
adapter calls. The coordinator's existing durable exclusive file lock,
execution-attempt evidence, terminal-state reconstruction, and first-terminal
result semantics ensure that repeated or concurrent identical execution calls
invoke native code at most once. Conflicting display proposals serialize
deterministically. Success and failure both survive coordinator restart and
are returned without another native attempt.

## Security Boundaries

Untrusted external values cannot create executable authorization without the
required HMAC authentication. Ordinary accidental API misuse and malformed
authorization records fail closed. Malicious Python already executing inside
the trusted worker process is outside the authorization boundary. Such code
could inspect process memory, retrieve keys, call `ctypes` directly,
monkeypatch objects, or call operating-system APIs.

Python private names, frozen dataclasses, slots, sentinels, and classmethods are
defensive API controls, not an in-process security sandbox. The authenticated
record protects the external authorization boundary; it does not claim to
sandbox malicious code in the same interpreter.

The low-level Windows native backend is trusted and must not be exposed to
untrusted input, transport, or the CLI helper. `WindowsNativeDisplayApi`
performs no authorization and must never be treated as an authentication
boundary. The supported governed path is `PowerCoordinator.execute` to an
authenticated, exactly matching `PowerExecutionAuthorization`, followed by
independent `WindowsDisplayAdapter` verification, current local checks, one
bounded native attempt, and durable coordinator evidence.

There is no subprocess use, shell, PowerShell, `cmd.exe`, `rundll32`, WMI
execution, `eval`, `exec`, GUI input synthesis, dynamic DLL/function choice,
caller-selected Win32 constant, executable path, elevation, administrator
requirement, or environment lookup in federation domain modules. No sleep,
hibernate, shutdown, Wake-on-LAN, scheduled wake, arbitrary action string, or
arbitrary command reaches the native backend. v0.1 adds no network traffic or
remote-control transport.

## Manual Windows Smoke Test

The procedure at `docs/windows_display_adapter_v0_1_smoke_test.md` documents
only currently supported helper inspection and dry-run. It does not instruct
operators to inject the native backend's method or imply that flags provide
governed execution. A controlled real smoke test is deferred until trusted
worker composition and authorization delivery exist. No controlled Windows
smoke test has been performed.

## Tests and Validation

- Focused:
  `python3 -m pytest -q tests/test_windows_display_adapter.py`
  - **80 passed**
- Adjacent federation command requested in the milestone:
  - **286 passed**
- Full repository:
  `python3 -m pytest -q`
  - **1067 passed**
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/raghub-windows-display-hardening-pycache python3 -m compileall -q federation scripts tests`
  - passed with no output
- `git diff --check`
  - passed with no output

Adversarial coverage includes HMAC authority key validation; canonical
authenticated binding of every authorization field; absent, malformed,
wrong-key, altered, and expired authorization refusal before native execution;
unchanged copy/deepcopy behavior; mandatory exact adapter verifier;
coordinator-issued authorization verification; Fedora-safe imports;
non-Windows native/session
refusal before DLL loading; strict deployment validation; exact Win32 mapping;
bounded timeout and zero return; typed success/failure; rejection of every
non-display power action and arbitrary strings; exact binding/authority;
offline, stale, sleeping and waking workers; missing capability; all active
session gates; idle and explicit lock policy; the normal supported coordinator
execution path; concurrent idempotency;
conflict serialization; restart durability; no retry; safe helper
defaults/help/version/dry-run; rejection of callback and raw-native helper
injection; dependency typing; source-level shell, elevation and dynamic-native
exclusions; and existing power, heartbeat, registry, routing and dispatch
regressions. Every native call in automated tests used a fake or an injected
fake Win32 function.

## Focused Hardening Pass Files

- `WINDOWS_DISPLAY_ADAPTER_V0_1_REPORT.md`
- `docs/windows_display_adapter_v0_1_smoke_test.md`
- `federation/power_adapter.py`
- `federation/power_coordinator.py`
- `federation/windows_display_adapter.py`
- `federation/windows_display_native.py`
- `scripts/windows_display_helper.py`
- `tests/test_windows_display_adapter.py`
- `tests/test_worker_power_management.py`
- `/home/matias/RAGHub-Reports/windows-display-adapter-v0-1-summary.txt`
- `/home/matias/RAGHub-Reports/windows-display-adapter-v0-1-hardening-summary.txt`

## Changed Repository Files

- `WINDOWS_DISPLAY_ADAPTER_V0_1_REPORT.md`
- `docs/windows_display_adapter_v0_1_smoke_test.md`
- `federation/__init__.py`
- `federation/assignment_registry.py`
- `federation/file_lock.py`
- `federation/heartbeat_registry.py`
- `federation/power_adapter.py`
- `federation/power_coordinator.py`
- `federation/task_dispatcher.py`
- `federation/windows_display_adapter.py`
- `federation/windows_display_native.py`
- `federation/windows_session.py`
- `scripts/windows_display_helper.py`
- `tests/test_windows_display_adapter.py`

Final repository counts after writing this report: **0 staged, 7 modified
tracked, 8 untracked**. Nothing was staged, committed, pushed, merged,
restored, or changed in another worktree.

## Deferred Work and Execution Confirmation

Fedora-to-Windows transport, network/controller communication, authorization
delivery, automatic Windows deployment, service installation, elevation, and
process launch into another user's session remain deferred. Sleep, hibernate,
shutdown, Wake-on-LAN, firmware/scheduled wake, and arbitrary Windows commands
are out of scope.

No real display action or system-power action ran during development or
automated testing.
