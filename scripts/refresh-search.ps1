param(
    [switch]$Force,
    # Opt-in only. Downloads and runs an embedding model. NOT part of the
    # default path; lexical search and the exact fallback need no embeddings.
    [switch]$WithEmbeddings
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

python scripts/search_lexical.py refresh
if ($LASTEXITCODE -ne 0) { throw "Search index refresh failed." }

if ($WithEmbeddings) {
    Write-Host "WARNING: -WithEmbeddings downloads a model. Outside the model-free default." -ForegroundColor Yellow
    if ($Force) {
        & qmd embed -f | Out-Host
    } else {
        & qmd embed | Out-Host
    }
    if ($LASTEXITCODE -ne 0) { throw "QMD embedding failed." }
}

python scripts/search_lexical.py status
if ($LASTEXITCODE -ne 0) { throw "Search status failed." }

python scripts/validate_repo.py --full
if ($LASTEXITCODE -ne 0) { exit 1 }

python scripts/validate_content_release.py
if ($LASTEXITCODE -ne 0) { exit 2 }

Write-Host "SEARCH REFRESH PASS" -ForegroundColor Green
