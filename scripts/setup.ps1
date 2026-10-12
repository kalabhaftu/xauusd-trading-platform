$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
if (Test-Path -LiteralPath 'requirements.lock') {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
    & '.\.venv\Scripts\python.exe' -m pip install --no-build-isolation --no-deps -e .
} else {
    & '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
}
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
Write-Output 'Configure the intended DEMO or LIVE account locally in .env, then run python -m app.main doctor. Automatic orders remain off until authorized.'
