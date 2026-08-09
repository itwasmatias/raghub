"""Host resource telemetry and capacity guard for RAGHub federation.

Provides deterministic host resource observation and capacity admission decisions.
This module observes only - it does not launch, stop, or modify workloads.
"""

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


def _canonical_json(value: dict) -> bytes:
    """Deterministic canonical JSON serialization."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(data: dict) -> str:
    """Compute SHA-256 fingerprint of canonical representation."""
    return hashlib.sha256(_canonical_json(data)).hexdigest()


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    """Validate and normalize timestamp to UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    """Format UTC timestamp for canonical serialization."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: str, field_name: str) -> str:
    """Validate non-empty identifier."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _nonnegative_int(value, field_name: str) -> int:
    """Validate non-negative integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _positive_int(value, field_name: str) -> int:
    """Validate positive integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _nonnegative_float(value, field_name: str) -> float:
    """Validate non-negative finite float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if not (value >= 0 and value != float('inf')):
        raise ValueError(f"{field_name} must be non-negative and finite")
    return value


def _optional_nonnegative_int(value, field_name: str) -> int | None:
    """Validate optional non-negative integer."""
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _optional_nonnegative_float(value, field_name: str) -> float | None:
    """Validate optional non-negative finite float."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number or None")
    value = float(value)
    if value != value:  # NaN check
        raise ValueError(f"{field_name} must not be NaN")
    if value == float('inf') or value == float('-inf'):
        raise ValueError(f"{field_name} must be finite")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _reserve_multiplier(value, field_name: str) -> float:
    """Validate reserve multiplier (must be >= 1.0, finite)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if value != value:  # NaN check
        raise ValueError(f"{field_name} must not be NaN")
    if value == float('inf') or value == float('-inf'):
        raise ValueError(f"{field_name} must be finite")
    if value < 1.0:
        raise ValueError(f"{field_name} must be >= 1.0")
    return value


def _threshold_ratio(value, field_name: str) -> float:
    """Validate threshold ratio (must be 0.0 < ratio <= 1.0, finite)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    value = float(value)
    if value != value:  # NaN check
        raise ValueError(f"{field_name} must not be NaN")
    if value == float('inf') or value == float('-inf'):
        raise ValueError(f"{field_name} must be finite")
    if value <= 0.0 or value > 1.0:
        raise ValueError(f"{field_name} must be in range (0.0, 1.0]")
    return value


@dataclass(frozen=True, slots=True)
class HostMemorySnapshot:
    """Immutable snapshot of host memory state.

    All values are in bytes. Captures physical memory and swap.
    """

    total_bytes: int
    available_bytes: int
    total_swap_bytes: int
    free_swap_bytes: int

    def __post_init__(self):
        """Validate memory snapshot."""
        object.__setattr__(self, "total_bytes", _nonnegative_int(self.total_bytes, "total_bytes"))
        object.__setattr__(self, "available_bytes", _nonnegative_int(self.available_bytes, "available_bytes"))
        object.__setattr__(self, "total_swap_bytes", _nonnegative_int(self.total_swap_bytes, "total_swap_bytes"))
        object.__setattr__(self, "free_swap_bytes", _nonnegative_int(self.free_swap_bytes, "free_swap_bytes"))

        if self.available_bytes > self.total_bytes:
            raise ValueError("available_bytes cannot exceed total_bytes")
        if self.free_swap_bytes > self.total_swap_bytes:
            raise ValueError("free_swap_bytes cannot exceed total_swap_bytes")


@dataclass(frozen=True, slots=True)
class HostCpuSnapshot:
    """Immutable snapshot of host CPU state.

    logical_count: Number of logical CPUs
    load_average_*: Raw Linux load averages (not percentages)

    Use normalized_load_1m() to get load per CPU.
    Normalized load is load per CPU, not a percentage.
    """

    logical_count: int
    load_average_1m: float | None
    load_average_5m: float | None
    load_average_15m: float | None

    def __post_init__(self):
        """Validate CPU snapshot."""
        object.__setattr__(self, "logical_count", _positive_int(self.logical_count, "logical_count"))
        object.__setattr__(self, "load_average_1m", _optional_nonnegative_float(self.load_average_1m, "load_average_1m"))
        object.__setattr__(self, "load_average_5m", _optional_nonnegative_float(self.load_average_5m, "load_average_5m"))
        object.__setattr__(self, "load_average_15m", _optional_nonnegative_float(self.load_average_15m, "load_average_15m"))

    def normalized_load_1m(self) -> float | None:
        """Calculate normalized 1-minute load average.

        Returns load_average_1m divided by logical CPU count.
        This is load per CPU, not a percentage.
        Returns None if load average is unavailable.
        """
        if self.load_average_1m is None:
            return None
        return self.load_average_1m / self.logical_count

    def normalized_load_5m(self) -> float | None:
        """Calculate normalized 5-minute load average."""
        if self.load_average_5m is None:
            return None
        return self.load_average_5m / self.logical_count

    def normalized_load_15m(self) -> float | None:
        """Calculate normalized 15-minute load average."""
        if self.load_average_15m is None:
            return None
        return self.load_average_15m / self.logical_count


