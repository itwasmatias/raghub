# Artifact Provenance Toolkit v0.1 Internal Adversarial Review

This is an internal review, not an independent review.

I attacked the implemented paths for manifest self-inclusion, dual replacement, symlink swaps, parent symlink traversal, path normalization collisions, Unicode ambiguity, case-insensitive collisions, file mutation during hashing, inode replacement, concurrent writers, torn writes, stale temporary files, malformed JSON, duplicate keys, digest confusion, absolute-path leakage, content leakage, terminal escapes, newline injection, reserved Windows names, device files, hard links, shared-lock limitations, and overclaimed authentication.

## Verdict

- High findings: none.
- Medium findings: none.
- Low and informational findings: 2.

## Findings

### 1. Unbounded manifest size can still drive memory usage

- Severity: Low
- File and line: [`tools/artifact_provenance/manifest.py`](../tools/artifact_provenance/manifest.py#L422-L449)
- Reproduction: write a very large JSON manifest file with a huge `artifacts` array, then run `inspect` or `verify` against it. The loader reads the full file into memory before validation.
- Violated invariant: verification does not impose a manifest-size guard, so a maliciously large manifest can still create a local denial-of-service path.
- Remediation: add an explicit size cap or a streaming/early-reject path for manifests above an allowed threshold.
- Regression test: add a synthetic oversized manifest case that is rejected before full parse or documented as intentionally unsupported.

### 2. Case-fold collision policy is conservative on case-sensitive filesystems

- Severity: Informational
- File and line: [`tools/artifact_provenance/manifest.py`](../tools/artifact_provenance/manifest.py#L408-L413), [`tools/artifact_provenance/manifest.py`](../tools/artifact_provenance/manifest.py#L473-L479)
- Reproduction: on a case-sensitive filesystem, create two distinct artifacts such as `a.txt` and `A.txt` and ask the toolkit to include both. The manifest loader/build path rejects them as duplicate normalized paths.
- Violated invariant: the current policy is not a lossless acceptance policy for every legal POSIX filename; it deliberately favors cross-platform collision safety over full path-name permissiveness.
- Remediation: keep the conservative policy and document it, or add an explicit opt-in mode for case-sensitive environments if broader acceptance is required.
- Regression test: retain the duplicate-normalized-path test and, if policy changes, add a platform-conditional acceptance test for distinct case-variant names.

## Notes

- The digest-only guarantee remains correctly bounded.
- No code path claimed authentication, signatures, legal admissibility, or chain of custody.
- The focused provenance tests, adjacent lock/serialization tests, full repository suite, and compile pass all completed successfully during this review window.
