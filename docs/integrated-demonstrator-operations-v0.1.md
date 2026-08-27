# Integrated Demonstrator v0.1 operator guide

The Integrated Demonstrator shows one bounded MissionaryX mission against an
isolated loopback test service. It starts that service at v1, authorizes one
deployment, commits v2, deliberately loses the response, preserves the result
as `INDETERMINATE`, blocks a blind retry, reconciles with a read-only external
state check, independently verifies v2 with a separate read, and only then
completes the mission.

## Run the demonstration

From the repository checkout, run:

```bash
python -m tools.integrated_demonstrator.run_demo
```

No operator input is required. The service binds only to an ephemeral port on
`127.0.0.1`, and the command shuts it down before returning. A successful run
prints a short PASS summary and the exact artifact paths.

Each run gets a new directory under:

```text
artifacts/integrated-demonstrator/<run-id>/
```

The directory contains the evidence report, Mission Control HTML and JSON
projection, three durable SQLite stores, an operator summary, and a manifest.
Open `mission-control.html` in a browser to view the evidence-backed story.
The rendered page uses the Mission Control v0.2 presentation layer — see
`docs/mission-control-demonstrator-v0.2.md` for a walkthrough of what a viewer
should notice and how every displayed value is bound to persisted evidence.

## Verify a preserved run

```bash
python -m tools.integrated_demonstrator.run_demo verify \
  artifacts/integrated-demonstrator/<run-id>
```

Verify mode does not deploy, reconcile, or mutate the mission. It checks every
manifested SHA-256 digest, copies the durable stores to a temporary directory,
reopens those copies through the canonical MissionaryX readers, reconstructs
Mission Control, and cross-checks the report, counters, preserved
`INDETERMINATE` history, final effect posture, independent verification, and
mission state. It exits nonzero on missing, corrupt, inconsistent, or tampered
evidence.

## Meaning and limits of PASS

PASS means this isolated run durably demonstrates exactly one authorized test
deployment, one successful v1-to-v2 transition, one injected response loss,
no blind retry, reconciliation to `SOMETHING_LANDED`, independent v2
verification, mission completion, zero duplicates, and zero unauthorized
operations according to the produced and cross-checked evidence.

This is non-guaranteed decision support for reviewing the MissionaryX control
flow. It is not a production deployment, production-host proof, security audit,
authentication claim, permission for spending, or authorization to operate on
any external system. Manifest hashes detect later byte changes relative to the
manifest; they are not a digital signature and do not authenticate who created
the bundle.