@dataclass(frozen=True, slots=True)
class HostStorageSnapshot:
    """Immutable snapshot of filesystem capacity.

    Only inspects configured storage roots - does not traverse directories.
    """

    root_path: str
    total_bytes: int
    free_bytes: int
    available_bytes: int

    def __post_init__(self):
        """Validate storage snapshot."""
        object.__setattr__(self, "root_path", _identifier(self.root_path, "root_path"))
        object.__setattr__(self, "total_bytes", _nonnegative_int(self.total_bytes, "total_bytes"))
        object.__setattr__(self, "free_bytes", _nonnegative_int(self.free_bytes, "free_bytes"))
        object.__setattr__(self, "available_bytes", _nonnegative_int(self.available_bytes, "available_bytes"))

        if self.free_bytes > self.total_bytes:
            raise ValueError("free_bytes cannot exceed total_bytes")
        if self.available_bytes > self.total_bytes:
            raise ValueError("available_bytes cannot exceed total_bytes")

        # Canonicalize path
        try:
            canonical = str(Path(self.root_path).resolve())
        except (OSError, ValueError) as exc:
            raise ValueError(f"root_path is invalid: {exc}") from exc
        object.__setattr__(self, "root_path", canonical)


@dataclass(frozen=True, slots=True)
class HostResourceSnapshot:
    """Immutable snapshot of host resource state.

    Captures memory, CPU, and storage at a single collection event.
    """

    node_id: str
    collected_at: datetime
    memory: HostMemorySnapshot
    cpu: HostCpuSnapshot
    storage: tuple[HostStorageSnapshot, ...]
    fingerprint: str

    def __post_init__(self):
        """Validate and fingerprint snapshot."""
        object.__setattr__(self, "node_id", _identifier(self.node_id, "node_id"))
        object.__setattr__(self, "collected_at", _normalize_timestamp(self.collected_at, "collected_at"))

        if not isinstance(self.memory, HostMemorySnapshot):
            raise TypeError("memory must be a HostMemorySnapshot")
        if not isinstance(self.cpu, HostCpuSnapshot):
            raise TypeError("cpu must be a HostCpuSnapshot")

        if not isinstance(self.storage, tuple):
            raise TypeError("storage must be a tuple")
        storage_list = list(self.storage)
        if not all(isinstance(item, HostStorageSnapshot) for item in storage_list):
            raise TypeError("storage must contain only HostStorageSnapshot values")

        # Ensure deterministic ordering by root_path
        storage_sorted = tuple(sorted(storage_list, key=lambda s: s.root_path))
        object.__setattr__(self, "storage", storage_sorted)

        # Check for duplicate root paths
        paths = [s.root_path for s in storage_sorted]
        if len(paths) != len(set(paths)):
            raise ValueError("storage contains duplicate root_path values")

        # Compute fingerprint
        payload = {
            "node_id": self.node_id,
            "collected_at": _format_timestamp(self.collected_at),
            "memory": {
                "total_bytes": self.memory.total_bytes,
                "available_bytes": self.memory.available_bytes,
                "total_swap_bytes": self.memory.total_swap_bytes,
                "free_swap_bytes": self.memory.free_swap_bytes,
            },
            "cpu": {
                "logical_count": self.cpu.logical_count,
                "load_average_1m": self.cpu.load_average_1m,
                "load_average_5m": self.cpu.load_average_5m,
                "load_average_15m": self.cpu.load_average_15m,
            },
            "storage": [
                {
                    "root_path": s.root_path,
                    "total_bytes": s.total_bytes,
                    "free_bytes": s.free_bytes,
                    "available_bytes": s.available_bytes,
                }
                for s in self.storage
            ],
        }
        computed = _fingerprint(payload)
        if self.fingerprint != computed:
            raise ValueError("snapshot fingerprint does not match computed value")


