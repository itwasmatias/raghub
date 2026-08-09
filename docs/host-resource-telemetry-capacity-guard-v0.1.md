# Host Resource Telemetry & Capacity Guard v0.1

## Mission

RAGHub needs a trustworthy, deterministic way to answer:

**"What resources does this host have right now?"**

and:

**"Is it currently safe to admit a proposed local workload?"**

This milestone establishes immutable host-resource evidence and a deterministic capacity-decision contract.

## Scope

**This is OBSERVATION + DECISION only.**

It does NOT:
- Launch workloads
- Stop workloads
- Suspend workloads
- Kill processes
- Schedule workloads
- Modify workloads
- Authorize execution

## Architectural Position

Host Resource Telemetry & Capacity Guard provides trustworthy evidence about host resources and makes capacity admission decisions.

**Future composition** (not implemented in this milestone):

```
Local Model Artifact Registry
    → Is the exact artifact trusted?

Host Resource Telemetry & Capacity Guard (THIS MILESTONE)
    → Does this host currently have adequate safe capacity?

Governed Server Lifecycle
    → Is starting the workload authorized?

Worker Execution / Approval
    → Is execution governed?

Local Model Runtime
    → Perform authorized inference
```

This milestone is independent and does not depend on or modify:
- Local model artifact registry
- Server/process lifecycle control
- Task routing
- Task dispatch
- Worker execution
- Approval center

## Integration with Existing RAGHub Contracts

**Existing contracts inspected:**
- `federation/node_record.py` - Node identity, status, capabilities
- `federation/heartbeat.py` - Worker heartbeat submission
- `federation/heartbeat_registry.py` - Durable heartbeat evidence
- `federation/worker_governance.py` - Budget governance with abstract availability/capacity
- `federation/worker_liveness.py` - Worker liveness states
- `federation/integrity.py` - HMAC-SHA256 authentication tags
- `federation/capability.py` - Node capability model

**What we reuse:**
- Integrity fingerprinting pattern (SHA-256 of canonical JSON)
- Frozen dataclass pattern (`frozen=True, slots=True`)
- Validation in `__post_init__`
- Enum for states and statuses
- Timezone-aware UTC timestamps

**What remains separate:**
- This module focuses solely on host resource observation and capacity decisions
- `WorkerProviderMetadata.availability` and `.capacity` are abstract
- This module provides concrete evidence to support those decisions
- Does not modify existing governance, routing, or dispatch contracts
- Future integration can compose capacity decisions with worker metadata

## Architecture

### Core Contracts

#### HostMemorySnapshot
Immutable snapshot of host memory state.

**Fields:**
- `total_bytes: int` - Total physical memory
- `available_bytes: int` - Available physical memory
- `total_swap_bytes: int` - Total swap space
- `free_swap_bytes: int` - Free swap space

**Validation:**
- All values must be non-negative integers
- `available_bytes <= total_bytes`
- `free_swap_bytes <= total_swap_bytes`

#### HostCpuSnapshot
Immutable snapshot of host CPU state.

**Fields:**
- `logical_count: int` - Number of logical CPUs (must be positive)
- `load_average_1m: float | None` - Raw 1-minute load average
- `load_average_5m: float | None` - Raw 5-minute load average
- `load_average_15m: float | None` - Raw 15-minute load average

**Methods:**
- `normalized_load_1m() -> float | None` - Returns `load_average_1m / logical_count`
- `normalized_load_5m() -> float | None` - Returns `load_average_5m / logical_count`
- `normalized_load_15m() -> float | None` - Returns `load_average_15m / logical_count`

**Important:** Normalized load is **load per CPU**, NOT CPU utilization percentage.

#### HostStorageSnapshot
Immutable snapshot of filesystem capacity for a configured root.

**Fields:**
- `root_path: str` - Canonical absolute path to storage root
- `total_bytes: int` - Total filesystem capacity
- `free_bytes: int` - Free filesystem space
- `available_bytes: int` - Available filesystem space (may differ from free on some platforms)

**Validation:**
- Path is canonicalized (resolved to absolute path)
- All byte values must be non-negative
- `free_bytes <= total_bytes`
- `available_bytes <= total_bytes`

#### HostResourceSnapshot
Immutable complete host resource snapshot.

**Fields:**
- `node_id: str` - Node identifier
- `collected_at: datetime` - UTC timestamp of collection
- `memory: HostMemorySnapshot` - Memory state
- `cpu: HostCpuSnapshot` - CPU state
- `storage: tuple[HostStorageSnapshot, ...]` - Storage states (sorted by root_path)
- `fingerprint: str` - SHA-256 fingerprint of canonical representation

