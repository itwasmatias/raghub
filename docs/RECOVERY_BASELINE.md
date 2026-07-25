# RAGHub Recovery Baseline

Verified on Fedora 44, Linux 7.1.4-202.fc44.x86_64, Python 3.14, on
2026-07-25.

## Canonical production application

- Server command: `.venv/bin/python -m scripts.sip run` (`make run-prod` is
  the wrapper).
- Application factory: `app.create_app`.
- Web framework: one Flask application.
- SIP production dashboard: `/`.
- Situation Room production dashboard: `/situation-room`.
- Secondary Evidence Explorer: `/situation-room/evidence`.
- Shared Situation Room contracts: `/api/situation-room` and
  `/api/situation-room/predict`.

`web_ui.py` is a legacy-compatible rendering/helper module used by the Flask
application. Its standalone `http.server` entry point is not the production
server.

## Legacy, replay, and archived components

- `/legacy`, `/situation-room/legacy`, and the standalone `web_ui.py` server
  are legacy compatibility surfaces.
- `/demo/nba` and `/api/demo/nba-opportunity` are explicit deterministic replay
  surfaces backed by `NBAReplayDemo`.
- `sip_site/` is a legacy Next.js experiment, not a production application.
  Its nested `.git` directory and generated `dist/` were moved out of the
  active tree.
- The FastAPI backup was moved to
  `archive/legacy_fastapi/app_fastapi_backup.py` and is outside active imports.
- The top-level cloned `pgvector/` repository and `raghub_code.zip` were moved
  out of the active tree. `connectors/pgvector/` remains the real connector.
- Relocated artifacts are recoverable for this run from
  `/tmp/raghub-recovery-phase0-archive-20260725`.

## Production and replay boundary

`ProductionBasketballRuntime` is implemented in
`sports/application/production_basketball_runtime.py` and is constructed by
`build_production_runtime()`. It does not inherit from or instantiate
`BasketballDemoRuntime`, and it does not import or expose `NBAReplayDemo`.
Replay creation remains explicit in the Flask `/demo/nba` routes.

The live betting board rejects records classified as `replay`, `model-only`,
`derived`, or `simulated`. Disabled, failed, or empty live feeds return an
unconfigured/unavailable state and do not insert fixtures.

## SIP scope

NBA is the flagship SIP league. The default is:

```env
ODDS_SPORTS=basketball_nba
```

NBA, WNBA, and MLB are accepted configuration values without hard-coding away
the reusable league abstraction. WNBA and MLB remain optional plugins. The
first production market remains pregame, full-game moneyline with at least two
complete distinct sportsbooks and an exact-event calibrated forecast; missing
requirements produce a no-bet result.

The existing history/training utility currently supports only optional WNBA
and MLB data. No validated NBA moneyline model artifact is present, so complete
NBA model/history production support is deferred.

## Fedora dependencies and SIGILL

The clean Fedora-core install succeeded from
`requirements-fedora-core.txt`. It contains the Flask/runtime, source,
connector, evidence, SQLite, OpenAI, and PostgreSQL connector dependencies.
`psycopg` plus its `psycopg-binary` implementation are retained;
`psycopg2-binary` was removed as an unused duplicate.

NumPy, pandas, SciPy, scikit-learn, and joblib are confined to
`requirements-windows-worker.txt`.

The exact installed crash source was `numpy==2.5.1`:

```bash
.venv/bin/python -c "import numpy"
# process exits 132; signal SIGILL (4)
```

`pandas==3.0.5` also exits 132 because its import loads NumPy. It is a
downstream symptom, not a second identified root cause. Isolated import and
minimal-operation probes passed for the installed `pydantic_core`, `tiktoken`,
`psycopg`, `psycopg_binary`, `psycopg2`, `regex`, `jiter`,
`charset_normalizer`, and `markupsafe`. `cryptography`, `lxml`, SciPy, and
scikit-learn were not installed in the original environment. The clean
Fedora-core environment does not install NumPy, pandas, SciPy, scikit-learn, or
psycopg2.

The diagnostic command is:

```bash
.venv/bin/python -m sports.compute.runtime_diagnostics --json
```

It runs every probe in an isolated subprocess and reports version, import and
operation results, exit code, signal, CPU flags, purpose, and node
classification.

## Verification baseline

- Clean Fedora-core installation: passed.
- Runtime diagnostics in the clean environment: all installed Fedora-core
  native modules passed; intentionally absent optional/worker modules report
  blocked/not installed.
- `python -c "import web_ui"`: passed in both repository and clean
  Fedora-core environments.
- `python -c "import app"`: passed in both environments.
- Database migration: passed; no pending Personal Edition migrations.
- Pytest collection: 283 tests collected successfully.
- Full suite: 283 passed in 10.00 seconds.
- Flask startup from the clean environment: passed without SIGILL.
- SIP `/`: HTTP 200 and rendered the NBA dashboard.
- Situation Room `/situation-room`: HTTP 200.
- Second SIP request/manual page refresh: HTTP 200; server remained alive.
- Clean shutdown: passed on `SIGINT`.

## Current data and operating status

Implemented live source adapters include The Odds API and SportsGameOdds for
moneylines; NBA Stats HTTP, NBA CDN, and ESPN for basketball data; and
Situation Room adapters for FRED, Treasury Fiscal Data, USAspending, SEC,
World Bank, Congress.gov, GDELT, and BEA. Availability still depends on
credentials, provider coverage, network access, quotas, and source health.
RAG retrieval retains Wikipedia and `connectors/pgvector`.

The checked-in Situation Room database currently contains one genuine stored,
open, experimental forecast:
`fred-cpi-above-3-next-quarter`, supported by stored FRED evidence, with six
probability-history records. It is explicitly uncalibrated because there are
not enough resolved outcomes. Simulated NBA replay scenarios are not counted
as forecasts.

Five Situation Room autonomy-cycle records are persisted. Autonomy enablement
is process-local and cycles run only when explicitly invoked; durable
always-on scheduling/monitoring is not yet verified. The Windows worker retains
authenticated registration, heartbeat/health, leases, and the compute-job
repository, but its only handler is the experimental
`RUN_HEAVY_FEATURE_PIPELINE` summary-statistics scaffold. It is not distributed
training or forecasting and does not affect Fedora startup while offline.

## Deferred product gaps

- Build and validate the complete NBA history, feature, calibrated moneyline
  model, and exact-event production forecast pipeline.
- Verify two complete live NBA sportsbooks with real credentials and provider
  subscription coverage.
- Add an operational persistent Situation Room scheduler and restart recovery.
- Accumulate resolved forecasts before claiming calibration.
- Decide whether to remove legacy Flask compatibility pages and `sip_site`
  after a later migration window.
- Define and validate a real model-related Windows-worker job in a later phase.

No Phase 1 feature work is included in this baseline.
