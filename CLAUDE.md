# Machine role (Fedora HP Pavilion)

- This machine: Claude Code handles programming and development.
- Claude.ai chat: planning and idea review only — not programming.
- Windows HP 14: ChatGPT, Codex, and GitHub Copilot handle programming there.
- Planning: compare approaches using both Claude.ai chat and ChatGPT.
- Isolation rule: this machine's repositories, branches, worktrees, commands,
  and assigned features stay separate from the Windows HP 14's setup, unless
  a handoff is deliberately prepared. Never assume work from the other
  machine is visible or mergeable here without an explicit handoff step.

# This worktree

- Path: /home/matias/raghub-controller-mission-orchestrator-v1
- Branch: feature/controller-mission-orchestrator-v1
- Must not modify: /home/matias/raghub, /home/matias/raghub-local-operator,
  /home/matias/raghub-ai-controller, /home/matias/raghub-controller-claude-native-ollama
- Must not: merge, push, deploy, force-reset, git clean unrelated files,
  delete worktrees, alter credentials, edit git remotes.
- Only controller-owned files get committed.

# Local environment quirk — read before touching the claude install

- This CPU has zero AVX support. Native `claude` builds (Bun runtime,
  2.0.15+) crash instantly with "Illegal instruction."
- Fix in place: ~/.local/bin/claude symlinks to ~/.local/npm/bin/claude,
  pinned at v2.0.14 (last pre-Bun release), installed via npm.
- Old broken native binary kept, not deleted, at:
  ~/.local/bin/claude-native-broken-avx
- DO NOT run `claude update` or `npm update -g @anthropic-ai/claude-code` —
  either will likely reintroduce the Bun/AVX crash.
- Consequence: no /effort or model/effortLevel settings on this version.
  Model is fixed at Sonnet 4.5.

# Current task: Mission Orchestrator v1

- Full directive: MISSION_ORCHESTRATOR_V1_PROMPT.md (this directory)
- Hand it to Claude Code with: @MISSION_ORCHESTRATOR_V1_PROMPT.md
- Report goes to: MISSION_ORCHESTRATOR_REPORT.md (this directory)
- Commit message on completion: "Add durable mission orchestration to AI controller"
