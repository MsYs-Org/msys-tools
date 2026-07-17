#requires -Version 5.1
<# Windows-native emergency path for hosts where WSL is unavailable. #>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$NativeArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# msys.cmd transfers the post---native argv through its private environment so
# cmd.exe can remove that routing token without truncating a long argument tail.
if ($env:MSYS_NATIVE_ARG_COUNT -match "^[0-9]+$") {
    $forwarded = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt [int]$env:MSYS_NATIVE_ARG_COUNT; $index++) {
        $forwarded.Add([string][Environment]::GetEnvironmentVariable("MSYS_NATIVE_ARG_$index"))
    }
    if ($forwarded.Count -gt 0) {
        $Command = $forwarded[0]
        $NativeArgs = [string[]]@($forwarded | Select-Object -Skip 1)
    }
}
$script:Workspace = Split-Path -Parent $PSScriptRoot
$script:ConfigPath = if ($env:MSYS_NATIVE_CONFIG) {
    $env:MSYS_NATIVE_CONFIG
} else {
    Join-Path $HOME ".config\msys-dev\native-windows.json"
}

function Write-NativeUsage {
    @"
MSYS native Windows path (no WSL)

  .\msys.cmd --native sync --repo msys-settings
  .\msys.cmd --native sync --repo msys-device-info --test --probe
  .\msys.cmd --native deliver --repo msys-settings
  .\msys.cmd --native fast --repo msys-settings --deliver
  .\msys.cmd --native ssh
  .\msys.cmd --native tail
  .\msys.cmd --native screenshot .\artifacts\home.png
  .\msys.cmd --native settings-smoke --screenshot .\artifacts\settings.png --force
  .\msys.cmd --native components
  .\msys.cmd --native start org.msys.settings:main
  .\msys.cmd --native stop org.msys.settings:main
  .\msys.cmd --native select-role input-method org.msys.input.touch:keyboard-lvgl
  .\msys.cmd --native call role:hal list_devices {}

Optional config: $script:ConfigPath
JSON keys: target, remote, runtime_dir, state_dir, log_file, display, ssh_key,
workspace. Passwords are never read or stored.

sync/fast options: --test runs the repository's bounded tests and --probe runs
its Xvfb probe in the same target build. Neither option runs doctor.
"@ | Write-Host
}

function Get-PropertyValue {
    param($Object, [string]$Name, $Default)
    if ($null -ne $Object) {
        $property = $Object.PSObject.Properties[$Name]
        if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            return $property.Value
        }
    }
    return $Default
}

function Test-SafeRemoteRoot {
    param([string]$Path)
    return (
        $Path.StartsWith("/") -and $Path -ne "/" -and
        $Path -notmatch "(?:^|/)\.\.(?:/|$)" -and
        $Path -notmatch "[\x00-\x1f\x7f]"
    )
}

function Read-NativeConfig {
    $document = $null
    if (Test-Path -LiteralPath $script:ConfigPath -PathType Leaf) {
        $document = Get-Content -LiteralPath $script:ConfigPath -Raw | ConvertFrom-Json
    }
    $workspace = [string](Get-PropertyValue $document "workspace" $script:Workspace)
    $workspace = (Get-Item -LiteralPath $workspace -ErrorAction Stop).FullName
    $target = [string](Get-PropertyValue $document "target" "root@192.168.1.215")
    $remote = [string](Get-PropertyValue $document "remote" "/opt/msys-dev")
    $runtimeDir = [string](Get-PropertyValue $document "runtime_dir" "/tmp/msys-main")
    $stateDir = [string](Get-PropertyValue $document "state_dir" "/opt/msys-state")
    $logFile = [string](Get-PropertyValue $document "log_file" "/tmp/msysd.log")
    $display = [string](Get-PropertyValue $document "display" ":24")
    $key = [string](Get-PropertyValue $document "ssh_key" (Join-Path $HOME ".ssh\msys-dev-windows-ed25519"))
    if ($target -notmatch "^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$") {
        throw "native config target is invalid: $target"
    }
    foreach ($pair in @(@("remote", $remote), @("runtime_dir", $runtimeDir), @("state_dir", $stateDir), @("log_file", $logFile))) {
        if (-not (Test-SafeRemoteRoot ([string]$pair[1]))) {
            throw "native config $($pair[0]) must be a non-root absolute path without '..'"
        }
    }
    if ($display -notmatch "^:[0-9]+(?:\.[0-9]+)?$") {
        throw "native config display is invalid: $display"
    }
    return [PSCustomObject]@{
        target = $target
        remote = $remote.TrimEnd("/")
        runtime_dir = $runtimeDir
        state_dir = $stateDir.TrimEnd("/")
        log_file = $logFile
        display = $display
        ssh_key = [Environment]::ExpandEnvironmentVariables($key)
        workspace = $workspace
    }
}

