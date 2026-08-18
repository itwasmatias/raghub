# Mission Observability v0.1

Mission Observability is MissionaryX's internal trustworthy mission/effect read
model. It correlates existing records without owning or changing any of them.

> Mission truth != Effect truth != Evidence truth
>
> Observability is a projection, not authority.

It is not a SIEM, a general telemetry platform, a compliance product, a mission
store, an effect ledger, an evidence store, or a state machine.

## Public API

```python
from federation.mission_observability import MissionObservability

reader = MissionObservability(
    mission_runtime,
    durable_effect_store,
    evidence_spine=evidence_spine,  # optional
)

observation = reader.observe(control_domain, mission_id)
timeline = reader.timeline(control_domain, mission_id)
```

`observe()` returns a frozen, slotted `MissionObservation`. All nested projection
models are also frozen and slotted. Collections stored in the projection are
tuples. Mission metadata and checkpoint progress are retained as canonical JSON
text so nested caller-owned dictionaries or lists cannot mutate the observation.
`to_dict()` returns a new serialization copy; changing that copy does not change
the projection. `to_json()` emits canonical deterministic JSON.

## Authoritative inputs

Mission lifecycle truth is read only through these `MissionRuntime` methods:

- `get_mission()`
- `list_transitions()`
- `list_checkpoints()`
- `list_effect_references()`

Effect truth is read only through these `DurableEffectStore` methods:

- `get_intent()`
- `get_dispatch()`
- `get_gateway_claim()`
- `get_reservation()`

`get_obligation()` is not used in v0.1 because none of the correlated public
records exposes an authoritative reconciliation-obligation ID. Observability
does not scan for, derive, guess, or construct obligation IDs.

Evidence is read only through `EvidenceSpine.records_for_mission()` and verified
with `EvidenceSpine.verify_evidence()` before inclusion.

## Read-only contract

Observation performs no lifecycle or effect mutation. It never creates a
mission, transition, checkpoint, effect reference, intent, dispatch,
reservation, obligation, claim, permit, receipt, or result. It does not release
or consume authority. It does not retrieve credentials, invoke a provider, run
a subprocess, access the network, retry an effect, probe reconciliation state,
or write a database or file.

The projection has no persistence of its own. Repeated observations are rebuilt
from the current public reads.

## Join and domain-isolation rules

Every lookup supplies the requested `control_domain`. The projection then checks
the bindings returned by the authoritative readers:

1. The requested mission specification must match the exact domain and mission
   ID.
2. Each referenced effect intent must exist in that domain and bind the exact
   mission ID.
3. A referenced dispatch must exist in that domain and bind the exact intent,
   attempt, and idempotency key.
4. A referenced gateway claim must exist and bind the exact intent and, when
   supplied by the mission reference, the exact dispatch. If the mission
   reference omits a dispatch ID, the claim's authoritative dispatch ID is read
   and verified instead of guessed.
5. Claim reservation, idempotency, and operation-digest bindings must match the
   intent.
6. The intent's authoritative reservation ID is read directly. The reservation
   must exist in the same domain and bind the exact intent.

A missing or contradictory join raises
`MissionObservabilityIntegrityError`. It is never converted into an incomplete
but apparently healthy result. A missing mission instead raises the distinct
`MissionObservabilityNotFoundError`. Integrity exceptions raised by an
authoritative store are allowed to propagate with their original type and
context.

Identical mission, intent, dispatch, claim, and reservation IDs may exist in
different valid ControlDomains. Domain-scoped reads and returned-object binding
checks keep them isolated.

## Evidence inclusion policy

`EvidenceSpine.records_for_mission(mission_id)` is not domain-scoped. Therefore a
record is included only when both of these correlation-key fields match exactly:

- `record.key.mission_id == requested mission_id`
- `record.key.domain_id == requested control_domain`

Records bound to another domain are omitted completely. They do not contribute
content, counts, timeline events, serialization, or the requested domain's
projection fingerprint. A same-ID record from another domain therefore cannot
contaminate or destabilize the projection.

Records whose `domain_id` is `None` are not attributed to a domain-scoped
mission. They are omitted, and `unbound_evidence_count` reports only how many
same-mission records lacked domain identity. Their IDs and content are not
projected.

Every included record is verified through an exact `EvidencePointer`. The public
projection contains correlation IDs, source revision, timestamps, summary,
source reference, and authoritative fingerprints. Raw evidence payload and
metadata are deliberately omitted. When no Evidence Spine is supplied,
`evidence_available` is false and the evidence tuple is empty; no substitute or
fabricated evidence is produced.

## Effect posture and indeterminate handling

An `EffectReference` remains what Mission Runtime defines it to be: a governed
effect reference without duplicating effect truth. Mission lifecycle never
determines effect outcome. In particular:

- mission `FAILED` does not imply `nothing_landed`;
- mission `COMPLETED` does not imply `something_landed`.

The public gateway-claim read exposes claim state but does not expose the
original terminal `nothing_landed` or `something_landed` result. Consequently
v0.1 does not derive either outcome, even from `claim.state == "terminal"`.
`ProjectedEffectStatus.UNKNOWN` is returned instead, while the authoritative
claim state and reservation disposition remain separately visible.

`claim.state == "indeterminate"` is itself an authoritative observable state.
It is represented as `ProjectedEffectStatus.INDETERMINATE`, included in
`unresolved_effects`, and sets `reconciliation_required` to true. That flag is a
read-only projection. Observability does not resolve the effect, create an
obligation, release or consume authority, retry the effect, or perform a
provider probe.

If reconstructing an authoritative terminal reservation requires evidence, the
supplied Evidence Spine is passed to `get_reservation()`. Without the required
spine, the authoritative store's fail-closed error is not replaced with
fabricated terminal evidence.

## Timeline semantics

The timeline is an immutable tuple projected from existing records. It may
contain events for:

- mission transitions and checkpoints;
- effect references, intents, and dispatches;
- authority reservation and disposition observations;
- gateway claim, handoff, receipt, and terminal-or-indeterminate phases;
- exactly domain-bound, verified evidence.

Each entry includes source type, source identity, recorded timestamp, a stable
source fingerprint, ControlDomain, and mission ID. Timeline fingerprints for
records that lack an authoritative fingerprint are fingerprints of canonical
projected source content. Evidence timeline entries retain the Evidence Spine's
authoritative record fingerprint.

Entries sort by recorded timestamp, then source type, source identity, and
source fingerprint. The latter fields provide a stable tie-breaker for equal
timestamps. Recorded timestamps from independent stores establish deterministic
display order only; their order does not prove causality across stores.

No new durable timeline event is created. A timeline entry is only a view of an
existing source record or recorded source phase.

## Projection fingerprint

`projection_fingerprint` is a SHA-256 fingerprint of canonical projected source
content, including the domain-isolated mission, effect, evidence, unresolved,
and timeline projections. It detects changes to what this read model exposes.

It is not an authoritative mission fingerprint, effect fingerprint, evidence
fingerprint, signature, authenticity proof, custody proof, or authorization
decision. It does not replace any source fingerprint.

`generated_at` records when the projection was produced and is intentionally
excluded from `projection_fingerprint`. Repeated reads over unchanged source
content therefore retain the same projection fingerprint even though their
generation timestamps differ.

## Concurrency and snapshot semantics

Mission Runtime and Durable Effect Store are independent authoritative stores,
and Evidence Spine is another independent input. There is no cross-store atomic
transaction. A `MissionObservation` is a correlated read projection over
individually authoritative records, not an atomic global snapshot.

The reader performs a bounded consistency check: it reads mission specification,
lifecycle, revision, and `updated_at` before correlation and reads them once more
after correlation. If any changes, it raises
`MissionObservabilityConcurrentChangeError` instead of returning a falsely
coherent mission view. It does not lock any store. Effect or evidence records may
still change independently between their public reads; consumers must interpret
the result under that documented non-atomic limitation.

## Security and non-secret projection

The projection copies only explicitly selected fields. It never exposes:

- Gateway `permit_verifier` or permit tokens
- Credential scopes, leases, or raw credentials
- Authentication secrets or integrity keys
- Raw evidence payload or metadata
- Raw mission specification metadata

Known exclusions:

- Mission metadata is not projected. Metadata source-content changes remain
  observable through the authoritative specification fingerprint.
- Gateway permit material is excluded.
- Raw credential and broker secret objects are excluded.
- Evidence Spine raw payload and metadata are excluded.

Observability is not a general secret classifier or DLP system. It does not
perform heuristic secret detection. Intentionally observable mission text
fields (objective, success criteria, constraints) remain mission-owned data,
and callers remain responsible for what is placed there.

Stable non-secret IDs, operation digests, and fingerprints needed for
correlation remain visible.

## v0.1 limitations

- No terminal `nothing_landed`/`something_landed` outcome is projected because
  the current public gateway-claim read does not retain that distinction.
- No reconciliation obligation is projected because no correlated public object
  supplies its authoritative ID.
- No provider status, probe, retry, or live telemetry is fetched.
- No cross-store atomic snapshot is claimed.
- Evidence without exact domain identity is not attributed.
- Raw evidence content is outside this narrow projection.
- This read model does not provide general SIEM, telemetry, compliance, or audit
  platform capabilities.

These limitations preserve the governing principle: observability reads truth;
observability does not become truth.
