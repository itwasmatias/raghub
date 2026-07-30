param(
  [string]$Config = "C:\RAGHubOperator\config.json",
  [switch]$Continuous,
  [switch]$Init
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Config)) { throw "Controller config not found: $Config" }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is required and was not found on PATH." }
if ($Init) { & $python.Source -m tools.ai_controller.cli --config $Config init; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; exit 0 }
$mode = if ($Continuous) { "--continuous" } else { "--once" }
& $python.Source -m tools.ai_controller.cli --config $Config run $mode
exit $LASTEXITCODE
