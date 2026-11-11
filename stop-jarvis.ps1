# Stop Jarvis (the backend and its venv launcher). Leaves Ollama and its models alone.
$port = (Get-Content "$PSScriptRoot\config.json" -Raw | ConvertFrom-Json).port
$ids = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
if (Test-Path "$PSScriptRoot\data\server.pid") { $ids += [int](Get-Content "$PSScriptRoot\data\server.pid") }
$stopped = 0
foreach ($id in ($ids | Select-Object -Unique)) { if (Get-Process -Id $id -ErrorAction SilentlyContinue) { taskkill /PID $id /T /F | Out-Null; $stopped++ } }
Remove-Item "$PSScriptRoot\data\server.pid" -ErrorAction SilentlyContinue
if ($stopped) { Write-Host "Jarvis stopped." } else { Write-Host "Jarvis was not running." }