**Validation:**
- Storage roots are sorted by path for deterministic ordering
- No duplicate storage root paths
- Fingerprint must match computed value

#### HostResourceRequirement
Immutable resource requirement for a proposed workload.

**Domain-neutral** - suitable for local model servers, research workers, data jobs, etc.

**Fields:**
- `minimum_available_memory_bytes: int` - Minimum required available memory
- `minimum_available_storage_bytes: int | None` - Minimum required storage (optional)
- `storage_root_path: str | None` - Storage root to check (required if storage_bytes set)
- `maximum_normalized_cpu_load: float | None` - Maximum acceptable normalized load (optional)
- `minimum_swap_headroom_bytes: int | None` - Minimum swap headroom required (optional)
- `reserve_memory_bytes: int` - Memory that must remain after admission (default: 0)
- `reserve_storage_bytes: int` - Storage that must remain after admission (default: 0)
- `fingerprint: str` - SHA-256 fingerprint

**Validation:**
- `storage_root_path` and `minimum_available_storage_bytes` must both be set or both be None
- All byte values must be non-negative
- Path is canonicalized if provided

#### HostCapacityPolicy
Immutable global safety policy for host capacity thresholds.

**Separates global safety policy from workload-specific requirements.**

**Fields:**
- `minimum_host_memory_reserve_bytes: int` - Minimum memory reserve (default: 1 GB)
- `minimum_swap_reserve_bytes: int` - Minimum swap reserve (default: 512 MB)
- `maximum_normalized_load_threshold: float` - Maximum normalized load (default: 2.0)
- `minimum_storage_reserve_bytes: int` - Minimum storage reserve (default: 10 GB)
- `swap_pressure_threshold_bytes: int` - Swap usage threshold (default: 1 GB)
- `fingerprint: str` - SHA-256 fingerprint

**Conservative defaults provided** by `create_policy()` helper.

#### HostCapacityStatus (Enum)
Deterministic capacity decision status.

**Values:**
- `AVAILABLE` - All requirements and reserves satisfied with healthy margin
- `CONSTRAINED` - Technically possible but warning/policy thresholds breached
- `UNSAFE_TO_START` - Hard requirement or mandatory reserve cannot be satisfied

#### HostCapacityReason (Enum)
Deterministic reason codes for capacity decisions.

**Hard failure reasons (UNSAFE_TO_START):**
- `INSUFFICIENT_MEMORY` - Cannot satisfy minimum memory requirement
- `MEMORY_RESERVE_VIOLATION` - Memory reserve would be violated
- `INSUFFICIENT_STORAGE` - Cannot satisfy minimum storage requirement
- `STORAGE_RESERVE_VIOLATION` - Storage reserve would be violated
- `TELEMETRY_UNAVAILABLE` - Required telemetry unavailable

**Warning reasons (CONSTRAINED):**
- `CONSTRAINED_MEMORY` - Memory available but less than 1.5x reserve
- `CONSTRAINED_CPU` - Load > 80% of policy threshold
- `CONSTRAINED_STORAGE` - Storage available but less than 1.5x reserve
- `HIGH_CPU_LOAD` - Normalized load exceeds threshold
- `SWAP_PRESSURE` - Swap usage exceeds threshold

**Reason ordering is deterministic** (sorted by reason value).

#### HostCapacityDecision
Immutable capacity decision result.

**Fields:**
- `snapshot_fingerprint: str` - Fingerprint of snapshot
- `requirement_fingerprint: str` - Fingerprint of requirement
- `policy_fingerprint: str` - Fingerprint of policy
- `status: HostCapacityStatus` - Decision status
- `reasons: tuple[HostCapacityReason, ...]` - Sorted deterministic reasons
- `fingerprint: str` - SHA-256 fingerprint of decision

**Important:** A capacity decision is **NOT** authorization to execute.

`AVAILABLE` means only that observed host resources satisfy the configured capacity contract.

Execution must still pass:
- Normal RAGHub routing
- Task dispatch
- Worker execution governance
- Approval center
- Capability checks
- Future host-lifecycle governance

## Linux/Fedora Collection Sources

The first concrete collector targets **Linux** because Fedora is the current execution host.

Data contracts remain platform-neutral where practical.

### Memory Collection
**Source:** `/proc/meminfo`

**Required fields:**
- `MemTotal` → `total_bytes` (converted from kB to bytes)
- `MemAvailable` → `available_bytes`
- `SwapTotal` → `total_swap_bytes`
- `SwapFree` → `free_swap_bytes`

