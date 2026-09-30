param(
    [switch]$SkipInstall
)

# Model-free lexical QMD collections. The registration logic lives in
# scripts/search_lexical.py (shared with the Linux helpers) and is driven by
# 00-system/configuration/qmd-collections.json. `qmd embed` is never run.
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Has-Command($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Has-Command "qmd")) {
    if ($SkipInstall) {
        throw "QMD is not installed or not visible in PATH."
    }
    if (-not (Has-Command "npm")) {
        throw "npm is required to install QMD. Install Node.js 22 or newer, restart Claude Desktop or PowerShell, and run again."
    }
    npm install -g @tobilu/qmd
    if ($LASTEXITCODE -ne 0) { throw "QMD installation failed." }
}

python scripts/search_lexical.py configure
if ($LASTEXITCODE -ne 0) { throw "Lexical search configuration failed." }

Write-Host ""
Write-Host "MODEL-FREE LEXICAL QMD COLLECTIONS CONFIGURED" -ForegroundColor Green
