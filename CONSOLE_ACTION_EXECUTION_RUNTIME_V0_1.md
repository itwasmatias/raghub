# Console Action Execution Runtime v0.1

## Overview

The Console Action Execution Runtime v0.1 provides a narrow, governed execution layer for the RAGHub Console Server. It is **NOT** a general shell and executes only explicitly allowlisted Console Action types with strict security controls.

## Scope

### In Scope for v0.1

- Execution of `inspect_git_status` action (Tier 1, no approval required)
- Fixed argv execution model (no arbitrary commands)
- Execution fingerprint authentication
- Assignment and dispatch binding verification
- Cross-process idempotency protection (H1)
- Bounded stdout/stderr capture (M2)
- Workspace TOCTOU protection via FD anchoring (M1)
- Fail-closed execution fingerprint verification (H3)
- Durable job state with atomic persistence
- Sensitive output scrubbing

### Out of Scope for v0.1

- Approval Center integration (separate milestone)
- AI Work Bridge
- Model/provider calls (OpenAI, Claude, Ollama)
- Autonomous coding agents
- Arbitrary shell or PowerShell execution
- Network worker transport
- Queue/broker infrastructure
- UI components
- Unrelated features (research runtime, sports, etc.)
- Deployment system
- Automatic git commit/push

## Architecture

### Allowlisted Action Model

The runtime uses an **allowlist model** rather than accepting arbitrary commands:

1. **Action Catalog** (`actions.py`): Defines fixed specifications for each action type
2. **Server Determines Execution**: The caller selects an action type and parameters; the server determines the actual `argv`
3. **No Client Command Injection**: Clients cannot specify:
   - `executable`, `argv`, `command`, `shell`
   - `env`, `environment`
   - `cwd`, `path`, `relative_path`
   - Arbitrary timeout overrides beyond policy
   - Arbitrary output limits
   - Arbitrary process-control parameters

### Execution Fingerprint

Every governed execution request has a canonical **execution fingerprint** (SHA-256 digest):

```python
execution_fingerprint(
    action_type=action_type,
    workspace_id=workspace_id,
    timeout_seconds=timeout_seconds,
    immutable_parameters=immutable_parameters,
)
```

The fingerprint binds:
- Action type
- Workspace identity
- Timeout
- Immutable action-specific parameters (e.g., `test_target` for test actions)

**H3 Security Requirement**: The runtime FAILS CLOSED on fingerprint verification:
- Missing job execution fingerprint → **reject**
- Missing authoritative dispatch fingerprint → **reject**
- Mismatch between job and offer → **reject**
- Malformed fingerprint (not 64-char lowercase hex) → **reject**
- Recomputed fingerprint mismatch → **reject**
- Composed/foreign dispatch evidence → **reject**

No governed action may execute without valid execution authority.

### Assignment and Dispatch Binding

Each job is linked to authoritative routing and dispatch evidence:

```
TaskRequest
  → Assignment (authenticated with integrity key)
    → Dispatch Offer (authenticated with integrity key)
      → Job (execution bound to offer)
```

The execution fingerprint is carried in `authorization_metadata`:
- Assignment metadata: `{"level": "...", "execution_fingerprint": "..."}`
- Dispatch offer metadata: `{"execution_fingerprint": "..."}`

Runtime verifies:
- `job.dispatch_offer_id` matches authoritative offer
- `offer.assignment_id` matches job
- `offer.task_id` matches job
- `offer.mission_id` matches job
- `offer.worker_node_id` matches job target
- Offer fingerprint matches job fingerprint
- Recomputed fingerprint matches job fingerprint

### Cross-Process Idempotency (H1)

Idempotency is enforced across processes using:

1. **Durable Transaction** (`jobs.py:transaction()`):
   - Uses `fcntl.flock()` for cross-process locking
   - Covers duplicate check, routing, offer creation, and job creation
   - Prevents duplicate side effects

2. **Execution Lock** (`jobs.py:execution_lock()`):
   - Per-job cross-process lock
   - Non-blocking with fallback to wait-and-poll
   - Ensures at most one execution per job

**Behavior**:
- Exact duplicate request (same idempotency key + same fingerprint) → reuse existing job
- Conflicting request (same key + different fingerprint) → reject with 409
- Concurrent duplicate submissions → create once, execute once

### Workspace FD Anchoring (M1)

To prevent TOCTOU attacks where an attacker replaces the workspace path after validation:

```
1. Validate workspace and resolve canonical path
2. Open workspace with O_DIRECTORY to obtain FD
3. Verify FD stat matches path stat (prevents race)
4. Execute with cwd=/proc/self/fd/{workspace_fd}
5. Pass workspace_fd to child via pass_fds
6. Close FD in finally block
```

The FD-anchored path ensures the child process operates on the validated directory, even if an attacker renames or symlinks the original path during execution.

**Tests**: `test_workspace_identity_is_anchored_during_runner_launch`

### Bounded Output (M2)

The `_run_bounded()` function prevents unbounded memory growth from large subprocess output:

1. **Concurrent Draining**: Uses `select()` to drain stdout/stderr simultaneously
2. **Independent Bounds**: Each stream bounded separately (prevents one stream from blocking the other)
3. **Fixed Memory**: Bounded at `max_output_bytes` per stream (default 100KB for `inspect_git_status`)
4. **Truncation Tracking**: Preserves truncation flags (`stdout_truncated`, `stderr_truncated`)
5. **Timeout Enforcement**: Kills process if timeout exceeded
6. **Proper Cleanup**: Reaps child process with `wait()`

Unlike `subprocess.run(capture_output=True)`, which can buffer unlimited output in memory, `_run_bounded()` enforces a strict bound during execution.

**Tests**: `test_default_runner_drains_large_stdout_and_stderr_with_fixed_memory_bound`

### Durable Job States

Jobs transition through well-defined states:

- **PENDING**: Created, not yet started (or awaiting approval for Tier 2)
- **RUNNING**: Currently executing
- **SUCCEEDED**: Completed successfully
- **FAILED**: Failed with error
- **TIMEOUT**: Exceeded timeout
- **CANCELLED**: Cancelled before completion
- **RECONCILIATION_REQUIRED**: Outcome is ambiguous (e.g., crash during execution)

