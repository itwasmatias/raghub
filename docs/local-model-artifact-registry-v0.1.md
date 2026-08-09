# Local Model Artifact Registry v0.1

The Local Model Artifact Registry is the durable authority over the **identity
and byte-level integrity** of local model artifacts (initially GGUF files) that
live inside explicitly trusted filesystem roots. It records which concrete file
was registered, the exact bytes it must contain, and whether the file on disk
still matches that bound identity. It is provider independent and transport
neutral: it hashes bytes with `hashlib` in bounded chunks and never launches a
subprocess, opens a socket, or touches the network.

## Purpose

`federation/local_model_artifact_registry.py` answers exactly one question:
*does a regular file at a canonical path still have the exact bytes and
filesystem identity that were bound when the artifact was registered?* It
provides immutable, provider-independent contracts for an artifact, its
container format, its verification state, and the registry itself. Every
identity-bearing field contributes to a deterministic SHA-256 fingerprint.

## Trust model and the execution boundary

**Verification does not authorize execution.** A `VERIFIED` artifact is
evidence about *artifact bytes and identity only*. It confirms that a specific
regular file still holds the exact registered bytes on a trusted path. It says
nothing about whether a model may be run.

Any later attempt to execute a model must still pass, independently, through the
full RAGHub governance chain:

1. **RAGHub authorization** — is this action permitted at all?
2. **Human approval** — has an authorized approver approved this specific
   action, where required?
3. **Worker execution** — is a governed worker actually allowed to perform the
   run under the dispatch and execution protocols?
4. **Lifecycle governance** — budget, power, heartbeat, and other operational
   controls.

This registry is a *composable evidence provider*. It never invokes a runtime,
never grants permission, and never shortcuts any of the steps above. Downstream
components that want to run a verified artifact must call `resolve`/`verify`
with their own independently verified identities and then perform their own
authorization.

## Trusted roots

Trusted roots must be **explicitly configured**; there is no implicit default.
Each root must be an absolute path to an existing directory and is canonicalized
at construction. Every artifact path is validated against these roots. The
registry rejects:

- paths outside every trusted root;
- traversal escapes (for example `<root>/../outside/model.gguf`), which are
  normalized lexically before the root check;
- any symlink in any path component below the root (leaf or directory), which is
  preferred over silently following links;
- nonexistent paths, and non-regular files including directories, FIFOs,
  sockets, and devices.

Files are opened with `O_NOFOLLOW` and `O_NONBLOCK`, and the opened descriptor
is re-checked with `fstat` so a swapped symlink or FIFO cannot be followed or
block the process.

## Identity and fingerprint

An artifact record binds: `artifact_id`, `model_id`, optional `alias` and
`display_name`, the canonical filesystem path, the expected lowercase SHA-256,
the observed file size, the format (GGUF), an optional quantization label,
runtime-compatibility metadata, immutable provenance metadata, timestamps, and
the derived lifecycle status. Physical evidence — the Linux `st_dev` and
`st_ino` of the concrete inode, where available — is recorded so that a file
replacement can be detected even when the replacement bytes are identical.

The deterministic `fingerprint` is a SHA-256 over the canonical serialization of
the immutable identity fields (id, model, alias, display, path, format,
quantization, expected hash, size, `st_dev`, `st_ino`, runtime compatibility,
and provenance metadata). Timestamps are not part of the fingerprint. Changing
any logical or physical identity evidence changes the fingerprint. No secrets
are included in fingerprints or in serialized evidence.

Metadata is accepted recursively and only when it is JSON-compatible: `None`,
`bool`, `int`, finite `float`, `str`, lists, and string-keyed dicts. Non-string
dict keys, non-finite floats (NaN/Infinity), cyclic structures, and opaque
objects are rejected. The stored representation is a fresh copy, and the value
exposed on a snapshot is a deeply immutable `MappingProxyType`/tuple structure,
so callers cannot alias or mutate registry state.

## Hashing

