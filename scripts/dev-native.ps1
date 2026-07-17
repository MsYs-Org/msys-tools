<#!
.SYNOPSIS
    Fast Windows-to-board MSYS development loop without WSL or Python.

.DESCRIPTION
    Archives selected repositories once, uploads one archive over SSH,
    extracts it in the private development tree, and optionally builds/tests
    natively on the AArch64 target using its isolated compiler and runtime.
#>

[CmdletBinding()]
param(
    [ValidateSet("sync", "build", "test", "sync-build", "sync-build-test")]
    [string]$Mode = "sync-build",
    [string]$Target = $env:MSYS_DEV_TARGET,
    [string]$RemoteRoot = $(if ($env:MSYS_DEV_REMOTE) { $env:MSYS_DEV_REMOTE } else { "/opt/msys-dev" }),
    [string]$SshKey = $env:MSYS_DEV_SSH_KEY,
    [string[]]$Repository = @(
        "msys-ui-lvgl", "msys-sdk", "msys-shell-native", "msys-settings",
        "msys-calculator", "msys-device-info", "msys-notes",
        "msys-file-manager", "msys-touch-calibration", "msys-input-touch"
    )
)

$ErrorActionPreference = "Stop"
$Workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Archive = Join-Path ([System.IO.Path]::GetTempPath()) ("msys-dev-{0}.tar.gz" -f ([guid]::NewGuid().ToString("N")))

function Invoke-Remote {
    param([string]$Command)
    $args = @()
    if ($SshKey) { $args += @("-i", $SshKey) }
    $args += @("-o", "BatchMode=yes", $Target, $Command)
    & ssh @args
    if ($LASTEXITCODE -ne 0) { throw "ssh failed with exit code $LASTEXITCODE" }
}

if (-not $Target) { throw "set -Target or MSYS_DEV_TARGET (for example root@192.168.1.215)" }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { throw "ssh is required" }
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) { throw "scp is required" }
if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { throw "tar.exe is required" }

$doSync = $Mode -in @("sync", "sync-build", "sync-build-test")
$doBuild = $Mode -in @("build", "sync-build", "sync-build-test")
$doTest = $Mode -in @("test", "sync-build-test")
$repoList = $Repository -join " "

try {
    if ($doSync) {
        foreach ($repo in $Repository) {
            if (-not (Test-Path (Join-Path $Workspace $repo))) { throw "repository not found: $repo" }
        }
        & tar.exe -czf $Archive -C $Workspace --exclude=.git --exclude=build --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache @Repository
        if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
        Invoke-Remote ("mkdir -p -m 0700 {0}/.incoming-dev" -f $RemoteRoot)
        $scpArgs = @()
        if ($SshKey) { $scpArgs += @("-i", $SshKey) }
        $scpArgs += @("-o", "BatchMode=yes", $Archive, ("{0}:{1}/.incoming-dev/source.tar.gz" -f $Target, $RemoteRoot))
        & scp @scpArgs
        if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }
        Invoke-Remote ('set -eu; root="{0}"; for repo in {1}; do rm -rf "$root/$repo"; done; tar -xzf "$root/.incoming-dev/source.tar.gz" -C "$root"; rm -rf "$root/.incoming-dev"' -f $RemoteRoot, $repoList)
    }
    if ($doBuild) {
        Invoke-Remote ('set -eu; root="{0}"; for repo in {1}; do make -C "$root/$repo" clean; make -j2 -C "$root/$repo" all; done' -f $RemoteRoot, $repoList)
    }
    if ($doTest) {
        Invoke-Remote ('set -eu; root="{0}"; make -C "$root/msys-ui-lvgl" test; make -C "$root/msys-shell-native" test' -f $RemoteRoot)
    }
} finally {
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
}

Write-Output "dev-native: $Mode complete"
