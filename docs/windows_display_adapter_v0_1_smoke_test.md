# Windows Display Adapter v0.1 Smoke-Test Status

No controlled Windows smoke test has been performed. The v0.1 command-line
helper supports inspection and safe dry-run only; it does not provide governed
real execution.

## 1. Safe helper behavior available now

On the intended Windows worker, while signed in as the active console user:

1. Run `python scripts/windows_display_helper.py --help` or `--version` to
   inspect the helper without attempting a display action.
2. Run `python scripts/windows_display_helper.py --dry-run --action
   display_off` or select `display_on`. Confirm the JSON result contains
   `attempted: false` and `code: "dry_run"`.
3. Confirm that flags alone cannot enable real execution. Even
   `--enable --confirm-real-execution --action display_off` returns
   `governed_composition_required` and performs no native call.

The helper accepts no adapter, authorization, native API, native `invoke`
method, arbitrary callback, command, executable path, Win32 constant, window
handle, DLL, function, serialized object, module name, or executable
configuration file.

## 2. Implemented governed-execution components

`PowerCoordinator` issues a canonically serialized, domain-separated
HMAC-SHA256 `PowerExecutionAuthorization`. The immutable authenticated payload
binds the proposal, worker, component, adapter, action, policy, controller and
integrity authorities, authorization sequence, issuance time, and expiration.
`WindowsDisplayAdapter` independently authenticates that record immediately
before its current worker, capability, session, and local-policy checks.

`WindowsNativeDisplayApi` is a trusted low-level primitive. It does not perform
authorization and must only be owned by trusted `WindowsDisplayAdapter`
composition. It must never be passed directly to the CLI, transport, untrusted
input, or an arbitrary callback, and must never be treated as an authentication
boundary.

## 3. Deferred production composition and delivery

Production worker composition and authenticated authorization delivery are not
wired in v0.1. Real helper flags therefore return
`governed_composition_required` and never construct or invoke a native backend.
Transport remains deferred.

Untrusted external values cannot create executable authorization without valid
HMAC authentication, and malformed records or accidental API misuse fail
closed. This is not an in-process sandbox. Malicious Python already executing
inside the trusted worker process is outside the authorization boundary: it
could inspect process memory and retrieve keys, monkeypatch objects, call
`ctypes` directly, or invoke operating-system APIs. Python private names,
frozen dataclasses, slots, sentinels, and classmethods are defensive API
controls, not a same-process security sandbox.

## 4. Future controlled Windows smoke test

**The real Windows smoke test must not be run yet.** Wait until trusted
production composition and authenticated authorization delivery exist and are
reviewed. The future procedure must exercise the complete
`PowerCoordinator.execute` to authenticated authorization to
`WindowsDisplayAdapter` path, verify exact deployment bindings and local
session policy, use a configured idle threshold, record the typed durable
result, avoid automatic retries, and never claim that a successful native
return proves the observed display state changed.

Before that future test, keep physical recovery available: remain at the
machine, keep keyboard and mouse access, know the monitor's physical power and
input controls, and have a second visible display path if the test setup
supports one. Stop after one bounded attempt and recover locally; do not add an
alternate command, retry loop, service, remote transport, or system-power
action.
