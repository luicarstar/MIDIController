param([string]$WorkRoot = (Join-Path $PSScriptRoot '..\work'))
$ErrorActionPreference = 'Stop'
$work = [IO.Path]::GetFullPath($WorkRoot)
$fixture = Join-Path $work ('build-protection-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture -Force | Out-Null
$fixture = [IO.Path]::GetFullPath($fixture)
if (-not $fixture.StartsWith($work.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Fixture must remain inside the work directory'
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\build.ps1') -Destination $fixture
[IO.File]::WriteAllText((Join-Path $fixture 'config.json'), 'SOURCE EXAMPLE')
$target = Join-Path $fixture 'dist\MIDIController'
function python {
    $resolved = [IO.Path]::GetFullPath($target)
    if (-not $resolved.StartsWith($fixture + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Mock build target escaped fixture'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    if ($global:mockBuildMode -eq 'failure') {
        $global:LASTEXITCODE = 1
    } else {
        New-Item -ItemType Directory -Path $resolved -Force | Out-Null
        $global:LASTEXITCODE = 0
    }
}
try {
    foreach ($global:mockBuildMode in @('failure', 'success')) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        $configPath = Join-Path $target 'config.json'
        $backupPath = Join-Path $target 'config.json.pre-keyboards.bak'
        $expansionPath = Join-Path $target 'config.json.pre-midi-expansion.bak'
        $userBytes = [Text.Encoding]::UTF8.GetBytes("USER CONFIG $global:mockBuildMode`r`n")
        $backupBytes = [Text.Encoding]::UTF8.GetBytes("ORIGINAL ROLLBACK BYTES`n")
        [IO.File]::WriteAllBytes($configPath, $userBytes)
        [IO.File]::WriteAllBytes($backupPath, $backupBytes)
        [IO.File]::WriteAllBytes($expansionPath, $backupBytes)
        $failed = $false
        try { & (Join-Path $fixture 'build.ps1') } catch { $failed = $true }
        if ($failed -ne ($global:mockBuildMode -eq 'failure')) { throw 'Unexpected build result' }
        if (-not (Test-Path -LiteralPath $configPath)) { throw 'User config disappeared after build' }
        if (-not (Test-Path -LiteralPath $backupPath)) { throw 'Migration rollback copy disappeared after build' }
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath)) -ne [Convert]::ToBase64String($userBytes)) {
            throw 'Build replaced user config with source example'
        }
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($backupPath)) -ne [Convert]::ToBase64String($backupBytes)) {
            throw 'Build changed rollback copy'
        }
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($expansionPath)) -ne [Convert]::ToBase64String($backupBytes)) {
            throw 'Build changed MIDI expansion rollback copy'
        }
        Write-Output "PASS: $global:mockBuildMode preserves exact config and rollback bytes"
    }
} finally {
    if ($fixture.StartsWith($work.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $fixture -Recurse -Force
    }
}
