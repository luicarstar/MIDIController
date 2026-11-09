param([switch]$Admin, [switch]$Test)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Test) {
    Push-Location $here
    try {
        python -B -m tests.offline_suite
        $testExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $testExitCode
}
if ($Admin) {
    Start-Process pythonw -ArgumentList "`"$here\main.py`"" -WorkingDirectory $here -Verb RunAs
} else {
    Start-Process pythonw -ArgumentList "`"$here\main.py`"" -WorkingDirectory $here
}
