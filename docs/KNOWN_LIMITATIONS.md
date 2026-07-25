# Known Limitations for 1.0.0-rc1

## SIP v1.0.0 Personal Edition

- Required production scope is WNBA and MLB pregame full-game moneyline only.
- A live The Odds API credential is not bundled. Without it, the feed remains
  explicitly unconfigured and live mode never substitutes event fixtures.
- Models are generated locally from retrieved historical results and are not
  committed as universal predictions. Refresh history and retrain before use.
- The baseline uses team season/recent performance, rest, and home status.
  Its displayed validation scores do not by themselves prove betting advantage.
  Missing injury, lineup, or player-availability inputs block qualification
  when marked required.
- WNBA player intelligence reuses the existing basketball analytics. MLB
  player research requires a future statistics-provider adapter.
- The local app has no authentication. LAN mode is for a trusted private
  network and should not be exposed directly to the public internet.
- Player props, live betting, spreads, totals, parlays, wagering execution, and
  bankroll automation are outside v1 scope.

- Production schedule, roster, lineup, and injury adapters still require
  deployment-specific configuration. The live sportsbook adapter requires a
  deployment-owned `ODDS_API_KEY`, quota monitoring, and jurisdiction
  review.
- The deterministic demonstration is a historical replay, not current advice.
- Liquidity is included only when a provider supplies it.
- Qualitative coaching and behavioral context requires an accessible source URL
  and remains labeled qualitative.
- Forecast subgroup metrics display sample-size warnings; the bundled replay is
  not a statistically meaningful calibration sample.
- The Flask in-process rate limiter is suitable for a single-process demo. A
  shared production gateway limiter is required for multi-worker deployment.
- Cross-machine Fedora/Windows access and production provider quotas require
  human deployment validation before the final tag.