function Quote-Sh {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
}

function Get-SshOptions {
    $arguments = @("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2")
    if (Test-Path -LiteralPath $script:Config.ssh_key -PathType Leaf) {
        $arguments += @("-o", "IdentitiesOnly=yes", "-i", $script:Config.ssh_key)
    }
    return [string[]]$arguments
}

function ConvertTo-RemoteEnvelope {
    param([Parameter(Mandatory = $true)][string]$RemoteCommand)
    # Windows PowerShell 5's native argv binder can consume quotes embedded in
    # an ssh remote command. Base64 keeps the argv ASCII-only; the target still
    # executes one ordinary POSIX sh session.
    $bytes = [Text.Encoding]::UTF8.GetBytes($RemoteCommand)
    $encoded = [Convert]::ToBase64String($bytes)
    return "printf %s '$encoded' | base64 -d | sh"
}

function Invoke-Ssh {
    param([Parameter(Mandatory = $true)][string]$RemoteCommand)
    & ssh.exe @(Get-SshOptions) $script:Config.target (ConvertTo-RemoteEnvelope $RemoteCommand)
    if ($LASTEXITCODE -ne 0) { throw "ssh failed with exit status $LASTEXITCODE" }
}

function Invoke-SshCapture {
    param([Parameter(Mandatory = $true)][string]$RemoteCommand)
    $lines = @(& ssh.exe @(Get-SshOptions) $script:Config.target (ConvertTo-RemoteEnvelope $RemoteCommand))
    if ($LASTEXITCODE -ne 0) {
        $detail = ($lines -join "`n").Trim()
        if ($detail.Length -gt 1200) { $detail = $detail.Substring($detail.Length - 1200) }
        if ($detail) { throw "ssh failed with exit status ${LASTEXITCODE}: $detail" }
        throw "ssh failed with exit status $LASTEXITCODE"
    }
    return ($lines -join "`n")
}

function Get-RemotePythonPrelude {
    return (
        "python=/opt/msys/current/.runtime/python/bin/python3; " +
        "if test ! -x `"`$python`"; then python=" + (Quote-Sh ($script:Config.remote + "/.runtime/python/bin/python3")) + "; fi; " +
        "test -x `"`$python`""
    )
}

function Get-RemotePythonInvocation {
    return (
        "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=" +
        (Quote-Sh ($script:Config.remote + "/msys-tools:/opt/msys/current/msys-tools")) +
        " `"`$python`" -B"
    )
}

function Invoke-RemoteControl {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][hashtable]$Payload,
        [switch]$ResponseOnly,
        [double]$Timeout = 30
    )
    $payloadJson = $Payload | ConvertTo-Json -Compress -Depth 8
    $remoteCommand = (
        "set -eu; " + (Get-RemotePythonPrelude) + "; " +
        (Get-RemotePythonInvocation) + " -m msys_tools.remote_ctl" +
        " --runtime-dir " + (Quote-Sh $script:Config.runtime_dir) +
        " --target " + (Quote-Sh $Target) +
        " --method " + (Quote-Sh $Method) +
        " --payload " + (Quote-Sh $payloadJson) +
        " --timeout " + ([string]$Timeout)
    )
    if ($ResponseOnly) { $remoteCommand += " --response-only" }
    return Invoke-SshCapture $remoteCommand
}

