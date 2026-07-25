# Fedora server and Windows compute worker

RAGHub keeps Fedora as the system of record. The web application, SQLite
databases, connectors, scheduler, evidence, qualification rules, and the
transparent pure-Python moneyline model run there. Native analytical packages
run only in the optional Windows worker.

## Why the split exists

On the Fedora AMD A8-3500M host, NumPy 2.5.1 terminates Python with `SIGILL`.
The CPU provides SSE/SSE2/SSE4a but not AVX. The failing extension is
`numpy._core._multiarray_umath`, which loads NumPy's bundled OpenBLAS library.
Because `SIGILL` cannot be caught by Python, Fedora never imports or probes
these packages in the server process.

Run the safe subprocess diagnostic:

```bash
make diagnose-runtime
```

Fedora uses `requirements-fedora-core.txt`. Windows uses
`requirements-windows-worker.txt`.

## Fedora setup

Generate a worker token without printing it into application logs:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Place the result in the Fedora `.env`:

```env
RAGHUB_COMPUTE_DATABASE=data/compute_jobs.db
RAGHUB_WORKER_TOKEN=replace-with-a-dedicated-random-token
```

Keep the API on a private LAN or Tailscale address. Do not forward the port
from the public internet.

```bash
make setup
make diagnose-runtime
make run-prod
```

Queue a deterministic cross-computer smoke job:

```bash
make queue-worker-smoke
make compute-health
```

The API health view is `GET /api/system/compute-health`.

## Windows 11 setup

Install a current 64-bit Python, then in PowerShell:

```powershell
git clone <your-private-repository-url> raghub
cd raghub
py -m venv .venv-worker
.\.venv-worker\Scripts\python.exe -m pip install --upgrade pip
.\.venv-worker\Scripts\python.exe -m pip install -r requirements-windows-worker.txt
Copy-Item .env.worker.example .env
```

Edit `.env` on Windows:

```env
RAGHUB_SERVER_URL=http://FEDORA_PRIVATE_ADDRESS:5000
RAGHUB_WORKER_TOKEN=the-same-dedicated-worker-token
RAGHUB_WORKER_NAME=windows-compute-1
RAGHUB_WORKER_POLL_SECONDS=10
RAGHUB_WORKER_CAPABILITIES=numpy,pandas,scipy,sklearn
```

Use a Fedora LAN address or Tailscale hostname, never a public address. Allow
outbound access from Python in Windows Firewall; an inbound worker port is not
needed because the worker polls Fedora.

Verify one job and then keep polling:

```powershell
.\.venv-worker\Scripts\python.exe -m raghub_worker --once
.\.venv-worker\Scripts\python.exe -m raghub_worker
```

Stop safely with `Ctrl+C`. For automatic startup, create a Windows Task
Scheduler task at login with the second command, set it to restart after
failure, and disable sleep while plugged in. Logs can be redirected by the
task to a user-owned log directory.

## Security and failure behavior

- Every worker request uses a dedicated bearer token compared in constant time.
- Job types are server-created and worker execution is restricted to an
  explicit handler allow-list. Payloads cannot contain Python or shell code.
- Results use canonical JSON and SHA-256 verification.
- No pickle is accepted.
- Duplicate snapshot/model jobs are suppressed.
- Claims use SQLite `BEGIN IMMEDIATE`, leases expire, and retry counts are
  bounded.
- Worker errors are truncated and newlines removed.
- When Windows is offline, Fedora keeps serving all core functions. Heavy jobs
  remain pending and health reports the worker offline. Existing forecasts
  remain inspectable but the normal SIP freshness gate decides whether they
  may qualify.

The initial allow-listed cross-computer operation is
`RUN_HEAVY_FEATURE_PIPELINE`. Additional training, calibration, and forecast
handlers must validate their structured input and output schemas before being
enabled.
