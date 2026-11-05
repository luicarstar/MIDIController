$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$target = Join-Path $PSScriptRoot 'dist\MIDIController'
$configFile = Join-Path $target 'config.json'
$savedConfig = if (Test-Path -LiteralPath $configFile) { [IO.File]::ReadAllBytes($configFile) } else { $null }
try {
    python -m PyInstaller --noconfirm MIDIController.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    $target = Join-Path $PSScriptRoot 'dist\MIDIController'
    if (-not (Test-Path (Join-Path $target 'config.json'))) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Destination $target
    }
    Write-Host "EXE: $target\MIDIController.exe"
} finally {
    if ($null -ne $savedConfig -and (Test-Path -LiteralPath $target)) {
        [IO.File]::WriteAllBytes($configFile, [byte[]]$savedConfig)
    }
    Pop-Location
}