@dataclass(frozen=True, slots=True)
class HostResourceRequirement:
    """Immutable resource requirement for a proposed workload.

    Domain-neutral - suitable for local model servers, research workers, etc.
    """

    minimum_available_memory_bytes: int
    minimum_available_storage_bytes: int | None
    storage_root_path: str | None
    maximum_normalized_cpu_load: float | None
    minimum_swap_headroom_bytes: int | None
    reserve_memory_bytes: int
    reserve_storage_bytes: int
    fingerprint: str

    def __post_init__(self):
        """Validate and fingerprint requirement."""
        object.__setattr__(
            self,
            "minimum_available_memory_bytes",
            _nonnegative_int(self.minimum_available_memory_bytes, "minimum_available_memory_bytes"),
        )
        object.__setattr__(
            self,
            "minimum_available_storage_bytes",
            _optional_nonnegative_int(self.minimum_available_storage_bytes, "minimum_available_storage_bytes"),
        )

        if self.storage_root_path is not None:
            object.__setattr__(self, "storage_root_path", _identifier(self.storage_root_path, "storage_root_path"))
            # Canonicalize
            try:
                canonical = str(Path(self.storage_root_path).resolve())
            except (OSError, ValueError) as exc:
                raise ValueError(f"storage_root_path is invalid: {exc}") from exc
            object.__setattr__(self, "storage_root_path", canonical)

        if self.maximum_normalized_cpu_load is not None:
            object.__setattr__(
                self,
                "maximum_normalized_cpu_load",
                _nonnegative_float(self.maximum_normalized_cpu_load, "maximum_normalized_cpu_load"),
            )

        object.__setattr__(
            self,
            "minimum_swap_headroom_bytes",
            _optional_nonnegative_int(self.minimum_swap_headroom_bytes, "minimum_swap_headroom_bytes"),
        )
        object.__setattr__(
            self,
            "reserve_memory_bytes",
            _nonnegative_int(self.reserve_memory_bytes, "reserve_memory_bytes"),
        )
        object.__setattr__(
            self,
            "reserve_storage_bytes",
            _nonnegative_int(self.reserve_storage_bytes, "reserve_storage_bytes"),
        )

        # Validate consistency
        if self.minimum_available_storage_bytes is not None and self.storage_root_path is None:
            raise ValueError("storage_root_path required when minimum_available_storage_bytes is set")
        if self.minimum_available_storage_bytes is None and self.storage_root_path is not None:
            raise ValueError("minimum_available_storage_bytes required when storage_root_path is set")

        # Compute fingerprint
        payload = {
            "minimum_available_memory_bytes": self.minimum_available_memory_bytes,
            "minimum_available_storage_bytes": self.minimum_available_storage_bytes,
            "storage_root_path": self.storage_root_path,
            "maximum_normalized_cpu_load": self.maximum_normalized_cpu_load,
            "minimum_swap_headroom_bytes": self.minimum_swap_headroom_bytes,
            "reserve_memory_bytes": self.reserve_memory_bytes,
            "reserve_storage_bytes": self.reserve_storage_bytes,
        }
        computed = _fingerprint(payload)
        if self.fingerprint != computed:
            raise ValueError("requirement fingerprint does not match computed value")


