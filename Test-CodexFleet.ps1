# Exercises validation and the real child-shell path using a mock Codex.
[CmdletBinding()]
param([switch]$Terminal)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Fleet.Common.ps1')
$testRoot = Join-Path $PSScriptRoot ('.test-run-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$checks = [Collections.Generic.List[string]]::new()
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Assert-Rejected([scriptblock]$Action, [string]$ExpectedText) {
    $message = ''
    try { & $Action | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -like "*$ExpectedText*") ("Expected rejection: " + $ExpectedText)
}
function Write-Config($Value, [string]$Destination) {
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Destination -Encoding UTF8
}
try {
    $mock = @'
$record = [ordered]@{ arguments=@($args); directory=(Get-Location).ProviderPath }
$outputPath = Join-Path $PSScriptRoot ([string]$args[1] + '.json')
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $outputPath -Encoding UTF8
$global:LASTEXITCODE = 0
'@
    $mockPath = Join-Path $testRoot 'mock codex.ps1'
    Set-Content -LiteralPath $mockPath -Value $mock -Encoding UTF8
    $configPath = Join-Path $testRoot "fleet O'Brien.json"
    $folder1 = Join-Path $testRoot 'Writer O''Brien [one] $cost'
    $folder2 = Join-Path $testRoot 'Writer Two'
    [IO.Directory]::CreateDirectory($folder1) | Out-Null
    [IO.Directory]::CreateDirectory($folder2) | Out-Null
    $id1 = [Guid]::NewGuid().ToString()
    $id2 = [Guid]::NewGuid().ToString()
    $configuration = [ordered]@{
        version=1; windowName=('codex-fleet-test-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
        codexPath=$mockPath
        agents=@(
            [ordered]@{name='Writer O''Brien $cost';directory=$folder1;sessionId=$id1;enabled=$true},
            [ordered]@{name='Writer Two';directory=$folder2;sessionId=$id2;enabled=$true}
        )
    }
    Write-Config $configuration $configPath
    $fleet = Get-FleetConfiguration $configPath
    Assert-True ($fleet.Agents.Count -eq 2) 'Expected two validated agents.'
    $selected = @(Select-FleetAgents $fleet @('Writer Two'))
    Assert-True ($selected.Count -eq 1 -and $selected[0].SessionId -eq $id2) 'Wrong selected session.'
    $checks.Add('Agent selection and literal folders')
    Assert-Rejected { Select-FleetAgents $fleet @('missing') } 'Unknown enabled agent'
    $checks.Add('Unknown agent rejected')
    $badPath = Join-Path $testRoot 'invalid.json'
    $configuration.agents[1].sessionId = $id1
    Write-Config $configuration $badPath
    Assert-Rejected { Get-FleetConfiguration $badPath } 'Duplicate session UUID'
    $configuration.agents[1].sessionId = $id2
    $checks.Add('Duplicate session rejected')
    $configuration.agents[1].sessionId = '--last'
    Write-Config $configuration $badPath
    Assert-Rejected { Get-FleetConfiguration $badPath } 'Invalid session UUID'
    $configuration.agents[1].sessionId = $id2
    $checks.Add('Implicit session selection rejected')
    $configuration.agents[1].directory = Join-Path $testRoot 'missing'
    Write-Config $configuration $badPath
    Assert-Rejected { Get-FleetConfiguration $badPath } 'Missing directory'
    $configuration.agents[1].directory = $folder2
    $checks.Add('Missing folder rejected')
    $configuration.agents[1].name = 'Writer; injected'
    Write-Config $configuration $badPath
    Assert-Rejected { Get-FleetConfiguration $badPath } 'Agent names'
    $configuration.agents[1].name = 'Writer Two'
    $checks.Add('Terminal command separator rejected')
    $configuration.agents[1].enabled = $false
    Write-Config $configuration $badPath
    Assert-True (@((Get-FleetConfiguration $badPath).Agents).Count -eq 1) 'Disabled agent was selected.'
    $configuration.agents[1].enabled = $true
    $checks.Add('Disabled agent omitted')

    $shellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ($Terminal) {
        & (Join-Path $PSScriptRoot 'Start-CodexFleet.ps1') -ConfigPath $configPath -CloseTabsOnExit -StaggerMilliseconds 300
    } else {
        foreach ($agent in $fleet.Agents) {
            $command = Get-FleetTabCommand $fleet $agent $shellPath -CloseTabsOnExit
            $shellArgs = @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$command.Arguments[-1])
            $commandLine = ($shellArgs | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
            $child = Start-Process -FilePath $shellPath -ArgumentList $commandLine -WindowStyle Hidden -PassThru
            if (-not $child.WaitForExit(20000)) { throw 'Mock child did not finish.' }
            Assert-True ($child.ExitCode -eq 0) ('Child failed: ' + $agent.Name)
            $child.Dispose()
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    foreach ($agent in $fleet.Agents) {
        $capture = Join-Path $testRoot ($agent.SessionId + '.json')
        while (-not (Test-Path -LiteralPath $capture) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 100
        }
        Assert-True (Test-Path -LiteralPath $capture) ('No child receipt: ' + $agent.Name)
        $record = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
        Assert-True ($record.directory -eq $agent.Directory) 'Child changed to the wrong folder.'
        $expected = @('resume',$agent.SessionId,'--cd',$agent.Directory)
        Assert-True ($record.arguments.Count -eq 4) 'Unexpected prompt or extra Codex argument.'
        for ($i=0; $i -lt 4; $i++) {
            Assert-True ($record.arguments[$i] -ceq $expected[$i]) ('Incorrect argument at index ' + $i)
        }
        $checks.Add(('Exact resume and cwd: ' + $agent.Name))
    }
    $result = [ordered]@{
        timestampUtc=[DateTime]::UtcNow.ToString('o'); passed=$true
        checks=$checks.ToArray(); terminalTest=[bool]$Terminal; realCodexStarted=$false; matlabStarted=$false
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'validation.json') -Encoding UTF8
    $result | ConvertTo-Json -Depth 5
    # Only remove this test's unique directory after resolving and checking it.
    $resolvedTest = (Resolve-Path -LiteralPath $testRoot).ProviderPath
    $expectedPrefix = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath.TrimEnd('\') + '\.test-run-'
    if (-not $resolvedTest.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unexpected test cleanup path; nothing removed.'
    }
    Remove-Item -LiteralPath $resolvedTest -Recurse -Force
} catch {
    Write-Warning ("Test fixtures retained at " + $testRoot)
    throw
}
