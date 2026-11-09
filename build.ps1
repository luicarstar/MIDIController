$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$target = Join-Path $PSScriptRoot 'dist\MIDIController'
$savedFiles = @{}
foreach ($name in @('config.json', 'config.json.pre-keyboards.bak', 'config.json.pre-midi-expansion.bak')) {
    $file = Join-Path $target $name
    if (Test-Path -LiteralPath $file) { $savedFiles[$name] = [IO.File]::ReadAllBytes($file) }
}
try {
    python -m PyInstaller --noconfirm MIDIController.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    $target = Join-Path $PSScriptRoot 'dist\MIDIController'
    if (-not (Test-Path (Join-Path $target 'config.json')) -and
            (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'config.json'))) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Destination $target
    }
    Write-Host "EXE: $target\MIDIController.exe"
} finally {
    if ($savedFiles.Count -gt 0) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($name in $savedFiles.Keys) {
            [IO.File]::WriteAllBytes((Join-Path $target $name), [byte[]]$savedFiles[$name])
        }
    }
    Pop-Location
}
