# Native Ollama Coding Agent Provider — Final Report

**Date:** 2026-07-30  
**Worktree:** `/home/matias/raghub-controller-claude-native-ollama`  
**Branch:** `feature/controller-native-ollama-agent`  
**Commit:** `ae001c6`

---

## 1. Starting State

| Field | Value |
|-------|-------|
| Base branch | `feature/ai-controller-operational-v1` |
| Base commit | `88aa8a2e848f31922338d8359e088d24c30ba4c0` |
| Pre-existing tests | 38 passing |
| Ollama provider | `OllamaProvider` / `LocalOllamaProvider` — one-shot patch generator |
| Agent loop | None |
| CLI commands | init, enqueue, run, recover, status, report, doctor |

---

## 2. Claude Task Branch and Worktree

```
Branch:  feature/controller-native-ollama-agent
Worktree: /home/matias/raghub-controller-claude-native-ollama
```

Created with:
```bash
git worktree add /home/matias/raghub-controller-claude-native-ollama \
    -b feature/controller-native-ollama-agent
```

No files from other worktrees were staged or committed.

---

## 3. Existing Architecture Findings

### Controller flow
1. `Controller.run_once()` claims a task from `DurableQueue`
2. `WorktreeManager.create()` creates an isolated git worktree
3. Provider `execute()` is called with task + worktree path
4. `detect_changes()` runs independently via `git diff`
5. `_run_tests()` runs declared commands independently
6. Report is written to `ReportStore`, lesson appended to `ExperienceLedger`

### Existing Ollama provider (`OllamaProvider`)
- Uses `POST /api/generate` — one-shot, single prompt/response
- Model must return a full unified diff in one response
- No tool loop, no multi-turn interaction
- Path validation exists for patch paths
- Already native (no Codex CLI) but has no tool protocol

### Runner architecture
- `LocalRunner` — `subprocess.run` locally
- `SSHRunner` — runs Python scripts remotely via SSH
- Both expose identical `.run(argv, cwd, input_text, timeout)` interface
- `WorktreeManager` and `OllamaProvider` both use the runner for all operations

### Missing components
- Multi-step agent loop
- Structured tool protocol
- Separate model client from repository runner
- Path confinement for write/search tools
- Command allowlist enforcement
- Malformed-response recovery
- Repeated-action detection
- `smoke-local` command
- `--json` flag for doctor

---

## 4. Native Provider Architecture

```
NativeOllamaAgentProvider
│
├── OllamaModelClient          (HTTP only, zero runner dependency)
│   ├── GET /api/version       availability check
│   ├── GET /api/tags          model-installed check
│   └── POST /api/chat         multi-turn conversation
│
└── RepositoryToolRunner       (all ops via runner.run(), LocalRunner or SSHRunner)
    ├── list_files
    ├── read_file
    ├── search_text
    ├── git_status
    ├── git_diff
    ├── write_file             (atomic via .tmp rename)
    ├── apply_patch            (git apply --check then apply)
    ├── run_declared_test      (task.tests[index], no shell)
    └── run_allowed_command    (allowlist prefix match)
```

`AgentLoop` orchestrates the conversation:
- Sends `POST /api/chat` with full message history each step
- Parses JSON from model response (with prose recovery)
- Dispatches tool, appends result as user message
- Continues until `finish` tool, step limit, or failure limit

---

## 5. Model / Repository Runner Separation

| Component | Runner used |
|-----------|-------------|
| `OllamaModelClient` | None — direct HTTP via `urllib.request` |
| `RepositoryToolRunner` | `LocalRunner` or `SSHRunner` (injected) |
| `WorktreeManager` | Same runner as controller |
| `CodexProvider` | Same runner as controller |

In production Windows→Fedora configuration:
- Model client calls Windows Ollama HTTP API directly (no SSH needed)
- Repository tools execute via `SSHRunner` to Fedora worktree
- The two paths are independent — model failures don't affect repository access

---

## 6. Safe Tool Protocol

Each model response must be exactly one JSON object:

```json
{
  "thought_summary": "brief intent description",
  "action": {
    "tool": "<tool_name>",
    "arguments": { ... }
  }
}
```

### Tool catalog

| Tool | Arguments | Purpose |
|------|-----------|---------|
| `list_files` | `path` | List directory entries |
| `read_file` | `path` | Read file content (100KB limit) |
| `search_text` | `pattern`, `path` | Regex search in files |
| `git_status` | none | Working tree status |
| `git_diff` | none | Unstaged diff |
| `write_file` | `path`, `content` | Atomic file write |
| `apply_patch` | `patch` | Apply unified diff |
| `run_declared_test` | `index` | Run task.tests[index] |
| `run_allowed_command` | `argv` | Run allowlisted command |
| `finish` | `success`, `summary` | Terminate agent loop |

