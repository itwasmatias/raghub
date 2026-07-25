# RAGHub Copilot integration instructions

- Preserve existing Codex and user changes. Check the current implementation and tests
  before modifying shared files; do not replace working contracts with parallel ones.
- Extend `/api/situation-room` and `/api/situation-room/predict` for Situation Room
  features. Keep the interactive and legacy views synchronized with those APIs.
- Preserve the forecast point schema: `day`, `base_case`, `upside`, `downside`,
  `base_low`, and `base_high`. Probabilities must be valid and normalized at every
  checkpoint.
- Keep observed facts distinct from inferred impact. Include confidence, time horizon,
  supporting evidence, source health, and invalidation conditions.
- Do not generate fictional news, odds, source rows, or statistics when a feed is empty
  or unavailable.
- Write young-adult-friendly summaries, define jargon, and explain direct everyday
  implications without presenting forecasts as guarantees or personalized advice.
- Update focused tests for shared contracts and run adjacent Flask and Situation Room
  regressions before handoff.
