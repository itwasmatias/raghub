from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class SQLiteBackupService:
    def backup(
        self, source: str | Path, destination: str | Path
    ) -> dict[str, object]:
        source_path = Path(source)
        destination_path = Path(destination)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source_path) as source_connection:
            with sqlite3.connect(destination_path) as backup_connection:
                source_connection.backup(backup_connection)
        return {
            "source": str(source_path),
            "destination": str(destination_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bytes": destination_path.stat().st_size,
            "sha256": self._checksum(destination_path),
        }

    def restore(
        self,
        backup: str | Path,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        backup_path = Path(backup)
        destination_path = Path(destination)
        if not backup_path.is_file():
            raise FileNotFoundError(backup_path)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        checksum = self._checksum(backup_path)
        if expected_sha256 is not None and checksum != expected_sha256:
            raise ValueError("Backup checksum does not match expected checksum.")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup_path) as backup_connection:
            with sqlite3.connect(destination_path) as destination_connection:
                backup_connection.backup(destination_connection)
        return {
            "backup": str(backup_path),
            "destination": str(destination_path),
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "sha256": checksum,
        }

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

