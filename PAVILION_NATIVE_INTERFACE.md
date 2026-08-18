# PavilionOS Native Interface v0.1

## Governing Rule

**PAVILIONOS PRESENTS MISSIONARYX TRUTH.**
**PAVILIONOS DOES NOT RECREATE MISSIONARYX TRUTH.**

## Purpose

The PavilionOS Native Interface provides a read-only presentation boundary for MissionaryX mission/effect state. It presents authoritative truth through the accepted Mission Observability API without owning, recreating, or duplicating authoritative state.

## Architecture

### Two Separate Paths

#### READ (v0.1 — Current)

```
MissionaryX authoritative state
    ↓
MissionObservability
    ↓
Pavilion Native Interface
    ↓
user
```

The current v0.1 milestone implements **ONLY** the READ path.

#### FUTURE CONSEQUENTIAL ACTION (Not in v0.1)

```
user
    ↓
Pavilion action request
    ↓
CanonicalPavilionCoordinator
    ↓
Governed Effect Gateway
    ↓
Canonical Pavilion Adapter
    ↓
provider
```

**IMPORTANT:** Future consequential Pavilion actions **MUST** use `CanonicalPavilionCoordinator.coordinate(...)`.

The Pavilion Native Interface v0.1 does **NOT** provide a competing Pavilion execution path.

## Authoritative Sources

### What This Interface Uses

- **MissionObservability** as the single public read interface
- `MissionObservability.observe(control_domain, mission_id) -> MissionObservation`
- `MissionObservability.timeline(control_domain, mission_id) -> tuple[MissionTimelineEvent, ...]`

### What This Interface Does NOT Use

- MissionRuntimeStore (direct access)
- DurableEffectStore (direct access)
- SQLite queries
- Independent state reconstruction

## Public API

### PavilionNativeInterface

```python
from pavilionos.native_interface import PavilionNativeInterface

interface = PavilionNativeInterface(observability)
```

#### Constructor

- **Parameters:**
  - `observability`: MissionObservability-compatible reader
- **Must NOT** receive:
  - Raw SQLite database paths
  - Direct authoritative store instances

#### Methods

##### mission_view(control_domain: str, mission_id: str) -> PavilionMissionView

Build immutable mission view for presentation.

- **Returns:** Immutable `PavilionMissionView` with:
  - `control_domain`, `mission_id`
  - `objective`, `owner_identity`, `agent_identity`
  - `lifecycle`, `revision`, `updated_at`, `created_at`
  - `effect_count`, `unresolved_effect_count`
  - `evidence_available`, `evidence_count`, `unbound_evidence_count`
  - `effects: tuple[PavilionEffectSummary, ...]`
  - `unresolved_effects: tuple[PavilionUnresolvedEffectSummary, ...]`
  - `timeline_summary: tuple[PavilionTimelineItem, ...]`
  - `projection_fingerprint`, `generated_at`
- **Raises:**
  - `PavilionNativeInterfaceSourceError`: Source observation integrity failure
  - `PavilionNativeInterfaceError`: Mission not found or other presentation error

##### render_mission(control_domain: str, mission_id: str) -> str

Render mission state as deterministic text suitable for native display.

- **Output:** Plain text (no ANSI escape codes)
- **Deterministic:** Same observation produces identical rendering
- **Sections:** Mission identity, objective, status, unresolved effects, recent timeline

##### timeline_view(control_domain: str, mission_id: str) -> tuple[PavilionTimelineItem, ...]

Build immutable timeline view for presentation.

- **Returns:** Timeline using accepted MissionObservation timeline identity
- **Does NOT:** Independently reconstruct causal ordering from stores

## View Models

All view models are **immutable** (frozen dataclasses with slots).

### PavilionMissionView

Primary mission presentation model. All values derived from `MissionObservation`. Does NOT add new truth-bearing state.

### PavilionEffectSummary

Immutable effect summary preserving exact status semantics:

- `effect_intent_id`, `effect_dispatch_id`, `gateway_claim_id`
- `projected_status: ProjectedEffectStatus`
- `status_basis: str`
- `reconciliation_required: bool`

### PavilionUnresolvedEffectSummary

Immutable unresolved effect summary:

- `effect_intent_id`, `effect_dispatch_id`, `gateway_claim_id`
- `projected_status: ProjectedEffectStatus`
- `reason: str`
- `reconciliation_required: bool`

### PavilionTimelineItem

Immutable timeline event preserving source identity:

- `source_type`, `source_id`
- `recorded_at`
- `source_fingerprint`
- `control_domain`, `mission_id`

## Effect Status Semantics

### Critical Preservation Rules

**UNKNOWN remains UNKNOWN:**
- Not converted to `nothing_landed`
- Not converted to `something_landed`
- Displayed as: `UNKNOWN`

