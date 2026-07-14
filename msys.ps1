#requires -Version 5.1
<#
.SYNOPSIS
  Small Windows entry point for the MSYS development CLI.

.DESCRIPTION
  Ordinary commands use a one-shot WSL process by default. Auto mode reuses a
  healthy local broker only after it was explicitly started; fast/q and accept
  select On automatically, while Off always stays one-shot. For the fastest repeated edit
  and debug loop, enter ``shell`` once and run ``msys debug`` (or ``m debug``)
  inside it. First-time key authentication deliberately stays interactive.

  The script is also safe to call from a subdirectory.  Local Windows paths
  such as .\artifacts\home.png and G:\Code\MsYs\msys-settings are translated
  to their normal /mnt/<drive>/... WSL paths before the Python CLI sees them.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [string]$Workspace,

    [string]$Distro,

    [ValidateSet("Auto", "On", "Off", "auto", "on", "off")]
    [string]$Broker = $env:MSYS_DEV_BROKER,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DevArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-MsysUsage {
    @"
MSYS Windows development shortcut

  .\msys.cmd setup --target root@192.168.1.215 --remote /opt/msys-dev --runtime-dir /tmp/msys-main --state-dir /opt/msys-state --profile desktop-spi --ssh-key /home/luorix/.ssh/msys-dev-ed25519 --ssh-control-persist 2h
  .\msys.cmd key                 # only once: installs the dedicated SSH key
  .\msys.cmd connect             # authenticate once; keeps SSH warm
  .\msys.cmd shell               # fastest loop: cd a repo, then mq/mqs/mqshot
  .\msys.cmd fast --repo msys-settings       # persistent broker + one debug bundle
  .\msys.cmd accept                           # persistent broker + one read-only acceptance
  .\msys.cmd fast --repo msys-settings --deliver --screenshot .\artifacts\settings.png
  .\msys.cmd quick --repo msys-settings --status  # sync/build + current health
  .\msys.cmd quick --repo msys-shell-native       # unchanged source skips upload/build
  .\msys.cmd debug               # one-shot WSL + one SSH runtime snapshot
  .\msys.cmd broker start        # optional opt-in; Auto then reuses this broker
  .\msys.cmd broker status
  .\msys.cmd debug --follow      # snapshot, then keep following the same log stream
  .\msys.cmd sync --repo msys-shell-native
  .\msys.cmd screenshot .\artifacts\home.png
  .\msys.cmd call role:hal set_state --field id=bluetooth:hci0 --field changes.powered=true

Short aliases: check=doctor, up=run, down=stop, log=tail, ps=components,
inspect=debug, connect=ssh-warm, disconnect=ssh-reset, key=setup-key.

Any normal msys-dev command can follow the shortcut, for example:
  .\msys.cmd role select navigation-bar org.msys.shell.native:navigation
  .\msys.cmd package deliver .\my-app --format maf --force

Optional wrapper settings:
  -Workspace G:\Code\MsYs     Windows workspace (default: this workspace)
  -Distro Ubuntu               selected WSL distribution
  -Broker Auto|On|Off           Auto=reuse only, On=start/require, Off=one-shot

For a nonstandard WSL automount, set MSYS_WSL_WORKSPACE to the Linux workspace
path (for example /work/msys).  MSYS_WSL_DISTRO may persist the distro choice.
Set MSYS_DEV_BROKER=On to require the broker or Off to force one-shot WSL.
"@ | Write-Host
}

function Get-MsysWorkspace {
    param([string]$Requested)

    if ([string]::IsNullOrWhiteSpace($Requested)) {
        $Requested = $env:MSYS_WORKSPACE
    }
    if ([string]::IsNullOrWhiteSpace($Requested)) {
        # This script lives in <workspace>\msys-tools.
        $Requested = Split-Path -Parent $PSScriptRoot
    }
    $item = Get-Item -LiteralPath $Requested -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "MSYS workspace must be a directory: $Requested"
    }
    $workspace = $item.FullName
    $entry = Join-Path $workspace "msys-tools\msys_tools\dev.py"
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Not an MSYS workspace (missing $entry). Use -Workspace with the directory containing msys-tools."
    }
    return $workspace
}

