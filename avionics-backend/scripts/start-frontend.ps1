param(
    [string]$Python = 'D:\anaconda\python.exe',
    [string]$BinDir = '',
    [string]$RuntimeDir = '',
    [string]$DataDir = 'E:\avionics-data\ground-station'
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
if (-not $BinDir) { $BinDir = Join-Path $project 'bin' }
$data = [System.IO.Path]::GetFullPath($DataDir)
if ([System.IO.Path]::GetPathRoot($data) -notin @('D:\', 'E:\')) {
    throw 'DataDir must be on D: or E:'
}
New-Item -ItemType Directory -Path "$data\.temp" -Force | Out-Null
$env:TEMP = "$data\.temp"
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PATH = "$BinDir;" + $env:PATH
if ($RuntimeDir) { $env:PATH = "$RuntimeDir;" + $env:PATH }
Push-Location "$project\frontend"
try {
    & $Python -B -m ground_station --bin-dir $BinDir --data-dir $data --config "$project\config\correlation-demo.ini"
    if ($LASTEXITCODE -ne 0) { throw "Frontend exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
