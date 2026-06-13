<#
.SYNOPSIS
  JARVIS installer (Windows / PowerShell).
.DESCRIPTION
  Creates a .venv, verifies Python >= 3.11, installs a dependency profile,
  and seeds .env from .env.example. Run from the jarvis/ directory.
.PARAMETER Profile
  One of: base, voice, docs, soc, lab, dev, all. Default: base.
.EXAMPLE
  ./scripts/install.ps1
  ./scripts/install.ps1 -Profile all
#>
[CmdletBinding()]
param(
    [ValidateSet("base", "voice", "docs", "soc", "lab", "dev", "all")]
    [string]$Profile = "base"
)

$ErrorActionPreference = "Stop"
$JarvisDir = Split-Path -Parent $PSScriptRoot
Set-Location $JarvisDir

Write-Host "==> JARVIS installer (profile: $Profile)" -ForegroundColor Cyan

# 1. Python version check
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { Write-Error "Python not found on PATH. Install Python 3.11+."; exit 1 }
$ver = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])").Trim()
$maj, $min = $ver.Split(".")
if ([int]$maj -lt 3 -or ([int]$maj -eq 3 -and [int]$min -lt 11)) {
    Write-Error "Python $ver found, but 3.11+ is required."; exit 1
}
Write-Host "    Python $ver OK" -ForegroundColor Green

# 2. Virtualenv
if (-not (Test-Path ".venv")) {
    Write-Host "    Creating .venv ..." -ForegroundColor Cyan
    & python -m venv .venv
}
$venvPy = Join-Path $JarvisDir ".venv\Scripts\python.exe"

# 3. Install profile
$req = "requirements\$Profile.txt"
if (-not (Test-Path $req)) { Write-Error "Profile file not found: $req"; exit 1 }
Write-Host "    Installing $req ..." -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r $req

# 4. Seed .env
if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "    Created .env from .env.example" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Cyan
Write-Host "  1. .\.venv\Scripts\Activate.ps1"
Write-Host "  2. python scripts/doctor.py"
Write-Host "  3. ollama serve   (then: python scripts/model_doctor.py)"
Write-Host "  4. python main.py"
