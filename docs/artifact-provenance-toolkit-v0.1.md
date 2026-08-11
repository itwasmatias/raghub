# Artifact Provenance Toolkit v0.1

## Scope

Artifact Provenance Toolkit v0.1 is a narrow digest-manifest primitive for explicitly selected local artifacts under a declared root.

It records deterministic metadata for file bytes that were read from local storage and later recomputes those digests for verification.

It is not EvidenceLedger, not authentication, and not a custody system.

## Guarantee

The only guarantee is `DIGEST_VERIFIED`:

- the verifier recomputed the selected digest algorithm over the artifact bytes;
- the recomputed digest matched the digest recorded in the external manifest.

That guarantee does not say who created the artifact, when it was created, whether the manifest is trusted, or whether an attacker replaced both files.

## Non-Guarantees

This toolkit does not provide:

- authentication;
- signatures;
- non-repudiation;
- legal admissibility;
- chain of custody;
- ControlDomain authority;
- AgentIdentity binding;
- DelegationGrant enforcement;
- credential mediation;
- continuous protection against replacement of both artifact and manifest.

## Manifest Schema

The manifest is canonical JSON written to an external file.

Top-level fields:

- `schema_version`: `artifact-provenance-manifest-v0.1`
- `guarantee`: `DIGEST_VERIFIED`
- `algorithm`: `sha256`
- `artifacts`: sorted list of artifact records

Artifact record fields:

- `logical_path`: root-relative logical path using `/`
- `size_bytes`: non-negative integer
- `sha256`: lowercase hex digest

Canonicalization rules:

- UTF-8 encoding;
- deterministic key ordering;
- compact JSON separators;
- trailing newline on disk;
- no raw artifact contents;
- no secrets;
- no absolute host-specific paths;
- no manifest self-digest inside the deterministic payload.

## API

Python API:

- `build_manifest(root, artifacts, algorithm="sha256", chunk_size=1048576)`
- `create_manifest(root, output_path, artifacts, algorithm="sha256", chunk_size=1048576)`
- `verify_manifest(root, manifest_path, chunk_size=1048576)`
- `inspect_manifest(manifest_path)`
- `load_manifest(path)`

Return types:

- `ArtifactProvenanceManifest`
- `ArtifactRecord`

Errors are raised as typed exceptions with stable exit codes:

- `10` - malformed manifest
- `11` - missing artifact
- `12` - digest or byte-size mismatch
- `13` - mutation detected while hashing
- `14` - unsafe path or root escape
- `15` - unsupported artifact type or algorithm
- `16` - manifest write failure

## CLI

Invocation:

```bash
/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance create \
  --root /path/to/root \
  --output /path/to/manifest.json \
  file-a.md nested/file-b.txt

/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance verify \
  --root /path/to/root \
  --manifest /path/to/manifest.json

/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance inspect \
  --manifest /path/to/manifest.json
```

CLI behavior:

- deterministic stdout;
- diagnostics on stderr;
- nonzero exit on malformed manifest, missing artifact, mismatch, unstable artifact, unsafe path, unsupported type, or unsupported algorithm;
- no automatic repair;
- no automatic rewriting of disputed manifests;
- `verify` and `inspect` never mutate artifacts or the manifest;
- `inspect` states that digest verification is not authentication.

## Path Safety

The toolkit requires a declared root and only accepts root-relative logical paths.

Rejected by default:

- absolute paths;
- `..` traversal;
- empty unsafe components;
- NULL bytes;
- newline or carriage return characters;
- duplicate normalized paths;
- output manifest aliasing a selected artifact;
- symlink traversal;
- regular-file replacement with unsupported types;
- directories, devices, sockets, FIFOs, and symlinks.

Path normalization uses `/` as the canonical separator. Backslashes are normalized to `/` in logical-path inputs.

## Mutation Handling

Files are streamed in bounded chunks and compared before and after hashing.

If the selected artifact changes during hashing, verification fails closed rather than recording an uncertain digest.

The manifest itself is loaded in a canonical form check, so duplicate JSON keys and non-canonical encodings fail.

## Atomic Write Behavior

Manifest creation writes to a temporary file in the destination directory, flushes data, fsyncs where supported, and atomically replaces the destination only after success.

If creation fails, the previous valid manifest is preserved.

Concurrent writers are coordinated with the existing repository file-lock abstraction.

## Cross-Platform Behavior

v0.1 is written to fail closed when safety cannot be established.

Supported behavior:

- SHA-256 only;
- deterministic JSON serialization;
- explicit root-relative paths;
- feature-detected POSIX file-descriptor traversal with `O_NOFOLLOW` where available;
- documented fallback behavior when that path is unavailable.

Limitations:

- Windows and POSIX do not expose identical path and file-identity semantics;
- unsupported filesystem objects fail closed;
- case-insensitive collision handling is conservative;
- hard-link and replacement attacks can still defeat a digest-only primitive if both artifact and manifest are replaced together.

## Examples

Create a manifest for synthetic files:

```bash
mkdir -p /tmp/artifact-provenance-demo
mkdir -p /tmp/artifact-provenance-demo/nested
printf 'alpha\n' >/tmp/artifact-provenance-demo/a.txt
printf 'beta\n' >/tmp/artifact-provenance-demo/nested/b.txt
/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance create \
  --root /tmp/artifact-provenance-demo \
  --output /tmp/artifact-provenance-demo/manifest.json \
  a.txt nested/b.txt
```

Verify it:

```bash
/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance verify \
  --root /tmp/artifact-provenance-demo \
  --manifest /tmp/artifact-provenance-demo/manifest.json
```

Inspect it:

```bash
/home/matias/raghub/.venv/bin/python -m tools.artifact_provenance inspect \
  --manifest /tmp/artifact-provenance-demo/manifest.json
```

## Relationship To Future M3 Evidence Spine

This toolkit is a digest-only substrate.

Future M3 responsibilities can layer on top of it:

- authenticated envelopes;
- detached signatures;
- ControlDomain bindings;
- AgentIdentity bindings;
- retention policy;
- independent timestamps;
- ENFORCED and ATTESTED status semantics;
- evidence-ledger integration.

## Why Self-Referential Checksums Are Prohibited

The manifest must not claim a digest of its own final bytes.

If the manifest includes its own final digest, then any edit to the manifest changes the value that would have to be inside the manifest, which makes the payload self-referential and destabilizes reproducibility.

The result would be a brittle or circular trust claim instead of a stable digest-manifest record for external artifacts.
