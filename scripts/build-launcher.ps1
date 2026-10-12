$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Run scripts/setup.ps1 first so the project virtual environment is installed.'
}
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install pyinstaller==6.22.3
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed; see pip output.' }
}
$BuildDirectory = Join-Path $ProjectRoot '.build\pyinstaller'
New-Item -ItemType Directory -Force -Path $BuildDirectory | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name AurumLauncher `
    --distpath $ProjectRoot --workpath $BuildDirectory --specpath $BuildDirectory `
    (Join-Path $PSScriptRoot 'launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'AurumLauncher.exe build failed.' }
$Launcher = Join-Path $ProjectRoot 'AurumLauncher.exe'
if (-not (Test-Path -LiteralPath $Launcher)) { throw 'Build finished without creating AurumLauncher.exe.' }
Write-Output "Created $Launcher"
Write-Output 'Keep this launcher beside the project .venv, .env, app, data, and logs folders.'
