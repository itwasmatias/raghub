# MissionaryX Integrated Demonstrator v0.1

## Status

Contract candidate. Implementation MUST NOT weaken or redefine existing
MissionaryX authority, effect, reconciliation, evidence, or mission-runtime
semantics.

## Purpose

Prove that MissionaryX can coordinate one consequential but bounded mission
end-to-end while preserving safety across an ambiguous post-dispatch failure.

The demonstrator MUST exercise real MissionaryX control-plane primitives.
A standalone simulation that merely displays the expected states does not pass.

## Mission

Objective:

    Deploy version 2 of an isolated test service,
    verify that version 2 became active,
    and produce durable evidence of the completed change.

Initial external state:

    active_version = 1

Required final external state:

    active_version = 2

The test service MUST be isolated from production systems and MUST expose only
the bounded capabilities needed by this demonstrator.

## Required Existing MissionaryX Primitives

The implementation MUST reuse the existing canonical components where their
semantics apply:

- MissionRuntime / MissionRuntimeStore
- authority evaluation and delegation-grant binding
- GovernedEffectGateway
- DurableEffectStore
- EffectState
- AuthorityDisposition
- ReconciliationObligation
- provider-boundary reconciliation evidence
- MissionObservability

The demonstrator MUST NOT introduce parallel definitions for:

- nothing_landed
- something_landed
- indeterminate
- authority reservation disposition
- reconciliation obligation
- mission lifecycle
- canonical evidence semantics

## Required Participants

At minimum:

1. Mission controller
2. One reasoning participant
3. Governed effect executor
4. Independent verifier

The reasoning participant MAY be local or remote.

No reasoning model is authoritative for:

- granting authority
- effect classification
- retry permission
- reconciliation outcome
- mission completion

Models propose.
Tools execute.
Evidence decides.
MissionaryX controls advancement.

## Authority Boundary

The mission MAY authorize only the bounded test deployment operation and
read-only verification needed for this demonstrator.

It MUST NOT authorize:

- production deployment
- financial spending
- arbitrary shell execution
- unrelated filesystem mutation
- credential access outside the test-service boundary
- widening of its own authority

Authority MUST remain bound to the correct grantee identity.

## Canonical Demonstration Sequence

### 1. Mission creation

A mission is durably created and started through MissionRuntime.

The mission specification MUST identify:

- control domain
- mission id
- objective
- bounded resource
- permitted capability
- expected target version

### 2. Governed effect preparation

Before any external mutation:

- authority is evaluated
- an effect intent is durably committed
- an authority reservation exists
- dispatch ownership is claimed
- a bounded dispatch permit is issued

No provider operation may escape before the required durable state exists.

### 3. Real external dispatch

The governed executor performs exactly one real operation:

    deploy version 2

against the isolated test service.

The test service MUST durably transition from version 1 to version 2.

### 4. Injected ambiguous failure

After the external service accepts/commits the operation, but before
MissionaryX receives authoritative completion confirmation, the demonstrator
MUST deliberately lose the confirmation path.

This failure MUST be intentional, deterministic, and visible in evidence.

MissionaryX therefore MUST NOT claim:

    nothing_landed

or:

    something_landed

at this point.

The canonical effect state MUST become:

    EffectState.INDETERMINATE

The resulting state MUST satisfy existing MissionaryX invariants:

- handoff_started = true
- receipt_recorded = false
- reconciliation_required = true
- reconciliation_obligation_id is present
- authority disposition remains RESERVED

### 5. Blind retry prohibition

While the effect is INDETERMINATE:

    automatic retry is forbidden

The demonstrator MUST prove that no second deployment operation is dispatched.

A duplicate deployment attempt is a demonstrator failure.

### 6. Durable reconciliation obligation

MissionaryX MUST create and persist a ReconciliationObligation bound to the
exact:

- control domain
- effect intent
- dispatch
- provider boundary

Reconciliation is owned by the control plane, not by the failed task.

### 7. External-state reconciliation

A bounded reconciliation probe MUST inspect the real external service.

The probe MUST determine the active version without reissuing the deployment.

Expected observation:

    active_version = 2

The observation itself is evidence, not authority.

### 8. Provider-boundary evidence

The reconciliation result MUST be represented using the repository's existing
provider-boundary reconciliation evidence semantics.

Evidence MUST remain bound to the exact effect intent, dispatch, obligation,
and control domain.

A mismatched or unverified evidence record MUST NOT settle the effect.

### 9. Effect resolution