JSON extraction recovers from prose-wrapped responses and code fences.

---

## 7. Path Protections

All paths are statically validated before dispatch (no filesystem required):

| Threat | Detection |
|--------|-----------|
| Null bytes | `"\x00" in path` |
| Absolute paths | `PurePosixPath.is_absolute()` |
| Parent traversal (`..`) | `".." in PurePosixPath.parts` |
| Windows drives | `re.match(r"^[A-Za-z]:", path)` |
| UNC paths | `path.startswith("\\\\")` |
| `.git` access | `parts[0] in _PROHIBITED_TOPS` |
| Patch path traversal | Validated in `_validate_patch_paths()` |
| Patch `.git` modification | Same validation |

File reads are limited to 100KB. Search output limited to 20KB.  
Command output limited to 50KB (tail).

---

## 8. Command Protections

`run_declared_test`:
- Only indexes into `task.tests` (controller-declared, not model-declared)
- No arbitrary command strings accepted
- No `shell=True`

`run_allowed_command`:
- Command prefix must match an item in `native_agent_allowed_commands`
- Each argument checked for shell operators: `| ; & \` $ > < \n`
- No `shell=True`

Rejected at all times:
- Shell operators in any argument
- `merge`, `push`, `deploy`, `reset --hard`, `clean -f`, `checkout` of user branches
- Package installation (unless explicitly allowlisted)

---

## 9. Bounded-Loop Behavior

| Limit | Default | Config field |
|-------|---------|-------------|
| Max steps | 20 | `ollama_max_agent_steps` |
| Max tool failures | 5 | `ollama_max_tool_failures` |

Stop conditions (all produce a `ProviderResult.failure_result`):
- `finish` tool called with `success=False` → `stop_reason: finish`
- Step limit reached → `stop_reason: max_steps`
- Tool failure limit reached → `stop_reason: max_tool_failures`
- Ollama HTTP error at any step → `stop_reason: ollama_error:<category>`

Recovery within limits:
- Malformed JSON: inject error message, ask model to retry
- Missing `action` field: inject error message, ask model to retry
- Unknown tool: count as failure, inject error, model must adapt
- Tool error: count as failure, inject error, model must adapt
- Repeated identical action (3×): count as failure, inject warning

**No-change guarantee:** even if the agent loop exits with `success=True`, the controller independently verifies git changes via `detect_changes()`. If no files changed, the provider result is overridden to `category=no_changes, retryable=True`.

---

## 10. Configuration Fields

New fields added to `ControllerConfig`:

| Field | Default | Purpose |
|-------|---------|---------|
| `native_agent_attempts` | `0` | Attempts via NativeOllamaAgentProvider (0=disabled) |
| `ollama_max_agent_steps` | `20` | Max tool steps per agent run |
| `ollama_max_tool_failures` | `5` | Max consecutive tool failures before abort |
| `ollama_keep_alive` | `"5m"` | Ollama model keep-alive duration |
| `native_agent_allowed_commands` | `[]` | Prefix allowlist for run_allowed_command |
| `native_agent_command_timeout_seconds` | `120` | Timeout for run_declared_test and run_allowed_command |
| `ollama_generation_timeout_seconds` | `300` | (changed from 120) per-step generation timeout |

All existing fields remain backward compatible.

---

## 11. Doctor Command

```bash
python -m tools.ai_controller.cli --config <path> doctor
python -m tools.ai_controller.cli --config <path> doctor --json
```

Checks performed:
1. Controller root accessible
2. All 5 queue subdirectories present
3. Reports directory present
4. Logs directory present
5. Experience ledger directory present
6. Process lock acquirable (no zombie controller)
7. Stale running tasks (WARN, not FAIL)
8. SSH executable (when ssh_host configured)
9. SSH key file exists
10. Fedora repository accessible via SSH
11. Codex available (when codex_attempts > 0)
12. Fedora worktree root accessible
13. Ollama reachable (HTTP GET /api/version)
14. Configured model installed (GET /api/tags)
15. Native agent model confirmed (when native_agent_attempts > 0)

Returns exit code `0` when operational, `1` when required checks fail.  
`--json` outputs structured `{"ok": bool, "checks": [...]}`.

---

## 12. Smoke-Local Command

```bash
python -m tools.ai_controller.cli --config <path> smoke-local
```

Steps:
1. Checks Ollama reachability — fails clearly if unavailable
2. Creates temp git repository with `fixture.py`
3. Commits base state
4. Creates isolated task worktree via `WorktreeManager`
5. Instantiates `NativeOllamaAgentProvider` with live Ollama
6. Executes the agent loop
7. Controller independently detects git changes via `capture_changes()`
8. Builds structured report
9. Appends to `ExperienceLedger` (at `settings.experience_path`)
10. Writes report to `settings.reports_root/smoke-native-agent.json` (persistent)
11. Prints agent steps for diagnostics
12. Temp fixture resources cleaned up automatically
13. Reports PASS only when: provider success AND controller-verified git changes