function ConvertTo-MsysWslPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    $full = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($full -notmatch "^(?<drive>[A-Za-z]):[\\/](?<tail>.*)$") {
        throw "Cannot infer a WSL path for '$full'. Set MSYS_WSL_WORKSPACE for a nonstandard mount."
    }
    $drive = $Matches.drive.ToLowerInvariant()
    $tail = $Matches.tail.Replace("\", "/")
    if ([string]::IsNullOrEmpty($tail)) {
        return "/mnt/$drive"
    }
    return "/mnt/$drive/$tail"
}

function ConvertTo-MsysArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    # Keep Linux/remote values untouched.  Only unmistakable Windows paths and
    # explicit .\ / ..\ local paths need translation.
    if ($Value -match "^[A-Za-z]:[\\/]") {
        return ConvertTo-MsysWslPath $Value
    }
    if ($Value -match "^(?:\.|\.\.)[\\/]*$") {
        return ConvertTo-MsysWslPath ([System.IO.Path]::GetFullPath($Value))
    }
    if ($Value -match "^(?:\.|\.\.)[\\/]") {
        return ConvertTo-MsysWslPath ([System.IO.Path]::GetFullPath($Value))
    }
    return $Value
}

function Test-MsysOption {
    param(
        [string[]]$Arguments,
        [string]$Name
    )
    foreach ($argument in $Arguments) {
        if ($argument -eq $Name -or $argument.StartsWith("$Name=")) {
            return $true
        }
    }
    return $false
}

function Get-MsysBrokerProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-MsysBrokerDirectory {
    $base = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($base)) {
        $base = Join-Path $HOME "AppData\Local"
    }
    $directory = Join-Path $base "MSYS\dev-brokers"
    [void][System.IO.Directory]::CreateDirectory($directory)
    return $directory
}

function Get-MsysBrokerStatePath {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceWindows,
        [Parameter(Mandatory = $true)][string]$WorkspaceWsl,
        [string]$DistroName
    )
    $identity = "{0}`n{1}`n{2}" -f $WorkspaceWindows.ToLowerInvariant(), $WorkspaceWsl, $DistroName
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($identity)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    $hex = ([System.BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    return Join-Path (Get-MsysBrokerDirectory) ("broker-" + $hex.Substring(0, 24) + ".json")
}

function Read-MsysBrokerState {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $protocol = Get-MsysBrokerProperty $state "protocol"
        $port = Get-MsysBrokerProperty $state "port"
        $token = Get-MsysBrokerProperty $state "token"
        if ($protocol -ne 1 -or $null -eq $port -or [string]::IsNullOrWhiteSpace([string]$token)) {
            return $null
        }
        $portNumber = [int]$port
        if ($portNumber -lt 1 -or $portNumber -gt 65535 -or ([string]$token).Length -lt 32) {
            return $null
        }
        return $state
    } catch {
        return $null
    }
}

function Save-MsysBrokerState {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$State
    )
    $temporary = $Path + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    $json = ([PSCustomObject]$State | ConvertTo-Json -Compress -Depth 4) + "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($temporary, $json, $encoding)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Remove-MsysBrokerState {
    param([Parameter(Mandatory = $true)][string]$Path)
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function New-MsysBrokerToken {
    $random = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($random)
    } finally {
        $generator.Dispose()
    }
    return ([System.BitConverter]::ToString($random)).Replace("-", "").ToLowerInvariant()
}

function Get-MsysBrokerPort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function ConvertTo-MsysWindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    # Quote with the CommandLineToArgvW backslash rules used by wsl.exe.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Open-MsysBrokerClient {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 500
    )
    $client = New-Object System.Net.Sockets.TcpClient
    $pending = $null
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            throw "timed out connecting to local broker on port $Port"
        }
        $client.EndConnect($pending)
        return $client
    } catch {
        $client.Dispose()
        throw
    } finally {
        if ($null -ne $pending) {
            $pending.AsyncWaitHandle.Close()
        }
    }
}

function Invoke-MsysBrokerFrameRequest {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][hashtable]$Request,
        [int]$TimeoutMilliseconds = 1500
    )
    $port = [int](Get-MsysBrokerProperty $State "port")
    $token = [string](Get-MsysBrokerProperty $State "token")
    $client = Open-MsysBrokerClient -Port $port -TimeoutMilliseconds $TimeoutMilliseconds
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutMilliseconds
        $stream.WriteTimeout = $TimeoutMilliseconds
        $writer = New-Object System.IO.StreamWriter($stream, $encoding, 4096, $true)
        $reader = New-Object System.IO.StreamReader($stream, $encoding, $false, 4096, $true)
        try {
            $Request["protocol"] = 1
            $Request["token"] = $token
            $writer.WriteLine(([PSCustomObject]$Request | ConvertTo-Json -Compress -Depth 4))
            $writer.Flush()
            $line = $reader.ReadLine()
            if ($null -eq $line) {
                throw "local broker closed the connection before replying"
            }
            return ($line | ConvertFrom-Json -ErrorAction Stop)
        } finally {
            $writer.Dispose()
            $reader.Dispose()
        }
    } finally {
        $client.Dispose()
    }
}

