<#
.SYNOPSIS
    Replacement-machine bootstrap script for Jarvis AI OS (Windows).
.DESCRIPTION
    Checks Python, Node, Git, Ollama, Graphify, OpenCode, installs Python dependencies,
    verifies optional encrypted DR bundle restoration, and runs preflight checks.
#>
param(
    [string]$RestoreBundle = "",
    [string]$Passphrase = ""
)

$ErrorActionPreference = "Stop"
Write-Host "=== Jarvis Replacement-Machine Bootstrap ===" -ForegroundColor Cyan

foreach ($cmd in @("python", "git", "node", "npm", "ollama", "opencode", "graphify")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        Write-Host "[OK] Found $cmd" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Missing optional CLI: $cmd" -ForegroundColor Yellow
    }
}

if (!(Test-Path ".venv")) {
    Write-Host "Creating Python virtual environment..."
    python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt

if (Test-Path "apps\frontend\package.json") {
    Push-Location apps\frontend
    npm ci
    npm run build
    Pop-Location
}

if ($RestoreBundle -and $Passphrase) {
    Write-Host "Verifying and restoring encrypted DR secrets & manifest from $RestoreBundle..." -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -c "
from pathlib import Path
from jarvis.config import DATA, SECRETS_PATH, load_settings
from jarvis.db.engine import DatabaseManager
from jarvis.recovery.backup import DisasterRecoveryManager
s = load_settings()
db = DatabaseManager(s.database.url, data_dir=DATA)
db.init_schema_sync()
dr = DisasterRecoveryManager(db, s, data_dir=DATA, secrets_path=SECRETS_PATH)
rep = dr.verify_backup(Path(r'$RestoreBundle'), passphrase=r'$Passphrase')
print('Verify report:', rep)
dr.restore_secrets_from_bundle(Path(r'$RestoreBundle'), passphrase=r'$Passphrase')
print('Secrets restored safely.')
"
}

Write-Host "Running Jarvis preflight verification..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe preflight.py
Write-Host "=== Bootstrap Complete ===" -ForegroundColor Green

