# sync_to_thesis_repo.ps1
# Copies code-only changes from the dev repo to the thesis repo.
# docs/ is intentionally excluded — the thesis repo's docs/Report/ is independent.
#
# Usage:
#   .\scripts\sync_to_thesis_repo.ps1           # dry-run (preview what would change)
#   .\scripts\sync_to_thesis_repo.ps1 -Commit   # actually copy files

param(
    [switch]$Commit
)

$src = "C:\Users\Adrien\Documents\TFE\PhenoHive"
$dst = "C:\Users\Adrien\Documents\TFE\Phenohive_2025-26"

if (-not (Test-Path $dst)) {
    Write-Error "Thesis repo not found at $dst"
    exit 1
}

$roboFlags = @("/E", "/PURGE", "/NJH", "/NJS", "/XD", "__pycache__", "wifi-connect", "/XF", "*.pyc", "*.local.*")
if (-not $Commit) {
    $roboFlags += "/L"   # list-only (dry-run)
    Write-Host "[DRY RUN] Pass -Commit to actually copy files." -ForegroundColor Yellow
    Write-Host ""
}

# Root-level files (no /PURGE — don't delete unrelated root files in thesis repo)
$rootFiles = @(
    "main.py",
    "config.defaults.ini",
    "Dockerfile",
    "docker-compose.yml",
    "conftest.py",
    "requirements.txt",
    "requirements-dev.txt",
    "README.md"
)

Write-Host "==> Root files" -ForegroundColor Cyan
$fileFlags = @("/NJH", "/NJS")
if (-not $Commit) { $fileFlags += "/L" }
robocopy $src $dst @rootFiles @fileFlags

# Directories synced with /PURGE so deletions propagate
$dirs = @("src", "tests", "scripts", "dietpi", "grafana", "infrastructure", "casing")

foreach ($dir in $dirs) {
    Write-Host "==> $dir\" -ForegroundColor Cyan
    robocopy "$src\$dir" "$dst\$dir" @roboFlags
}

Write-Host ""
if ($Commit) {
    Write-Host "Done. Review changes in $dst before committing." -ForegroundColor Green
} else {
    Write-Host "Dry run complete. Run with -Commit to apply." -ForegroundColor Yellow
}
