# Starts or reuses the local Fleet Studio server and opens the browser.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Fleet.Common.ps1')
$pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$pythonPath = if ($pythonCommand) { $pythonCommand.Source } else { $null }
if (-not $pythonPath -or $pythonPath -like '*\WindowsApps\*') {
    $pythonPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Python 3 is required to start Fleet Studio.' }
$arguments = @((Join-Path $PSScriptRoot 'fleet_studio.py'), '--open')
$commandLine = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
Start-Process -FilePath $pythonPath -ArgumentList $commandLine -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
