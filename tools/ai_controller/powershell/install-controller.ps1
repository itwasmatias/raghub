param([string]$Root = "C:\RAGHubOperator")
$ErrorActionPreference = "Stop"
$dirs = @(
  $Root,
  "$Root\queue",
  "$Root\queue\pending",
  "$Root\queue\running",
  "$Root\queue\succeeded",
  "$Root\queue\failed",
  "$Root\queue\invalid",
  "$Root\reports",
  "$Root\logs"
)
foreach ($dir in $dirs) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Install Python 3.11+ before running the controller." }
Write-Host "Controller directories ready at $Root."
Write-Host "Copy tools/ai_controller/config.example.json to $Root\config.json and edit the Fedora and Ollama paths."