@dataclass(frozen=True, slots=True)
class HostCapacityPolicy:
    """Immutable policy for host capacity safety thresholds.

    Separates global safety policy from workload-specific requirements.
    """

    minimum_host_memory_reserve_bytes: int
    minimum_swap_reserve_bytes: int
    maximum_normalized_load_threshold: float
    minimum_storage_reserve_bytes: int
    swap_pressure_threshold_bytes: int
    memory_constrained_reserve_multiplier: float
    cpu_constrained_threshold_ratio: float
    storage_constrained_reserve_multiplier: float
    fingerprint: str

    def __post_init__(self):
        """Validate and fingerprint policy."""
        object.__setattr__(
            self,
            "minimum_host_memory_reserve_bytes",
            _nonnegative_int(self.minimum_host_memory_reserve_bytes, "minimum_host_memory_reserve_bytes"),
        )
        object.__setattr__(
            self,
            "minimum_swap_reserve_bytes",
            _nonnegative_int(self.minimum_swap_reserve_bytes, "minimum_swap_reserve_bytes"),
        )
        object.__setattr__(
            self,
            "maximum_normalized_load_threshold",
            _nonnegative_float(self.maximum_normalized_load_threshold, "maximum_normalized_load_threshold"),
        )
        object.__setattr__(
            self,
            "minimum_storage_reserve_bytes",
            _nonnegative_int(self.minimum_storage_reserve_bytes, "minimum_storage_reserve_bytes"),
        )
        object.__setattr__(
            self,
            "swap_pressure_threshold_bytes",
            _nonnegative_int(self.swap_pressure_threshold_bytes, "swap_pressure_threshold_bytes"),
        )
        object.__setattr__(
            self,
            "memory_constrained_reserve_multiplier",
            _reserve_multiplier(self.memory_constrained_reserve_multiplier, "memory_constrained_reserve_multiplier"),
        )
        object.__setattr__(
            self,
            "cpu_constrained_threshold_ratio",
            _threshold_ratio(self.cpu_constrained_threshold_ratio, "cpu_constrained_threshold_ratio"),
        )
        object.__setattr__(
            self,
            "storage_constrained_reserve_multiplier",
            _reserve_multiplier(self.storage_constrained_reserve_multiplier, "storage_constrained_reserve_multiplier"),
        )

        # Compute fingerprint
        payload = {
            "minimum_host_memory_reserve_bytes": self.minimum_host_memory_reserve_bytes,
            "minimum_swap_reserve_bytes": self.minimum_swap_reserve_bytes,
            "maximum_normalized_load_threshold": self.maximum_normalized_load_threshold,
            "minimum_storage_reserve_bytes": self.minimum_storage_reserve_bytes,
            "swap_pressure_threshold_bytes": self.swap_pressure_threshold_bytes,
            "memory_constrained_reserve_multiplier": self.memory_constrained_reserve_multiplier,
            "cpu_constrained_threshold_ratio": self.cpu_constrained_threshold_ratio,
            "storage_constrained_reserve_multiplier": self.storage_constrained_reserve_multiplier,
        }
        computed = _fingerprint(payload)
        if self.fingerprint != computed:
            raise ValueError("policy fingerprint does not match computed value")


class HostCapacityStatus(str, Enum):
    """Status of host capacity decision."""

    AVAILABLE = "available"  # All requirements and reserves satisfied with healthy margin
    CONSTRAINED = "constrained"  # Possible but warning thresholds breached
    UNSAFE_TO_START = "unsafe_to_start"  # Hard requirement or reserve cannot be satisfied


class HostCapacityReason(str, Enum):
    """Deterministic reason codes for capacity decisions."""

    INSUFFICIENT_MEMORY = "insufficient_memory"
    MEMORY_RESERVE_VIOLATION = "memory_reserve_violation"
    HIGH_CPU_LOAD = "high_cpu_load"
    SWAP_PRESSURE = "swap_pressure"
    INSUFFICIENT_STORAGE = "insufficient_storage"
    STORAGE_RESERVE_VIOLATION = "storage_reserve_violation"
    TELEMETRY_UNAVAILABLE = "telemetry_unavailable"
    CONSTRAINED_MEMORY = "constrained_memory"
    CONSTRAINED_CPU = "constrained_cpu"
    CONSTRAINED_STORAGE = "constrained_storage"


