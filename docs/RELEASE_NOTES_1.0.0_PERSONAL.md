# SIP v1.0.0 Personal Edition

This release supports WNBA and MLB pregame full-game moneyline research.

The main application provides an honest provider status, canonical upcoming
events, complete-book counts, calibrated forecast status, qualified analytical
choices, and detailed no-bet checklists. Live mode never loads fixtures.

The Odds API credential is not bundled. Until `ODDS_API_KEY` is configured,
the dashboard reports the feed as unconfigured and shows no live events.

The baseline model is intentionally not bundled as calibrated. Run the
documented chronological training command with legitimate historical rows.
When the data is missing or inadequate, the command exits with
`INSUFFICIENT_CALIBRATION_DATA`; the UI reports `MODEL_NOT_TRAINED`.

Out of scope: player props, live betting, parlays, wagering automation, and
bankroll automation.
