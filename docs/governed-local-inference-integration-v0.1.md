# Governed Local Inference Integration v0.1

## Purpose

This integration permits one already-authorized local inference request to use
one already-running server only when the authoritative server lifecycle remains
exactly `ATTESTED`. A successful response from `127.0.0.1:18080` is never
authority by itself.

The integration composes, rather than replaces, these accepted contracts:

- `LocalModelServerLifecycleStore` is the authority for lifecycle history.
- lifecycle process operations prove Linux PID birth identity, executable and
  argv identity, and exact endpoint ownership.
- `WorkerExecutionCoordinator` is the authority for the distinct inference
  task. The inference attempt must be `RUNNING`; server authority does not grant
  inference authority.
- `GovernedLocalModelRuntime` owns request authorization, replay/conflict
  behavior, adapter dispatch, local-only enforcement, and response validation.
- `LlamaCppLocalAdapter` remains the only HTTP inference implementation.

The coordinator has no start, stop, terminate, launch, download, cloud, or
fallback operation.

## Binding

`GovernedLocalInferenceRequest` fingerprints the integration request identity,
worker, inference attempt and execution fingerprint, lifecycle identity,
expected lifecycle record and attestation fingerprints, governed endpoint,
backend and profile, and the accepted runtime request fingerprint. The runtime
request itself binds its task, assignment, dispatch offer, execution authority,
input, model descriptor, generation settings, locality, tools, and approval
requirements.

Immediately before runtime dispatch, the coordinator reloads the authenticated
lifecycle record and requires `ATTESTED`. It checks the record and attestation
fingerprints; process birth/executable/argv observation; endpoint owner PID;
artifact evidence and SHA; binary evidence and SHA; model alias; invocation;
profile; backend; endpoint; worker; and lifecycle-attestation linkages.

Immediately after the runtime returns, it repeats the authoritative lifecycle,
process, and endpoint checks. A changed or unprovable identity is reported as
`GovernedLocalInferenceReconciliationRequired`; the runtime result is not
persisted as a normal trusted integration result.

The unavoidable boundary is between each operating-system observation and the
adapter's socket operations. v0.1 minimizes that interval with checks directly
around runtime dispatch, but it cannot make `/proc`, socket ownership, HTTP, and
model execution one atomic kernel transaction. Post-execution verification
therefore detects ambiguity but cannot prove whether a changed process produced
some or all response bytes.

## Evidence and replay

Successful integration evidence references the exact lifecycle record,
attestation, process, endpoint observations, artifact, binary, runtime request,
and runtime result fingerprints. Runtime timing, token usage, resource data,
and output remain in the accepted runtime result rather than being duplicated.
The final integration result and evidence have deterministic fingerprints.

`GovernedLocalInferenceStore` persists successful results in authenticated
append-only JSONL. Reusing an integration identity with different content fails
closed. Exact integration replay returns the durable result without calling the
runtime. The runtime remains the authority for runtime-request replay and
ambiguous adapter execution; the integration does not create a competing retry
model.

The store provides integrity and replay evidence, not confidentiality. Raw
prompts are excluded from integration evidence; no credentials or environment
values are persisted.

## Failure model

The integration distinguishes authority, lifecycle, live identity, conflicting
request identity, and post-dispatch reconciliation failures. Accepted runtime
contract, adapter, locality, response-validation, conflict, and reconciliation
errors propagate without cloud fallback or retry.

## Non-goals

- server start, stop, repair, adoption, restart, or process termination;
- direct inference HTTP or arbitrary endpoints;
- alternate artifact, capacity, adapter, runtime, or authority contracts;
- cloud, hybrid routing, model download, benchmarking, or a multi-task pilot.
