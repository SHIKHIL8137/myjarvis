<#
.SYNOPSIS
    Replacement-machine bootstrap & disaster recovery script for Jarvis AI OS (Windows).
.DESCRIPTION
    1. Checks prerequisites (Python, Node, Git, Ollama, Graphify, OpenCode, optional PostgreSQL tools).
    2. Sets up Python `.venv` and offline React frontend dist bundle.
    3. Verifies and restores encrypted DR bundle (`secrets`, `settings.json`, DB/SQLite or PG, memory, notes, documents).
    4. Runs Alembic migrations, rebuilds Graphify project graphs when registered, and runs `preflight.py`.
#>
param(
    [string]$RestoreBundle = "",
    [string]$Passphrase = "",
    [string]$PostgresUrl = ""
)

$ErrorActionPreference = "Stop"
Write-Host "=== Jarvis Replacement-Machine Bootstrap & Recovery ===" -ForegroundColor Cyan

foreach ($cmd in @("python", "git", "node", "npm", "ollama", "opencode", "graphify", "pg_dump", "pg_restore")) {
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

if ($PostgresUrl) {
    $env:JARVIS_DATABASE_URL = $PostgresUrl
    .\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
}

if ($RestoreBundle -and $Passphrase) {
    Write-Host "Running isolated DR verify + full restore from $RestoreBundle..." -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -c "
from pathlib import Path
from jarvis.config import DATA, SECRETS_PATH, ROOT, load_settings
from jarvis.db.engine import DatabaseManager
from jarvis.recovery.backup import DisasterRecoveryManager

s = load_settings()
db = DatabaseManager(r'$PostgresUrl' or s.database.url, data_dir=DATA)
db.init_schema_sync()
dr = DisasterRecoveryManager(db, s, data_dir=DATA, secrets_path=SECRETS_PATH)
res = dr.restore_to_directory(
    Path(r'$RestoreBundle'),
    passphrase=r'$Passphrase',
    target_data_dir=DATA,
    target_notes_dir=ROOT / 'notes',
    target_pg_url=r'$PostgresUrl' or None,
)
print('Recovery Report:', res)
db.sync_engine.dispose()
"
}

Write-Host "Running Jarvis preflight verification..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe preflight.py
Write-Host "=== Bootstrap & Recovery Complete ===" -ForegroundColor Green