**Parsing rules:**
- Defensive parsing with fail-closed behavior
- Rejects duplicate required fields
- Rejects negative values
- Rejects malformed numbers
- Converts kB to bytes deterministically (× 1024)
- Does not fabricate unavailable telemetry

**No subprocess execution** - uses `Path.read_text()` only.

### CPU Collection
**Sources:**
- `os.cpu_count()` → `logical_count`
- `os.getloadavg()` → `load_average_1m`, `load_average_5m`, `load_average_15m`

**Behavior:**
- Returns `None` for load averages if `os.getloadavg()` unavailable (platform-specific)
- No shell execution
- Pure Python standard library

### Storage Collection
**Source:** `os.statvfs()` for configured roots only

**Collected fields:**
- `f_blocks * f_frsize` → `total_bytes`
- `f_bfree * f_frsize` → `free_bytes`
- `f_bavail * f_frsize` → `available_bytes`

**Security:**
- Only inspects explicitly configured storage roots
- Canonicalizes paths via `Path.resolve()`
- Fails if root does not exist
- Fails if root is not a directory
- Does **NOT** recursively traverse directories
- Does **NOT** scan user files
- Only reads filesystem metadata

## Memory Semantics

Capacity decisions reason primarily from **available memory** rather than total memory.

**Admission logic:**
```python
memory_after_workload = available_memory - required_memory
total_reserve = policy_reserve + workload_reserve

if memory_after_workload < 0:
    → INSUFFICIENT_MEMORY
elif memory_after_workload < total_reserve:
    → MEMORY_RESERVE_VIOLATION
elif memory_after_workload < total_reserve * 1.5:
    → CONSTRAINED_MEMORY
```

**Swap is NOT counted as equivalent to RAM** unless policy explicitly says so.

No memory predictor in this milestone - requirements are supplied by caller.

## Swap Semantics

**Swap pressure detection:**
```python
swap_used = total_swap - free_swap

if swap_used > policy.swap_pressure_threshold_bytes:
    → SWAP_PRESSURE (CONSTRAINED)
```

**Swap headroom check (if required):**
```python
if free_swap < required_headroom + policy_reserve:
    → SWAP_PRESSURE (CONSTRAINED)
```

## Linux Load Average Semantics

**Raw load averages are preserved** without modification.

**Raw Linux load average is NOT a percentage.**

It represents the number of processes in the run queue or waiting for I/O, averaged over time.

**Normalized load** is defined as:
```python
normalized_load = load_average / logical_cpu_count
```

**Normalized load is NOT CPU utilization.**

It is simply load per CPU, providing a scale-independent measure.

**Example:**
- 8 CPUs with load average 16.0
- Normalized load = 16.0 / 8 = 2.0
- This means 2.0 processes per CPU on average, NOT 200% utilization

**If load information unavailable** on a platform, `None` is returned - no fabrication.

## Storage Root Boundary

**Collector only inspects storage paths explicitly configured by the caller.**

**Canonicalization:**
- Paths resolved to absolute canonical form via `Path.resolve()`
- Symlinks are followed

**Security:**
- Fails closed on nonexistent roots
- Fails closed on inaccessible roots
- Fails closed on non-directory roots
- Does NOT recursively traverse storage roots
- Only filesystem capacity metadata is collected

## Snapshot Semantics

Collection does not freeze the kernel.

**The snapshot is a bounded sequence of observations** made during one collection event.

A single `collected_at` timestamp represents the collection operation.

Does not imply perfect simultaneous hardware measurement.

## Decision Semantics

### AVAILABLE
All requirements and reserves are satisfied with healthy margin.

No warning conditions detected.

### CONSTRAINED
Technically possible to start, but one or more warning/policy thresholds are breached.

Examples:
- Memory available but < 1.5x reserve
- Load > 80% of policy threshold
- Swap usage above policy threshold
- Load exceeds requirement or policy threshold

### UNSAFE_TO_START
A hard workload requirement or mandatory reserve cannot be satisfied.

Examples:
- Insufficient memory
- Memory reserve violation
- Insufficient storage
- Storage reserve violation
- Required telemetry unavailable

## Deterministic Reason Codes

Machine-readable typed reason codes rather than prose-only decisions.

**Reason ordering is deterministic** (sorted by enum value).

Human-readable explanations may accompany them but are **not the sole machine contract**.

Repeated evaluation of identical immutable evidence + requirement + policy produces equivalent deterministic decisions.

## Fingerprint Model

Uses **deterministic canonical serialization:**
- Canonical JSON (sorted keys, no whitespace: `{"a":1,"b":2}`)
- SHA-256 hash of canonical bytes
- All evidence-bearing fields included

