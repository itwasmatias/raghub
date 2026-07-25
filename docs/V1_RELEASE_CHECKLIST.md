# RAGHub/SIP v1.0 Release Checklist

Target tag: `v1.0.0`

Current candidate: `1.0.0-rc1`

## Automated release gates

- [ ] Full pytest suite completes without failures or hangs.
- [ ] NBA release-candidate tests pass.
- [ ] Deterministic replay reaches the Learn stage.
- [ ] Market normalization failure cases pass.
- [ ] Provider timeout and cached-snapshot tests pass.
- [ ] Database backup and restore checksum test passes.
- [ ] Python compilation and `git diff --check` pass.
- [ ] No likely secrets are committed.

Run:

```bash
.venv/bin/python scripts/release_gate.py
```

## Production pipeline

- [ ] NBA teams and players provider configured.
- [ ] Current and previous seasons loaded.
- [ ] League totals and recent game logs loaded.
- [ ] Schedules, rosters, lineups, and injuries configured.
- [ ] Player minutes, usage, shooting, bench, and substitution inputs verified.
- [ ] Licensed sportsbook feed configured.
- [ ] Final results provider configured.
- [ ] Source health shows record counts, retrieval times, freshness, and errors.
- [ ] One-provider failure preserves the last valid snapshot.

## Deployment checks requiring human sign-off

- [ ] Fresh Fedora installation succeeds from documented commands.
- [ ] Production environment validation succeeds.
- [ ] Windows computer can access the deployed application.
- [ ] Read-only demo mode blocks all mutation requests.
- [ ] Stop and emergency-disable controls are verified.
- [ ] Backup and restore are performed against a copy of production data.
- [ ] Provider quotas, terms, and rate limits are reviewed.
- [ ] No external or costly action can execute without explicit permission.

## Definition-of-done demonstration

- [ ] Open `/demo/nba` without editing code.
- [ ] Show the labeled injury/lineup observation.
- [ ] Show affected rotation and player features.
- [ ] Show current, previous, recent, and weighted statistics.
- [ ] Show at least two normalized complete sportsbook markets.
- [ ] Show hypothesis, forecast, ranking, risks, and evidence trace.
- [ ] Show final outcome, Brier score, calibration context, and stored lesson.
- [ ] Show actionable degraded health when a component is unavailable.

Only after every item is signed off:

```bash
git tag -a v1.0.0 -m "RAGHub v1.0.0 - SIP NBA Flagship Release"
```

