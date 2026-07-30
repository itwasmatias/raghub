param([int]$ProcessId)
$ErrorActionPreference = "Stop"
if ($ProcessId) { Stop-Process -Id $ProcessId -ErrorAction Stop; Write-Host "Stopped controller process $ProcessId" }
else { Write-Host "Pass -ProcessId for a graceful stop; the controller observes process termination between tasks." }
