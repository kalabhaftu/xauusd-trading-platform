$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Run scripts/setup.ps1 first to install the developer build environment.'
}
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install pyinstaller==6.22.3
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed; see pip output.' }
}

$BuildDirectory = Join-Path $ProjectRoot '.build\pyinstaller'
$Distribution = Join-Path $ProjectRoot 'dist'
New-Item -ItemType Directory -Force -Path $BuildDirectory, $Distribution | Out-Null
$Arguments = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'AurumLauncher',
    '--distpath', $Distribution, '--workpath', $BuildDirectory, '--specpath', $BuildDirectory,
    '--hidden-import', 'MetaTrader5', '--hidden-import', 'app.main', '--hidden-import', 'app.dashboard.home',
    '--collect-all', 'streamlit', '--collect-all', 'plotly', '--collect-all', 'pandas',
    '--collect-all', 'numpy', '--collect-all', 'sqlalchemy', '--collect-all', 'alembic',
    '--collect-all', 'backtesting', '--collect-all', 'bokeh', '--collect-all', 'pydantic',
    '--collect-all', 'pydantic_settings',
    '--add-data', "$ProjectRoot\app;app",
    '--add-data', "$ProjectRoot\migrations;migrations",
    '--add-data', "$ProjectRoot\configs;configs",
    '--add-data', "$ProjectRoot\alembic.ini;.",
    '--add-data', "$PSScriptRoot\ExportSessions.mq5;scripts",
    '--add-data', "$PSScriptRoot\SessionExporterEA.mq5;scripts",
    (Join-Path $PSScriptRoot 'launcher.py')
)
& $Python -m PyInstaller @Arguments
if ($LASTEXITCODE -ne 0) { throw 'Aurum build failed; see PyInstaller output.' }
$Launcher = Join-Path $Distribution 'AurumLauncher.exe'
if (-not (Test-Path -LiteralPath $Launcher)) { throw 'Build finished without creating AurumLauncher.exe.' }
& $Launcher --self-test
if ($LASTEXITCODE -ne 0) { throw 'Standalone launcher self-test failed.' }
Write-Output "Created standalone $Launcher"
Write-Output 'Share the EXE only. End users need the MT5 desktop terminal and their broker account, not the source folder, Python environment, or developer settings.'