function Get-RepoName {
    param([string[]]$Arguments)
    $name = $null
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ($Arguments[$index] -eq "--repo") {
            if ($index + 1 -ge $Arguments.Count) { throw "--repo requires a value" }
            $name = $Arguments[++$index]
        } elseif ($Arguments[$index].StartsWith("--repo=")) {
            $name = $Arguments[$index].Substring(7)
        }
    }
    if ([string]::IsNullOrWhiteSpace($name)) {
        $leaf = Split-Path -Leaf (Get-Location).Path
        if ($leaf.StartsWith("msys-")) { $name = $leaf }
    }
    if ([string]::IsNullOrWhiteSpace($name) -or $name -notmatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$") {
        throw "select one repository with --repo NAME"
    }
    return $name
}

function Get-OptionValue {
    param([string[]]$Arguments, [string]$Name)
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ($Arguments[$index] -eq $Name) {
            if ($index + 1 -ge $Arguments.Count) { throw "$Name requires a value" }
            return $Arguments[$index + 1]
        }
        if ($Arguments[$index].StartsWith($Name + "=")) {
            return $Arguments[$index].Substring($Name.Length + 1)
        }
    }
    return $null
}

function Get-RepoPath {
    param([string]$Name)
    $path = (Get-Item -LiteralPath (Join-Path $script:Config.workspace $Name) -ErrorAction Stop).FullName
    $root = $script:Config.workspace.TrimEnd("\", "/") + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "repository escapes the configured workspace: $Name"
    }
    return $path
}

function Get-PythonUnitTestCommand {
    param([string]$Stage)
    $pythonPath = $Stage + "/files/app:" + $script:Config.remote + "/msys-sdk"
    return (
        "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=" + (Quote-Sh $pythonPath) +
        " `"`$python`" -B -m unittest discover -s tests -v"
    )
}

function Assert-BuildChecksSupported {
    param([string]$Name, [switch]$RunTest, [switch]$RunProbe)
    $testRepositories = @(
        "msys-ui-lvgl", "msys-shell-native", "msys-settings",
        "msys-file-manager", "msys-touch-calibration", "msys-input-touch",
        "msys-calculator", "msys-device-info", "msys-notes"
    )
    $probeRepositories = @(
        "msys-ui-lvgl", "msys-shell-native", "msys-settings",
        "msys-file-manager", "msys-touch-calibration", "msys-input-touch",
        "msys-device-info", "msys-notes"
    )
    if ($RunTest -and $Name -notin $testRepositories) {
        throw "--test is not supported by repository $Name"
    }
    if ($RunProbe -and $Name -notin $probeRepositories) {
        throw "--probe is not supported by repository $Name"
    }
}

