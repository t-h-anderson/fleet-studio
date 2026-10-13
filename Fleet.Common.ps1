Set-StrictMode -Version Latest
function Get-FleetConfiguration {
    param([Parameter(Mandatory)][string]$ConfigPath)
    $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).ProviderPath
    $config = Get-Content -LiteralPath $resolvedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.version -ne 1) { throw 'Unsupported fleet config version; expected 1.' }
    if ($config.windowName -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') {
        throw 'windowName must contain only letters, digits, underscores and hyphens.'
    }
    $names = @{}; $sessions = @{}; $agents = @()
    foreach ($entry in $config.agents) {
        if ($entry.PSObject.Properties['enabled'] -and -not $entry.enabled) { continue }
        $name = [string]$entry.name
        if ([string]::IsNullOrWhiteSpace($name) -or $name -match '[\x00-\x1f;"]') {
            throw 'Agent names must be nonempty without control characters, semicolons or double quotes.'
        }
        if ($names.ContainsKey($name)) { throw "Duplicate agent name: $name" }
        $names[$name] = $true
        $sessionGuid = [Guid]::Empty
        if (-not [Guid]::TryParseExact([string]$entry.sessionId, 'D', [ref]$sessionGuid)) {
            throw "Invalid session UUID for $name"
        }
        $sessionId = $sessionGuid.ToString()
        if ($sessions.ContainsKey($sessionId)) { throw "Duplicate session UUID: $sessionId" }
        $sessions[$sessionId] = $true
        $directory = [string]$entry.directory
        if ([string]::IsNullOrWhiteSpace($directory)) { throw "Missing directory for $name" }
        if (-not [IO.Path]::IsPathRooted($directory)) {
            $directory = Join-Path (Split-Path -Parent $resolvedConfig) $directory
        }
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            throw ("Missing directory for {0}: {1}" -f $name,$directory)
        }
        $directory = (Resolve-Path -LiteralPath $directory).ProviderPath
        $agents += [pscustomobject]@{ Name=$name; Directory=$directory; SessionId=$sessionId }
    }
    if ($agents.Count -eq 0) { throw 'No enabled agents in the fleet config.' }
    $codexPath = [string]$config.codexPath
    if (-not (Test-Path -LiteralPath $codexPath -PathType Leaf)) { throw "Codex executable not found: $codexPath" }
    $codexStateRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    [pscustomobject]@{
        ConfigPath=$resolvedConfig; WindowName=[string]$config.windowName
        CodexPath=$codexPath; CodexStateRoot=$codexStateRoot; Agents=$agents
    }
}
function Select-FleetAgents {
    param($Fleet, [string[]]$Name)
    if (-not $Name -or $Name.Count -eq 0) { return $Fleet.Agents }
    foreach ($requested in $Name) {
        if (-not @($Fleet.Agents | Where-Object Name -EQ $requested).Count) { throw "Unknown enabled agent: $requested" }
    }
    $Fleet.Agents | Where-Object { $_.Name -in $Name }
}
function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    "'" + $Value.Replace("'", "''") + "'"
}
function ConvertTo-WindowsArgument {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    # CommandLineToArgvW quoting handles embedded quotes and trailing slashes.
    '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
function Get-FleetTabCommand {
    param($Fleet, $Agent, [string]$PowerShellPath, [switch]$CloseTabsOnExit)
    $runner = Join-Path $PSScriptRoot 'Resume-CodexAgent.ps1'
    # Quote literals before encoding; JSON data is never evaluated as code.
    $script = '& ' + (ConvertTo-PowerShellLiteral $runner) +
        ' -ConfigPath ' + (ConvertTo-PowerShellLiteral $Fleet.ConfigPath) +
        ' -Name ' + (ConvertTo-PowerShellLiteral $Agent.Name)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    $commandArguments = @('-w', $Fleet.WindowName, 'new-tab', '--title', $Agent.Name,
        '--suppressApplicationTitle', $PowerShellPath, '-NoLogo', '-NoProfile')
    if (-not $CloseTabsOnExit) { $commandArguments += '-NoExit' }
    $commandArguments += @('-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
    [pscustomobject]@{
        Arguments=$commandArguments
        CommandLine=(($commandArguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' ')
    }
}
function Initialize-FleetLockReader {
    if ('CodexFleet.LockReader' -as [type]) { return }
    # Query live writer-lock owners without modifying Codex state.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CodexFleet {
 public static class LockReader {
  [StructLayout(LayoutKind.Sequential)]
  struct UniqueProcess {
   public uint pid;
   public System.Runtime.InteropServices.ComTypes.FILETIME start;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  struct ProcessInfo {
   public UniqueProcess process;
   [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string app;
   [MarshalAs(UnmanagedType.ByValTStr, SizeConst=64)] public string service;
   public uint type, status, session;
   [MarshalAs(UnmanagedType.Bool)] public bool restartable;
  }
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
  static extern int RmStartSession(out uint handle, int flags, System.Text.StringBuilder key);
  [DllImport("rstrtmgr.dll", CharSet=CharSet.Unicode)]
  static extern int RmRegisterResources(uint handle, uint nFiles, string[] files,
   uint nApps, IntPtr apps, uint nServices, IntPtr services);
  [DllImport("rstrtmgr.dll")]
  static extern int RmGetList(uint handle, out uint needed, ref uint count,
   [In, Out] ProcessInfo[] processes, ref uint reasons);
  [DllImport("rstrtmgr.dll")]
  static extern int RmEndSession(uint handle);
  public static uint[] Owners(string filename) {
   uint handle;
   int code = RmStartSession(out handle, 0, new System.Text.StringBuilder(33));
   if (code != 0) throw new System.ComponentModel.Win32Exception(code);
   try {
    code = RmRegisterResources(handle, 1, new string[] {filename}, 0, IntPtr.Zero, 0, IntPtr.Zero);
    if (code != 0) throw new System.ComponentModel.Win32Exception(code);
    uint needed, count=0, reasons=0;
    code = RmGetList(handle, out needed, ref count, null, ref reasons);
    if (code == 0) return new uint[0];
    for (int attempt=0; attempt<3; attempt++) {
     if (code != 234) throw new System.ComponentModel.Win32Exception(code);
     var rows = new ProcessInfo[needed];
     count = needed;
     code = RmGetList(handle, out needed, ref count, rows, ref reasons);
     if (code == 0) {
      var pids = new uint[count];
      for (int i=0; i<count; i++) pids[i] = rows[i].process.pid;
      return pids;
     }
    }
    throw new InvalidOperationException("Writer owners changed during inspection; retry.");
   } finally { RmEndSession(handle); }
  }
 }
}
'@
}
function Get-FleetSessionOwners {
    param($Fleet, $Agent)
    $lockPath = Join-Path $Fleet.CodexStateRoot ('thread-writer-locks\' + $Agent.SessionId + '.lock')
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { return }
    Initialize-FleetLockReader
    [CodexFleet.LockReader]::Owners($lockPath)
}
function Test-FleetTabOpen {
    param([string]$SessionId)
    $mutex = $null; $held = $false
    try {
        try { $mutex = [Threading.Mutex]::OpenExisting('Local\CodexFleet-' + $SessionId) }
        catch [Threading.WaitHandleCannotBeOpenedException] { return $false }
        try { $held = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $held = $true }
        return -not $held
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}
