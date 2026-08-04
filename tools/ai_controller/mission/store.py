"""
Durable mission state storage.

Directory layout::

    missions_root/
        pending/{mission_id}.json
        running/{mission_id}.json
        paused/{mission_id}.json
        succeeded/{mission_id}.json
        failed/{mission_id}.json
        cancelled/{mission_id}.json
        events/{mission_id}.jsonl
        reports/{mission_id}.json
        .store.lock

Each per-mission JSON file stores both the static definition and the live
state so a single atomic replace is sufficient for any update::

    {
        "definition": { … MissionDefinition … },
        "state":      { … MissionState … }
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from tools.ai_controller._locking import FileLock
from tools.ai_controller.reports import report_lock_path
from .models import MissionDefinition, MissionState, MissionStatus

logger = logging.getLogger(__name__)

# All status directory names, in a stable order used for iteration.
_STATUS_DIRS: list[str] = [
    "pending",
    "running",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
    "budget_exhausted",
]


class MissionStateConflictError(ValueError):
    """Raised when one mission has more than one state envelope."""

    def __init__(self, mission_id: str, candidates: list[tuple[MissionStatus, Path]]):
        self.mission_id = mission_id
        self.candidates = candidates
        locations = ",".join(path.as_posix() for _, path in candidates)
        super().__init__(
            f"mission has multiple state envelopes: {mission_id}: {locations}"
        )


def _atomic_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* via a temp-file rename for atomicity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class MissionStore:
    """Filesystem-backed store for mission definitions and runtime state.

    All mutations that move or create files acquire ``self._lock`` so that
    concurrent schedulers never corrupt the directory structure.
    """

    def __init__(self, missions_root: Path, *, create: bool = True) -> None:
        self._root = Path(missions_root)
        self._lock = FileLock(self._root / ".store.lock")
        if create:
            self.ensure_dirs()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def ensure_dirs(self) -> None:
        """Create all required subdirectories if they do not already exist."""
        for name in _STATUS_DIRS + ["events", "reports"]:
            (self._root / name).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dir(self, status: MissionStatus | str) -> Path:
        name = status.value if isinstance(status, MissionStatus) else str(status)
        return self._root / name

    def _path(self, status: MissionStatus | str, mission_id: str) -> Path:
        return self._dir(status) / f"{mission_id}.json"

    def _load_envelope(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, definition: MissionDefinition, initial_state: MissionState) -> None:
        """Persist a brand-new mission.

        Raises
        ------
        FileExistsError
            If the mission_id already appears in any status directory.
        """
        mission_id = definition.mission_id
        with self._lock:
            existing = self.find_mission(mission_id)
            if existing is not None:
                raise FileExistsError(
                    f"mission already exists in '{existing[0].value}': {mission_id}"
                )
            dest = self._path(initial_state.status, mission_id)
            envelope = {
                "definition": definition.to_dict(),
                "state": initial_state.to_dict(),
            }
            _atomic_json(dest, envelope)
        logger.info("mission created mission_id=%s status=%s", mission_id, initial_state.status.value)

    def find_mission(self, mission_id: str) -> tuple[MissionStatus, Path] | None:
        """Locate which status directory holds *mission_id*.

        Returns ``(status, path)`` or ``None`` if not found.

        .. note::
            The caller is responsible for holding ``self._lock`` when this
            result is used for mutation decisions; reading without the lock is
            safe for informational purposes.
        """
        candidates = self.find_mission_candidates(mission_id)
        if len(candidates) > 1:
            raise MissionStateConflictError(mission_id, candidates)
        return candidates[0] if candidates else None

    def find_mission_candidates(
        self,
        mission_id: str,
    ) -> list[tuple[MissionStatus, Path]]:
        """Return every status envelope candidate for *mission_id*."""
        candidates: list[tuple[MissionStatus, Path]] = []
        for name in _STATUS_DIRS:
            path = self._root / name / f"{mission_id}.json"
            if path.exists():
                candidates.append((MissionStatus(name), path))
        return candidates

    def load_state(self, mission_id: str) -> MissionState:
        """Load the current ``MissionState`` for *mission_id*.

        Raises
        ------
        FileNotFoundError
            If the mission does not exist in any status directory.
        """
        found = self.find_mission(mission_id)
        if found is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        _, path = found
        envelope = self._load_envelope(path)
        return MissionState.from_dict(envelope["state"])

    def load_definition(self, mission_id: str) -> MissionDefinition:
        """Load the ``MissionDefinition`` stored alongside the state.

        Raises
        ------
        FileNotFoundError
            If the mission does not exist in any status directory.
        """
        found = self.find_mission(mission_id)
        if found is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        _, path = found
        envelope = self._load_envelope(path)
        return MissionDefinition.from_dict(envelope["definition"])

    def update_state(self, mission_id: str, new_state: MissionState) -> None:
        """Overwrite the state inside the current status directory.

        The mission must already exist; its status directory does **not**
        change — use :meth:`transition` to move between directories.

        Raises
        ------
        FileNotFoundError
            If the mission does not exist.
        """
        with self._lock:
            found = self.find_mission(mission_id)
            if found is None:
                raise FileNotFoundError(f"mission not found: {mission_id}")
            _, path = found
            envelope = self._load_envelope(path)
            envelope["state"] = new_state.to_dict()
            _atomic_json(path, envelope)
        logger.debug("state updated mission_id=%s", mission_id)

    def transition(
        self,
        mission_id: str,
        new_status: MissionStatus,
        new_state: MissionState,
    ) -> None:
        """Move the mission file to the *new_status* directory atomically.

        Steps (all under lock):

        1. Locate the current file.
        2. Write the updated envelope (with *new_state*) to a temp path
           inside the **destination** directory.
        3. ``os.replace()`` the temp file to its final destination.
        4. Delete the old file.

        Raises
        ------
        FileNotFoundError
            If the mission does not exist in any directory.
        """
        with self._lock:
            found = self.find_mission(mission_id)
            if found is None:
                raise FileNotFoundError(f"mission not found: {mission_id}")
            old_status, old_path = found

            new_dest = self._path(new_status, mission_id)
            old_envelope = self._load_envelope(old_path)
            new_envelope = {
                "definition": old_envelope["definition"],
                "state": new_state.to_dict(),
            }

            # Write to destination dir first, then remove old file.
            _atomic_json(new_dest, new_envelope)
            if old_path != new_dest:
                try:
                    old_path.unlink()
                except FileNotFoundError:
                    pass  # already gone — race with another process; acceptable

        logger.info(
            "mission transitioned mission_id=%s %s -> %s",
            mission_id,
            old_status.value,
            new_status.value,
        )

    def list_missions(self, status: MissionStatus | None = None) -> list[str]:
        """Return sorted mission IDs, optionally filtered to a single *status*."""
        dirs = (
            [self._dir(status)]
            if status is not None
            else [self._root / name for name in _STATUS_DIRS]
        )
        ids: list[str] = []
        for directory in dirs:
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                if not path.name.startswith("."):
                    ids.append(path.stem)
        return sorted(set(ids))

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def write_report(self, mission_id: str, report: dict) -> Path:
        """Write *report* to ``reports/{mission_id}.json`` atomically."""
        path = self._root / "reports" / f"{mission_id}.json"
        with FileLock(report_lock_path(path.parent)):
            _atomic_json(path, report)
        logger.info("mission report written mission_id=%s path=%s", mission_id, path)
        return path

    def load_report(self, mission_id: str) -> dict | None:
        """Load the report for *mission_id*, or ``None`` if it does not exist."""
        path = self._root / "reports" / f"{mission_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load report for %s: %s", mission_id, exc)
            return None
