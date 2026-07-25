# SIP v1.0.0 Personal Edition

SIP is a local sports intelligence and discovery application. NBA is the
flagship league and Version 1 supports **pregame, full-game NBA moneylines**.
WNBA and MLB remain optional league plugins.
It compares real sportsbook observations with an exact-event calibrated
forecast and returns either:

- a **qualified analytical choice**, or
- a structured **no-bet result** explaining every failed evidence gate.

SIP never places wagers, weakens thresholds to create picks, or substitutes
fixtures when live data is missing.

## Intelligence lifecycle

```text
Observe -> Retrieve -> Analyze -> Forecast -> Explain -> Monitor -> Learn
```

The application preserves the existing RAGHub lifecycle, player-intelligence,
claim-graph, replay, and research components. The Personal Edition adds one
production spine:

```text
The Odds API
  -> NBA event retrieval (optional WNBA / MLB plugins)
  -> canonical event and team normalization
  -> complete per-book home/away moneyline pairing
  -> chronological baseline forecast
  -> Platt probability calibration
  -> centralized evidence and value gates
  -> persisted qualification result and lifecycle history
```

## Situation Room

The primary Situation Room is a decision workspace ordered as:

```text
Intelligence Brief
-> Active Forecasts
-> Watchlist Changes
-> Active Investigations
-> Upcoming Events
-> Autonomous Research Trace
-> System Health
```

The full entity and relationship graph is available separately at
`/situation-room/evidence`. The main page does not display raw graph nodes or
edges.

The first stored forecasting workflow uses authenticated FRED CPIAUCSL
observations. It asks whether year-over-year CPI will be at least 3% by a fixed
resolution deadline. The record includes its evidence, probability history,
resolution criteria, official source, assumptions, and update triggers. It is
explicitly labeled experimental until enough forecasts have resolved to
measure calibration.

Situation Room persistence uses:

```env
RAGHUB_SITUATION_ROOM_DB=data/situation_room.db
RAGHUB_GDELT_LANGUAGES=
```

English is the default GDELT language. Additional comma-separated languages
may be configured. Low-relevance, duplicate, malformed, non-English, sports,
and entertainment records are quarantined instead of becoming alerts.

The SQLite schema initializes automatically and records:

- inspectable evidence
- falsifiable forecasts and probability history
- autonomous-cycle research traces
- quarantined low-relevance feed items

Available inspection endpoints include `/api/forecasts`,
`/api/situation-room/brief`, `/api/investigations`,
`/api/watchlists/changes`, `/api/autonomy/cycles/latest`,
`/api/evidence/{evidence_id}`, and `/api/system/health`.

## Supported scope

- Flagship league: NBA
- Optional league plugins: WNBA and MLB
- Timing: pregame
- Market: full-game moneyline
- Sportsbooks: DraftKings, FanDuel, and BetMGM when returned by the configured provider
- Storage: local SQLite
- Model: interpretable logistic baseline with chronological validation and
  Platt calibration

Player props, live betting, spreads, totals, parlays, automated wagering, and
bankroll automation are outside the required v1 scope.

## Fedora setup

Requirements:

- Fedora Linux with Python 3.11 or newer
- Network access for live refreshes
- A SportsGameOdds or The Odds API key with NBA sportsbook coverage

```bash
cd /home/matias/raghub
make setup
```

This older Fedora host must use the pure-Python core dependency set. NumPy,
pandas, SciPy, and scikit-learn are isolated to an optional Windows worker.
See [docs/HYBRID_COMPUTE.md](docs/HYBRID_COMPUTE.md) for the verified runtime
diagnostic, private worker setup, security boundary, and offline behavior.

`make setup` creates `.venv`, installs pinned dependencies, copies
`.env.example` to `.env` if needed, and applies SQLite migrations.

Edit `.env`:

