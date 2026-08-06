"""Worker heartbeat timing policy."""

from datetime import timedelta


HEARTBEAT_INTERVAL = timedelta(seconds=30)
WORKER_LEASE_DURATION = timedelta(seconds=90)
