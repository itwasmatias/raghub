# Approval Center v0.1

Approval Center v0.1 is the durable human-approval authority for governed
RAGHub actions. It records what exact action was requested, the authenticated
requester and approver identities, immutable routing linkage, the canonical
execution fingerprint, expiration, and one terminal decision. It does not run
commands, route tasks, dispatch work, or manufacture approvals.

## Lifecycle and authority

An authenticated requester creates an approval-required request. The server
assigns canonical creation and expiration timestamps. An actor configured as an
authorized approver may transition the request once from `pending` to
`approved` or `rejected`. A request at or past expiration becomes `expired` and
cannot be approved. Terminal decisions are immutable; exact decision replay is
idempotent, while a conflicting replay fails closed.

Requester identity must match the authenticated creation actor. Approvers come
from coordinator configuration rather than request fields. Approval Center does
not accept an `approved=true` flag or worker-provided evidence as authority.

## Execution fingerprint binding

`make_execution_fingerprint` canonically hashes the action type, workspace
identity, authorization level, approval requirement, and immutable action
parameters. Request creation rejects a missing, malformed, or mismatched
fingerprint. Decisions bind both the execution fingerprint and the complete
request fingerprint. `verify_approval` succeeds only for an unexpired approved
request whose mission, task, assignment, dispatch offer, and execution
fingerprint all exactly match the caller's expected identities.

## Persistence, integrity, and concurrency

`ApprovalStore` writes an append-only JSONL history protected by the existing
cross-thread and cross-process `FileLock`. Each canonical record has a sequence,
predecessor authentication tag, and HMAC-SHA-256 tag created with the shared
federation integrity primitives. Writes are flushed and fsynced. Reads reject
truncated data, duplicate JSON keys, non-canonical JSON, schema errors, sequence
or predecessor breaks, invalid authentication, duplicate requests, multiple
decisions, foreign decisions, and incomplete evidence.

Request creation and decisions are checked and appended while holding the same
durable lock. An exact duplicate request reuses the authoritative request;
the same request identity with different immutable content conflicts. Racing
terminal decisions produce one authoritative result rather than last-writer
wins behavior.

## Inspection and integration boundary

`ApprovalCoordinator.inspect` and `list_requests` return immutable snapshots.
Public serialization exposes linkage and fingerprints but omits HMAC tags,
integrity keys, and raw immutable parameter values. Expiration is derived from
durable server timestamps, so restart preserves validity semantics.

Future Console Action Execution Runtime and AI Work Bridge integrations should
call `verify_approval` with their independently verified identities. Approval
Center provides evidence only; it never invokes an execution runtime.

## Explicitly out of scope

Automatic or model-based approval, shells and command execution, routing,
dispatch, worker transport, external providers, queues, retries, UI, mobile
notifications, and AI Work Bridge are outside v0.1.