@dataclass(frozen=True, slots=True)
class HostCapacityDecision:
    """Immutable capacity decision result.

    A capacity decision is NOT authorization to execute.
    AVAILABLE means observed host resources satisfy the configured capacity contract.
    Execution must still pass normal RAGHub routing, dispatch, and governance.
    """

    snapshot_fingerprint: str
    requirement_fingerprint: str
    policy_fingerprint: str
    status: HostCapacityStatus
    reasons: tuple[HostCapacityReason, ...]
    fingerprint: str

    def __post_init__(self):
        """Validate and fingerprint decision."""
        if not isinstance(self.snapshot_fingerprint, str) or len(self.snapshot_fingerprint) != 64:
            raise ValueError("snapshot_fingerprint must be a SHA-256 hex string")
        if not isinstance(self.requirement_fingerprint, str) or len(self.requirement_fingerprint) != 64:
            raise ValueError("requirement_fingerprint must be a SHA-256 hex string")
        if not isinstance(self.policy_fingerprint, str) or len(self.policy_fingerprint) != 64:
            raise ValueError("policy_fingerprint must be a SHA-256 hex string")

        if not isinstance(self.status, HostCapacityStatus):
            try:
                object.__setattr__(self, "status", HostCapacityStatus(self.status))
            except (TypeError, ValueError) as exc:
                raise ValueError("status is invalid") from exc

        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")
        reason_list = list(self.reasons)
        if not all(isinstance(r, HostCapacityReason) for r in reason_list):
            try:
                reason_list = [HostCapacityReason(r) for r in reason_list]
            except (TypeError, ValueError) as exc:
                raise ValueError("reasons contains invalid value") from exc

        # Ensure deterministic ordering
        reason_sorted = tuple(sorted(set(reason_list), key=lambda r: r.value))
        object.__setattr__(self, "reasons", reason_sorted)

        # Compute fingerprint
        payload = {
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "requirement_fingerprint": self.requirement_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "status": self.status.value,
            "reasons": [r.value for r in self.reasons],
        }
        computed = _fingerprint(payload)
        if self.fingerprint != computed:
            raise ValueError("decision fingerprint does not match computed value")


class HostResourceCollector:
    """Collects host resource telemetry using platform-native interfaces.

    Linux/Fedora implementation using standard library only.
    No subprocess execution. No network access.
    """

    def collect_memory(self) -> HostMemorySnapshot:
        """Collect memory snapshot from /proc/meminfo.

        Raises:
            ValueError: If required fields are missing or malformed
            OSError: If /proc/meminfo cannot be read
        """
        meminfo_path = Path("/proc/meminfo")

        try:
            content = meminfo_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(f"Failed to read {meminfo_path}: {exc}") from exc

        fields = {}
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            parts = line.split(":", 1)
            if len(parts) != 2:
                continue

            key = parts[0].strip()
            value_part = parts[1].strip()

            # Check for duplicate required fields
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                if key in fields:
                    raise ValueError(f"/proc/meminfo contains duplicate {key} field")

            # Parse value and unit
            value_parts = value_part.split()
            if not value_parts:
                continue

            try:
                value_num = int(value_parts[0])
            except ValueError:
                continue

            # Handle unit (typically kB)
            if len(value_parts) > 1:
                unit = value_parts[1].lower()
                if unit == "kb":
                    value_bytes = value_num * 1024
                else:
                    # Unsupported unit
                    continue
            else:
                # No unit means bytes
                value_bytes = value_num

            if value_bytes < 0:
                raise ValueError(f"/proc/meminfo {key} value is negative")

            fields[key] = value_bytes

        # Validate required fields
        required = ["MemTotal", "MemAvailable", "SwapTotal", "SwapFree"]
        missing = [field for field in required if field not in fields]
        if missing:
            raise ValueError(f"/proc/meminfo missing required fields: {missing}")

        return HostMemorySnapshot(
            total_bytes=fields["MemTotal"],
            available_bytes=fields["MemAvailable"],
            total_swap_bytes=fields["SwapTotal"],
            free_swap_bytes=fields["SwapFree"],
        )

    def collect_cpu(self) -> HostCpuSnapshot:
        """Collect CPU snapshot using os.cpu_count() and os.getloadavg().

        Load averages are raw Linux values (not percentages).
        Load average unavailable on some platforms returns None.

        Raises:
            ValueError: If CPU count is invalid
        """
        cpu_count = os.cpu_count()
        if cpu_count is None or cpu_count <= 0:
            raise ValueError("os.cpu_count() returned invalid value")

        # Load average may not be available on all platforms
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
            load_1m = float(load_1m)
            load_5m = float(load_5m)
            load_15m = float(load_15m)
        except (OSError, AttributeError):
            # getloadavg not available on this platform
            load_1m = None
            load_5m = None
            load_15m = None

        return HostCpuSnapshot(
            logical_count=cpu_count,
            load_average_1m=load_1m,
            load_average_5m=load_5m,
            load_average_15m=load_15m,
        )

    def collect_storage(self, roots: tuple[str, ...]) -> tuple[HostStorageSnapshot, ...]:
        """Collect filesystem capacity for configured roots.

        Only inspects configured roots - does not traverse directories.

        Args:
            roots: Tuple of storage root paths to inspect

        Returns:
            Tuple of HostStorageSnapshot, one per root, sorted by canonical path

        Raises:
            ValueError: If root does not exist or is not a directory
            OSError: If filesystem metadata cannot be read
        """
        if not isinstance(roots, tuple):
            raise TypeError("roots must be a tuple")

        snapshots = []
        for root in roots:
            if not isinstance(root, str):
                raise TypeError("roots must contain only strings")

            root_path = Path(root)

            # Canonicalize and validate
            try:
                canonical = root_path.resolve()
            except (OSError, ValueError) as exc:
                raise ValueError(f"Invalid storage root {root}: {exc}") from exc

            if not canonical.exists():
                raise ValueError(f"Storage root does not exist: {canonical}")

            if not canonical.is_dir():
                raise ValueError(f"Storage root is not a directory: {canonical}")

            # Collect filesystem capacity
            try:
                stat = os.statvfs(canonical)
            except OSError as exc:
                raise OSError(f"Failed to read filesystem capacity for {canonical}: {exc}") from exc

            total_bytes = stat.f_blocks * stat.f_frsize
            free_bytes = stat.f_bfree * stat.f_frsize
            available_bytes = stat.f_bavail * stat.f_frsize

            snapshots.append(
                HostStorageSnapshot(
                    root_path=str(canonical),
                    total_bytes=total_bytes,
                    free_bytes=free_bytes,
                    available_bytes=available_bytes,
                )
            )

        # Sort by canonical path for deterministic ordering
        return tuple(sorted(snapshots, key=lambda s: s.root_path))

    def collect(
        self,
        node_id: str,
        storage_roots: tuple[str, ...] = (),
    ) -> HostResourceSnapshot:
        """Collect complete host resource snapshot.

        Args:
            node_id: Node identifier
            storage_roots: Tuple of storage root paths to inspect (default: empty)

        Returns:
            Complete HostResourceSnapshot with fingerprint

        Raises:
            ValueError: If any telemetry is malformed or invalid
            OSError: If telemetry cannot be collected
        """
        collected_at = datetime.now(timezone.utc)

        memory = self.collect_memory()
        cpu = self.collect_cpu()
        storage = self.collect_storage(storage_roots)

        # Compute fingerprint
        payload = {
            "node_id": node_id,
            "collected_at": _format_timestamp(collected_at),
            "memory": {
                "total_bytes": memory.total_bytes,
                "available_bytes": memory.available_bytes,
                "total_swap_bytes": memory.total_swap_bytes,
                "free_swap_bytes": memory.free_swap_bytes,
            },
            "cpu": {
                "logical_count": cpu.logical_count,
                "load_average_1m": cpu.load_average_1m,
                "load_average_5m": cpu.load_average_5m,
                "load_average_15m": cpu.load_average_15m,
            },
            "storage": [
                {
                    "root_path": s.root_path,
                    "total_bytes": s.total_bytes,
                    "free_bytes": s.free_bytes,
                    "available_bytes": s.available_bytes,
                }
                for s in storage
            ],
        }
        fingerprint = _fingerprint(payload)

        return HostResourceSnapshot(
            node_id=node_id,
            collected_at=collected_at,
            memory=memory,
            cpu=cpu,
            storage=storage,
            fingerprint=fingerprint,
        )