function Get-TargetBuildCommand {
    param(
        [string]$Name,
        [string]$Stage,
        [switch]$RunTest,
        [switch]$RunProbe
    )
    $stageQ = Quote-Sh $Stage
    $sdkQ = Quote-Sh ($script:Config.remote + "/msys-sdk")
    switch ($Name) {
        "msys-sdk" { return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 clean; MAKEFLAGS= MFLAGS= make -j1 CFLAGS='-Os -g0 -DNDEBUG -std=c11 -Wall -Wextra -Wpedantic' all" }
        "msys-core" { return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 -C native clean; MAKEFLAGS= MFLAGS= make -j1 -C native OPTIMIZE=-Os DEBUG_INFO=-g0 all" }
        "msys-shell-native" {
            $targets = @("all")
            if ($RunTest) { $targets += "test" }
            if ($RunProbe) { $targets += "lvgl-probe" }
            $targetText = $targets -join " "
            return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 SDK_DIR=$sdkQ clean; MAKEFLAGS= MFLAGS= make -j1 SDK_DIR=$sdkQ CFLAGS='-Os -g0 -DNDEBUG -std=c11 -Wall -Wextra -Wpedantic -Werror' $targetText"
        }
        "msys-ui-lvgl" {
            $targets = @("stage")
            if ($RunTest) { $targets += "test" }
            if ($RunProbe) { $targets += "probe" }
            return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 clean; MAKEFLAGS= MFLAGS= make -j2 $($targets -join ' ')"
        }
        "msys-settings" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("all")
            if ($RunProbe) { $targets += "probe" }
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_DIR=$uiQ clean; MAKEFLAGS= MFLAGS= make -j2 UI_DIR=$uiQ $($targets -join ' ')"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-file-manager" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("stage")
            if ($RunTest) { $targets += "test" }
            if ($RunProbe) { $targets += "probe" }
            return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ clean; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ $($targets -join ' ')"
        }
        "msys-touch-calibration" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("all")
            if ($RunProbe) { $targets += "probe" }
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_DIR=$uiQ clean; MAKEFLAGS= MFLAGS= make -j2 UI_DIR=$uiQ $($targets -join ' ')"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-input-touch" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("stage")
            if ($RunProbe) { $targets += "probe" }
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ clean; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ $($targets -join ' ')"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-calculator" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_DIR=$uiQ clean; MAKEFLAGS= MFLAGS= make -j2 UI_DIR=$uiQ all"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-device-info" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("all")
            if ($RunProbe) { $targets += "probe" }
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_DIR=$uiQ clean; MAKEFLAGS= MFLAGS= make -j2 UI_DIR=$uiQ $($targets -join ' ')"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-notes" {
            $uiQ = Quote-Sh ($script:Config.remote + "/msys-ui-lvgl")
            $targets = @("stage")
            if ($RunProbe) { $targets += "probe" }
            $command = "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ clean; MAKEFLAGS= MFLAGS= make -j1 UI_ROOT=$uiQ SDK_ROOT=$sdkQ $($targets -join ' ')"
            if ($RunTest) { $command += "; " + (Get-PythonUnitTestCommand $Stage) }
            return $command
        }
        "msys-openstick-ch347" {
            return "cd $stageQ; chmod 0755 files/x11display/bin/ch347_dirty_usb_sink files/x11display/bin/ch347_st7796_test files/x11display/bin/xdamage_shm_capture scripts/*.sh files/x11display/scripts/*.sh; bash -n scripts/msys_ch347_x11_provider.sh files/x11display/scripts/*.sh"
        }
        "msys-hal" { return "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 -C native MSYS_SDK_DIR=$sdkQ clean; MAKEFLAGS= MFLAGS= make -j1 -C native MSYS_SDK_DIR=$sdkQ CFLAGS='-Os -g0 -DNDEBUG' all" }
        "msys-x11-session" { return "cd $stageQ; MAKEFLAGS= MFLAGS= make clean; MAKEFLAGS= MFLAGS= make SDK_ROOT=$sdkQ CFLAGS='-Os -g0 -DNDEBUG -Wall -Wextra -Werror -std=c11' all" }
        "msys-audio" {
            $runtimeRoot = Quote-Sh ($Stage + "/files/runtime/aarch64")
            $inventoryCode = 'import hashlib,json,pathlib,sys;p=pathlib.Path(sys.argv[1]);rel="files/runtime/aarch64/bin/msys-hci-bootstrap";f=p/rel;inv=p/"files/runtime/aarch64/runtime.json";d=json.loads(inv.read_text());e={"path":rel,"size":f.stat().st_size,"sha256":hashlib.sha256(f.read_bytes()).hexdigest()};d["files"]=sorted([x for x in d["files"] if x.get("path")!=rel]+[e],key=lambda x:x["path"]);inv.write_text(json.dumps(d,indent=2)+"\n")'
            return (
                "cd $stageQ; MAKEFLAGS= MFLAGS= make -j1 -C native clean; " +
                "MAKEFLAGS= MFLAGS= make -j1 -C native all; " +
                "MAKEFLAGS= MFLAGS= make -j1 -C native DESTDIR=$runtimeRoot install; " +
                (Get-RemotePythonInvocation) + " -c " + (Quote-Sh $inventoryCode) + " $stageQ"
            )
        }
        default { return ":" }
    }
}

function Sync-Repository {
    param(
        [string]$Name,
        [switch]$RunTest,
        [switch]$RunProbe
    )
    Assert-BuildChecksSupported -Name $Name -RunTest:$RunTest -RunProbe:$RunProbe
    $repo = Get-RepoPath $Name
    $token = [Guid]::NewGuid().ToString("N")
    $archive = Join-Path ([IO.Path]::GetTempPath()) ("msys-native-" + $Name + "-" + $token + ".tar")
    $remoteArchive = $script:Config.remote + "/.sync-upload-" + $Name + "-" + $token + ".tar"
    $stage = $script:Config.remote + "/.sync/" + $Name + ".new." + $token
    $destination = $script:Config.remote + "/" + $Name
    $previous = $script:Config.remote + "/." + $Name + ".previous"
    try {
        & tar.exe -cf $archive --exclude=.git --exclude=build --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.cache --exclude="*.pyc" -C $repo .
        if ($LASTEXITCODE -ne 0) { throw "tar failed with exit status $LASTEXITCODE" }
        & scp.exe @(Get-SshOptions) $archive ("{0}:{1}" -f $script:Config.target, $remoteArchive)
        if ($LASTEXITCODE -ne 0) { throw "scp failed with exit status $LASTEXITCODE" }
        $build = Get-TargetBuildCommand -Name $Name -Stage $stage -RunTest:$RunTest -RunProbe:$RunProbe
        $remoteCommand = (
            "set -eu; " + (Get-RemotePythonPrelude) + "; archive=" + (Quote-Sh $remoteArchive) + "; stage=" + (Quote-Sh $stage) + "; " +
            "trap 'rm -f `"`$archive`"; rm -rf `"`$stage`"' EXIT HUP INT TERM; " +
            "mkdir -p " + (Quote-Sh ($script:Config.remote + "/.sync")) + "; rm -rf `"`$stage`"; mkdir -p `"`$stage`"; " +
            "tar -tf `"`$archive`" | while IFS= read -r entry; do case `"`$entry`" in /*|../*|*/../*|*/..) exit 65;; esac; done; " +
            "tar -xf `"`$archive`" -C `"`$stage`"; " + $build + "; " +
            "rm -rf " + (Quote-Sh $previous) + "; moved=0; " +
            "if test -e " + (Quote-Sh $destination) + "; then mv " + (Quote-Sh $destination) + " " + (Quote-Sh $previous) + "; moved=1; fi; " +
            "if mv `"`$stage`" " + (Quote-Sh $destination) + "; then :; else status=`$?; if test `"`$moved`" = 1 && test ! -e " + (Quote-Sh $destination) + "; then mv " + (Quote-Sh $previous) + " " + (Quote-Sh $destination) + "; fi; exit `"`$status`"; fi; " +
            "trap - EXIT HUP INT TERM; rm -f `"`$archive`""
        )
        Invoke-Ssh $remoteCommand
        Write-Host "[ok] synced $Name"
    } finally {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    }
}

function Deliver-Repository {
    param([string]$Name)
    $token = [Guid]::NewGuid().ToString("N")
    $source = $script:Config.remote + "/" + $Name
    $output = $script:Config.remote + "/.native-build/" + $Name + "-" + $token
    $metadata = $output + "/build.json"
    $payloadFile = $output + "/payload.json"
    $stageCode = 'import json,os,pathlib,re,sys;m=json.loads(pathlib.Path(sys.argv[1]).read_text());out=pathlib.Path(sys.argv[2]).resolve();a=pathlib.Path(m["artifact"]).resolve();h=m["sha256"];p=m["package"];v=m["version"];assert out in a.parents and re.fullmatch(r"[a-f0-9]{64}",h) and re.fullmatch(r"[A-Za-z0-9._-]+",p) and re.fullmatch(r"[A-Za-z0-9._+~-]+",v);d=pathlib.Path(sys.argv[3])/"updates/staged-rpc";d.mkdir(parents=True,exist_ok=True,mode=0o700);s=d/(h+".maf");os.replace(a,s);s.chmod(0o600);print(json.dumps({"path":str(s),"sha256":h,"package":p,"version":v,"remote":True,"require_sha256":True,"require_content_hashes":True},separators=(",",":")))'
    $cleanupStageCode = 'import json,pathlib,re,sys;p=pathlib.Path(json.loads(pathlib.Path(sys.argv[1]).read_text())["path"]).resolve();d=(pathlib.Path(sys.argv[2])/"updates/staged-rpc").resolve();assert p.parent==d and re.fullmatch(r"[a-f0-9]{64}\.maf",p.name);p.unlink(missing_ok=True)'
    $buildCommand = (
        "set -eu; " + (Get-RemotePythonPrelude) + "; mkdir -p " + (Quote-Sh $output) +
        "; trap 'rm -rf " + (Quote-Sh $output) + "' EXIT HUP INT TERM; cd " + (Quote-Sh $script:Config.remote) + "; " +
        (Get-RemotePythonInvocation) + " -m msys_tools.dev package build " + (Quote-Sh $source) +
        " --root " + (Quote-Sh $script:Config.remote) + " --output " + (Quote-Sh $output) + " --format maf --force"
    )
    if ($Name -in @("msys-settings", "msys-notes", "msys-calculator", "msys-device-info", "msys-file-manager", "msys-touch-calibration", "msys-input-touch")) {
        $buildCommand += " --overlay " + (Quote-Sh ($script:Config.remote + "/msys-sdk/msys_sdk=files/app/msys_sdk"))
    }
    $buildCommand += (
        " > " + (Quote-Sh $metadata) + "; " +
        (Get-RemotePythonInvocation) + " -c " + (Quote-Sh $stageCode) + " " +
        (Quote-Sh $metadata) + " " + (Quote-Sh $output) + " " + (Quote-Sh $script:Config.state_dir) +
        " > " + (Quote-Sh $payloadFile) + "; " +
        "if " + (Get-RemotePythonInvocation) + " -m msys_tools.remote_ctl --runtime-dir " +
        (Quote-Sh $script:Config.runtime_dir) + " --target role:install-agent --method install_archive" +
        " --payload `"`$(cat " + (Quote-Sh $payloadFile) + ")`" --timeout 120; then status=0; else status=`$?; fi; " +
        (Get-RemotePythonInvocation) + " -c " + (Quote-Sh $cleanupStageCode) + " " +
        (Quote-Sh $payloadFile) + " " + (Quote-Sh $script:Config.state_dir) + "; " +
        "trap - EXIT HUP INT TERM; rm -rf " + (Quote-Sh $output) + "; exit `"`$status`""
    )
    $response = Invoke-SshCapture $buildCommand
    $result = $response | ConvertFrom-Json
    if ($result.response.type -ne "return") { throw "install-agent rejected $Name" }
    Write-Host "[ok] installed $Name"
}

function Show-HealthAndLogs {
    $payloadJson = "{}"
    $marker = "__MSYS_NATIVE_LOG__"
    $remoteCommand = (
        "set -eu; " + (Get-RemotePythonPrelude) + "; " + (Get-RemotePythonInvocation) +
        " -m msys_tools.remote_ctl --runtime-dir " + (Quote-Sh $script:Config.runtime_dir) +
        " --target msys.core --method list_components --payload " + (Quote-Sh $payloadJson) +
        " --response-only; printf '\n" + $marker + "\n'; tail -n 12 " + (Quote-Sh $script:Config.log_file) + " 2>/dev/null || true"
    )
    $text = Invoke-SshCapture $remoteCommand
    $position = $text.IndexOf($marker, [StringComparison]::Ordinal)
    if ($position -lt 0) { throw "combined health report is incomplete" }
    $response = $text.Substring(0, $position).Trim() | ConvertFrom-Json
    if ($response.type -ne "return") { throw "Core health request failed" }
    $components = @($response.payload.components)
    $ready = @($components | Where-Object { $_.state -eq "ready" }).Count
    $bad = @($components | Where-Object { $_.state -notin @("ready", "stopped", "declared") })
    if ($bad.Count -eq 0) { Write-Host "[ok] health ready=$ready total=$($components.Count)" }
    else { Write-Host "[warn] health ready=$ready total=$($components.Count) unhealthy=$($bad.Count)" }
    $logs = $text.Substring($position + $marker.Length).Trim()
    if ($logs) { Write-Host "recent log:"; Write-Output $logs }
}

function Save-Screenshot {
    param([string[]]$Arguments)
    if ($Arguments.Count -lt 1) { throw "screenshot requires a Windows output path" }
    $force = $Arguments -contains "--force"
    $output = [IO.Path]::GetFullPath($Arguments[0])
    if ((Test-Path -LiteralPath $output) -and -not $force) { throw "output exists; pass --force: $output" }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $output)) | Out-Null
    $token = [Guid]::NewGuid().ToString("N")
    $remotePath = "/tmp/msys-screenshot-$token.png"
    $capture = "set -eu; " + (Get-RemotePythonPrelude) + "; " + (Get-RemotePythonInvocation) + " -m msys_tools.remote_screenshot --runtime-dir " + (Quote-Sh $script:Config.runtime_dir) + " --output " + (Quote-Sh $remotePath) + " --backend auto --timeout 20 --display " + (Quote-Sh $script:Config.display)
    [void](Invoke-SshCapture $capture)
    $temporary = $output + "." + $token + ".part"
    try {
        & scp.exe @(Get-SshOptions) ("{0}:{1}" -f $script:Config.target, $remotePath) $temporary
        if ($LASTEXITCODE -ne 0) { throw "screenshot download failed with exit status $LASTEXITCODE" }
        $stream = [IO.File]::OpenRead($temporary)
        try {
            $header = New-Object byte[] 8
            if ($stream.Read($header, 0, 8) -ne 8 -or [BitConverter]::ToString($header) -ne "89-50-4E-47-0D-0A-1A-0A") { throw "downloaded screenshot is not PNG" }
        } finally { $stream.Dispose() }
        Move-Item -LiteralPath $temporary -Destination $output -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        try { Invoke-Ssh ("rm -f " + (Quote-Sh $remotePath)) } catch { }
    }
    Write-Host "[ok] screenshot $output"
}

