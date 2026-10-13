# Internal tab entry point. Use Start-CodexFleet.ps1 to open a fleet.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$Name
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Fleet.Common.ps1')
$fleet = Get-FleetConfiguration -ConfigPath $ConfigPath
$selected = @(Select-FleetAgents -Fleet $fleet -Name @($Name))
$agent = $selected[0]
$mutex = [Threading.Mutex]::new($false, 'Local\CodexFleet-' + $agent.SessionId)
$held = $false
try {
    try { $held = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { Write-Warning "$Name already has a fleet tab. No duplicate Codex was started."; return }
    $owners = @(Get-FleetSessionOwners -Fleet $fleet -Agent $agent)
    if ($owners.Count -gt 0) {
        Write-Warning "$Name is already running (PID $($owners -join ',')). No duplicate Codex was started."
        return
    }
    Set-Location -LiteralPath $agent.Directory
    Write-Host ("Resuming {0}" -f $agent.Name)
    Write-Host ("Folder: {0}" -f $agent.Directory)
    Write-Host ("Session: {0}" -f $agent.SessionId)
    # --cd suppresses the saved-vs-current-directory prompt.
    # No prompt argument, fork, new session or goal change is submitted.
    & $fleet.CodexPath resume $agent.SessionId --cd $agent.Directory
    if ($LASTEXITCODE -ne 0) { Write-Warning "Codex exited with code $LASTEXITCODE." }
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
