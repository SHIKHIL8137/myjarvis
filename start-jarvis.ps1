# Start Jarvis (backend + UI) and open the browser. Usage: .\start-jarvis.ps1 [-NoBrowser]
param([switch]$NoBrowser)
$root = $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force data, logs | Out-Null
$port = (Get-Content config.json -Raw | ConvertFrom-Json).port
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { Write-Host "Jarvis already running on port $port"; if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }; exit 0 }
try { Invoke-RestMethod http://localhost:11434/api/version -TimeoutSec 3 | Out-Null } catch {
  Write-Host "Ollama is not running - starting it..."; Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 4 }
$p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "server.py" -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$root\logs\server.out" -RedirectStandardError "$root\logs\server.err"
$p.Id | Set-Content data\server.pid
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 500
  try { $h = Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 2; Write-Host "Jarvis is up: http://127.0.0.1:$port/  (Ollama $($h.ollama.version), $($h.notes) notes)"
        if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }; exit 0 } catch {}
}
Write-Host "Jarvis did not start. See logs\server.err"; Get-Content "$root\logs\server.err" -Tail 15; exit 1
