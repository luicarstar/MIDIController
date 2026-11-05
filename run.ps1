param([switch]$Admin, [switch]$Test)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Test) {
    python -m unittest discover -s (Join-Path $here 'tests') -t $here -v
    exit $LASTEXITCODE
}
if ($Admin) {
    Start-Process pythonw -ArgumentList "`"$here\main.py`"" -WorkingDirectory $here -Verb RunAs
} else {
    Start-Process pythonw -ArgumentList "`"$here\main.py`"" -WorkingDirectory $here
}