function Invoke-SettingsSmoke {
    param([string[]]$Arguments)
    $timeoutText = Get-OptionValue $Arguments "--timeout"
    if ($null -eq $timeoutText) { $timeoutText = "12" }
    $timeout = 0.0
    if (-not [double]::TryParse($timeoutText, [ref]$timeout) -or $timeout -le 0 -or $timeout -gt 120) {
        throw "--timeout must be greater than zero and at most 120 seconds"
    }
    $screenshot = Get-OptionValue $Arguments "--screenshot"
    $displayLog = Get-OptionValue $Arguments "--display-log"
    if ($null -eq $displayLog) { $displayLog = "/tmp/ch347_dirty_usb_x11/live.log" }
    $force = $Arguments -contains "--force"
    $remoteCommand = (
        "set -u; " + (Get-RemotePythonPrelude) + "; " +
        (Get-RemotePythonInvocation) + " -m msys_tools.remote_settings_smoke" +
        " --runtime-dir " + (Quote-Sh $script:Config.runtime_dir) +
        " --timeout " + (Quote-Sh $timeoutText) +
        " --display " + (Quote-Sh $script:Config.display) +
        " --display-log " + (Quote-Sh $displayLog)
    )
    if ($null -ne $screenshot) { $remoteCommand += " --capture" }
    # The JSON report is useful on failure too; keep SSH successful and apply
    # the smoke status after decoding the one remote reply.
    $remoteCommand += "; status=`$?; exit 0"
    $document = Invoke-SshCapture $remoteCommand | ConvertFrom-Json
    if ([string]$document.schema -ne "msys.settings-smoke.v1") {
        throw "settings-smoke returned the wrong schema"
    }
    if ($null -ne $screenshot -and $document.ok -eq $true) {
        $output = [IO.Path]::GetFullPath($screenshot)
        if ((Test-Path -LiteralPath $output) -and -not $force) { throw "output exists; pass --force: $output" }
        [IO.Directory]::CreateDirectory((Split-Path -Parent $output)) | Out-Null
        try { $png = [Convert]::FromBase64String([string]$document.screenshot.png_base64) }
        catch { throw "settings-smoke returned an invalid screenshot encoding" }
        $signature = [byte[]](0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a)
        if ($png.Length -lt 8 -or [BitConverter]::ToString($png, 0, 8) -ne [BitConverter]::ToString($signature)) {
            throw "settings-smoke returned a non-PNG screenshot"
        }
        $temporary = $output + "." + [Guid]::NewGuid().ToString("N") + ".part"
        try {
            [IO.File]::WriteAllBytes($temporary, $png)
            Move-Item -LiteralPath $temporary -Destination $output -Force:$force
        } finally {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        $document.screenshot.PSObject.Properties.Remove("png_base64")
        $document.screenshot | Add-Member -NotePropertyName saved -NotePropertyValue $output
    }
    $document | ConvertTo-Json -Depth 12
    if ($document.ok -ne $true) { throw "settings-smoke failed: $($document.error)" }
    Write-Host "[ok] settings LVGL smoke"
}

$script:Config = Read-NativeConfig
$commandName = $Command.ToLowerInvariant()
switch ($commandName) {
    { $_ -in @("help", "-h", "--help") } { Write-NativeUsage; exit 0 }
    "config" { Write-Host "config: $script:ConfigPath"; $script:Config | ConvertTo-Json -Depth 3; exit 0 }
    "sync" {
        Sync-Repository -Name (Get-RepoName $NativeArgs) `
            -RunTest:($NativeArgs -contains "--test") `
            -RunProbe:($NativeArgs -contains "--probe")
        exit 0
    }
    "deliver" {
        $repo = Get-RepoName $NativeArgs
        Deliver-Repository $repo
        Show-HealthAndLogs
        exit 0
    }
    { $_ -in @("fast", "q") } {
        $repo = Get-RepoName $NativeArgs
        Sync-Repository -Name $repo `
            -RunTest:($NativeArgs -contains "--test") `
            -RunProbe:($NativeArgs -contains "--probe")
        if ($NativeArgs -contains "--deliver") { Deliver-Repository $repo }
        Show-HealthAndLogs
        $screenshot = Get-OptionValue $NativeArgs "--screenshot"
        if ($null -ne $screenshot) { Save-Screenshot @([string]$screenshot, "--force") }
        exit 0
    }
    "ssh" {
        if ($NativeArgs.Count -eq 0) { & ssh.exe @(Get-SshOptions) $script:Config.target }
        else { & ssh.exe @(Get-SshOptions) $script:Config.target ($NativeArgs -join " ") }
        exit $LASTEXITCODE
    }
    { $_ -in @("tail", "log") } { Invoke-Ssh ("tail -n 200 -f " + (Quote-Sh $script:Config.log_file)); exit 0 }
    { $_ -in @("components", "ps") } { Write-Output (Invoke-RemoteControl -Target "msys.core" -Method "list_components" -Payload @{}); exit 0 }
    { $_ -in @("start", "stop") } {
        if ($NativeArgs.Count -ne 1 -or $NativeArgs[0] -notmatch "^[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$") {
            throw "$commandName syntax: $commandName PACKAGE:COMPONENT"
        }
        Write-Output (Invoke-RemoteControl -Target "msys.core" -Method $commandName -Payload @{ component = $NativeArgs[0] } -Timeout 30)
        exit 0
    }
    "select-role" {
        if ($NativeArgs.Count -ne 2 -or
            $NativeArgs[0] -notmatch "^[A-Za-z0-9._-]+$" -or
            $NativeArgs[1] -notmatch "^[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$") {
            throw "select-role syntax: select-role ROLE PACKAGE:COMPONENT"
        }
        Write-Output (Invoke-RemoteControl -Target "msys.core" -Method "select_role" `
            -Payload @{ role = $NativeArgs[0]; provider = $NativeArgs[1] } -Timeout 30)
        exit 0
    }
    "call" {
        if ($NativeArgs.Count -lt 2 -or $NativeArgs.Count -gt 3) { throw "call syntax: call TARGET METHOD [JSON_OBJECT]" }
        $payload = @{}
        if ($NativeArgs.Count -eq 3) {
            $decoded = $NativeArgs[2] | ConvertFrom-Json
            if ($null -eq $decoded -or $decoded -isnot [PSCustomObject]) { throw "call payload must be a JSON object" }
            foreach ($property in $decoded.PSObject.Properties) { $payload[$property.Name] = $property.Value }
        }
        Write-Output (Invoke-RemoteControl -Target $NativeArgs[0] -Method $NativeArgs[1] -Payload $payload)
        exit 0
    }
    "screenshot" { Save-Screenshot $NativeArgs; exit 0 }
    "settings-smoke" { Invoke-SettingsSmoke $NativeArgs; exit 0 }
    default { throw "unsupported native command '$Command'; run --native help" }
}
