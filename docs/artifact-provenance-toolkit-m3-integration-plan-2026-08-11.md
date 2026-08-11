# Artifact Provenance Toolkit v0.1 M3 Integration Plan

This plan describes how the digest-only primitive from v0.1 can sit beneath a future M3 Evidence Spine.

It does not implement M3.

## Role In The Stack

Artifact Provenance Toolkit v0.1 provides deterministic digest-manifest creation and verification for selected local artifacts.

In a future stack, it can act as the lowest integrity layer under a broader evidence system that may add:

- authenticated envelopes;
- detached signatures;
- ControlDomain bindings;
- AgentIdentity bindings;
- retention policy;
- independent timestamps;
- explicit ENFORCED and ATTESTED states;
- ledger-level evidence reconciliation.

## Why It Is Not EvidenceLedger

This toolkit only proves that a verifier recomputed a digest and matched it against an externally stored manifest entry.

It does not establish:

- who produced the artifact;
- who approved the manifest;
- whether a system identity was authenticated;
- whether the artifact remained protected continuously;
- whether the record is admissible or legally authoritative.

That is why it is a digest primitive, not EvidenceLedger.

## Binding Points For Later Layers

Future layers can bind at these points:

- ControlDomain: authorize who may publish or attach manifests.
- AgentIdentity: bind manifests or envelopes to an authenticated actor.
- Detached signatures: sign the manifest bytes or an outer envelope.
- Authenticated envelopes: package manifest bytes, provenance metadata, and policy assertions.
- Retention: govern how long manifests and evidence records remain available.
- Independent timestamps: provide externally trusted timing separate from digest creation.

## State Semantics

`DIGEST_VERIFIED` remains a byte-level recomputation result only.

Future M3 states such as `ATTESTED` or `ENFORCED` must remain distinct and should only appear when the higher-order authority and policy checks have been implemented and reviewed independently.

## Compatibility Boundaries

The v0.1 manifest schema should remain readable by future layers so long as the deterministic payload stays compatible.

Future layers should avoid silently rewriting v0.1 manifests, because the current primitive deliberately rejects self-referential checksum claims and does not claim authority over its own final bytes.

Subsystem logs may reference manifest paths or digests, but those logs should not be treated as contractual evidence unless they are separately authenticated and retained under a future policy.

## Independent Review Required Before Reuse

Before reusing this layer in M3, independently review:

- path safety and root-escape handling;
- manifest canonicalization rules;
- digest algorithm selection;
- file-mutation detection;
- cross-platform file-identity behavior;
- interaction with any future signatures or authenticated envelopes;
- storage and retention policy;
- authority boundaries between digest verification, authentication, and enforcement.
