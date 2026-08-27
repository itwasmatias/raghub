# Mission Control Demonstrator v0.2

Mission Control v0.2 is a presentation layer over the verified Integrated
Demonstrator v0.1 evidence bundle. It does not change effect, authority,
reconciliation, or mission semantics. It exists so that a first-time viewer can
understand — in about thirty seconds — what MissionaryX actually did during a
run.

## One command

From the repository checkout:

```bash
python -m tools.integrated_demonstrator.run_demo
```

Every run produces an isolated bundle under
`artifacts/integrated-demonstrator/<run-id>/`, including:

- `mission-control.html` — the human-facing Mission Control v0.2 page
- `mission-control.json` — the projection the HTML is rendered from
- `evidence-report.json` — the authoritative evidence report
- `manifest.json` — SHA-256 hashes over the bundle
- `effects.sqlite3`, `mission.sqlite3`, `service.sqlite3` — durable stores
- `run-summary.txt` — plain-text run summary

Open `mission-control.html` in any modern browser.

## What the viewer should notice

The page is organized so the story is visible before any scroll:

1. **Human mission title.** "Deploy test service v2 and verify the result" is
   the headline. The internal mission identifier
   (`mission-integrated-demonstrator-ambiguous-v1`) is present as secondary
   technical metadata, not as the headline.
2. **Mission status.** `MISSION COMPLETED`, with the external result
   (`v2 ACTIVE`) and evidence status (`VERIFIED`) shown as facts.
3. **Bounded operation counts.** One authorized external operation, one
   injected connection failure, zero duplicate operations, zero unauthorized
   operations — every number is derived from the persisted evidence report and
   the durable service state.
4. **Mission timeline.** The centerpiece of the page. Each event is styled
   by severity so the uncertainty → blocked retry → reconciliation →
   confirmed effect story is visible at a glance:
   - `step-warn` on `Response lost; effect is INDETERMINATE`.
   - `step-blocked` on `Automatic retry BLOCKED`.
   - `step-reconcile` on `Reconciliation started` and
     `External service state observed`.
   - `step-ok` on `Effect resolved as SOMETHING_LANDED`,
     `Independent verifier: PASS`, and `Mission completed`.
5. **Authority.** Allowed and denied scopes are presented side-by-side. Both
   sets are drawn directly from `report["authority"]`.
6. **Key metrics.** A compact reference table of the numbers that determined
   the mission outcome.
7. **Evidence.** A verification table with a `Verified` or `PASS` badge and a
   short explanation for each source of truth.
8. **Technical details** (collapsed). Mission ID, effect intent, effect
   dispatch, gateway claim, authority reservation, reconciliation obligation,
   starting repository commit, evidence schema version, the evidence artifact
   list, and the raw machine-readable evidence report.

## Why the uncertain outcome is the point

`INDETERMINATE` is the correct posture after a lost confirmation. The dispatch
was submitted and handed off to the test service, then the response was
deliberately dropped. MissionaryX does not know, at that moment, whether the
external operation landed.

Blind automatic retry is refused because retrying could have applied the same
external change twice. MissionaryX preserves the ambiguity as authoritative
evidence and moves to reconciliation.

Reconciliation reads external truth (`GET /state`) and observes
`active_version == 2` — proof that the original dispatch landed. The effect is
then resolved as `SOMETHING_LANDED`. A second, independent read repeats the
observation to guard against reconciliation-time inconsistency.

The mission completes only after this evidence chain is durable.

## Evidence binding

Every number and status shown on the page is derived from persisted evidence
at render time:

| Display area          | Source                                              |
| --------------------- | --------------------------------------------------- |
| Human mission title   | `evidence-report.json["human_mission_title"]`       |
| Internal mission ID   | Mission runtime store (`MissionRuntimeStore`)       |
| Mission status        | `PavilionNativeInterface.mission_view().lifecycle`  |
| Authorized operations | `report["deployment_attempt_count"]`                |
| Duplicate operations  | `report["duplicate_deployment_count"]`              |
| Unauthorized ops.     | `report["unauthorized_operation_count"]`            |
| Injected failures     | `report["injected_failure_count"]`                  |
| Effect posture        | `report["final_effect_posture"]`                    |
| Independent verifier  | `report["independent_verification_result"]`         |
| Authority allow/deny  | `report["authority"]` (partitioned by disposition)  |
| Timeline story        | `MissionObservation` transitions + checkpoints      |
| Technical identifiers | `evidence-report.json` (intent, dispatch, claim, …) |

`tools/integrated_demonstrator/run_demo.py::_validate_sources` re-runs every
one of these binds during `verify` mode and rejects any drift between the
manifest, the report, the durable stores, and the reconstructed HTML.

## Regression coverage

Two test files cover the presentation:

- `tests/test_mission_control.py` — projection contents, evidence report
  contract, and rendering under an incomplete/inconsistent report.
- `tests/test_mission_control_presentation_v0_2.py` — human title, evidence
  binding of every metric, authority partition, timeline story ordering,
  severity classes, retry-blocked statement, technical detail disclosure,
  responsive/semantic markup, and deterministic projection/render.

Additional coverage:

- `tests/test_integrated_demonstrator_operationalization.py` — full
  run/verify/tamper contract on the packaged bundle.
- `tests/test_integrated_reconciliation_verification_evidence.py` —
  reconciliation, authority disposition, and durable store contract.

## What this demonstration proves and does not prove

Proves — for one isolated loopback run:

- Exactly one authorized external operation was dispatched.
- The dispatch was executed once against the isolated test service.
- A confirmation loss produced an `INDETERMINATE` outcome that was preserved.
- Automatic retry was refused while the effect was unresolved.
- Reconciliation established external truth from a bounded read.
- The original operation was confirmed to have landed exactly once.
- No duplicate or unauthorized operations occurred.
- Independent verification passed against the same external state.
- The mission completed with durable, hash-manifested evidence.

Does not prove — this demonstration is intentionally bounded:

- No production deployment.
- No authentication of the human operator.
- No permission to spend, orchestrate, or affect any external system.
- No claim about long-running mission survival, distributed operation, or
  multi-tenant isolation.
- Manifest hashes detect byte changes; they are not a digital signature.

The loopback test service is a demonstration participant, not a deployment
target.
