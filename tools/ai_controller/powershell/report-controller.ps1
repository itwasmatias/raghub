param(
  [string]$Config = "C:\RAGHubOperator\config.json",
  [string]$TaskId
)
$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is required and was not found on PATH." }
if ($TaskId) {
  & $python.Source -m tools.ai_controller.cli --config $Config report --task-id $TaskId
} else {
  & $python.Source -m tools.ai_controller.cli --config $Config report
}
exit $LASTEXITCODE