Bytes are hashed with `hashlib.sha256` in bounded chunks (default 1 MiB, and
configurable for testing). The registry never reads an entire artifact into
memory at once, never shells out to an external hashing tool, and never uses the
network. Expected digests must use strict canonical lowercase SHA-256 syntax
(64 hex characters).

## Durability and restart safety

The registry is an append-only JSONL log, consistent with the existing
federation durability conventions:

- each line is a canonical JSON envelope with a schema version, a contiguous
  sequence number, a predecessor authentication tag, an event type, and a
  payload;
- every record carries an HMAC-SHA-256 authentication tag produced with the
  shared `federation.integrity` helpers over the canonical unsigned envelope;
- records form a predecessor authentication chain (genesis tag for the first
  record);
- writes are serialized with the cross-process `federation.file_lock` `fcntl`
  lock, flushed, and `fsync`ed; the parent directory is `fsync`ed when the store
  file is first created.

Decoding fails closed. It rejects truncated data (a missing trailing newline),
non-UTF-8 bytes, malformed JSON, duplicate JSON keys, schema or sequence errors,
a broken predecessor chain, an invalid authentication tag, a non-canonical
payload, a fingerprint that does not match the identity, and any state-machine
violation. The HMAC key is held privately and is **never serialized**.

## Registration, idempotency, and conflicts

`register` reads and hashes the concrete file, confirms the bytes match the
expected digest (failing closed on a mismatch), checks the GGUF magic, and binds
the identity. Idempotency and conflicts are exact:

- registering the same `artifact_id` with an identical immutable registration
  returns the equivalent existing state without appending a duplicate event;
- registering the same `artifact_id` with any different immutable field is a
  conflict and is rejected;
- the same content registered under a different `artifact_id`/`model_id` (for
  example two independent copies) is allowed independently, because each is its
  own logical identity.

Under concurrency the cross-process lock guarantees a single authoritative
outcome: concurrent identical registrations produce exactly one registration
event, and concurrent conflicting registrations produce exactly one winner while
the others fail closed with a deterministic conflict.

## Verification and invalidation

`verify` re-reads the actual filesystem evidence and compares it to the bound
identity. It fails closed on replacement (changed `st_dev`/`st_ino`), content
change, size change, truncation, hash mismatch, and disappearance, as well as a
path that has become a symlink or a non-regular file. When a previously verified
or registered artifact no longer matches, an auditable invalidation event is
appended and the current state is no longer `VERIFIED`.

History is never rewritten. Once an artifact is invalidated it does not
resurrect, even if the original bytes are later restored under a new inode. The
full `registered → verified → invalidated` trail remains queryable via
`history`, and read-only `resolve`, `current`, and `list_artifacts` return
deterministic, detached, immutable snapshots.

## Limitations

- The registry verifies bytes and filesystem identity, not model semantics or
  correctness. It does not parse GGUF beyond checking the leading magic.
- It relies on the trusted roots being genuinely trusted; it does not defend
  against a root directory that is itself controlled by an adversary.
- There is an unavoidable time-of-check/time-of-use gap between a `verify` and
  any later use of the bytes; the registry narrows it with `O_NOFOLLOW`,
  `fstat`, and inode evidence, but callers that need atomicity must hold their
  own reference to the verified descriptor.
- Concurrency correctness depends on a working POSIX/Windows advisory file lock
  as provided by `federation.file_lock`.

## Non-goals

- It does not download, fetch, or resolve remote artifacts.
- It does not run, load, or serve any model, and it starts no server or
  subprocess.
- It does not authorize execution, grant approvals, route tasks, dispatch work,
  or govern worker lifecycle.
- It does not manage disk space, garbage collection, or artifact deletion.

## Future composition

Later components — for example a llama.cpp adapter, a local model runtime, or a
local-only pilot — may compose this registry as an evidence source. The intended
pattern is: independently establish authorization and approval, resolve or
verify the artifact through this registry to confirm byte identity, and only
then perform a governed worker execution under RAGHub lifecycle governance. The
registry remains a pure, provider-independent integrity authority at the bottom
of that stack.