function Test-MsysBroker {
    param(
        [Parameter(Mandatory = $true)]$State,
        [int]$TimeoutMilliseconds = 350
    )
    try {
        $response = Invoke-MsysBrokerFrameRequest -State $State -Request @{ type = "ping" } -TimeoutMilliseconds $TimeoutMilliseconds
        return ((Get-MsysBrokerProperty $response "type") -eq "ready" -and (Get-MsysBrokerProperty $response "protocol") -eq 1)
    } catch {
        return $false
    }
}

function Invoke-MsysBrokerCommand {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $port = [int](Get-MsysBrokerProperty $State "port")
    $token = [string](Get-MsysBrokerProperty $State "token")
    $client = $null
    $writer = $null
    $reader = $null
    try {
        $client = Open-MsysBrokerClient -Port $port -TimeoutMilliseconds 750
        $encoding = New-Object System.Text.UTF8Encoding($false)
        $stream = $client.GetStream()
        # A command can legitimately run for hours (tail --follow), so only the
        # initial TCP connection has a short timeout.
        $stream.ReadTimeout = -1
        $stream.WriteTimeout = 5000
        $writer = New-Object System.IO.StreamWriter($stream, $encoding, 4096, $true)
        $reader = New-Object System.IO.StreamReader($stream, $encoding, $false, 4096, $true)
        $request = [ordered]@{
            protocol = 1
            token = $token
            type = "run"
            argv = [string[]]@($Arguments)
        }
        $writer.WriteLine(([PSCustomObject]$request | ConvertTo-Json -Compress -Depth 5))
        $writer.Flush()
        while ($true) {
            $line = $reader.ReadLine()
            if ($null -eq $line) {
                return [PSCustomObject]@{ Connected = $false; ExitCode = 255; Error = "local broker closed the command stream" }
            }
            $frame = $line | ConvertFrom-Json -ErrorAction Stop
            $frameType = [string](Get-MsysBrokerProperty $frame "type")
            if ($frameType -eq "output") {
                [Console]::Out.Write([string](Get-MsysBrokerProperty $frame "data"))
                continue
            }
            if ($frameType -eq "done") {
                return [PSCustomObject]@{ Connected = $true; ExitCode = [int](Get-MsysBrokerProperty $frame "exit_code"); Error = $null }
            }
            if ($frameType -eq "error") {
                $message = [string](Get-MsysBrokerProperty $frame "message")
                return [PSCustomObject]@{ Connected = $true; ExitCode = 2; Error = "broker: $message" }
            }
            return [PSCustomObject]@{ Connected = $true; ExitCode = 2; Error = "broker: unexpected frame '$frameType'" }
        }
    } catch {
        return [PSCustomObject]@{ Connected = $false; ExitCode = 255; Error = $_.Exception.Message }
    } finally {
        if ($null -ne $writer) { $writer.Dispose() }
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Start-MsysBroker {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)]$WslCommand,
        [Parameter(Mandatory = $true)][string]$WorkspaceWsl,
        [string]$DistroName
    )
    $existing = Read-MsysBrokerState $StatePath
    if ($null -ne $existing -and (Test-MsysBroker $existing)) {
        return $existing
    }
    Remove-MsysBrokerState $StatePath

    $directory = Split-Path -Parent $StatePath
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($StatePath)
    $stdoutPath = Join-Path $directory ($baseName + ".stdout.log")
    $stderrPath = Join-Path $directory ($baseName + ".stderr.log")
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $port = Get-MsysBrokerPort
        $token = New-MsysBrokerToken
        # Persist the state before launching WSL so the broker can read its
        # secret from the current user's state file. This keeps the token out
        # of the WSL command line (and therefore out of ordinary `ps` output).
        $state = @{
            schema = "msys.dev-broker-state.v1"
            protocol = 1
            port = $port
            token = $token
            pid = 0
            workspace = $WorkspaceWsl
            distro = $DistroName
            started_utc = [DateTime]::UtcNow.ToString("o")
            stdout_log = $stdoutPath
            stderr_log = $stderrPath
        }
        Save-MsysBrokerState -Path $StatePath -State $state
        $stateWslPath = ConvertTo-MsysWslPath $StatePath
        $arguments = @()
        if (-not [string]::IsNullOrWhiteSpace($DistroName)) {
            $arguments += @("-d", $DistroName)
        }
        $arguments += @(
            "--cd", $WorkspaceWsl,
            "--exec", "env",
            "PYTHONPATH=$WorkspaceWsl/msys-tools",
            "MSYS_DEV_ROOT=$WorkspaceWsl",
            "PYTHONDONTWRITEBYTECODE=1",
            "python3", "-m", "msys_tools.dev_broker",
            "--host", "127.0.0.1",
            "--port", "$port",
            "--token-state", $stateWslPath,
            "--workspace", $WorkspaceWsl,
            "--idle-seconds", "14400"
        )
        $commandLine = (($arguments | ForEach-Object { ConvertTo-MsysWindowsCommandLineArgument $_ }) -join " ")
        try {
            $process = Start-Process -FilePath $WslCommand.Source -ArgumentList $commandLine -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        } catch {
            $lastError = $_.Exception.Message
            Remove-MsysBrokerState $StatePath
            continue
        }
        $state["pid"] = $process.Id
        Save-MsysBrokerState -Path $StatePath -State $state
        for ($wait = 0; $wait -lt 20; $wait++) {
            Start-Sleep -Milliseconds 75
            $candidate = Read-MsysBrokerState $StatePath
            if ($null -ne $candidate -and (Test-MsysBroker -State $candidate -TimeoutMilliseconds 100)) {
                return $candidate
            }
            if ($process.HasExited) {
                break
            }
        }
        $lastError = "broker did not become ready (see $stderrPath)"
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-MsysBrokerState $StatePath
    }
    throw "Could not start the local MSYS broker: $lastError"
}