**Terminal States**: `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `RECONCILIATION_REQUIRED`

**Invariants**:
- Terminal jobs cannot be automatically rerun
- Failed commands are not silently retried
- Interrupted RUNNING state survives restart and requires reconciliation
- Pre-existing RUNNING job is not automatically retried (fails with RECONCILIATION_REQUIRED)

### Reconciliation Behavior

If execution is interrupted (e.g., `KeyboardInterrupt`, crash) while RUNNING:
1. Job is marked `RECONCILIATION_REQUIRED`
2. Subsequent `execute()` call does **not** retry automatically
3. Manual intervention required to determine if action completed

This prevents duplicate execution of non-idempotent operations.

### Approval Boundary

**Tier 1 Actions** (e.g., `inspect_git_status`):
- No approval required
- Executed immediately if dispatch offer exists

**Tier 2 Actions** (e.g., `run_test_target`, `run_repository_tests`):
- `approval_required=True`
- Remain in `PENDING` state
- Runtime rejects execution without valid approval evidence
- Full Approval Center integration is out of scope for v0.1

The runtime **preserves** the existing approval requirement contract without implementing approval workflows.

## Security Invariants

The runtime FAILS CLOSED on:

- Unknown or non-allowlisted action type
- Approval-required action without valid approval evidence
- Malformed authorization or approval metadata
- **Missing execution fingerprint** (H3)
- **Mismatched execution fingerprint** (H3)
- Mutated job parameters
- Composed or foreign dispatch offer
- Mismatched assignment, mission, task, or worker identity
- Invalid workspace
- **Workspace retargeting** (M1 TOCTOU protection)
- Workspace symlink/junction/path escape
- Corrupted durable job state
- Interrupted/ambiguous execution
- Duplicate/conflicting idempotency use

## Process Execution Security

### Fixed Execution Parameters

- **argv**: Fixed by action specification (e.g., `["git", "status", "--short"]`)
- **shell**: Always `False` (no shell expansion)
- **cwd**: FD-anchored workspace (M1)
- **timeout**: Bounded by action policy (max 30s for `inspect_git_status`)
- **max_output_bytes**: Bounded by action policy (100KB for `inspect_git_status`)
- **pass_fds**: Workspace FD only
- **env**: Not manipulated by client

### Output Scrubbing

All stdout/stderr is scrubbed for sensitive patterns before persistence:
- API keys (`API_KEY=...`, `OPENAI_API_KEY=...`)
- Bearer tokens (`Bearer ...`)
- Passwords (`PASSWORD=...`)
- Secrets (`SECRET=...`)

Sensitive values are replaced with `[REDACTED]`.

## Limitations

### Process Tree Termination

The current timeout implementation terminates only the direct child process. If the child spawns descendants, they may escape timeout termination. This is within the REVISE requirements for v0.1 but may be addressed in future versions with process group isolation.

### Reconciliation Contract

The runtime does not provide automatic reconciliation for interrupted executions. Manual intervention is required to determine the actual outcome of ambiguous executions.

### Immutable Parameters (Future)

The v0.1 runtime only executes `inspect_git_status`, which has no immutable action-specific parameters. Future versions supporting actions like `run_test_target` (with `test_target` parameter) must:
1. Persist immutable parameters in durable job state
2. Reconstruct immutable parameters for fingerprint recomputation
3. Verify parameter integrity before execution

See `runtime.py:240-245` for implementation guidance.

## Testing

The implementation includes focused regression tests for:

1. Fixed argv execution and dispatch linkage
2. Execution-shaping field rejection
3. Non-allowlisted action rejection
4. Approval-required action boundary
5. Bounded stdout/stderr with independent truncation
6. Sensitive output scrubbing
7. Command failure terminal state
8. Interrupted execution reconciliation
9. Pre-existing RUNNING job reconciliation
10. Exact idempotent replay (execute once)
11. Conflicting idempotency rejection
12. Concurrent duplicate request handling (threads)
13. Concurrent execute call handling (claim once)
14. Cross-process duplicate submission (execute once)
15. Corrupt job persistence fail-closed
16. Failed atomic replacement preserves prior state
17. **H3**: Missing execution fingerprint rejection
18. **H3**: Malformed execution fingerprint rejection
19. **H3**: Missing offer fingerprint rejection
20. **H3**: Workspace job tampering rejection
21. **H3**: Composed foreign offer rejection
22. **M1**: Workspace FD anchoring during retargeting
23. **M1**: Invalid/replaced workspace rejection
24. **M2**: Large output drains without deadlock

## Files Modified

### Core Implementation
- `tools/ai_controller/operations_api/console_server/runtime.py`: Execution runtime with M1, M2, H3
- `tools/ai_controller/operations_api/console_server/jobs.py`: Job tracker with H1 cross-process locking
- `tools/ai_controller/operations_api/console_server/actions.py`: Action catalog and fingerprint
- `tools/ai_controller/operations_api/console_server/app.py`: Console Server with idempotency
- `tools/ai_controller/operations_api/serialization.py`: Sensitive output scrubbing

### Federation Integration
- `federation/assignment_registry.py`: Extended metadata for execution fingerprint
- `federation/task_dispatcher.py`: Extended metadata for execution fingerprint

### Tests
- `tests/test_console_action_execution_runtime.py`: Focused regression suite (27 tests)

## Deployment Considerations

### Storage Requirements
- Job storage: `{job_storage_path}/job-{uuid}.json` (one file per job)
- Assignment storage: `{job_storage_path}/../assignments.jsonl` (append-only log)
- Dispatch storage: `{job_storage_path}/../dispatch.jsonl` (append-only log)
- Locks: `{job_storage_path}/.jobs.lock`, `{job_storage_path}/.{job_id}.execution.lock`

### Integrity Key

The Console Server requires a 32-byte integrity key for assignment/dispatch authentication. This key must be:
- Randomly generated (e.g., `secrets.token_bytes(32)`)
- Shared between Console Server and federation components
- Protected at rest

### Workspace Registration

Workspaces must be pre-registered with canonical paths. The runtime will reject:
- Symlinks
- Relative paths
- Paths that change canonical identity after registration

## Future Work (Out of Scope for v0.1)

- Approval Center v0.1 integration
- Additional Tier 1 actions (`inspect_git_log`, `inspect_runtime_status`)
- Additional Tier 2 actions (`run_test_target`, `run_repository_tests`)
- Process group isolation for complete subprocess tree termination
- Automatic reconciliation for common interrupted execution scenarios
- Immutable parameter persistence and reconstruction for test actions
- Network-based worker execution (currently local-only)
- Advanced output filtering and transformation
- Execution metrics and observability

## References

- Task Dispatch v0.1: `TASK_DISPATCH_V0_1_REPORT.md`
- Controller Operations Room API v0.1: `CONTROLLER_OPERATIONS_ROOM_API_V0_1_REPORT.md`
- Federation contracts: `federation/task_router.py`, `federation/task_dispatcher.py`