**INDETERMINATE remains INDETERMINATE:**
- Not converted to `failed`
- Reconciliation requirement displayed explicitly
- Displayed as: `INDETERMINATE — reconciliation required`

**Mission lifecycle does NOT imply effect outcome:**
- Mission `FAILED` ≠ effects `nothing_landed`
- Mission `COMPLETED` ≠ effects `something_landed`
- Indeterminate effects remain visibly unresolved

### ProjectedEffectStatus Values

From `federation.mission_observability`:

- `UNKNOWN`: Public reads do not expose unambiguous effect outcome
- `INDETERMINATE`: Authoritative gateway claim is indeterminate

## Evidence Presentation

- **evidence_available:** Preserved exactly from `MissionObservation`
- **evidence_count:** Count of verified domain-matching evidence
- **unbound_evidence_count:** Count of evidence without domain attribution
- **Excluded:** Raw evidence payload, metadata, credentials

## Timeline Semantics

- **Source:** Uses `MissionObservation.timeline` exclusively
- **Ordering:** Preserves observability-provided order, does NOT reconstruct independently
- **Chronological display:** Does NOT prove causal ordering across authoritative stores
- **Identity:** Retains source type, source ID, recorded timestamp, source fingerprint
- **Determinism:** Equal timestamps use stable tie-breaker from observability

## Fingerprint and Source Identity

- **projection_fingerprint:** Preserved exactly from `MissionObservation`
- **Does NOT:** Generate second "authoritative" fingerprint
- **Purpose:** Represents observability projection identity, not mission truth

## Confidentiality Boundary

### Does NOT Expose

- `permit_verifier` or permit tokens
- Raw credentials or credential leases
- Raw `EvidenceSpine` payload or metadata
- Raw `MissionSpecification.metadata`
- Gateway internal state beyond public projection

### Does Expose (Per MissionObservability Contract)

- Mission `objective` (projected)
- Owner and agent identity
- Effect intent/dispatch/claim identifiers
- Public effect status (`UNKNOWN`, `INDETERMINATE`)
- Evidence summaries (not raw payload)

**Not Generic DLP:** This interface preserves Mission Observability's confidentiality contract. It is not a general-purpose data loss prevention system.

## Read-Only Contract

### v0.1 Does NOT

- Call `CanonicalPavilionCoordinator.coordinate(...)`
- Call `CanonicalPavilionAdapter.dispatch(...)`
- Issue permits or consume credentials
- Execute reconciliation
- Invoke providers
- Execute subprocesses
- Make network requests
- Query SQLite directly
- Mutate mission lifecycle
- Mutate effect state
- Mutate authority reservations
- Create local cache or database

### v0.1 ONLY

- Reads through `MissionObservability`
- Presents immutable views
- Renders deterministic text
- Maintains presentation state (no persistence)

## Error Model

### PavilionNativeInterfaceError

Base error for presentation failures.

### PavilionNativeInterfaceSourceError

Raised when source `MissionObservability` fails with integrity error. Indicates corrupted or contradictory source observation.

**Fail-Closed:** A corrupted source observation produces an explicit error, NOT a partially rendered "healthy" mission.

### Distinguishable Errors

- **Mission not found:** Propagated as `PavilionNativeInterfaceError` (cause: `MissionObservabilityNotFoundError`)
- **Integrity failure:** Wrapped as `PavilionNativeInterfaceSourceError`
- **Empty mission:** Not created from integrity failures

## Domain Isolation

Same `mission_id` in two `ControlDomains` remains isolated:

- Exact requested `control_domain` forwarded to observability
- No cross-domain leakage
- Different domains produce different `projection_fingerprint`

## Immutability Guarantees

- All view models are `frozen=True, slots=True` dataclasses
- Nested collections are immutable tuples
- Individual items are frozen dataclasses
- Mutation attempts raise `FrozenInstanceError`
- Repeated rendering does not mutate source observation

## v0.1 Limitations

### Current Milestone Provides

- ✅ Read-only mission/effect observation
- ✅ Immutable presentation views
- ✅ Deterministic text rendering
- ✅ Timeline presentation
- ✅ Effect status preservation
- ✅ Confidentiality boundary enforcement

### Current Milestone Does NOT Provide

- ❌ Consequential action execution
- ❌ Effect reconciliation
- ❌ Provider invocation
- ❌ Authority writes
- ❌ Credential retrieval
- ❌ Mission lifecycle mutations
- ❌ Interactive TUI/GUI
- ❌ ANSI/color rendering
- ❌ Web server/API

### Future Governed Action Requirement

When PavilionOS needs to execute consequential actions (not in v0.1), those actions **MUST** use:

