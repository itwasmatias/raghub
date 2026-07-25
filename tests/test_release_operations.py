import sqlite3
from pathlib import Path

import pytest

from sports.release.backup import SQLiteBackupService


def test_sqlite_backup_and_restore_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE values_table (value TEXT)")
        connection.execute("INSERT INTO values_table VALUES ('preserved')")
    backup = tmp_path / "backups" / "source.db.backup"
    restored = tmp_path / "restored.db"

    manifest = SQLiteBackupService().backup(source, backup)
    SQLiteBackupService().restore(
        backup,
        restored,
        expected_sha256=manifest["sha256"],
    )

    with sqlite3.connect(restored) as connection:
        value = connection.execute(
            "SELECT value FROM values_table"
        ).fetchone()[0]
    assert value == "preserved"
    assert manifest["bytes"] > 0


def test_restore_refuses_checksum_mismatch_and_existing_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example (id INTEGER)")
    backup = tmp_path / "backup.db"
    SQLiteBackupService().backup(source, backup)
    target = tmp_path / "target.db"
    target.touch()

    with pytest.raises(FileExistsError):
        SQLiteBackupService().restore(backup, target)
    target.unlink()
    with pytest.raises(ValueError, match="checksum"):
        SQLiteBackupService().restore(
            backup, target, expected_sha256="incorrect"
        )
