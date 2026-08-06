"""Import-safe facade for Fedora-only durable registry file locking."""

try:
    import fcntl as fcntl
except ImportError:
    class _UnavailableFileLock:
        LOCK_EX = 0
        LOCK_SH = 0
        LOCK_UN = 0

        @staticmethod
        def flock(*args):
            raise RuntimeError("durable federation registries require POSIX file locks")

    fcntl = _UnavailableFileLock()