**Fingerprint changes when:**
- Any evidence field changes (memory, CPU, storage, timestamp)
- Requirement values change
- Policy values change
- Decision status changes
- Decision reasons change

**Nested values are detached** from caller-owned mutable structures.

Unsupported arbitrary Python objects are rejected.

**Never includes secrets.**

## Concurrency Model

Collection and evaluation are safe under normal concurrent use.

**Pure decision logic** - `HostCapacityGuard.evaluate()` has no mutable global state.

Repeated evaluation of identical immutable evidence produces equivalent deterministic decisions.

**No module-global caches.**

**No background threads.**

**No mutable singletons.**

## Security Boundaries

**No subprocess execution:**
- Does not shell out to `free`, `df`, `top`, `ps`, `vmstat`, `lscpu`, `awk`, `grep`, `cat`
- Uses Python standard library only
- Direct reads from `/proc/meminfo`, `os.cpu_count()`, `os.getloadavg()`, `os.statvfs()`

**No network access:**
- Does not make HTTP requests
- Does not access cloud APIs
- Does not access Hugging Face
- Does not download packages/models
- Does not call OpenAI or external services

**No process control:**
- Does not launch servers
- Does not kill processes
- Does not suspend workers
- Does not alter cgroups, nice levels, CPU affinity
- Does not allocate memory as a test
- Does not fill disk space
- Does not change swap
- Does not write to `/proc` or `/sys`
- Does not modify host configuration

**Storage boundaries:**
- Only inspects explicitly configured roots
- Does not scan arbitrary directory trees
- Does not inspect unrelated process contents
- Does not read user files

## Limitations

**This milestone does NOT:**
- Provide durable event storage (persistence is out of scope unless needed)
- Predict future resource usage
- Monitor resources over time
- Integrate with task routing/dispatch
- Authorize workload execution
- Launch or manage workloads
- Verify artifact integrity (separate concern)
- Control server lifecycle (separate concern)

**Platform support:**
- Linux/Fedora implementation provided
- Data contracts are platform-neutral where practical
- Load average may be `None` on non-Unix platforms
- Future: Windows/macOS collectors can be added

**Load average limitations:**
- Raw Linux load includes I/O wait, not just CPU
- Normalized load is a heuristic, not precise CPU measurement
- High load may indicate I/O bottleneck rather than CPU saturation

**Snapshot timing:**
- Observations are bounded but not perfectly simultaneous
- Memory, CPU, and storage are read sequentially during one collection event
- Sub-second variations possible

## Non-Goals

**This milestone explicitly does NOT:**
- Implement artifact verification
- Implement server lifecycle control
- Modify task routing
- Modify task dispatch
- Modify worker execution
- Modify approval center
- Integrate with Pilot #1 or Pilot #2
- Depend on llama.cpp adapter
- Depend on local model artifact registry
- Depend on cached-token semantics
- Automatically apply capacity decisions to routing policy

## Future Composition

Future milestones may compose this decision with:

**Local Model Artifact Registry:**
```python
artifact_trusted = artifact_registry.verify(model_artifact)
capacity_available = capacity_guard.evaluate(snapshot, requirement, policy)

if artifact_trusted and capacity_available.status == AVAILABLE:
    # Proceed to lifecycle governance
```

**Worker Metadata Integration:**
```python
# Future: inform WorkerProviderMetadata.capacity from capacity decision
metadata = WorkerProviderMetadata(
    node_id=node_id,
    capacity=(
        WorkerCapacity.AVAILABLE if decision.status == AVAILABLE
        else WorkerCapacity.SATURATED if decision.status == UNSAFE_TO_START
        else WorkerCapacity.UNKNOWN
    ),
    ...
)
```

**Routing Integration:**
```python
# Future: use capacity decision in routing evaluation
if capacity_decision.status == UNSAFE_TO_START:
    routing_decision.reject_node(node_id, reasons=capacity_decision.reasons)
```

These integrations are **deliberately not implemented** in this milestone to maintain independence and cherry-pick safety.

## Testing

**63 comprehensive deterministic tests** covering:

- Valid snapshot construction
- Malformed /proc/meminfo rejection
- Duplicate required field rejection
- Missing required field handling
- Negative value rejection
- Logical impossibility rejection (available > total)
- CPU count validation
- Raw load average preservation
- Normalized load calculation correctness
- Normalized load not mislabeled as utilization
- Valid filesystem capacity snapshot
- Nonexistent/invalid storage root handling (collector level)
- Canonical storage root preservation
- Multiple storage roots deterministic ordering
- Snapshot fingerprint determinism
- Snapshot fingerprint changes with evidence
- Nested metadata immutability
- NaN/infinity rejection
- Unsupported opaque metadata rejection
- Requirement fingerprint determinism
- Requirement identity changes alter fingerprint
- Policy fingerprint determinism
- Policy changes alter fingerprint
- Adequate host returns AVAILABLE
- Hard memory shortage returns UNSAFE_TO_START
- Memory reserve violation handling
- Hard storage shortage returns UNSAFE_TO_START
- Storage reserve violation handling
- High CPU load produces correct result
- Swap pressure produces correct result
- Multiple simultaneous reasons preserved
- Reason ordering determinism
- Repeated identical evaluation produces identical result
- Decision fingerprint changes with snapshot/requirement/policy
- Optional telemetry handling
- Unavailable mandatory telemetry fails closed
- Zero cloud behavior (no network)
- No subprocess usage
- No shell execution
- No process lifecycle behavior
- Returned evidence immutability
- Collector does not recursively read storage roots

**Real Fedora smoke test** (read-only):
- Collects actual host telemetry
- Does NOT modify machine
- Does NOT assert exact RAM/load/storage values
- Validates collector can read real /proc/meminfo, load averages, filesystem capacity

## Usage Example

```python
from federation.host_resource_capacity import (
    HostResourceCollector,
    HostCapacityGuard,
    create_requirement,
    create_policy,
    HostCapacityStatus,
)

# Collect current host state
collector = HostResourceCollector()
snapshot = collector.collect(
    node_id="worker-1",
    storage_roots=("/var/raghub/models",),
)

# Define workload requirement (e.g., for local model server)
requirement = create_requirement(
    minimum_available_memory_bytes=8 * 1024**3,  # 8 GB
    minimum_available_storage_bytes=50 * 1024**3,  # 50 GB
    storage_root_path="/var/raghub/models",
    maximum_normalized_cpu_load=1.5,
    reserve_memory_bytes=2 * 1024**3,  # Keep 2 GB extra
)

# Define safety policy (or use conservative defaults)
policy = create_policy(
    minimum_host_memory_reserve_bytes=2 * 1024**3,  # 2 GB
    maximum_normalized_load_threshold=2.0,
)

# Evaluate capacity decision
guard = HostCapacityGuard()
decision = guard.evaluate(snapshot, requirement, policy)

if decision.status == HostCapacityStatus.AVAILABLE:
    print("Safe to admit workload")
elif decision.status == HostCapacityStatus.CONSTRAINED:
    print(f"Constrained: {[r.value for r in decision.reasons]}")
    # May still proceed with caution
else:  # UNSAFE_TO_START
    print(f"Unsafe to start: {[r.value for r in decision.reasons]}")
    # Must NOT start workload
```

## Files

**Implementation:**
- `federation/host_resource_capacity.py` (934 lines)

**Tests:**
- `tests/test_host_resource_capacity.py` (63 tests)

**Documentation:**
- `docs/host-resource-telemetry-capacity-guard-v0.1.md` (this file)

**No other files modified.**

## Validation

All tests pass:
```
pytest tests/test_host_resource_capacity.py -q
63 passed
```

Python compiles cleanly:
```
python3 -m compileall -q federation tests
```

No git issues:
```
git diff --check
```

## Remaining Risks

**Platform portability:**
- Load average unavailable on Windows (returns None)
- /proc/meminfo specific to Linux
- Future: Windows Performance Counters, macOS sysctl

**Load average interpretation:**
- Includes I/O wait, not just CPU
- May not directly correlate with performance
- Normalized load is heuristic

**Snapshot timing:**
- Observations sequential, not atomic
- Sub-second resource variations possible

**No historical analysis:**
- Single point-in-time observation
- No trending or prediction
- Future: time-series analysis separate concern

**No automatic integration:**
- Does not automatically update routing decisions
- Does not automatically reject tasks
- Requires explicit composition in future milestones

## Success Criteria

**Achieved:**
- ✓ Immutable evidence contracts with deterministic fingerprints
- ✓ Linux/Fedora collector using standard library only
- ✓ Zero subprocess execution
- ✓ Zero network access
- ✓ Zero process control
- ✓ Deterministic capacity decision logic
- ✓ Separate requirement, policy, and decision contracts
- ✓ Machine-readable status and reason codes
- ✓ 63 comprehensive tests all passing
- ✓ Platform-neutral data contracts
- ✓ Conservative failure behavior
- ✓ Independent of parallel work streams
- ✓ Cherry-pick safe

**Ready for future composition** with:
- Artifact registry
- Server lifecycle governance
- Routing decisions
- Worker metadata
