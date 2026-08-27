# MissionaryX Real Worker Integration v0.1

This milestone extends the accepted Integrated Demonstrator and Mission Control
path with one bounded proposal worker. The worker is advisory: its output is
strict JSON data, not executable text. MissionaryX still owns proposal parsing,
authority, the controlled executor, effect evidence, retry blocking,
reconciliation, independent verification, completion, and bundle verification.

## Offline acceptance

The default command uses deterministic test infrastructure and does not contact
an external model:

```bash
python -m tools.integrated_demonstrator.run_demo --worker-mode deterministic
```

Successful output explicitly says `LIVE EXTERNAL WORKER VERIFIED: NO` and emits
the evidence bundle and Mission Control v0.3 paths.

## Configured live worker

Live mode uses a non-streaming OpenAI-compatible Chat Completions request and
requires explicit configuration:

```bash
export MISSIONARYX_WORKER_BASE_URL=http://configured-worker-host:port
export MISSIONARYX_WORKER_MODEL=configured-model-id
export MISSIONARYX_WORKER_API_KEY=local-secret
python -m tools.integrated_demonstrator.run_demo --worker-mode live
```

The bounded request body omits sampling controls such as `temperature`, `top_p`,
`frequency_penalty`, and `presence_penalty`. For Crucible's bounded API profile,
configure its accepted output ceiling explicitly:

```bash
export MISSIONARYX_WORKER_MAX_OUTPUT_TOKENS=128
```

The generic adapter default remains `512`; provider-specific output limits are
configured through the existing environment setting rather than imposed on every
OpenAI-compatible worker.

Authentication is required by default. A provider that intentionally does not
require bearer authentication must be configured explicitly with
`MISSIONARYX_WORKER_REQUIRE_AUTH=0`. The key is loaded only from the environment,
is excluded from representations, prompts, evidence, Mission Control, summaries,
and errors, and is never printed.

Optional bounded settings are:

- `MISSIONARYX_WORKER_CONNECT_TIMEOUT` (default `2` seconds)
- `MISSIONARYX_WORKER_READ_TIMEOUT` (default `30` seconds)
- `MISSIONARYX_WORKER_MAX_OUTPUT_TOKENS` (default `512`, maximum `4096`)
- `MISSIONARYX_WORKER_RETRY_LIMIT` (`0` or `1`; default `0`)
- `MISSIONARYX_WORKER_IDENTITY` (configured worker identity)
- `MISSIONARYX_WORKER_PROVIDER` (configured provider identity)

The command checks `GET /ready` before requesting a proposal. `GET /health` is
only a liveness probe and is never treated as model readiness.

| HTTP/status payload | MissionaryX interpretation |
| --- | --- |
| `200 {"status":"ready"}` | `WORKER_READY` |
| `503 {"status":"model_not_loaded"}` | `WORKER_NOT_READY / MODEL_NOT_LOADED` |
| `503 {"status":"model_loading"}` | `WORKER_NOT_READY / MODEL_LOADING` |
| `409 {"status":"busy"}` | `WORKER_BUSY` |
| `401` or `403` | authentication failure |
| malformed response | readiness protocol failure |
| timeout/connection failure | worker transport failure |

Not-ready and busy results do not loop. A configured retry applies only to the
advisory proposal transport, remains bounded to one retry, reuses the same
correlation ID, and cannot create more than one controlled deployment.

## Proposal and authority boundary

The worker receives only the mission objective, small current-state fields, the
proposal schema, the configured identities, and this exact allowed catalog:

```json
{
  "type": "deploy_service_version",
  "arguments": {"target": "test-service", "version": "v2"}
}
```

Unknown actions, fields, types, identities, correlations, or arguments fail
closed. A valid but out-of-authority proposal is persisted as denied and cannot
create an effect intent or reach the test service. Trusted code maps only the
exact authorized catalog entry to the fixed `POST /deploy-v2` executor. No model
text is evaluated, passed to a shell, or used as a URL or command.

## Evidence and presentation

Successful bundles persist the configured worker, provider, model, request ID,
schemas, readiness, whether an external HTTP proposal response was actually
received, attempt and retry evidence, typed proposal, authority decision, and
exact executor digest binding. Mission checkpoints preserve the worker request,
readiness, proposal validation, authority decision, and executor binding before
the existing indeterminate-effect story.

Mission Control v0.3 displays the worker as `Proposal / Planner`, the proposal,
and MissionaryX's validation and authorization decisions. Deterministic runs are
visibly labeled `Deterministic test infrastructure`; they are never presented as
real external-model verification.

## Crucible boundary

The adapter is compatible with the documented Crucible readiness routes and
non-streaming OpenAI-compatible chat shape. This coding milestone does not prove
the corrected Crucible IPA or physical iPhone endpoint. Physical-device
acceptance remains a separate owner-performed gate.
