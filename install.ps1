# Datawright setup for Windows. Run from the repository folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"

$python = $null
foreach ($v in "3.13", "3.12", "3.11") {
    try {
        $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { $python = $exe; break }
    } catch { }
}
if (-not $python) {
    Write-Host "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}
Write-Host "Using $python"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git was not found. Install it from https://git-scm.com/download/win, then run this again."
    exit 1
}

& $python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env - open it and paste your ANTHROPIC_API_KEY (optional)."
}

& .\.venv\Scripts\datawright.exe check ".\fixtures\pbip\retail-issues"
Write-Host ""
Write-Host "Installed. Try:"
Write-Host "  .\.venv\Scripts\datawright demo --offline"
Write-Host "  .\.venv\Scripts\datawright serve      (then open http://127.0.0.1:8765)"
