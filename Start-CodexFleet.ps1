<#
.SYNOPSIS
Open configured Codex agents in Windows Terminal PowerShell tabs.
.EXAMPLE
.\Start-CodexFleet.ps1 -Name 'Coordinator','Agent 2'
.EXAMPLE
.\Start-CodexFleet.ps1 -Check
.EXAMPLE
.\Start-CodexFleet.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'fleet.json'),
    [string[]]$Name,
    [switch]$List,
    [switch]$Check,
    [ValidateRange(0,10000)][int]$StaggerMilliseconds = 1000,
    [switch]$CloseTabsOnExit
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Fleet.Common.ps1')
$fleet = Get-FleetConfiguration -ConfigPath $ConfigPath
$agents = @(Select-FleetAgents -Fleet $fleet -Name $Name)
if ($List) { $agents | Select-Object Name,Directory,SessionId; return }
$terminal = Get-Command wt.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
$powerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powerShellPath -PathType Leaf)) { throw "PowerShell not found: $powerShellPath" }
# Inspect all selected sessions before opening any tabs.
$plan = foreach ($agent in $agents) {
    $owners = @(Get-FleetSessionOwners -Fleet $fleet -Agent $agent)
    $busy = ($owners.Count -gt 0) -or (Test-FleetTabOpen -SessionId $agent.SessionId)
    [pscustomobject]@{
        Name=$agent.Name; Directory=$agent.Directory; SessionId=$agent.SessionId
        Status=$(if ($busy) { 'Already open - skip' } else { 'Ready' })
        OwnerPids=($owners -join ','); Agent=$agent
    }
}
if ($Check -or $WhatIfPreference) {
    $plan | Select-Object Name,Status,OwnerPids,Directory,SessionId
    return
}
$opened = 0
foreach ($item in $plan) {
    if ($item.Status -ne 'Ready') { Write-Host ($item.Name + ': already open; skipped.'); continue }
    if ($PSCmdlet.ShouldProcess($item.Name, 'Open PowerShell tab and resume exact Codex session')) {
        $command = Get-FleetTabCommand -Fleet $fleet -Agent $item.Agent -PowerShellPath $powerShellPath -CloseTabsOnExit:$CloseTabsOnExit
        # Visible windows are intentional: these are the user's interactive tabs.
        $process = Start-Process -FilePath $terminal.Source -ArgumentList $command.CommandLine -PassThru
        if ($process.WaitForExit(10000) -and $process.ExitCode -ne 0) {
            throw "Windows Terminal could not open $($item.Name); exit code $($process.ExitCode)."
        }
        $process.Dispose()
        $opened++
        Write-Host ($item.Name + ': tab opened.')
        if ($StaggerMilliseconds -gt 0) { Start-Sleep -Milliseconds $StaggerMilliseconds }
    }
}
Write-Host ("Opened {0} tab(s) in window '{1}'." -f $opened,$fleet.WindowName)
