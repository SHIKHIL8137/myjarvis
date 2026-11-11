# Offline verification. Turn Wi-Fi OFF first (taskbar > network > Wi-Fi off, or Airplane mode), then run: .\offline-test.ps1
# It refuses to run while the internet is reachable, so a pass really means "worked with the network down".
Set-Location $PSScriptRoot
$online = $false
foreach ($h in "1.1.1.1", "8.8.8.8") { if (Test-Connection $h -Count 1 -Quiet -ErrorAction SilentlyContinue) { $online = $true } }
if ($online) { Write-Host "The internet is still reachable. Turn Wi-Fi off (and disconnect Ethernet/Tailscale), then run this again." -ForegroundColor Yellow; exit 2 }
Write-Host "Network is down. Restarting Jarvis and running the live preflight + drill offline..."
.\stop-jarvis.ps1 | Out-Null
.\start-jarvis.ps1 -NoBrowser
if ($LASTEXITCODE -ne 0) { exit 1 }
New-Item -ItemType Directory -Force logs | Out-Null
& .\.venv\Scripts\python.exe preflight.py 2>&1 | Tee-Object -FilePath logs\offline-test.txt
$code = $LASTEXITCODE
if ($code -eq 0) { Write-Host "`nOFFLINE TEST PASSED (output saved to logs\offline-test.txt). Open http://127.0.0.1:4700/ and talk to Jarvis." -ForegroundColor Green }
else { Write-Host "`nOFFLINE TEST FAILED - see logs\offline-test.txt" -ForegroundColor Red }
exit $code
