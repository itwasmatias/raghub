# Small Local-Only Capability Pilot v0.1

This pilot defines twelve small, deterministically ordered tasks for measuring one
governed local CHAT model on commodity hardware. It projects its tasks into the
existing Local Equivalence benchmark and `SINGLE_LOCAL` experiment contracts. It
does not establish equivalence, select a winner, or treat model agreement as proof.

Objective rubrics cover exact answers, labels, numeric answers, required-fact
extraction, and fixed formats. Execution success and objective correctness are
reported independently. A required-facts rubric only establishes that the named
facts appeared; it is not a general quality judgment. Tasks without an objective
rubric remain unevaluable.

Run the real entrypoint only after independent review:

```text
python -m tools.ai_controller.run_local_only_pilot \
  --endpoint http://127.0.0.1:8080 \
  --output /explicit/path/report.json \
  --authority-manifest /explicit/path/authority.json \
  --run-id pilot-run-001 \
  --profile-fingerprint <lowercase-sha256> \
  --model-alias raghub-qwen2.5-0.5b-q4km \
  --model-sha256 74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db
```

The authority manifest must contain exactly one entry per pilot task and only
`execution_attempt_id`, `assignment_id`, `dispatch_offer_id`, and
`execution_fingerprint`. Those identities must come from the existing governed
worker execution path. The entrypoint uses CHAT mode, no tools, a fixed seed and
temperature, at most 96 output tokens, and an empty cloud surface. The adapter
continues to require loopback, exact loaded-model alias attestation, chat-template
attestation, bounded responses, no redirects, and no retries.

The entrypoint neither starts llama-server nor downloads weights. The supplied
model SHA-256 is bound into configuration and report provenance; current
llama-server metadata attests the loaded alias but does not independently expose a
cryptographic digest of the loaded model bytes. Operational setup must therefore
verify the model file digest before starting the separately managed server.