function Stop-MsysBroker {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)]$State
    )
    try {
        [void](Invoke-MsysBrokerFrameRequest -State $State -Request @{ type = "stop" } -TimeoutMilliseconds 1000)
    } catch {
        # If WSL exited already, the state file is still safe to remove below.
    }
    $brokerProcessId = Get-MsysBrokerProperty $State "pid"
    if ($null -ne $brokerProcessId) {
        Start-Sleep -Milliseconds 100
        Stop-Process -Id ([int]$brokerProcessId) -Force -ErrorAction SilentlyContinue
    }
    Remove-MsysBrokerState $StatePath
}

if ($Command -in @("help", "-h", "--help", "/?")) {
    Write-MsysUsage
    return
}

$workspaceWindows = Get-MsysWorkspace $Workspace
$usesImplicitWorkspace = [string]::IsNullOrWhiteSpace($Workspace) -and [string]::IsNullOrWhiteSpace($env:MSYS_WORKSPACE)
if ($usesImplicitWorkspace -and -not [string]::IsNullOrWhiteSpace($env:MSYS_WSL_WORKSPACE)) {
    $workspaceWsl = $env:MSYS_WSL_WORKSPACE
} else {
    $workspaceWsl = ConvertTo-MsysWslPath $workspaceWindows
}
if (-not $workspaceWsl.StartsWith("/")) {
    throw "MSYS_WSL_WORKSPACE must be an absolute Linux path: $workspaceWsl"
}