class HostCapacityGuard:
    """Evaluates host capacity decisions.

    Pure decision logic with no mutable state.
    Repeated evaluation of identical evidence produces identical decisions.
    """

    def evaluate(
        self,
        snapshot: HostResourceSnapshot,
        requirement: HostResourceRequirement,
        policy: HostCapacityPolicy,
    ) -> HostCapacityDecision:
        """Evaluate host capacity decision.

        Args:
            snapshot: Current host resource snapshot
            requirement: Workload resource requirement
            policy: Host capacity policy

        Returns:
            Immutable HostCapacityDecision with status and reasons
        """
        if not isinstance(snapshot, HostResourceSnapshot):
            raise TypeError("snapshot must be a HostResourceSnapshot")
        if not isinstance(requirement, HostResourceRequirement):
            raise TypeError("requirement must be a HostResourceRequirement")
        if not isinstance(policy, HostCapacityPolicy):
            raise TypeError("policy must be a HostCapacityPolicy")

        reasons = []

        # Memory checks
        memory_after_workload = snapshot.memory.available_bytes - requirement.minimum_available_memory_bytes
        total_memory_reserve = policy.minimum_host_memory_reserve_bytes + requirement.reserve_memory_bytes

        if memory_after_workload < 0:
            reasons.append(HostCapacityReason.INSUFFICIENT_MEMORY)
        elif memory_after_workload < total_memory_reserve:
            reasons.append(HostCapacityReason.MEMORY_RESERVE_VIOLATION)
        elif memory_after_workload < total_memory_reserve * policy.memory_constrained_reserve_multiplier:
            # Constrained: less than policy multiplier × reserve
            reasons.append(HostCapacityReason.CONSTRAINED_MEMORY)

        # Swap pressure check
        swap_used = snapshot.memory.total_swap_bytes - snapshot.memory.free_swap_bytes
        if swap_used > policy.swap_pressure_threshold_bytes:
            reasons.append(HostCapacityReason.SWAP_PRESSURE)

        # Swap headroom check
        if requirement.minimum_swap_headroom_bytes is not None:
            if snapshot.memory.free_swap_bytes < requirement.minimum_swap_headroom_bytes + policy.minimum_swap_reserve_bytes:
                reasons.append(HostCapacityReason.SWAP_PRESSURE)

        # CPU load check
        normalized_load = snapshot.cpu.normalized_load_1m()
        if normalized_load is None:
            if requirement.maximum_normalized_cpu_load is not None:
                reasons.append(HostCapacityReason.TELEMETRY_UNAVAILABLE)
        else:
            if requirement.maximum_normalized_cpu_load is not None:
                if normalized_load > requirement.maximum_normalized_cpu_load:
                    reasons.append(HostCapacityReason.HIGH_CPU_LOAD)

            if normalized_load > policy.maximum_normalized_load_threshold:
                reasons.append(HostCapacityReason.HIGH_CPU_LOAD)
            elif normalized_load > policy.maximum_normalized_load_threshold * policy.cpu_constrained_threshold_ratio:
                # Constrained: load > policy ratio × threshold
                reasons.append(HostCapacityReason.CONSTRAINED_CPU)

        # Storage checks
        if requirement.storage_root_path is not None:
            storage_matches = [
                s for s in snapshot.storage
                if s.root_path == requirement.storage_root_path
            ]

            if not storage_matches:
                reasons.append(HostCapacityReason.TELEMETRY_UNAVAILABLE)
            else:
                storage_snap = storage_matches[0]
                storage_after_workload = storage_snap.available_bytes - requirement.minimum_available_storage_bytes
                total_storage_reserve = policy.minimum_storage_reserve_bytes + requirement.reserve_storage_bytes

                if storage_after_workload < 0:
                    reasons.append(HostCapacityReason.INSUFFICIENT_STORAGE)
                elif storage_after_workload < total_storage_reserve:
                    reasons.append(HostCapacityReason.STORAGE_RESERVE_VIOLATION)
                elif storage_after_workload < total_storage_reserve * policy.storage_constrained_reserve_multiplier:
                    # Constrained: less than policy multiplier × reserve
                    reasons.append(HostCapacityReason.CONSTRAINED_STORAGE)

        # Determine status
        unsafe_reasons = {
            HostCapacityReason.INSUFFICIENT_MEMORY,
            HostCapacityReason.MEMORY_RESERVE_VIOLATION,
            HostCapacityReason.INSUFFICIENT_STORAGE,
            HostCapacityReason.STORAGE_RESERVE_VIOLATION,
            HostCapacityReason.TELEMETRY_UNAVAILABLE,
        }

        constrained_reasons = {
            HostCapacityReason.CONSTRAINED_MEMORY,
            HostCapacityReason.CONSTRAINED_CPU,
            HostCapacityReason.CONSTRAINED_STORAGE,
            HostCapacityReason.HIGH_CPU_LOAD,
            HostCapacityReason.SWAP_PRESSURE,
        }

        reasons_set = set(reasons)

        if reasons_set & unsafe_reasons:
            status = HostCapacityStatus.UNSAFE_TO_START
        elif reasons_set & constrained_reasons:
            status = HostCapacityStatus.CONSTRAINED
        else:
            status = HostCapacityStatus.AVAILABLE

        # Ensure deterministic ordering
        reasons_sorted = tuple(sorted(set(reasons), key=lambda r: r.value))

        # Compute decision fingerprint
        payload = {
            "snapshot_fingerprint": snapshot.fingerprint,
            "requirement_fingerprint": requirement.fingerprint,
            "policy_fingerprint": policy.fingerprint,
            "status": status.value,
            "reasons": [r.value for r in reasons_sorted],
        }
        fingerprint = _fingerprint(payload)

        return HostCapacityDecision(
            snapshot_fingerprint=snapshot.fingerprint,
            requirement_fingerprint=requirement.fingerprint,
            policy_fingerprint=policy.fingerprint,
            status=status,
            reasons=reasons_sorted,
            fingerprint=fingerprint,
        )