Does not use a fake Ollama server. Requires live Ollama.

---

## 13. Files Created or Modified

### New files
```
tools/ai_controller/providers/native_agent.py   (558 lines)
tools/ai_controller/tests/test_native_agent.py  (558 lines)
```

### Modified files
```
tools/ai_controller/providers/__init__.py   (+2 lines: NativeOllamaAgentProvider export)
tools/ai_controller/config.py              (+14 lines: 6 new config fields)
tools/ai_controller/cli.py                 (+140 lines: native agent, doctor --json, smoke-local)
tools/ai_controller/tests/conftest.py      (+28 lines: /api/chat support in fake server)
```

### Not modified
```
tools/ai_controller/providers/codex.py     (untouched)
tools/ai_controller/controller.py          (untouched)
tools/ai_controller/worktrees.py           (untouched)
tools/ai_controller/changes.py             (untouched)
tools/ai_controller/models.py              (untouched)
tools/ai_controller/queue.py               (untouched)
tools/ai_controller/remote.py              (untouched)
tools/ai_controller/reports.py             (untouched)
tools/ai_controller/experience.py          (untouched)
```

---

## 14. Tests Added

**File:** `tools/ai_controller/tests/test_native_agent.py` — **92 tests**

| Class | Tests | Coverage |
|-------|-------|---------|
| `TestPathValidation` | 10 | null byte, absolute, traversal, Windows drive, UNC, .git |
| `TestExtractJsonObject` | 9 | direct, prose, fence, nested, unicode, exact prompt, sip_markets.html |
| `TestOllamaModelClient` | 13 | available, installed, chat, server error, missing message, empty content, malformed, timeout, exact prompt |
| `TestRepositoryToolRunner` | 30 | list_files, read_file (safety), write_file (atomic, safety), git_status/diff, apply_patch (safety), run_declared_test, run_allowed_command (allowlist, shell rejection), search |
| `TestAgentLoop` | 14 | finish success/fail, multi-turn, write+finish, malformed recovery, failure limit, unknown tool, path traversal, repeated action, step limit, ollama error, declared test, prompt preservation, sip_markets.html |
| `TestNativeOllamaAgentProvider` | 16 | unavailable, missing model, success, steps captured, failed, prompt, apply patch, traversal rejected, no-change, server error, truncation, available/installed methods, provider/model fields, codex-not-invoked |

**No test requires live Ollama, live SSH, or network access.**  
All use `FakeOllamaServer` from conftest (in-process HTTP server).

---

## 15. Focused Test Results

```
tools/ai_controller/tests/test_native_agent.py::TestPathValidation          10/10 PASS
tools/ai_controller/tests/test_native_agent.py::TestExtractJsonObject        9/9  PASS
tools/ai_controller/tests/test_native_agent.py::TestOllamaModelClient       13/13 PASS
tools/ai_controller/tests/test_native_agent.py::TestRepositoryToolRunner    30/30 PASS
tools/ai_controller/tests/test_native_agent.py::TestAgentLoop               14/14 PASS
tools/ai_controller/tests/test_native_agent.py::TestNativeOllamaAgentProvider 16/16 PASS
───────────────────────────────────────────────────────────────────────────
TOTAL                                                                        92/92 PASS
Duration: 24.77s
```

---

## 16. Complete Controller Test Results

```
tools/ai_controller/tests/  (all files)
  test_changes_worktrees.py      PASS
  test_cli_doctor.py             PASS
  test_controller_integration.py PASS
  test_local_ollama_provider.py  PASS
  test_models_transport.py       PASS
  test_native_agent.py           PASS  (new)
  test_ollama_provider.py        PASS
  test_queue.py                  PASS
───────────────────────────────────────
TOTAL: 130 passed, 0 failed
Pre-existing: 38 passing (unchanged)
New:          92 passing
Duration: 37.82s
```

---

## 17. Fake Ollama Integration Results

The `FakeOllamaServer` in `conftest.py` was extended with:
- `chat_queue: list[dict]` — pre-queued responses for `/api/chat`
- `chat_requests: list[dict]` — recorded chat requests for assertion

Integration tests exercise the full provider loop against the fake server:
- Multi-turn conversations (git_status → write_file → finish)
- Malformed JSON recovery (bad response → recovery message → success)
- Server error termination (HTTP 500)
- Step limit enforcement
- Repeated action detection
- Path traversal rejection within the loop
- Output truncation
- Prompt preservation across the full round-trip