# Preserve a PowerShell subdirectory inside the workspace. This makes one
# persistent shell land in the repository being edited, where `mq` can infer
# the exact sync repository without another option or a workspace-wide scan.
$workingDirectoryWsl = $workspaceWsl
$currentWindows = [System.IO.Path]::GetFullPath((Get-Location).Path)
$workspacePrefix = $workspaceWindows.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
if ($currentWindows.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $relativeWorkingDirectory = $currentWindows.Substring($workspacePrefix.Length).Replace("\", "/")
    if (-not [string]::IsNullOrWhiteSpace($relativeWorkingDirectory)) {
        $workingDirectoryWsl = $workspaceWsl.TrimEnd("/") + "/" + $relativeWorkingDirectory
    }
}

$translatedArgs = @()
foreach ($argument in $DevArgs) {
    $translatedArgs += ConvertTo-MsysArgument $argument
}

$interactiveShell = $false
$brokerControl = $null
$fastBrokerDefault = $false
switch ($Command.ToLowerInvariant()) {
    "setup" {
        if (Test-MsysOption $translatedArgs "--root") {
            throw "setup owns --root. Select another workspace with -Workspace instead."
        }
        $cliArgs = @("config", "set", "--root", $workspaceWsl) + $translatedArgs
        break
    }
    "key" { $cliArgs = @("setup-key") + $translatedArgs; break }
    "connect" { $cliArgs = @("ssh-warm") + $translatedArgs; break }
    "disconnect" { $cliArgs = @("ssh-reset") + $translatedArgs; break }
    "check" { $cliArgs = @("doctor") + $translatedArgs; break }
    "up" { $cliArgs = @("run") + $translatedArgs; break }
    "down" { $cliArgs = @("stop") + $translatedArgs; break }
    { $_ -in @("log", "logs") } { $cliArgs = @("tail") + $translatedArgs; break }
    "ps" { $cliArgs = @("components") + $translatedArgs; break }
    "inspect" { $cliArgs = @("debug") + $translatedArgs; break }
    { $_ -in @("fast", "q") } {
        $fastBrokerDefault = $true
        $fastArgs = @($translatedArgs)
        if (-not (Test-MsysOption $fastArgs "--repo") -and $currentWindows.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $relative = $currentWindows.Substring($workspacePrefix.Length).Replace("\", "/")
            $candidateRepo = $relative.Split("/")[0]
            if ($candidateRepo -match "^msys-[A-Za-z0-9._-]+$" -and (Test-Path -LiteralPath (Join-Path $workspaceWindows $candidateRepo) -PathType Container)) {
                $fastArgs = @("--repo", $candidateRepo) + $fastArgs
            }
        }
        $cliArgs = @("fast") + $fastArgs
        break
    }
    "accept" {
        $fastBrokerDefault = $true
        $cliArgs = @("accept") + $translatedArgs
        break
    }
    "call" {
        # Keep each --field KEY=VALUE pair as ordinary argv. Unlike an inline
        # JSON object, this survives PowerShell -> cmd.exe -> PowerShell -> WSL
        # without relying on nested quote preservation.
        $cliArgs = @("call") + $translatedArgs
        break
    }
    "broker" {
        if ($translatedArgs.Count -gt 1) {
            throw "broker accepts at most one action: start, status, restart, or stop."
        }
        if ($translatedArgs.Count -eq 0) {
            $brokerControl = "status"
        } else {
            $brokerControl = $translatedArgs[0].ToLowerInvariant()
        }
        if ($brokerControl -notin @("start", "status", "restart", "stop")) {
            throw "unknown broker action '$brokerControl'; use start, status, restart, or stop."
        }
        $cliArgs = @()
        break
    }
    { $_ -in @("shell", "console", "session") } {
        if ($translatedArgs.Count -ne 0) {
            throw "shell does not take msys-dev arguments. Start it, then run 'msys <command>' inside it."
        }
        $interactiveShell = $true
        $cliArgs = @()
        break
    }
    default { $cliArgs = @($Command) + $translatedArgs; break }
}

$wsl = Get-Command "wsl.exe" -ErrorAction SilentlyContinue
if ($null -eq $wsl) {
    $wsl = Get-Command "wsl" -ErrorAction SilentlyContinue
}
if ($null -eq $wsl) {
    throw "WSL was not found. Install/enable WSL, then run this command again."
}

$selectedDistro = if (-not [string]::IsNullOrWhiteSpace($Distro)) {
    $Distro
} else {
    $env:MSYS_WSL_DISTRO
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$brokerStatePath = Get-MsysBrokerStatePath -WorkspaceWindows $workspaceWindows -WorkspaceWsl $workspaceWsl -DistroName $selectedDistro

if ($null -ne $brokerControl) {
    $state = Read-MsysBrokerState $brokerStatePath
    switch ($brokerControl) {
        "status" {
            if ($null -ne $state -and (Test-MsysBroker $state)) {
                $port = Get-MsysBrokerProperty $state "port"
                $started = Get-MsysBrokerProperty $state "started_utc"
                Write-Host "MSYS local broker is ready (127.0.0.1:$port, started $started)."
                exit 0
            }
            if ($null -ne $state) {
                Remove-MsysBrokerState $brokerStatePath
            }
            Write-Host "MSYS local broker is not running. Start it explicitly with '.\msys.cmd broker start' or use -Broker On."
            exit 1
        }
        "stop" {
            if ($null -eq $state) {
                Write-Host "MSYS local broker is not running."
                exit 0
            }
            Stop-MsysBroker -StatePath $brokerStatePath -State $state
            Write-Host "MSYS local broker stopped."
            exit 0
        }
        "restart" {
            if ($null -ne $state) {
                Stop-MsysBroker -StatePath $brokerStatePath -State $state
            }
            $state = Start-MsysBroker -StatePath $brokerStatePath -WslCommand $wsl -WorkspaceWsl $workspaceWsl -DistroName $selectedDistro
            Write-Host ("MSYS local broker ready on 127.0.0.1:{0}." -f (Get-MsysBrokerProperty $state "port"))
            exit 0
        }
        "start" {
            $state = Start-MsysBroker -StatePath $brokerStatePath -WslCommand $wsl -WorkspaceWsl $workspaceWsl -DistroName $selectedDistro
            Write-Host ("MSYS local broker ready on 127.0.0.1:{0}." -f (Get-MsysBrokerProperty $state "port"))
            exit 0
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Broker)) {
    $brokerMode = if ($fastBrokerDefault) { "on" } else { "auto" }
} else {
    $brokerMode = $Broker.ToLowerInvariant()
}
if ($brokerMode -notin @("auto", "on", "off")) {
    throw "Broker must be Auto, On, or Off."
}

# Key setup and initial password authentication require a real interactive
# stdin. Auto only reuses a healthy explicitly started broker; it never starts
# one. The fast/q and accept workflows select On when no explicit -Broker value was given;
# other commands retain Auto. Off always uses one-shot WSL.
$actionName = $Command.ToLowerInvariant()
$requiresInteractiveWsl = $interactiveShell -or $actionName -in @(
    "key", "setup-key", "connect", "ssh-warm", "shell", "console", "session"
)
if (-not $requiresInteractiveWsl -and $brokerMode -ne "off") {
    $state = $null
    try {
        $state = Read-MsysBrokerState $brokerStatePath
        if ($null -ne $state -and -not (Test-MsysBroker $state)) {
            Remove-MsysBrokerState $brokerStatePath
            $state = $null
        }
        if ($null -eq $state -and $brokerMode -eq "on") {
            $state = Start-MsysBroker -StatePath $brokerStatePath -WslCommand $wsl -WorkspaceWsl $workspaceWsl -DistroName $selectedDistro
        }
        if ($null -ne $state) {
            $brokerResult = Invoke-MsysBrokerCommand -State $state -Arguments $cliArgs
            if ($brokerResult.Connected) {
                if (-not [string]::IsNullOrWhiteSpace([string]$brokerResult.Error)) {
                    [Console]::Error.WriteLine([string]$brokerResult.Error)
                }
                exit ([int]$brokerResult.ExitCode)
            }
            throw $brokerResult.Error
        }
    } catch {
        if ($brokerMode -eq "on") {
            throw "MSYS local broker is required but unavailable: $($_.Exception.Message)"
        }
        [Console]::Error.WriteLine("MSYS local broker unavailable; using one-shot WSL compatibility mode: $($_.Exception.Message)")
    }
}

$wslArgs = @()
if (-not [string]::IsNullOrWhiteSpace($selectedDistro)) {
    $wslArgs += @("-d", $selectedDistro)
}
if ($interactiveShell) {
    $shellRc = "$workspaceWsl/msys-tools/scripts/msys-dev-shell.rc"
    $wslArgs += @(
        "--cd", $workingDirectoryWsl,
        "--exec", "env",
        "PYTHONPATH=$workspaceWsl/msys-tools",
        "MSYS_DEV_ROOT=$workspaceWsl",
        "MSYS_DEV_PYTHONPATH=$workspaceWsl/msys-tools",
        "PYTHONDONTWRITEBYTECODE=1",
        "bash", "--noprofile", "--rcfile", $shellRc, "-i"
    )
} else {
    $wslArgs += @(
        "--cd", $workingDirectoryWsl,
        "--exec", "env",
        "PYTHONPATH=$workspaceWsl/msys-tools",
        "MSYS_DEV_ROOT=$workspaceWsl",
        "PYTHONDONTWRITEBYTECODE=1",
        "python3", "-m", "msys_tools.dev"
    )
    $wslArgs += $cliArgs
}

& $wsl.Source @wslArgs
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) {
    $exitCode = 0
}
exit $exitCode
