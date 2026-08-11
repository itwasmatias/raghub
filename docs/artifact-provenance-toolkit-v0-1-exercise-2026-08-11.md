# Artifact Provenance Toolkit v0.1 Exercise

This exercise used only synthetic fixtures under a temporary directory:

- temp root: `/tmp/artifact-provenance-exercise-cjuzekuj`
- manifest: `/tmp/artifact-provenance-exercise-cjuzekuj/manifest.json`

## Scenario

1. Create a manifest for two synthetic files.
2. Verify the manifest.
3. Modify one synthetic file.
4. Show verification failing.
5. Restore the file by recreating the synthetic fixture only.
6. Recreate the manifest.
7. Verify again.

## Observed CLI Results

```text
[create-1] rc=0
[create-1] stdout=created DIGEST_VERIFIED manifest with 2 artifacts
[create-1] stderr=
[verify-1] rc=0
[verify-1] stdout=verified DIGEST_VERIFIED manifest with 2 artifacts
[verify-1] stderr=
[verify-2] rc=12
[verify-2] stdout=
[verify-2] stderr=byte size mismatch for a.txt
[create-2] rc=0
[create-2] stdout=created DIGEST_VERIFIED manifest with 2 artifacts
[create-2] stderr=
[verify-3] rc=0
[verify-3] stdout=verified DIGEST_VERIFIED manifest with 2 artifacts
[verify-3] stderr=
```

## Result

The tool behaved as expected:

- deterministic manifest creation succeeded twice;
- verification succeeded before the modification;
- verification failed after the modification with exit code `12`;
- restoration by recreating the synthetic fixture and manifest succeeded;
- the manifest never needed to be repaired in place.
