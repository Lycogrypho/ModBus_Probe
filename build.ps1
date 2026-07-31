#Requires -Version 5.1
<#
.SYNOPSIS
    Build ModBus Probe Windows executables with PyInstaller.

.DESCRIPTION
    Compiles modbus_logger.py and modbus_logger_async.py into standalone .exe files.
    Output is placed in .\dist\

.PARAMETER Clean
    Remove build\ and dist\ before compiling (default: $false).

.PARAMETER AsyncOnly
    Build only the async logger.

.PARAMETER SyncOnly
    Build only the sync logger.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Clean
    .\build.ps1 -SyncOnly -Clean
#>
param(
    [switch]$Clean,
    [switch]$AsyncOnly,
    [switch]$SyncOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg) {
    Write-Host "    OK  $msg" -ForegroundColor Green
}

function Write-Err([string]$msg) {
    Write-Host "    ERR $msg" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# Ensure PyInstaller
# ---------------------------------------------------------------------------

Write-Step "Checking PyInstaller"
# When a venv is active its Scripts\ folder is prepended to PATH, so "python"
# resolves to the venv interpreter.  Outside a venv we fall back to "py" (the
# Windows Python Launcher) so the script also works without an active venv.
$PY = if ($env:VIRTUAL_ENV) { "python" } else { "py" }
$pyExe = (Get-Command $PY -ErrorAction SilentlyContinue).Source
if (-not $pyExe) { Write-Err "$PY not found on PATH"; exit 1 }
$venvLabel = if ($env:VIRTUAL_ENV) { " (venv: $env:VIRTUAL_ENV)" } else { " (system Python)" }
Write-Host "    Using: $pyExe$venvLabel" -ForegroundColor Gray
& $PY -m pip show pyinstaller 2>$null | Out-Null
if (-not $?) {
    Write-Host "    PyInstaller not found - installing..." -ForegroundColor Yellow
    & $PY -m pip install pyinstaller
    if (-not $?) { Write-Err "pip install pyinstaller failed"; exit 1 }
}
Write-Ok "PyInstaller available"

# ---------------------------------------------------------------------------
# Optional clean
# ---------------------------------------------------------------------------

if ($Clean) {
    Write-Step "Cleaning build artefacts"
    foreach ($dir in "build", "dist") {
        if (Test-Path $dir) {
            Remove-Item $dir -Recurse -Force
            Write-Ok "Removed $dir\"
        }
    }
    Get-ChildItem -Filter "*.spec" -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Ok "Removed $($_.Name)"
    }
}

# ---------------------------------------------------------------------------
# Shared PyInstaller options
# ---------------------------------------------------------------------------

$CommonArgs = @(
    "--onefile",
    "--console",
    "--hidden-import=sqlite3",
    "--collect-submodules=pymodbus"
)

# ---------------------------------------------------------------------------
# Build function
# ---------------------------------------------------------------------------

function Invoke-Build([string]$Script) {
    Write-Step "Building $Script"
    & $PY -m PyInstaller @CommonArgs $Script
    if ($LASTEXITCODE -ne 0) {
        Write-Err "PyInstaller failed for $Script (exit $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
    $exeName = [System.IO.Path]::GetFileNameWithoutExtension($Script) + ".exe"
    $exe = Join-Path "dist" $exeName
    if (Test-Path $exe) {
        $sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
        Write-Ok "$exe  ($sizeMB MB)"
    }
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

if (-not $AsyncOnly) { Invoke-Build "modbus_logger.py" }
if (-not $SyncOnly)  { Invoke-Build "modbus_logger_async.py" }

# ---------------------------------------------------------------------------
# Drop config template in dist\
# ---------------------------------------------------------------------------

Write-Step "Copying config template"
if (Test-Path "example_config.json") {
    Copy-Item "example_config.json" (Join-Path "dist" "example_config.json") -Force
    Write-Ok "dist\example_config.json"
} else {
    Write-Host "    (example_config.json not found - skipped)" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Build complete. Executables are in .\dist\" -ForegroundColor Green
Write-Host "Deploy the .exe alongside config.json (copy from example_config.json and edit)." -ForegroundColor Gray
