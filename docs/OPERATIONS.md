# Operations and Troubleshooting

## SIP v1.0 Personal Edition

Run these commands from `/home/matias/raghub`:

```bash
make migrate
make refresh-data
make evaluate-model
make run-prod
```

Run `make scheduler` as a separate supervised process. It prevents overlapping
runs, bounds retries, and persists execution status in `sip_job_runs`. It never
places wagers or performs financial actions.

`SIP_HOST=127.0.0.1` is the safe default. Use `0.0.0.0` only on a trusted LAN,
restrict port 5000 to that firewall zone, and do not forward it from the
router. Personal Edition has no authentication.

For log management, prefer systemd/journald and configure `SystemMaxUse` and
`MaxRetentionSec`, or apply normal `logrotate` rules to redirected JSON logs.
API keys are not included in structured log fields.

Safe restart:

1. Stop the web and scheduler processes.
2. Run `make migrate`.
3. Run `make refresh-data`.
4. Start `make run-prod`.
5. Start `make scheduler`.

SQLite WAL and busy timeout support normal concurrent reads and safe restarts.
Never delete a WAL file while either process is active.

## Configuration

Copy `.env.example` to `.env`. Production startup requires:

- `RAGHUB_ENV=production`
- a randomly generated `RAGHUB_SECRET_KEY`
- an explicit lifecycle database path
- configured credentials only for enabled providers

Missing production secrets cause safe startup failure. Never commit `.env`.

## Health

`GET /api/system/health` returns component status, latency where known, last
successful refresh, cached-snapshot availability, retry delay, and request ID.
Every HTTP response includes `X-Request-ID`.

An unconfigured odds adapter is reported as unconfigured. This is intentional:
the ranking engine abstains instead of manufacturing markets.

## Backup

Use `SQLiteBackupService` from a maintenance shell:

```python
from sports.release.backup import SQLiteBackupService
manifest = SQLiteBackupService().backup(
    "data/intelligence_lifecycle.db",
    "backups/intelligence_lifecycle.db.backup",
)
print(manifest)
```

Store the returned SHA-256 checksum separately. Test restores into a new path;
the restore service refuses to overwrite an existing database.

## Secret rotation

1. Create a new secret in the deployment secret manager.
2. Stop new autonomous cycles.
3. Replace the provider secret or `RAGHUB_SECRET_KEY`.
4. Restart the service.
5. Verify `/api/system/health`.
6. Revoke the previous secret.
7. Record the rotation in the operations audit.

## Common failures

### Graph unavailable

Use the component diagnostic rather than the browser’s generic HTTP message.
Check cause, last successful refresh, cached availability, retry delay, and
request ID.

### Provider timeout

The release pipeline retries temporary timeouts within its configured limit.
If all attempts fail, it marks the dataset `failed_cached` and retains the
previous valid records.

### Odds not ranked

Inspect normalization exclusions. Common reasons are a missing opposing
outcome, stale quote, suspended quote, unresolved player identity, or fewer
than two complete sportsbook markets.

For the supported live feed, set `ODDS_API_KEY` only in the server environment,
select `ODDS_PROVIDER`, and run Refresh. Confirm `/betting` reports `LIVE MARKET DATA`,
shows a retrieval message and timestamp, and lists at least two complete books
for each assessed market. A `HISTORICAL REPLAY` card is an illustrative,
non-current system demonstration and must never be treated as a live quote.

### Database locked

SQLite repositories use WAL and busy timeouts. Stop duplicate processes, allow
the active transaction to finish, and retry. Do not delete journal files from a
running database.
