# MissionaryX Local Development Readiness v0.1

This is a fail-closed readiness surface for a prepared local MissionaryX
development machine. It observes the repository, Python environment, external
wheelhouse, llama.cpp checkout, llama-server binary, and local model artifact.
It does not install, repair, start, stop, or modify any of them.

Local readiness is machine evidence, not repository acceptance. Git history,
deterministic tests, exact fingerprints and digests, and human review remain
authoritative. A model-generated answer is never sufficient evidence of
correctness.

## Static readiness

From any directory inside the MissionaryX checkout, run:

```bash
./tools/check-local-development-readiness
```

Static mode is the default. It performs these checks:

1. The selected path is the root of a Git checkout whose
   `missionaryx-state.json` identifies MissionaryX.
2. Python is executable, is version 3.11 or newer, and the recursive
   development requirement files contain inspectable exact pins. If `.venv`
   exists, its interpreter and pinned top-level package versions must match.
3. Every wheel in the external wheelhouse is covered by `SHA256SUMS`, every
   manifest digest verifies, and pip can resolve `requirements-dev.txt` using
   a no-index, no-cache, ignore-installed dry run.
4. The llama.cpp path is its Git checkout root at the source checkpoint fixed
   by the existing `LocalModelServerProfile`.
5. `llama-server` is a regular executable whose SHA-256 equals the existing
   profile's binary identity.
6. The GGUF is a regular file whose bytes match explicit trusted SHA-256
   material. Its filename alone is never accepted as identity.

Successful static output ends with:

```text
LOCAL_DEVELOPMENT_STATIC_READY
```

Static mode performs no HTTP request or inference. It never starts or stops
llama-server, signals a process, mutates lifecycle state, modifies Git or local
assets, invokes a package index, requires internet access, or contacts a cloud
service. A failure in any required dimension returns nonzero and no ready
summary is printed.

## Explicit adapter-level live readiness

If the governed lifecycle has already placed the expected server on its
loopback endpoint, an operator may request the additional smoke check:

```bash
./tools/check-local-development-readiness --live
```

Live mode does not launch a replacement and does not terminate the existing
server. It rejects non-loopback endpoints before adapter construction. It uses
the existing MissionaryX `LlamaCppLocalAdapter` to require one unambiguous
`/v1/models` identity and then sends a deliberately short completion with no
tools and at most eight output tokens. Readiness depends on structural success
and non-empty output, not on the semantic quality of the answer.

The returned adapter evidence must have all of these exact properties:

```text
status == succeeded
locality == local
remote_execution is False
cloud_escalation_count == 0
cloud_cost_usd == 0.0
error_code is None
output_text is non-empty
```

Successful live output ends with:

```text
LOCAL_DEVELOPMENT_LIVE_READY
```

This is adapter-level live readiness only. The manually established live proof
and this smoke path do not prove that the full durable
`LocalModelServerLifecycleCoordinator` plus
`GovernedLocalInferenceIntegration` production chain was exercised live.
Deterministic tests cover those stronger governed contracts. A listening port
or this adapter response must not be promoted into lifecycle or execution
authority.

If no server is already listening, `--live` fails clearly and nonzero. It does
not attempt recovery or startup.

## Paths and fixed profile identity

Defaults are relative to `Path.home()`:

```text
$HOME/missionaryx-offline-wheelhouse
$HOME/local-ai/llama.cpp
$HOME/local-ai/llama.cpp/build/bin/llama-server
$HOME/local-ai/models/qwen2.5-0.5b-instruct/
  qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Use `--wheelhouse`, `--llama-cpp-source`, `--llama-server`, `--model`,
`--python`, or `--repository` for explicit machine-specific paths. `--endpoint`
can select another literal loopback address and port for an already-running
server. Run `--help` for the complete argument contract.

The source checkpoint, binary digest, endpoint default, context size, and live
model alias come from the existing protected `LocalModelServerProfile`; the
tool does not duplicate a server lifecycle or competing profile. That profile
inherits the historical model alias `raghub-qwen2.5-0.5b-q4km`. This is naming
debt, not a new MissionaryX canonical alias, and this milestone does not rename
it.

## Currently verified local material

The prepared development host was manually verified with:

- llama.cpp source checkpoint:
  `876a4321163249c43ca4e986818fab5ab081f282`
- profile-bound llama-server SHA-256:
  `51101f3ef423200f9096ed07fc186e4670fa7b99ccea2d9a571fdd2ff5851b04`
- Qwen2.5 0.5B Instruct Q4_K_M GGUF SHA-256:
  `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`
- external wheelhouse: 41 wheels with a verified `SHA256SUMS` manifest

These facts describe exact observed material. Re-run the tool for current
machine readiness; documentation alone does not establish that the assets are
still present or unchanged.

## Offline wheelhouse reconstruction

The repository does not vendor the wheelhouse. The workflow first downloads
the complete pinned dependency closure while a package source is available,
then records and verifies local digests:

```bash
wheelhouse="$HOME/missionaryx-offline-wheelhouse"
mkdir -p "$wheelhouse"
python3.14 -m pip download \
  --dest "$wheelhouse" \
  --requirement requirements-dev.txt

(
  cd "$wheelhouse"
  sha256sum -- *.whl > SHA256SUMS
  sha256sum -c SHA256SUMS
)
```

On the reconstruction target, verify the copied manifest, create a fresh
environment, and install exclusively from the local wheelhouse:

```bash
wheelhouse="$HOME/missionaryx-offline-wheelhouse"
(
  cd "$wheelhouse"
  sha256sum -c SHA256SUMS
)

python3.14 -m venv .venv
.venv/bin/python -m pip install \
  --no-index \
  --find-links "$wheelhouse" \
  --requirement requirements-dev.txt
```

This proves a no-index local reconstruction using the prepared wheelhouse. The
proof did not physically air-gap the machine. It does not establish that the
workflow works on every fresh Fedora machine: native wheels in the current
wheelhouse are CPython 3.14 / x86_64 specific where applicable, and a target
still needs a compatible Python interpreter and system runtime.

The readiness tool repeats manifest verification and a non-installing resolver
dry run. It never invokes the download step.

## Appropriate use of the small local model

The verified Qwen2.5 0.5B model is useful for bounded local assistance, smoke
tests, explanation, simple drafting, and simple debugging support. Its output
is non-guaranteed decision support. It is not equivalent to Claude, Codex,
ChatGPT, or another large hosted model.

Hosted models remain optional accelerators, not acceptance authority.
MissionaryX development can also proceed without any model. Reviewers must
verify claims against source, tests, Git history, exact evidence, and the
relevant human decision.
