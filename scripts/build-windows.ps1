<#
.SYNOPSIS
  Build the JARVIS Windows distribution: frontend -> PyInstaller (dist\jarvis\) -> smoke test -> optional Inno Setup installer.
.DESCRIPTION
  Run from anywhere; paths are resolved from this script's location. Nothing is downloaded or installed implicitly:
  if PyInstaller is missing in .venv the script stops and tells you the exact command.
  AI models are NOT bundled (see INSTALLATION.md).
.PARAMETER SkipFrontend  Reuse apps\frontend\dist as-is.
.PARAMETER SkipSmoke     Do not launch the packaged app to check /api/health.
.PARAMETER RunTests      Run the full pytest suite first and abort on failure.
#>
param([switch]$SkipFrontend, [switch]$SkipSmoke, [switch]$RunTests)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No .venv found. Create it first (see INSTALLATION.md, developer install)." }

Write-Host "=== JARVIS Windows build ===" -ForegroundColor Cyan

if ($RunTests) {
    Write-Host "[1/5] pytest" -ForegroundColor Yellow
    & $Py -m pytest -q -m "not live and not mic and not integration and not pg"
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - aborting build" }
}

if (-not $SkipFrontend) {
    Write-Host "[2/5] frontend build" -ForegroundColor Yellow
    Push-Location (Join-Path $Root "apps\frontend")
    try {
        if (-not (Test-Path "node_modules")) { npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed" } }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $Root "apps\frontend\dist\index.html"))) { throw "apps\frontend\dist\index.html missing - run without -SkipFrontend" }

Write-Host "[3/5] PyInstaller" -ForegroundColor Yellow
& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller is not installed in .venv. Install it explicitly:  .\.venv\Scripts\python.exe -m pip install pyinstaller" }
& $Py -m PyInstaller (Join-Path $Root "jarvis_desktop.spec") --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$Exe = Join-Path $Root "dist\jarvis\jarvis.exe"
if (-not (Test-Path $Exe)) { throw "dist\jarvis\jarvis.exe was not produced" }
Copy-Item (Join-Path $Root "INSTALLATION.md") (Join-Path $Root "dist\jarvis\INSTALLATION.md") -Force

if (-not $SkipSmoke) {
    Write-Host "[4/5] smoke test (launch packaged app, check /api/health, exit cleanly)" -ForegroundColor Yellow
    & $Py (Join-Path $Root "scripts\smoke_test.py") --exe $Exe
    if ($LASTEXITCODE -ne 0) { throw "Smoke test failed" }
}

Write-Host "[5/5] installer (optional)" -ForegroundColor Yellow
$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $Iscc = $p; break }
    }
}
if ($Iscc) {
    & $Iscc (Join-Path $Root "scripts\setup.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer: dist\JARVIS-Setup-*.exe" -ForegroundColor Green
} else {
    Write-Warning "Inno Setup 6 not found - skipping installer. The portable folder dist\jarvis\ is ready to zip."
}
Write-Host "=== Build complete: dist\jarvis\jarvis.exe ===" -ForegroundColor Green
