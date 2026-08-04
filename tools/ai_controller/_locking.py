from __future__ import annotations

import os
from pathlib import Path
import threading


class FileLock:
    _condition = threading.Condition()
    _owners: dict[str, tuple[int, int, object | None]] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self._key = os.fspath(Path(path).absolute())
        self._owner_thread_id: int | None = None
        self._depth = 0

    def __enter__(self) -> "FileLock":
        thread_id = threading.get_ident()
        with self._condition:
            while True:
                owner = self._owners.get(self._key)
                if owner is None:
                    self._owners[self._key] = (thread_id, 1, None)
                    break
                if owner[0] == thread_id:
                    self._owners[self._key] = (
                        thread_id,
                        owner[1] + 1,
                        owner[2],
                    )
                    self.handle = owner[2]
                    self._owner_thread_id = thread_id
                    self._depth += 1
                    return self
                self._condition.wait()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            with self._condition:
                self._owners.pop(self._key, None)
                self._condition.notify_all()
            raise
        with self._condition:
            self._owners[self._key] = (thread_id, 1, self.handle)
        self._owner_thread_id = thread_id
        self._depth = 1
        return self

    def __exit__(self, *_: object) -> None:
        if self._depth == 0:
            return
        thread_id = threading.get_ident()
        with self._condition:
            if self._owner_thread_id != thread_id:
                raise RuntimeError("file lock released by a non-owner")
            owner = self._owners.get(self._key)
            if owner is None or owner[0] != thread_id:
                raise RuntimeError("file lock released by a non-owner")
            self._depth -= 1
            if owner[1] > 1:
                self._owners[self._key] = (
                    thread_id,
                    owner[1] - 1,
                    owner[2],
                )
                if self._depth == 0:
                    self._owner_thread_id = None
                return
            handle = owner[2]
            if handle is None:
                self._depth += 1
                raise RuntimeError(
                    "file lock owner has no operating-system handle"
                )
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._owners.pop(self._key, None)
            self.handle = None
            self._owner_thread_id = None
            self._condition.notify_all()
