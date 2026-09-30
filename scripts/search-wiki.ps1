param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$Question,

    [ValidateSet("canonical", "captures", "all")]
    [string]$Scope = "canonical",

    [int]$Limit = 10,

    # Deterministic exact/normalised substring search instead of BM25.
    [switch]$Exact
)

# Model-free. Lexical mode relaxes deterministically and reports the strategy
# used; to rephrase or translate a query, call again with the new wording.
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

if ($Exact) {
    if ($Scope -eq "all") { throw "-Exact supports -Scope canonical or captures." }
    python scripts/search_lexical.py exact "$Question" --scope $Scope -n $Limit
} else {
    python scripts/search_lexical.py search "$Question" --scope $Scope -n $Limit
}
if ($LASTEXITCODE -ne 0) { throw "Search failed." }