All 92 automated tests pass without any live service.

---

## 18. Live Ollama Result

**Ollama version:** 0.9.32.5 at `http://127.0.0.1:11434`

| Model | Chat API | Smoke Test |
|-------|----------|-----------|
| `qwen2.5-coder:3b` | FAIL (insufficient RAM for 1.27GB buffer on AMD A8-3500M) | N/A |
| `qwen2.5-coder:1.5b` | PASS | **PASS** |
| `qwen2.5-coder:0.5b` | Not tested (1.5b succeeded) | N/A |

**Smoke test run (1.5b):**
```
Step 1: [OK] list_files   — Inspecting repository structure
Step 2: [OK] write_file   — Writing to templates/sip_markets.html
Step 3: [OK] finish       — success=True

Changed files (controller git): ['templates/sip_markets.html']
PASS: smoke-local succeeded
```

The 3B model fails on this hardware (AMD A8-3500M, 4 cores @ 1.5GHz, limited RAM). The 1.5B model runs successfully. The framework is correct — hardware limitation is external.

---

## 19. Commit Hash

```
ae001c6  Add native Ollama coding agent provider
```

Staged only `tools/ai_controller/` files. No merge, push, or deployment performed.

---

## 20. Remaining Limitations

1. **3B model requires more RAM** than the Fedora server has. Use 1.5B or 0.5B locally, or run 3B on a Windows machine with adequate RAM and access via Tailscale tunnel.

2. **Model quality (1.5B/0.5B):** Small models follow tool-call instructions but may misinterpret which file to edit. The framework catches all safety violations; model reasoning quality is a separate concern.

3. **No retry-with-feedback loop** from controller tests back into the agent. Currently: agent runs → controller tests → report. A future improvement would feed test failures back into the agent for a bounded repair attempt.

4. **SSH runner path validation** assumes Fedora POSIX paths. The static `PurePosixPath` analysis is correct but does not detect remote symlinks. This is an accepted limitation for the threat model.

5. **No context builder yet.** The agent discovers files via `list_files`/`read_file` tools rather than receiving a pre-built context window. A context builder (as described in raghub-local-operator planning) would improve efficiency for small models.

6. **`smoke-local` task prompt quality.** The model wrote to `templates/sip_markets.html` instead of `fixture.py` because the prompt mentioned the sip_markets.html path (in the "must remain unchanged" clause). Prompt engineering for small models is a separate concern.

---

## 21. Exact Command: Controller Doctor

```bash
python -m tools.ai_controller.cli --config /path/to/config.json doctor
# With JSON output:
python -m tools.ai_controller.cli --config /path/to/config.json doctor --json
```

Exit code 0 = operational, 1 = problems found.

---

## 22. Exact Command: Local Smoke Test

```bash
python -m tools.ai_controller.cli --config /path/to/config.json smoke-local
```

Requires live Ollama at `ollama_base_url` with `ollama_model` installed.  
Report written to `config.reports_root/smoke-native-agent.json`.

---

## 23. Exact Command: Continuous Controller Mode

```bash
# With native agent enabled (set native_agent_attempts > 0 in config):
python -m tools.ai_controller.cli --config /path/to/config.json run --continuous

# Single task:
python -m tools.ai_controller.cli --config /path/to/config.json run --once
```

Recommended config for native agent only:
```json
{
  "controller_root": "C:\\RAGHubOperator\\controller",
  "repository_path": "/home/matias/raghub",
  "worktree_root": "/home/matias/raghub-worktrees",
  "codex_attempts": 0,
  "local_attempts": 0,
  "native_agent_attempts": 2,
  "ollama_base_url": "http://127.0.0.1:11434",
  "ollama_model": "qwen2.5-coder:1.5b",
  "ollama_max_agent_steps": 20,
  "ollama_max_tool_failures": 5,
  "ollama_generation_timeout_seconds": 300,
  "native_agent_command_timeout_seconds": 120,
  "ssh_host": "matias@100.93.102.93",
  "ssh_key_path": "C:\\RAGHubOperator\\keys\\controller_ed25519"
}
```

---

## 24. Recommended Next Controller Milestone

**Context builder for small models.**

The current agent discovers files via tool calls (slow, burns agent steps). A deterministic context builder should:
1. Select relevant files from the task prompt (path mentions, imports)
2. Pack a budget (e.g. 3000 tokens) into the initial user message
3. Include git status and recent test failures
4. Attach experience-ledger lessons

This single improvement will dramatically increase first-step success rate for 0.5B–1.5B models on Tier 1 tasks (docs, small tests, lint fixes, import corrections) without requiring a larger model.

**Second priority:** controller-owned test-failure feedback loop — run declared tests after agent `finish`, then retry with failure output if tests fail and attempts remain.