def create_requirement(
    minimum_available_memory_bytes: int,
    *,
    minimum_available_storage_bytes: int | None = None,
    storage_root_path: str | None = None,
    maximum_normalized_cpu_load: float | None = None,
    minimum_swap_headroom_bytes: int | None = None,
    reserve_memory_bytes: int = 0,
    reserve_storage_bytes: int = 0,
) -> HostResourceRequirement:
    """Create a HostResourceRequirement with computed fingerprint.

    Helper to avoid manual fingerprint computation.
    """
    payload = {
        "minimum_available_memory_bytes": minimum_available_memory_bytes,
        "minimum_available_storage_bytes": minimum_available_storage_bytes,
        "storage_root_path": storage_root_path,
        "maximum_normalized_cpu_load": maximum_normalized_cpu_load,
        "minimum_swap_headroom_bytes": minimum_swap_headroom_bytes,
        "reserve_memory_bytes": reserve_memory_bytes,
        "reserve_storage_bytes": reserve_storage_bytes,
    }
    fingerprint = _fingerprint(payload)

    return HostResourceRequirement(
        minimum_available_memory_bytes=minimum_available_memory_bytes,
        minimum_available_storage_bytes=minimum_available_storage_bytes,
        storage_root_path=storage_root_path,
        maximum_normalized_cpu_load=maximum_normalized_cpu_load,
        minimum_swap_headroom_bytes=minimum_swap_headroom_bytes,
        reserve_memory_bytes=reserve_memory_bytes,
        reserve_storage_bytes=reserve_storage_bytes,
        fingerprint=fingerprint,
    )


