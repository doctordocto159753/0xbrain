$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

Write-Host "1. Source and repository validation"
python scripts/validate_repo.py --full
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "2. Content release validation"
python scripts/validate_content_release.py
if ($LASTEXITCODE -ne 0) { exit 2 }

Write-Host "3. QMD status and collection configuration"
qmd status
if ($LASTEXITCODE -ne 0) { exit 3 }

$Collections = (qmd collection list 2>&1 | Out-String)
$Required = @(
    "wiki", "wiki-source-records", "wiki-derivatives", "wiki-captures"
)
foreach ($Name in $Required) {
    if ($Collections -notmatch [regex]::Escape($Name)) {
        Write-Error "Missing QMD collection: $Name"
        exit 4
    }
}

Write-Host "4. Exact canonical test (deterministic, no index)"
python scripts/search_lexical.py exact "relation loss" --scope canonical -n 5
if ($LASTEXITCODE -ne 0) { exit 5 }

Write-Host "5. Lexical canonical test (BM25 via QMD, model-free)"
python scripts/search_lexical.py search "relation loss" --scope canonical -n 8
if ($LASTEXITCODE -ne 0) { exit 6 }

Write-Host "6. Index freshness"
python scripts/search_lexical.py status
if ($LASTEXITCODE -ne 0) { exit 7 }

Write-Host "INSTALLATION VERIFICATION PASS"
