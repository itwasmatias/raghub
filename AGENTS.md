# Collaborative development rules

- Treat all existing modified and untracked files as active collaborative work. Inspect
  `git status`, current contracts, and focused tests before editing; never discard or
  rewrite another assistant's changes to simplify an implementation.
- Integrate with existing services, routes, templates, and payloads instead of creating
  parallel APIs or duplicate UI state. Preserve backward-compatible fields unless a
  coordinated migration and tests cover every consumer.
- For Situation Room work, `/api/situation-room` is the shared snapshot contract and
  `/api/situation-room/predict` is the shared forecast contract. The interactive and
  legacy views must derive from those contracts.
- Forecast path points use `day`, `base_case`, `upside`, `downside`, `base_low`, and
  `base_high`. Scenario probabilities must be finite, remain within `[0, 1]`, and sum
  to approximately `1` at every checkpoint.
- Keep sourced observations separate from derived conclusions. Show confidence,
  horizon, evidence, source health, and invalidation conditions. Never invent fallback
  news, odds, statistics, or evidence.
- Use plain language for user-facing explanations, define specialist terms, and label
  research conclusions as non-guaranteed decision support.
- Add or update focused tests before implementation when changing a shared contract.
  Run the repository virtualenv test target plus adjacent route/UI regressions.
- Before handoff, re-read overlapping files to detect concurrent edits and report any
  verification limitation honestly.
