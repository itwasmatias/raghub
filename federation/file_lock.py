"""Cross-platform process-safe file locking for durable registries.

POSIX implementation uses fcntl.flock().
Windows implementation uses msvcrt.locking() with exclusive locks.

Both backends provide process-safe mutual exclusion. Process crashes
automatically release operating-system locks.
"""

import platform
import sys

# Lock operation constants (fcntl-compatible)
LOCK_SH = 1  # Shared lock
LOCK_EX = 2  # Exclusive lock
LOCK_UN = 8  # Unlock


class FileLockError(Exception):
    """Raised when file locking operations fail."""
    pass


def _get_backend():
    """Determine the appropriate locking backend based on platform."""
    system = platform.system()

    if system in ("Linux", "Darwin"):
        return "posix"
    elif system == "Windows":
        return "windows"
    else:
        # Conservative default: attempt POSIX, fail if unavailable
        return "posix"


# Selected backend
_BACKEND = _get_backend()


if _BACKEND == "posix":
    # POSIX implementation using fcntl
    try:
        import fcntl as _fcntl

        def flock(fd, operation):
            """Apply or remove an advisory lock on an open file descriptor.

            Args:
                fd: File descriptor (from file.fileno())
                operation: LOCK_SH, LOCK_EX, or LOCK_UN

            Raises:
                FileLockError: If lock acquisition fails
            """
            try:
                _fcntl.flock(fd, operation)
            except (OSError, IOError) as exc:
                raise FileLockError(f"POSIX flock failed: {exc}") from exc

    except ImportError as exc:
        def flock(fd, operation):
            raise FileLockError(
                "POSIX file locking unavailable: fcntl module not found"
            ) from exc


elif _BACKEND == "windows":
    # Windows implementation using msvcrt.locking
    #
    # IMPORTANT: msvcrt.locking() provides mandatory locking on Windows.
    # We use exclusive locks (LK_LOCK/LK_UNLCK) for both readers and writers
    # to ensure process-safe mutual exclusion, since Windows file locking
    # semantics differ from POSIX advisory locks.
    #
    # This is conservative but safe: all access serializes exclusively.

    try:
        import msvcrt
        import os as _os

        # Windows locking constants
        _LK_LOCK = 1    # Lock (blocks until acquired)
        _LK_UNLCK = 0   # Unlock

        # Maximum file size to lock (use a large bounded value)
        _LOCK_LENGTH = 0x7FFFFFFF  # ~2GB, safe for 32-bit systems

        def flock(fd, operation):
            """Apply or remove a process-safe lock on Windows.

            Args:
                fd: File descriptor (from file.fileno())
                operation: LOCK_SH, LOCK_EX, or LOCK_UN

            Note:
                On Windows, both LOCK_SH and LOCK_EX acquire exclusive locks
                due to platform locking semantics. All access serializes.

            Raises:
                FileLockError: If lock acquisition fails
            """
            try:
                if operation == LOCK_UN:
                    # Unlock the file
                    _os.lseek(fd, 0, _os.SEEK_SET)
                    msvcrt.locking(fd, _LK_UNLCK, _LOCK_LENGTH)
                elif operation in (LOCK_SH, LOCK_EX):
                    # Acquire exclusive lock (blocking)
                    _os.lseek(fd, 0, _os.SEEK_SET)
                    msvcrt.locking(fd, _LK_LOCK, _LOCK_LENGTH)
                else:
                    raise FileLockError(f"Invalid lock operation: {operation}")
            except (OSError, IOError) as exc:
                raise FileLockError(f"Windows locking failed: {exc}") from exc

    except ImportError as exc:
        def flock(fd, operation):
            raise FileLockError(
                "Windows file locking unavailable: msvcrt module not found"
            ) from exc


# Provide a testable Windows backend facade for Fedora testing
class _WindowsLockingFake:
    """Fake Windows locking backend for testing on non-Windows platforms.

    This allows Windows locking code paths to be tested on Fedora without
    importing actual Windows-only modules. Uses POSIX fcntl as the underlying
    mechanism.
    """

    def __init__(self):
        if _BACKEND == "posix":
            import fcntl as _fcntl
            self._fcntl = _fcntl
        else:
            self._fcntl = None

    def flock(self, fd, operation):
        """Emulate Windows exclusive locking using POSIX fcntl."""
        if self._fcntl is None:
            raise FileLockError(
                "Windows locking fake requires POSIX fcntl backend"
            )

        try:
            # Windows uses exclusive locks for all operations
            if operation == LOCK_UN:
                self._fcntl.flock(fd, self._fcntl.LOCK_UN)
            elif operation in (LOCK_SH, LOCK_EX):
                # Both shared and exclusive become exclusive on Windows
                self._fcntl.flock(fd, self._fcntl.LOCK_EX)
            else:
                raise FileLockError(f"Invalid lock operation: {operation}")
        except (OSError, IOError) as exc:
            raise FileLockError(f"Fake Windows locking failed: {exc}") from exc


# Export the active backend and make the fake available for testing
fcntl = sys.modules[__name__]  # Self-reference for drop-in fcntl replacement
windows_locking_fake = _WindowsLockingFake()