def create_policy(
    *,
    minimum_host_memory_reserve_bytes: int = 1024 * 1024 * 1024,  # 1 GB default
    minimum_swap_reserve_bytes: int = 512 * 1024 * 1024,  # 512 MB default
    maximum_normalized_load_threshold: float = 2.0,  # 2.0 default
    minimum_storage_reserve_bytes: int = 10 * 1024 * 1024 * 1024,  # 10 GB default
    swap_pressure_threshold_bytes: int = 1024 * 1024 * 1024,  # 1 GB swap used default
    memory_constrained_reserve_multiplier: float = 1.5,  # 1.5x reserve default
    cpu_constrained_threshold_ratio: float = 0.8,  # 80% of threshold default
    storage_constrained_reserve_multiplier: float = 1.5,  # 1.5x reserve default
) -> HostCapacityPolicy:
    """Create a HostCapacityPolicy with computed fingerprint.

    Helper to avoid manual fingerprint computation.
    Conservative defaults provided.
    """
    payload = {
        "minimum_host_memory_reserve_bytes": minimum_host_memory_reserve_bytes,
        "minimum_swap_reserve_bytes": minimum_swap_reserve_bytes,
        "maximum_normalized_load_threshold": maximum_normalized_load_threshold,
        "minimum_storage_reserve_bytes": minimum_storage_reserve_bytes,
        "swap_pressure_threshold_bytes": swap_pressure_threshold_bytes,
        "memory_constrained_reserve_multiplier": memory_constrained_reserve_multiplier,
        "cpu_constrained_threshold_ratio": cpu_constrained_threshold_ratio,
        "storage_constrained_reserve_multiplier": storage_constrained_reserve_multiplier,
    }
    fingerprint = _fingerprint(payload)

    return HostCapacityPolicy(
        minimum_host_memory_reserve_bytes=minimum_host_memory_reserve_bytes,
        minimum_swap_reserve_bytes=minimum_swap_reserve_bytes,
        maximum_normalized_load_threshold=maximum_normalized_load_threshold,
        minimum_storage_reserve_bytes=minimum_storage_reserve_bytes,
        swap_pressure_threshold_bytes=swap_pressure_threshold_bytes,
        memory_constrained_reserve_multiplier=memory_constrained_reserve_multiplier,
        cpu_constrained_threshold_ratio=cpu_constrained_threshold_ratio,
        storage_constrained_reserve_multiplier=storage_constrained_reserve_multiplier,
        fingerprint=fingerprint,
    )
