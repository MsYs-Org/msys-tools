# Host Service Installation

MSYS is a user-space session, not a replacement for Linux PID 1. The host
service launcher starts `msysd --foreground` inside a detached background
subshell, records its PID, and implements `start`, `stop`, `restart`, and
`status`. The host init mechanism only invokes that launcher after normal boot.

No part of this flow calls systemd, D-Bus, logind, polkit, `apt`, `pip`, or
another target package manager. It expects the same SSH, POSIX `sh`, basic file
utilities, and isolated MSYS Python runtime used by the development flow.

## Detection and selection

```powershell
python -m msys_tools.dev host-service detect
```

`auto` selects the first detected mechanism in this order:

1. active OpenRC (`/run/openrc` plus `/sbin/openrc-run`);
2. SysV-compatible `/etc/init.d`;
3. an existing regular `/etc/rc.local`.

Detection is advisory. An operator can explicitly select `sysv`, `openrc`, or
`rc-local`. A custom startup file is never guessed and requires both:

```powershell
python -m msys_tools.dev host-service install --backend hook --hook /etc/board-startup.sh
```

The custom file must be a POSIX shell hook. Its parent directory must already
exist. `rc-local` may create `/etc/rc.local` when `/etc` exists.

## Plan, install, and start

Always inspect the target-specific plan first:

```powershell
python -m msys_tools.dev host-service install --backend auto --dry-run
```

Dry-run may connect over SSH to read prior state and detect the host, but it
does not upload, write, enable, start, stop, or remove anything.

Install for the next boot:

```powershell
python -m msys_tools.dev host-service install --backend sysv
```

The default paths come from persistent `msys-dev` configuration:

- workspace: `/opt/msys-dev`;
- isolated Python: `/opt/msys-dev/.runtime/python/bin/python3`;
- runtime directory: configured `runtime_dir`, normally `/tmp/msys-main` or
  `/run/msys/main`;
- log: configured `log_file`, normally `/tmp/msysd.log`;
- state: `/opt/msys-state`;
- profile: `mobile-spi`.

These are development-mode defaults. Passing `--release-root /opt/msys`
switches the generated launcher to the formal layout: source and isolated
Python come from `/opt/msys/current`, while the launcher and installation state
remain stable under `/opt/msys/service`. See
[system-release.md](system-release.md) for staging, activation, health rollback,
and retention.

They can be overridden with `--remote`, `--remote-python`, `--runtime-dir`,
`--log-file`, `--state-dir`, and `--profile`.

The launcher exports the source roots for core, SDK, shell, X11, HAL, and the
installer, then invokes only the isolated Python above. When present it adds
the repository-owned shell, HAL, canonical X11 session, and legacy OpenStick
CH347 declarations through repeatable `msysd --manifest` arguments. The
canonical X11 declaration is added before the board-specific CH347 provider.
This is the same overlay used by `msys-dev run`; installing the host startup
hook does not copy manifests into core or install a distribution Python
package.

Although developer synchronization includes the split application repositories,
the launcher intentionally never passes their manifests to `msysd`.
Ordinary applications must be installed with `package deliver msys-calculator`
(or the matching repository) so
the package registry, integrity verification, update, and rollback rules apply.

Every launcher also exports `MSYS_PLATFORM_PYTHONPATH` as exactly the matching
`msys-sdk` source root and sets `PYTHONDONTWRITEBYTECODE=1`. Core may pass the
narrow SDK path only to components declared as system packages; it continues
to clear host Python paths for ordinary applications. In formal mode the path
is computed after resolving `current`, so it cannot drift to another release.

Installation intentionally does not start immediately: a `msysd` launched by
`msys-dev run` does not have the managed PID file, so starting another instance
would conflict over X11 and runtime sockets. Stop the development session and
request immediate startup explicitly:

```powershell
python -m msys_tools.dev stop
python -m msys_tools.dev host-service install --backend sysv --start-now
```

The launcher also scans `/proc/*/cmdline` for an MSYS daemon using the same
runtime directory. If a development instance is still present, `start` refuses
to create a duplicate and `status` reports it as external. It never adopts or
stops a process that was not started through the managed launcher.

## Backend behavior

### SysV

The installer writes `/etc/init.d/msys`. It uses an existing `update-rc.d` or
`chkconfig` when available; otherwise it creates only the conventional MSYS
runlevel symlinks after checking that none belong to another service. These are
init registration utilities, not package installers.

### OpenRC

The installer writes `/etc/init.d/msys` using `/sbin/openrc-run` and registers
it with the existing `rc-update` command.

### rc.local and custom hooks

The installer inserts exactly one bounded block immediately after a shebang:

```sh
# >>> MSYS HOST SERVICE >>>
/opt/msys-dev/.service/msys-service start >/dev/null 2>&1 || :
# <<< MSYS HOST SERVICE <<<
```

Reinstallation replaces the block idempotently. Uninstallation removes only
that block. Other lines, including an existing `exit 0`, remain in their
original order. Malformed or nested MSYS markers cause the operation to stop
instead of guessing how to rewrite the file.

## Status and uninstall

```powershell
python -m msys_tools.dev host-service status
python -m msys_tools.dev host-service uninstall --dry-run
python -m msys_tools.dev host-service uninstall
```

Status distinguishes persisted installation metadata, startup integration,
and the managed launcher process. It returns a nonzero status when installation
is incomplete or `msysd` is stopped.

The startup integration is bound to an exact launcher path, not merely to the
generic MSYS managed marker. This matters because development and formal
layouts share `/etc/init.d/msys`. For example, querying a formal layout while
the development service is still installed reports:

```json
{
  "installation_state": "absent",
  "launcher": "absent",
  "startup_integration": "foreign-managed",
  "issues": [
    "installation-state-absent",
    "launcher-absent",
    "startup-integration-bound-to-another-launcher"
  ]
}
```

This is a cross-layout conflict, not a partially installed formal service.
Run `host-service status` without `--release-root` to inspect the development
owner. A formal-layout uninstall refuses to disable or remove that integration,
including in `--dry-run` preflight.

Uninstall first asks the managed launcher to stop, disables its native init
registration, and removes only managed files or the managed hook block. It does
not delete the MSYS workspace, Python runtime, packages, update history, logs,
or `/opt/msys-state`.

## Safety and recovery

Generated standalone files contain this marker:

```text
# Managed by msys-dev host-service v1
```

Install and uninstall refuse to replace a symlink, non-regular file, or
unmarked file at a managed destination. Backend and integration path are saved
in `/opt/msys-dev/.service/installation.state` (or
`/opt/msys/service/installation.state` in formal mode), allowing `status` and
`uninstall --backend auto` to use the actual installed backend rather than
guessing again.

If installation was interrupted before the state file was committed, rerun the
same explicit install command; managed writes are idempotent only when the
existing integration is bound to the expected launcher. This is the supported
reconcile operation. An exact-bound partial installation can also be inspected
and removed with the same explicit backend and hook path. A managed integration
bound to a different launcher, malformed hook markers, symlinks, and unmarked
native integration files are all refused before any write, stop, disable, or
remove action. Never add the managed marker to an unrelated file merely to
force deletion.