```env
SIP_MODE=live
SIP_HOST=127.0.0.1
SIP_PORT=5000
SIP_PERSONAL_DATABASE=data/sip_personal.db
SIP_MODEL_PATH=data/models/moneyline-v1.json
ODDS_FEED_ENABLED=true
ODDS_PROVIDER=sportsgameodds
ODDS_API_KEY=
ODDS_BASE_URL=https://api.sportsgameodds.com/v2
ODDS_REGIONS=us
ODDS_SPORTS=basketball_nba
ODDS_MARKETS=h2h
ODDS_REQUEST_TIMEOUT_SECONDS=15
ODDS_MAX_EVENTS_PER_REQUEST=10
ODDS_MAX_QUOTE_AGE_SECONDS=600
ODDS_REFRESH_MINUTES=10

MIN_MODEL_EDGE=0.03
MIN_DATA_QUALITY=0.75
MIN_COMPLETE_BOOKS=2
FORECAST_MAX_AGE_SECONDS=3600
```

Obtain the credential from SportsGameOdds or The Odds API and place it only in
your local `.env` as `ODDS_API_KEY`. Never commit `.env`; it is ignored.
SportsGameOdds authentication uses the `x-api-key` header. The key is used only
by the backend and is never returned to the browser or system-status APIs.
Subscription coverage is reported independently for each configured league.

### Situation Room data credentials

The Situation Room uses authenticated federal sources for its macroeconomic
regime and legislative monitor:

```env
FRED_API_KEY=replace-with-your-fred-key
API_DATA_GOV_KEY=replace-with-your-api-data-gov-key
```

`FRED_API_KEY` retrieves CPI, unemployment, and the federal-funds rate from
the official FRED observations API. `API_DATA_GOV_KEY` retrieves recent bills
from Congress.gov. `CONGRESS_API_KEY` remains a backward-compatible alias, but
`API_DATA_GOV_KEY` is preferred. Keys stay server-side and are not included in
Situation Room responses or provider-error messages.