```python
from pavilionos.canonical_coordinator import CanonicalPavilionCoordinator

coordinator = CanonicalPavilionCoordinator(...)
result = coordinator.coordinate(action_request)
```

**Do NOT** extend `PavilionNativeInterface` with direct provider execution.

## Example Usage

### Basic Mission View

```python
from federation.mission_observability import MissionObservability
from federation.mission_runtime import MissionRuntime
from federation.durable_effect_store import DurableEffectStore
from pavilionos.native_interface import PavilionNativeInterface

# Set up authoritative sources
runtime = MissionRuntime(db_path="missions.sqlite3")
store = DurableEffectStore("effects.sqlite3")

# Create observability interface
observability = MissionObservability(runtime, store)

# Create Pavilion native interface
interface = PavilionNativeInterface(observability)

# Get immutable view
view = interface.mission_view("control-domain", "mission-123")

print(f"Mission: {view.mission_id}")
print(f"State: {view.lifecycle.value}")
print(f"Effects: {view.effect_count}")
print(f"Unresolved: {view.unresolved_effect_count}")
```

### Rendering

```python
# Get deterministic text rendering
rendered = interface.render_mission("control-domain", "mission-123")
print(rendered)
```

### Timeline

```python
# Get timeline view
timeline = interface.timeline_view("control-domain", "mission-123")
for event in timeline[-5:]:  # Last 5 events
    print(f"{event.recorded_at} [{event.source_type}] {event.source_id}")
```

## Testing Contract

The test suite (`tests/test_pavilion_native_interface.py`) proves:

1. ✅ All lifecycle states render correctly
2. ✅ Lifecycle/revision copied exactly from observation
3. ✅ Objective comes only from accepted projection
4. ✅ Zero effects represented correctly
5. ✅ Effect count matches observation
6. ✅ UNKNOWN remains UNKNOWN
7. ✅ UNKNOWN not rendered as nothing_landed
8. ✅ UNKNOWN not rendered as something_landed
9. ✅ INDETERMINATE remains visibly indeterminate
10. ✅ reconciliation_required visible
11. ✅ Mission FAILED does not rewrite effect outcome
12. ✅ Mission COMPLETED does not rewrite effect outcome
13. ✅ Unresolved-effect count is exact
14. ✅ evidence_available preserved exactly
15. ✅ Unbound evidence count represented
16. ✅ projection_fingerprint preserved exactly
17. ✅ Timestamps remain timezone-aware
18. ✅ Timeline uses observation timeline identities
19. ✅ Timeline ordering not independently reconstructed
20. ✅ Equal timestamps remain deterministic
21. ✅ View models are immutable
22. ✅ Nested collections cannot be mutated
23. ✅ Render output is deterministic
24. ✅ No raw mission metadata appears
25. ✅ permit_verifier cannot appear
26. ✅ Raw evidence payload/metadata cannot appear
27. ✅ No credential/lease objects exposed
28. ✅ Observability not-found behavior distinguishable
29. ✅ Integrity failure propagated explicitly
30. ✅ No authoritative writer APIs invoked
31. ✅ CanonicalPavilionCoordinator.coordinate never called
32. ✅ CanonicalPavilionAdapter.dispatch never called
33. ✅ No subprocess/network/SQL path exists
34. ✅ Same mission ID in two domains isolated
35. ✅ Unresolved effects identified prominently
36. ✅ No local cache/database created
37. ✅ Repeated render does not mutate source

## Implementation Boundary

### Protected Existing Implementations (Unchanged)

- ✅ `pavilionos/canonical_adapter.py`
- ✅ `pavilionos/canonical_coordinator.py`
- ✅ `pavilionos/authorization_envelope.py`
- ✅ `federation/mission_observability.py`
- ✅ `federation/mission_runtime.py`
- ✅ `federation/mission_runtime_store.py`
- ✅ `federation/mission_state.py`
- ✅ `federation/durable_effect_store.py`
- ✅ `federation/effect_gateway.py`
- ✅ `federation/effect_safety.py`
- ✅ `research_mission/evidence_spine.py`

### New v0.1 Implementation

- ➕ `pavilionos/native_interface.py`
- ➕ `tests/test_pavilion_native_interface.py`
- ➕ `PAVILION_NATIVE_INTERFACE.md` (this document)

## Version History

### v0.1 (Current)

- Initial read-only presentation interface
- Immutable mission/effect/timeline views
- Deterministic text rendering
- Confidentiality boundary enforcement
- Effect status semantics preservation
- Domain isolation
- No effectful operations
- Protected boundary validation

## See Also

- `federation/mission_observability.py` — Authoritative source interface
- `pavilionos/canonical_coordinator.py` — Future governed action path
- `tests/test_pavilion_native_interface.py` — Contract validation
