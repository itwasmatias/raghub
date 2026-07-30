param([string]$Config = "C:\RAGHubOperator\config.json")
$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is required and was not found on PATH." }
& $python.Source -m tools.ai_controller.cli --config $Config status
exit $LASTEXITCODE