After changing `.env`, restart the application and open
[http://127.0.0.1:5000/situation-room](http://127.0.0.1:5000/situation-room).
The Sources panel reports `live`, `degraded`, or `unconfigured` separately for
FRED and Congress.gov, including a retrieval timestamp.

## Database and migrations

```bash
make migrate
```

Migration 1 creates:

- `sip_events`
- `sip_quotes`
- `sip_forecasts`
- `sip_evaluations`
- `sip_feed_state`
- `sip_job_runs`
- `sip_schema_migrations`

Migrations are transactional and safe to rerun. SQLite WAL and busy timeout are
enabled for safe local restarts.

## Refresh live data

```bash
make refresh-data
```

The same operation is available through the dashboard’s **Refresh data and
qualification** button. Provider timeout retries are bounded. A failed refresh
records an unavailable/stale status and never inserts fixture data.

## Optional league-plugin model utilities

The current history command supports only the optional WNBA and MLB plugins.
It does not train an NBA production model. NBA history/model completion is
deferred until a later recovery phase:

```bash
make prepare-history
```

SIP stores those optional normalized source records in `data/training/` and
keeps the leagues separate. Train the optional plugin models:

```bash
make train-model
```

Evaluate operational status:

```bash
make evaluate-model
```

Training sorts games chronologically, trains on the earlier 80% partition,
fits Platt calibration only on the later 20% validation partition, and saves
separate optional WNBA and MLB artifacts with:

- training and validation periods
- feature and model versions
- sample size
- calibration method and status
- Brier score and log loss

Fewer than 50 usable chronological examples, fewer than 10 validation games,
or a validation set without both outcomes returns
`INSUFFICIENT_CALIBRATION_DATA`. A missing model returns
`MODEL_NOT_TRAINED`. Neither state is labeled calibrated. Each odds refresh
loads a matching artifact only when one genuinely exists. With no validated
NBA artifact, NBA events return an explicit no-bet result instead of using a
WNBA/MLB model or replay fixture.

## Start the application

Safe localhost default:

```bash
make run
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

Production-style local command using the configured host and port:

```bash
make run-prod
```

The Flask debugger and reloader are disabled. Stop cleanly with `Ctrl+C`.

Run the refresh scheduler in a second terminal:

```bash
make scheduler
```

The scheduler prevents overlapping runs, retries temporary failures, persists
job status, and shuts down on `SIGINT` or `SIGTERM`.

## Home-network access

Keep `SIP_HOST=127.0.0.1` unless LAN access is needed. For a trusted home LAN:

```env
SIP_HOST=0.0.0.0
RAGHUB_SECRET_KEY=replace-with-a-long-random-value
```

Then use the Fedora server’s LAN address:

```text
http://SERVER_LAN_IP:5000
```

Allow the port only on the trusted firewall zone. This application does not
add authentication by default; do not forward the port from the router or
expose it directly to the public internet.

## Main areas and APIs

- Dashboard: `/`
- Games: `/#games`
- Choices and no-bet results: `/#choices`
- Research: `/#research`
- System status: `/#status`
- Legacy RAGHub intelligence: `/legacy`
- Personal snapshot: `/api/sip/dashboard`
- Normalized games: `/api/sip/games`
- Choices and diagnostics: `/api/sip/choices`
- System status: `/api/sip/status`
- Manual refresh: `POST /api/sip/refresh`
- Health: `/health` and `/api/system/health`

## Qualification rules

The centralized service requires:

1. Configured, successful, fresh feed.
2. Valid future canonical event.
3. Paired home and away full-game moneylines from at least two distinct books.
4. Exact event/market/period/selection forecast.
5. Trained and validated calibrated model.
6. Sufficient feature completeness and forecast freshness.
7. Data quality at or above `MIN_DATA_QUALITY`.
8. Model edge at or above `MIN_MODEL_EDGE`.

American odds are converted to decimal and implied probability. Each book’s
paired probabilities are normalized to remove vig. SIP then calculates market
consensus, model edge, best price, and expected value.

No-bet reason codes include:

```text
ODDS_FEED_NOT_CONFIGURED, ODDS_FEED_UNAVAILABLE, ODDS_DATA_STALE,
EVENT_NORMALIZATION_FAILED, EVENT_ALREADY_STARTED, NO_COMPLETE_BOOKS,
INSUFFICIENT_COMPLETE_BOOKS, MARKET_PAIRING_FAILED, FORECAST_MISSING,
MODEL_NOT_TRAINED, FORECAST_NOT_CALIBRATED, FORECAST_STALE,
FORECAST_MARKET_MISMATCH, INSUFFICIENT_FEATURE_DATA, EDGE_BELOW_THRESHOLD,
DATA_QUALITY_BELOW_THRESHOLD
```

## Tests

Focused Personal Edition tests:

```bash
.venv/bin/pytest -q \
  tests/test_personal_edition.py \
  tests/test_personal_odds_source.py \
  tests/test_personal_model.py \
  tests/test_personal_app.py
```

Complete suite:

```bash
make test
```

Release gate:

```bash
.venv/bin/python scripts/release_gate.py
```

## Logging and maintenance

Operational commands emit structured JSON logs without API keys. For
systemd, send stdout/stderr to journald and configure retention with
`SystemMaxUse`/`MaxRetentionSec`. For file logging, use `logrotate`; do not
store logs inside the Git repository.

Safe restart:

1. Stop the web process and scheduler.
2. Run `make migrate`.
3. Run `make refresh-data`.
4. Start `make run-prod`.
5. Start `make scheduler` separately.

## Troubleshooting

- `ODDS_FEED_NOT_CONFIGURED`: set `ODDS_API_KEY`.
- `ODDS_FEED_UNAVAILABLE`: confirm network access, provider quota, and key.
- `ODDS_DATA_STALE`: run `make refresh-data`.
- `MODEL_NOT_TRAINED`: provide legitimate history and run `make train-model`.
- `INSUFFICIENT_CALIBRATION_DATA`: add more chronological completed games.
- `INSUFFICIENT_COMPLETE_BOOKS`: wait for two distinct books to post both
  teams’ moneylines.
- Database locked: stop duplicate processes and retry; do not delete WAL files
  from a running process.

## Data and responsible use

Odds remain provider-owned observations and may change after retrieval.
Availability varies by location. SIP output is research-oriented analytical
decision support, not financial advice or a guaranteed result. Verify the
current price and applicable law before acting.

See [Personal release notes](docs/RELEASE_NOTES_1.0.0_PERSONAL.md),
[operations](docs/OPERATIONS.md), [known limitations](docs/KNOWN_LIMITATIONS.md),
and the [private-use notice](PRIVATE_USE_NOTICE.md).
