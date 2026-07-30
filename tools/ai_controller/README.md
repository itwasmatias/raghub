# RAGHub AI Controller

Run from the repository root with Python 3.11+:

```powershell
python -m tools.ai_controller.cli --config C:\RAGHubOperator\config.json init
python -m tools.ai_controller.cli --config C:\RAGHubOperator\config.json doctor
python -m tools.ai_controller.cli --config C:\RAGHubOperator\config.json enqueue task.json
python -m tools.ai_controller.cli --config C:\RAGHubOperator\config.json run --once
python -m tools.ai_controller.cli --config C:\RAGHubOperator\config.json report
```

The controller claims JSON tasks atomically, creates a dedicated Git worktree and `controller/<task-id>` branch, invokes Codex over SSH first, falls back to a native Ollama HTTP adapter, runs declared tests, and records structured reports plus an experience ledger. Failed worktrees remain available for inspection. No merge, push, deployment, or primary-checkout mutation is performed.