After verified reconciliation proves that version 2 is active, MissionaryX may
resolve the previously indeterminate effect as:

    EffectState.SOMETHING_LANDED

The authority reservation MUST transition according to existing
MissionaryX effect-safety rules.

The original ambiguous dispatch MUST NOT be erased or rewritten.

### 10. Independent verification

A verifier distinct from the effect executor MUST independently read the
external test-service state.

Required result:

    active_version = 2

The verifier MUST NOT perform the deployment operation.

Verification evidence MUST identify what was checked and the observed value.

### 11. Mission completion

The mission MUST NOT transition to COMPLETE merely because:

- the executor returned success
- a model said the deployment succeeded
- reconciliation observed some external change
- version 2 appeared once without verified evidence

Mission completion requires all of the following:

- governed dispatch occurred exactly once
- ambiguous failure was recorded
- effect entered INDETERMINATE
- blind retry remained blocked
- durable reconciliation obligation existed
- external reconciliation confirmed version 2
- provider-boundary evidence was accepted
- effect was resolved as SOMETHING_LANDED
- independent verification confirmed version 2
- mission effect reference is durable
- mission evidence is observable
- no unresolved effect remains for this mission

Only then may MissionRuntime complete the mission.

## Evidence Requirements

The final machine-readable report MUST identify at least:

- control_domain
- mission_id
- mission objective
- mission lifecycle
- mission revision
- effect_intent_id
- effect_dispatch_id
- gateway_claim_id
- reconciliation_obligation_id
- effect state history
- authority disposition history where available
- injected failure identifier
- reconciliation probe result
- external version observed
- independent verification result
- duplicate dispatch count
- unauthorized operation count
- final external version
- relevant evidence pointers
- implementation commit SHA

The report MUST preserve uncertainty instead of rewriting history.

Required history includes the transition:

    indeterminate -> reconciled landed effect

not merely the final landed state.

## Mission Control Timeline

MissionObservability MUST be able to project a timeline containing the
substantive sequence:

1. mission created
2. mission started
3. authority accepted
4. effect intent committed
5. dispatch authorized
6. external handoff started
7. confirmation lost
8. effect indeterminate
9. retry blocked
10. reconciliation started
11. version 2 externally observed
12. effect resolved
13. independent verification passed
14. mission completed

The user interface may simplify presentation, but it MUST NOT fabricate events
that are absent from authoritative state/evidence.

## Acceptance Criteria

Integrated Demonstrator v0.1 passes only if deterministic tests prove:

1. exactly one external deployment operation occurs
2. external state changes from version 1 to version 2
3. confirmation loss produces INDETERMINATE
4. INDETERMINATE retains RESERVED authority
5. retry is blocked while unresolved
6. a durable reconciliation obligation survives store reopen
7. reconciliation reads external state without redispatch
8. mismatched reconciliation evidence is rejected
9. valid reconciliation evidence resolves the ambiguity
10. independent verification confirms version 2
11. the mission cannot complete before resolution and verification
12. the mission can complete after all required evidence exists
13. MissionObservability exposes the relevant history
14. final evidence report is machine-readable
15. zero duplicate deployment operations occurred
16. zero unauthorized operations occurred

Existing regression suites for reused MissionaryX primitives MUST remain
passing.

## Failure Conditions

The demonstrator fails if any implementation:

- retries automatically while outcome is uncertain
- treats task failure as proof that no effect occurred
- treats task success as proof that an effect occurred
- allows a model to settle effect status
- permits mission completion with an unresolved effect
- bypasses GovernedEffectGateway for the deployment
- changes authoritative evidence retrospectively
- accepts reconciliation evidence for the wrong intent/dispatch/domain
- performs more than one deployment operation
- widens authority outside the test-service boundary

## Non-Goals

v0.1 does NOT include:

- production deployment
- arbitrary user-defined missions
- workflow designer
- plugin marketplace
- autonomous financial authority
- broad shell authority
- polished commercial UI
- KC Metro Crime Forecasting Lab integration

The KC Metro Crime Forecasting Lab is the first serious domain mission planned
after the Integrated Demonstrator proves MissionaryX end-to-end.

## Core Demonstration Claim

The demonstrator is successful only if MissionaryX can truthfully show:

    One intended external operation was dispatched.
    Confirmation was deliberately lost.
    The effect became indeterminate.
    MissionaryX did not retry blindly.
    Reconciliation discovered that version 2 had actually landed.
    Independent verification confirmed version 2.
    The mission completed with durable evidence.
    Duplicate operations: 0.
    Unauthorized operations: 0.
