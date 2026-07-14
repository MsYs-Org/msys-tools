# msys-tools

The default workspace sync also carries `msys-openstick-ch347`, the
self-contained AArch64 CH347 display-output package source. Installing that
package replaces the development `/root/x11display` fallback under the same
component identity; no profile edit is required.

Development tools for building MSYS from Windows and deploying to Linux devices.

This repository is for the developer workstation. It is not required by the
runtime image and does not add systemd or D-Bus dependencies.

## Goals

- Upload the workspace to a Linux target over SSH.
- Start/stop/debug `msysd` remotely.
- Stream logs back to the Windows terminal.
- Generate simple device bootstrap scripts.
- Keep target devices free of heavyweight development services.

## Quick start

### Fast path from Windows PowerShell

From the workspace root, use the included shortcut instead of spelling out a
new `wsl env PYTHONPATH=... python3 -m ...` command every time. For the fastest
edit/debug loop, enter the persistent WSL shell once; every `msys`/`m` command
then avoids another PowerShell-to-WSL startup and still reuses the SSH control
connection.

If PowerShell reports that script execution is disabled, use `.\msys.cmd` for
every command below. It applies a process-only `-ExecutionPolicy Bypass`,
forwards the original arguments, and returns the same exit code; it does not
change the workstation policy.

```powershell
# First time only: write the WSL-side development configuration.
.\msys.cmd setup --target root@192.168.1.215 --remote /opt/msys-dev `
  --runtime-dir /tmp/msys-main --state-dir /opt/msys-state `
  --profile desktop-spi --ssh-key /home/luorix/.ssh/msys-dev-ed25519 `
  --ssh-control-persist 2h

.\msys.cmd key          # password once, installs the dedicated SSH key
.\msys.cmd connect      # optional: pre-warm the reusable SSH master
.\msys.cmd fast --repo msys-settings
.\msys.cmd shell        # fastest loop: enter once
# Inside the resulting [msys-dev] prompt:
cd msys-shell-native
mq                                  # infers this repo; sync/build/run/ready
mqs                                 # sync + current status, no restart
mqshot ../artifacts/shell.png       # sync + status + screenshot
msys debug                          # or: m debug
msys debug --follow
```

`fast` (short alias `q`) is the preferred formal-runtime-safe edit loop. The
first call starts one loopback-only WSL broker; later calls reuse it, so each
edit does not boot another WSL process. It atomically syncs only the selected
repository, leaves the immutable live release and its existing `msysd` alone,
then obtains health, current release, critical component summary, disk/memory,
and recent warning/error lines in one SSH execution:

```powershell
.\msys.cmd fast --repo msys-settings
# From G:\Code\MsYs\msys-settings, q also infers --repo msys-settings:
.\msys.cmd q
.\msys.cmd fast --repo msys-settings --screenshot .\artifacts\settings.png --force
```

Source sync by itself is not live deployment for the formal SysV release; the
command says so explicitly. Opt in to the existing MAF build/install
transaction with `--deliver`. Repeated `--repo` values are fingerprinted and
built and transactionally installed in the given order. Pure MAF packages are
built directly from the Windows/WSL workspace, so their source is not uploaded
once before the archive is uploaded again. Repositories with target-native ELF
artifacts (HAL, Native Shell, and X11 policy) still sync/build first. Use
`--full-sync` when remote development source must also be refreshed. The final
health/log/screenshot report runs only once after every package succeeds.
Delivery is never implicit:

At the workspace root, a bare `\.\msys.cmd q` selects no repository and is a
diagnostic-only call. This prevents an accidental full-workspace sync or a
native Core rebuild; enter a repository or pass `--repo` to synchronize code.

```powershell
.\msys.cmd fast --repo msys-settings --deliver --screenshot .\artifacts\settings.png --force
.\msys.cmd fast --repo msys-settings --repo msys-apps `
  --repo msys-input-touch --deliver
```

`fast --deliver` also accepts repeatable
`--overlay SOURCE=RELATIVE_DEST`. Canonical `msys-settings`, `msys-apps`, and
`msys-input-touch` deliveries each require the sibling SDK at runtime, so when
no explicit overlay is given, `fast` prints a notice and automatically applies
this overlay to each applicable package independently:

```text
msys-sdk/msys_sdk=files/app/msys_sdk
```

Any explicit `--overlay` list is authoritative and disables that default. To
keep ownership unambiguous, explicit overlays are accepted only when exactly
one repository is selected; batch delivery with an explicit overlay fails
before synchronization. This prevents canonical apps from passing archive
validation and then failing with `ModuleNotFoundError` on the target.

For a release/runtime acceptance pass without syncing or changing target
state, use `accept`. The Windows shortcut starts/reuses the same loopback WSL
broker as `fast`, while the target work is one SSH execution. It reports the
installed Settings, Apps, Input, Shell, CH347/X11 component versions and
states, the active display session, declared windows, bounded warning/error
lines, disk/memory/swap, and optional screenshot:

```powershell
.\msys.cmd accept
.\msys.cmd accept --expect-window role=desktop `
  --screenshot .\artifacts\accept.png --force
.\msys.cmd accept --strict-logs --json
```

The default is read-only: it never syncs, starts/stops a component, installs a
package, or injects input. `--expect-window` is repeatable and accepts exact
`component=`, `identity=`, `role=`, or `title=` checks. Recent matched log lines
come only from the latest daemon session when its control-socket marker is
available, and are evidence only unless `--strict-logs` is selected. Screenshot
bytes travel inside the same bounded SSH archive, without SCP or a second
cleanup call.

Core and tools remain release inputs and are rejected before synchronization,
even when included in a delivery batch; they must use compose/stage/activate.
Batch delivery stops immediately on the first build or install failure and
does not print a final successful report. Install-agent self-update preserves
the agent's failure and points to the external/offline
`msys_install.cli install-archive` path. `--run` is also explicit and is only
for starting a stopped development runtime; `fast` never starts a second
daemon by default. Use `--logs N`, `--no-logs`, or `--json` to adjust the
bounded report. Inside `msys.cmd shell`, `mf` provides the same flow and infers
the current repository.

`quick` (alias: `deploy`) is the thin everyday workflow. It reuses the normal
atomic `sync`, including the target-native builds already required by selected
Core/Shell/HAL/X11 repositories, then calls the normal `run` readiness path.
It does not run `doctor` or another full validation pass by default:

```powershell
# Start a stopped development runtime after syncing one repository.
.\msys.cmd quick --repo msys-settings

# Keep an already-running runtime untouched; report its health and capture it.
.\msys.cmd quick --repo msys-settings --status `
  --screenshot .\dist\settings.png --force

# Opt into the complete doctor gate before any synchronization.
.\msys.cmd quick --repo msys-settings --safe
```

Repository sync now records one deterministic source fingerprint only after a
successful atomic swap. The next `sync`/`quick` prepares its staging directory,
probes all selected markers, and detects target `rsync` in one SSH call, then skips upload and native build
for exact matches. Cache/VCS files excluded by sync are excluded from the hash;
source content, paths, symlinks, empty directories, file sizes, and executable
bits are included. Use `--full-sync` only to repair a manually altered remote
development tree:

```powershell
.\msys.cmd quick --repo msys-shell-native --full-sync
```

The default run path already waits for Core readiness, so `quick` does not add
a duplicate status call. `--status` selects status-only behavior instead of
starting another runtime. When combined with `--screenshot`, health and PNG are
returned by one bounded SSH report instead of a status call plus capture/SCP/
cleanup calls. `quick` never stops or restarts an existing runtime.

For repeated commands, `.\msys.cmd debug` starts or reuses the loopback-only
local broker by default, so each snapshot does not start WSL again. The broker
can still be managed explicitly:

```powershell
.\msys.cmd broker start # Auto now reuses this healthy broker
.\msys.cmd debug
.\msys.cmd broker status
.\msys.cmd broker stop
```

Auto mode never starts a broker; it only reuses one created explicitly by
`broker start`. The `fast`/`q`, `quick`/`deploy`, `accept`, `ui-accept`, and
`debug` shortcuts select On automatically unless an explicit broker mode is
supplied. `-Broker On` starts/requires it, and `-Broker Off` always uses
one-shot WSL. The broker listener is strictly `127.0.0.1`, its per-session
token stays in the current user's
`%LOCALAPPDATA%\MSYS\dev-brokers` state file, and it receives an argument array
rather than a shell command; the token is not placed in the WSL process
command line. It exposes neither the board nor any port on the LAN. An idle
broker exits after four hours; restart it explicitly when desired.

For an entirely interactive Linux terminal, the existing persistent shell is
the recommended repeated edit/debug path. It starts WSL once, preserves the
PowerShell subdirectory, and keeps using the configured SSH ControlMaster:

```powershell
.\msys.cmd shell
# Inside the resulting prompt:
m debug
cd msys-shell-native
mq
mqs
mqshot ../artifacts/shell.png
m tail
```

The wrapper maps normal Windows paths such as `.\artifacts\home.png` to WSL
paths and passes every ordinary `msys-dev` command through unchanged. `key`
and `connect` deliberately use an interactive one-shot WSL command because an
SSH password prompt needs a real terminal. If a workstation cannot use
localhost forwarding, keep the default Auto mode without starting a broker,
or choose `-Broker Off`/`MSYS_DEV_BROKER=Off` explicitly. The frequent
`fast`, `quick`, `accept`, `ui-accept`, and `debug` paths start or reuse the local broker
automatically, so repeated PowerShell commands do not repeatedly start WSL.
`-Distro NAME` selects a non-default WSL distribution. For a nonstandard mount,
set `MSYS_WSL_WORKSPACE` to the absolute Linux workspace path.

The longer `wsl env ... python3 -m msys_tools.dev ...` examples below remain
the direct Linux form for automation. In normal Windows PowerShell, replace the
entire prefix with `.\msys.cmd`; for example, use `.\msys.cmd screenshot
.\artifacts\home.png`, not another hand-written `wsl` command.

```powershell
python -m msys_tools.dev config set --target root@192.168.1.50 --remote /opt/msys-dev
python -m msys_tools.dev setup-key
python -m msys_tools.dev runtime bootstrap
python -m msys_tools.dev sync
python -m msys_tools.dev doctor
python -m msys_tools.dev run
python -m msys_tools.dev status
python -m msys_tools.dev tail
```

`sync` prefers `rsync` when available and falls back to `tar` plus `scp`.
Both paths upload into a per-repository staging directory and only then swap
the remote tree, retaining one `.previous` tree. A partial transfer therefore
does not first erase the usable source tree.

Native runtime repositories have an additional atomic pre-activation step.
`sync` builds them single-threaded inside their remote staging trees and
verifies each target executable before swapping the repository. This covers
the native Core migration artifact, the single-process Xlib Shell, the
single-process HAL, and the X11 policy. A failed build leaves the previous
repository active, so a Windows/WSL binary cannot replace an AArch64 runtime
binary.

SSH uses `ControlMaster` and `ControlPersist=10m`, so one password entry can be
reused by following commands. `setup-key` installs a dedicated development SSH
key and removes the password prompt for normal work.

Configuration is persistent. The following is enough once per WSL user; later
commands do not need repeated `env`, `--target`, `--remote`, or key arguments:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev config set `
  --root /mnt/g/Code/MsYs `
  --target root@192.168.1.215 `
  --remote /opt/msys-dev `
  --runtime-dir /tmp/msys-main `
  --state-dir /opt/msys-state `
  --profile desktop-spi `
  --ssh-key /home/luorix/.ssh/msys-dev-ed25519 `
  --ssh-control-persist 10m
```

The JSON file is atomically replaced with owner-only permissions under
`~/.config/msys-dev/config.json` in WSL. `MSYS_DEV_TARGET`, `MSYS_DEV_REMOTE`,
`MSYS_DEV_ROOT`, `MSYS_DEV_REMOTE_PYTHON`, `MSYS_DEV_SSH_KEY`,
`MSYS_DEV_SSH_CONTROL`, and `MSYS_DEV_SSH_CONTROL_PERSIST` remain supported as
temporary overrides. Use `config show` or `config unset KEY...` to inspect or
remove persisted values.

The OpenStick acceptance setup currently uses `desktop-spi`. This is a
persisted choice, not a hard-coded shell dependency: `mobile-spi`,
`mobile-spi-pill`, and the HDMI profiles remain selectable with `config set
--profile ...` or a one-shot `run --profile ...`.

## Device assumptions

Every target needs an SSH server, `sh`, `tar`, ordinary `cp`/`mv`/`uname`, and
the isolated MSYS Python. Source synchronization is intentionally stricter:
`make`, `cc`, and `c++` are build-required because Core, Shell, HAL, the native
X11 policy, and board capture binaries are compiled in remote staging. Missing build tools make
`doctor` fail instead of allowing a later `sync` to fail halfway through. MSYS
only reports the missing capability; it never invokes a target package
manager. `rsync` and a distribution `python`/`python3` remain optional.

Graphical profiles add runtime requirements. For the current `desktop-spi`
profile these are Bash, `xdpyinfo`, either an executable Xorg or Xvfb server,
and a successful `import tkinter` in the isolated Python. `doctor` reports
Xorg and Xvfb separately and names the selected available server. HDMI and
headless/custom profiles are classified independently from the persisted or
explicit `--profile` value.

Deployment artifacts are reported by stage. The native policy and CH347
provider wrapper come from `sync --repo msys-x11-session`; the CH347 start/stop
scripts, shared library, and five native capture binaries come from
`sync-x11display`. Missing artifacts are shown as `deploy-required` with the
specific `workspace-sync` or `x11display-sync` recovery step.

`doctor` performs all target checks in one read-only SSH invocation, validates
local manifests, and reports how to bootstrap the isolated runtime. On a new
device, bootstrap the runtime, provision build/runtime capabilities in the
device image, run both synchronization stages, and then expect `doctor` to
pass:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev doctor --profile desktop-spi
```

The current Python reference runtime uses an isolated Python under:

```text
/opt/msys-dev/.runtime/python/bin/python3
```

Install it with:

```powershell
python -m msys_tools.dev runtime bootstrap
```

`runtime bootstrap` downloads a trusted aarch64 Python standalone archive from
`astral-sh/python-build-standalone`, caches it locally, uploads it, and extracts
it under `.runtime/python`. Do not use the target OS package manager for MSYS
development runtime unless you explicitly want to.

## Complete source synchronization

The default synchronization set includes contracts, core, SDK, the native and
compatibility shells, X11, HAL, Settings, the ordinary application collection (`msys-apps`), the
replaceable touch-input provider, installer, and tools. Missing requested
repositories are an error instead of being
silently skipped. Limit a debugging upload explicitly when needed:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev sync --repo msys-core --repo msys-hal
```

If an older configuration persisted a custom repository subset, add
`msys-x11-session` to that subset or run `config unset repos` to return to the
complete default. `MSYS_DEV_REPOS=msys-core,msys-x11-session` remains a
temporary comma-separated override; surrounding whitespace and duplicates are
normalized.

Except for the atomic native X11 policy build described above, synchronization
copies source trees. Ordinary application manifests such as
`msys-apps/manifest.json` are deliberately not appended to the canonical
`msysd --manifest` startup list: doing so would bypass the installer registry
and make manual applications look like system services. Install or update them
with the package delivery flow below.

The board-owned `x11display` tree has a separate target-native delivery path:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev sync-x11display --local x11display --destination /root/x11display
```

This command uploads into `/root/x11display.new`, runs separate `make clean`
and `make all` steps there, and verifies all five runtime binaries as regular,
executable, non-symlink files: `ch347_dirty_usb_sink`, `ch347_st7796_test`,
`ch347_irq_test`, `ch347_app_gate`, and `xdamage_shm_capture`. Only that verified
tree can replace `/root/x11display`; the previous tree is retained as
`/root/x11display.previous`. Upload, extraction, build, or validation failure
leaves the active tree untouched and removes both the incoming archive and
`.new` tree. The target therefore needs its already-provisioned compiler and
X11/Xext/Xdamage development files, but the command never invokes a package
manager.

`run` keeps the core profile/config directory but passes each repository-owned
canonical manifest through repeatable `msysd --manifest` arguments when the
file exists:

- `msys-shell-native/manifest.json`
- `msys-shell-pyside/manifest.json`
- `msys-hal/manifest.json`
- `msys-x11-session/manifest.json` (window policy and display-session providers)
- `msys-openstick-ch347/manifest.json` (self-contained OpenStick CH347
  provider). The nested X11-session CH347 document is a package-build template
  and is never passed directly to Core because its package root would be wrong.

The same wiring is emitted by `host-service`. Its `PYTHONPATH` contains HAL,
and all supervisors use `/opt/msys-dev/.runtime/python/bin/python3`; they do not
fall back to the distribution Python or install target packages.

`run` first verifies the isolated Python and native policy, then refuses any
process or socket state already associated with the selected runtime. After
launch it waits for `control.sock`, successful Core RPC, active critical roles,
and every eager background/session component to report `ready`. Timeout or
startup failure returns nonzero and prints a bounded log tail.

`status` emits `msys.runtime-status.v1` JSON and returns nonzero for a stopped,
unreachable, or partially ready session. `stop` matches only msysd processes
whose exact `--runtime-dir` equals the configured runtime, waits for exit, and
then removes that runtime's dead Unix socket. It never performs a global
`pgrep`/`pkill`; a regular file, symlink, or still-listening foreign socket is
left untouched and reported as an error.

## Common remote debug commands

From `G:\Code\MsYs` on Windows/PowerShell, use WSL so the Linux SSH control
socket and Python tooling are consistent:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev components
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev tail
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev shield show
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev shield hide
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev notify "Hello" --timeout-ms 2500
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev screenshot /mnt/g/Code/MsYs/artifacts/openstick.png
```

`shield` controls visibility through the replaceable `screen-shield` role; it
does not hard-code the reference Shell component. `shield show` resolves the
role's active/preferred provider, asks Core to make that exact provider ready,
then calls typed `role:screen-shield.show`. `shield hide` never stops a process:
it calls typed `role:screen-shield.hide` only when the role has a running
provider. If none is running, it succeeds as an explicit
`already_hidden=true`, `reason=provider-not-running` no-op. Every Core and role
reply, including the final `msys.screen-shield.status.v1` visibility, is
validated; an RPC error or mismatched terminal state makes the command return
nonzero. Use `--timeout SECONDS` to bound the complete operation.

Notification timeouts are explicitly bounded to 500-6000 ms, so a malformed
debug request cannot leave a toast permanently covering the display.

The default `mobile-spi` profile starts a phone-like shell:

- top `system-chrome` bar: `320x42+0+0`
- center app area: `320x396+0+42`
- bottom `navigation-bar`: `320x42+0+438`

`navigation-bar` is a replaceable role. The reference implementation supports
both three-button navigation and a gesture pill:

```powershell
# Three-button navigation, current default
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev run --profile mobile-spi

# Gesture pill navigation
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev run --profile mobile-spi-pill
```

Providers can also be switched transactionally while the session is running.
The selection is persisted under `/opt/msys-state`:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev roles
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev role select navigation-bar org.msys.shell.pyside:navigation-pill
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev role reset navigation-bar
```

For `display-output`, select/reset returns only after the Core migration reaches
`succeeded` or `rolled-back`. A rollback or timeout is a nonzero command result;
the final structured migration record includes the error and rollback health.

Applications and non-exclusive services use the same protocol without becoming
system roles. Discovery lists callable interfaces and passive capabilities;
`call` wakes an on-demand provider and works with any implementation language:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev discover
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev discover --kind interface --name org.msys.demo.echo.v1
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev call interface:org.msys.demo.echo.v1 ping --idempotent
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev call component:org.msys.sdk.native:echo ping --idempotent
```

From PowerShell, use repeatable `--field KEY=VALUE` instead of embedding a JSON
object in `--payload`. It survives the `.cmd`/PowerShell/WSL boundary without
special escaping. Valid JSON literals retain their type; other values are sent
as strings:

```powershell
.\msys.cmd call role:hal get --field id=network:wlan0
.\msys.cmd call role:hal set_state --field id=bluetooth:hci0 `
  --field changes.powered=true --field changes.priority=10
```

The second example sends `{"id":"bluetooth:hci0","changes":{"powered":true,
"priority":10}}`: `id` is a string, `powered` a Boolean, and `priority` a
number. Dotted paths support at most four safe identifier segments. Duplicate
leaves, parent/child scalar-object conflicts, and malformed `KEY=VALUE` fields
fail locally before SSH. The existing `--payload '{"key":"value"}'` option
remains available for shells where JSON quoting is already controlled; it
cannot be combined with `--field`.

The Apps key opens the replaceable `task-switcher` role. Back dismisses that
panel first; a second Back closes the foreground MSYS component through its
normal lifecycle rather than reporting it as a crash.

The actual X11 pointer path can be exercised remotely without installing
`xdotool` or any target package. With no selector, `tap` and `swipe` resolve the
currently visible `_MSYS_WINDOW_ROLE=navigation-bar` window from the native
window list. This follows whichever Native or PySide provider owns the
replaceable role instead of assuming one shell identity. An explicit
`--identity` still wins. Coordinates are relative to the selected window; for
the 320-pixel three-button bar these trigger Back, Home, and Apps respectively:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev tap 50 20
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev tap 160 20
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev tap 270 20
```

`tap` resolves `DISPLAY` from the active
`<runtime-dir>/display-session.json`; it no longer assumes the OpenStick
`:24` display. The gesture pill can be exercised through the same native XTest
helper with an upward swipe (coordinates are relative to the navigation
window):

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 160 20 160 2 --duration-ms 220
```

Those coordinates fit the compact 24-pixel desktop pill. A 42-pixel mobile
navigation area may instead use `160 34 160 5`. Coordinates are relative to
the selected navigation window, not the physical screen.

For a right-edge navigation bar after a landscape rotation, swipe inward with
for example `swipe 48 210 20 210`. `--runtime-dir` follows the persisted
development configuration. `--display :24` remains an explicit recovery
override for diagnosing a missing session document; normal debugging should
use the live session. Coordinates are bounded to 0-32767 and duration to
40-5000 ms before SSH is opened. Neither command invokes `xdotool`.

`swipe` exposes all three explicit selectors implemented by the native policy
helper. If no selector is supplied it uses the active `navigation-bar` role.
Role discovery accepts only a visible role window, so it will not click through
to the launcher or an application. Older policy helpers without
`--list-windows` use a three-entry Native/PySide identity fallback. Use one of:

```powershell
# Stable WM identity
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --identity org.example.app

# Title-only fallback for an identity-less legacy window
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --title "Legacy App"

# Exact identity plus title match
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --window org.example.app "Example App"
```

`--identity ID --title TITLE` remains an accepted spelling of the exact-window
selector. Selector text, coordinates, duration, and recovery `DISPLAY` are
validated locally before the multiplexed SSH connection is used.

Stable X11 window handles can be inspected and controlled without hand-writing
JSON in PowerShell. `wm list` returns `msys.window.v1` records; copy an exact
`msys.x11-window.v1:...` id into one of the typed actions:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm list
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm focus --window-id msys.x11-window.v1:PID:GEN:TOKEN
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm move-resize --window-id msys.x11-window.v1:PID:GEN:TOKEN --x 0 --y 42 --width 320 --height 396
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm minimize --window-id msys.x11-window.v1:PID:GEN:TOKEN
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm close --window-id msys.x11-window.v1:PID:GEN:TOKEN
```

`move`, `resize`, and `move-resize` accept only their required geometry fields,
and all ranges are checked locally before SSH. The generation-bearing handle
is still revalidated by X11 policy before every focus/minimize/move/resize or
close operation, so an old XID cannot control a newly created window. Existing
`wm home`, `wm back`, `wm recents`, `wm list_windows`, and `wm close_active`
spellings remain available. `wm recents` uses the navigation-action route and
opens the visible task switcher; `wm list`/`wm list_windows` remain read-only
window snapshot queries.

For visual debugging without synthetic input, `screenshot` captures one PNG
from the active `display-session.json` (the OpenStick session is normally
`:24`) and downloads it through the same key and multiplexed SSH connection.
The target helper prefers an existing `scrot`; when that is absent or fails,
it can use an existing `ffmpeg -f x11grab` binary. It never invokes apt, pip,
or another package manager. Force one provisioned backend with `--backend
scrot` or `--backend ffmpeg`, and use `--display :24` only when recovering from
a missing active-session document:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev screenshot /mnt/g/Code/MsYs/artifacts/home.png
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev screenshot /mnt/g/Code/MsYs/artifacts/home.png --display :24 --backend ffmpeg --force
```

The remote filename is a random, strictly validated root-owned `/tmp` path.
The downloaded bytes must match the reported size and PNG signature before a
new local output is committed. The CLI attempts and verifies remote cleanup on
capture, transfer, validation, and local-output failures; it will not replace
an existing workstation image unless `--force` is explicit.

`font-doctor` checks the rendering backend rather than trusting the installed
font list. By default it probes `/opt/msys/current/.runtime/python/bin/python3`
when that executable exists, then falls back to the configured development
runtime. An explicit `--python` always wins. It starts the selected isolated
Python with bytecode writes disabled,
opens real Tk Label/Button/Entry/Text/Treeview controls on the active display,
and reports the actual selected family, per-CJK-glyph advances, mapped
Xft/Fontconfig/FreeType libraries, and probe PSS. Use the candidate runtime path
before a system-release switch:

```powershell
.\msys.cmd font-doctor --python /opt/msys/releases/CANDIDATE/.runtime/python/bin/python3
# In the persistent `msys.cmd shell`: msys font-doctor --python /opt/msys/releases/CANDIDATE/.runtime/python/bin/python3
```

Exit status `0` is a working CJK outline backend, `3` is a completed but
unhealthy probe such as `Noto Sans CJK SC -> fixed 10`, and `2` means the probe
could not run. A package merely appearing in `font.families()` is not accepted.

`visual-smoke` checks the semantic navigation route without hard-coded touch
coordinates. It calls only typed Core and `role:window-manager` methods for
Home, application start, Back, and Recents. To make restoration unambiguous it
fails before mutation unless the chosen manual app is stopped and the device
is already at a clean Home session with no managed or external user windows.
After either success or a mid-test failure it stops only the app that it
started, when necessary, and raises Home again:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev visual-smoke org.msys.apps:calculator
```

The structured `msys.visual-smoke.v1` result includes bounded typed step
outcomes and the cleanup record. A nonzero status means either the route or
its restoration failed; inspect that JSON before continuing interactive tests.

For the complete P0 phone UI route, `ui-accept` (alias `p0-ui`) runs one remote
helper through one SSH connection. It records the initially running manual
application set, then exercises Notes, Calculator, and Device Info in order:

```powershell
.\msys.cmd ui-accept
.\msys.cmd ui-accept --timeout 20 `
  --display-log /tmp/ch347_dirty_usb_x11/live.log
```

The `msys.p0-ui-acceptance.v1` JSON verifies the effective workarea, exact
component/window identities, real bounded P6 window thumbnails, three-card
Recents data and visible overlay, card activation and close, Back dismissal,
Back application exit, and a bounded notification toast. After all three test
apps are ready, the same helper also reports `/proc/*/smaps_rollup` RSS/PSS for
Core, Native Shell, Native HAL, Notes, Calculator, and Device Info. Missing
processes, permissions, or kernel files are explicit `unavailable` evidence;
they do not start another SSH connection. The route uses typed Core,
window-manager, task-switcher, and broadcast calls rather than synthetic touch
coordinates. In `finally`, it hides the test Recents overlay, stops only test
applications that were not originally running, restarts any original manual
application that the route closed, and restores the original foreground order
or Home. A restoration mismatch makes the command fail.

When the display sink emits a line such as `dirty_stats frame=...`, the newest
record is included under `dirty_stats`. This is evidence-only: an older sink
without that record does not fail UI acceptance. The command does not switch
releases, install packages, change layout, inject input, or retain screenshots.

The native policy follows live X11 resolution and can switch mobile, kiosk, or
desktop placement without restarting applications:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev layout show
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev layout set --profile mobile --orientation landscape --insets auto
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev layout set --profile kiosk --insets 0,0,0,0
```

For a legacy child Toplevel that did not inherit its process WM_CLASS, include
a title fallback, for example `tap 190 345 --identity
org.msys.shell.intent-chooser --title "MSYS Intent Chooser"`.

These are development-only synthetic X11 pointer gestures. They reach the same
Tk press/motion/release bindings as the CH347 touchscreen and are useful for
separating UI event bugs from touch calibration/driver bugs.

OpenStick CH347 display sessions are managed as an MSYS component. Apps that
want to draw directly to the current SPI/X11 display should use:

```sh
DISPLAY=:24 /opt/your-app
```

The display provider itself wraps:

```sh
CH347_TOUCH=1 DEBUG=1 FPS=60 XCAP_IDLE_FPS=1 WM=none \
  /root/x11display/scripts/start_ch347_dirty_usb_x11.sh
```

## Install as a normal host service

MSYS does not need to be PID 1. `host-service` installs a small launcher which
starts `msysd` as a normal background process after the host has booted. It
does not call systemd, D-Bus, `apt`, `pip`, or any target package manager. The
launcher uses the isolated Python already installed under `.runtime/python`.
Development launchers also export `MSYS_PLATFORM_PYTHONPATH` containing only
the matching `msys-sdk` root; Core may grant that narrow platform ABI to
declared system packages while ordinary applications remain fully cleared.

First inspect the available host startup mechanisms and the exact dry-run:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service detect
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend auto --dry-run
```

Then explicitly select the desired integration, or keep `auto`:

```powershell
# Debian/BusyBox-style SysV init
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend sysv

# OpenRC
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend openrc

# Existing or newly created /etc/rc.local
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend rc-local

# Board-specific POSIX shell startup hook
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend hook --hook /etc/board-startup.sh
```

Installation enables the next-boot integration but does not start a second
`msysd` over a development instance. Stop the development instance first and
add `--start-now` when immediate startup is wanted. Inspect or remove it with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service status
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service uninstall --dry-run
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service uninstall
```

The installer records its selected backend under
`/opt/msys-dev/.service/installation.state`. Reinstall and uninstall only
replace files carrying the MSYS managed marker; custom hooks receive a bounded
managed block, while all other user content remains in place. Uninstall leaves
the workspace, isolated runtime, installed applications, and `/opt/msys-state`
untouched. Native startup files must also reference the exact expected launcher;
this prevents a formal-layout command from adopting or deleting a development
service that shares `/etc/init.d/msys`. See
[docs/host-service.md](docs/host-service.md) for detection order, paths, and
recovery details.

For a formal deployment, first stage a complete target-built release, then
install the service against the stable release root rather than the development
workspace:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev release stage 2026.07.12-1 --activate
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev stop
# Run host-service uninstall here first only if a development-tree service is installed.
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev host-service install --backend sysv --release-root /opt/msys --start-now
```

The formal launcher lives at `/opt/msys/service/msys-service`, resolves and
pins `/opt/msys/current` at each start, and never runs out of the mutable
development tree. Subsequent health-gated switches use:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev release stage 2026.07.12-2 --activate --restart-service --health-timeout 120
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev release rollback --restart-service --health-timeout 120
```

Release health checks allow 90 seconds by default so cold display and HAL
providers can finish initialization. `--health-timeout SECONDS` accepts 10 to
180 seconds on `stage`, `activate`, and `rollback`; the same deadline is used
for the candidate and an automatic recovery start.

See [docs/system-release.md](docs/system-release.md) for the pointer journal,
retention, service migration, and recovery flow.

## Create and run an application

`app new` creates a small offline project without downloading a framework,
running a package manager, changing `DEFAULT_REPOS`, or touching the target.
The supported starters are `tk`, `python`, `c`, `cpp`, `qt`, and `electron`:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev app new /mnt/g/Code/MsYs/hello-msys --id org.example.hello --name "Hello MSYS" --template tk
```

Every starter has a strict `msys.manifest.v1`, a `files/` package boundary,
framework-specific source/build instructions, and a validated-style
`files/share/i18n/catalog.json`. The catalog uses
`msys.i18n.catalog.v1`, includes complete `en-US` and `zh` maps, and supplies
the `app.name`/`app.summary` keys named by the
manifest's optional `x-msys-i18n` presentation metadata. Python/Tk, Qt,
Electron, and the C/C++ catalog generator all use
`MSYS_LOCALE > LC_ALL > LC_MESSAGES > LANG`, normalize POSIX locale spellings,
and merge partial parent overlays in the same order. Region maps such as
`zh-CN` are intentionally omitted until an application has a real regional
difference; developers add only the differing keys. Tk/Python, Qt, and Electron
starter pages also wrap long text and scroll vertically on small screens.

Python and Tk starters can run directly on the isolated development Python
runtime and inherit neither host site-packages nor `PYTHONPATH`. Their README
explains how to bundle a private interpreter for a portable release. C/C++
manifests deliberately remain package-invalid until `files/bin/app` has been
cross-compiled. Qt additionally documents `files/runtime/qt` libraries and xcb
plugins; Electron requires its executable distribution at
`files/runtime/electron/electron`. No template silently falls back to a target
package or downloads its missing runtime.

New Python/Tk starters also configure Tk's named fonts through an installed
CJK-capable family list shared with the bundled MSYS UIs. Developers can set
`MSYS_UI_FONT_FAMILY` without changing source. The selector is not a renderer:
anti-aliased Tk text requires the system release's Xft-enabled Tk runtime.
Raw Xlib C/C++ starters intentionally remain window/protocol examples; choose
the Qt starter or integrate Xft/Pango before shipping a polished multilingual
UI.

After preparing any required artifact, one command performs the normal strict
developer loop:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev app run /mnt/g/Code/MsYs/hello-msys
```

`app run` composes the existing directory validation, content-hashed package
build, verified `install-archive`, and exact `start-component` operations. It
does not duplicate install or mIPC protocols. `--no-start` stops after atomic
installation; `--component`, `--output`, `--format maf`, `--force`,
`--manifest`, and repeatable `--overlay SOURCE=DEST` cover multi-component and
bundled-runtime projects. If `--component` is omitted, the manifest must expose
exactly one launchable component unless `--no-start` is also selected.

## Self-contained package delivery

MSYS packages are directories or tar archives containing `manifest.json`.
They install into `/opt/msys-state` and do not use the target OS package
manager.

The workstation package commands reuse the validation and update code from the
sibling `msys-install` repository. Nothing is installed into the Windows or
target Linux package manager. From WSL, validate a source package, build a
content-hashed archive, and generate an update index with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package validate /mnt/g/Code/MsYs/my-package
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package build /mnt/g/Code/MsYs/my-package --output /mnt/g/Code/MsYs/dist
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package build /mnt/g/Code/MsYs/my-package --format maf --output /mnt/g/Code/MsYs/dist
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package index /mnt/g/Code/MsYs/dist --base-url https://updates.example/msys/
```

Build-time overlays can vendor a sibling runtime into an otherwise
self-contained package without modifying either source tree:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package build /mnt/g/Code/MsYs/my-package --format maf --overlay msys-sdk/msys_sdk=vendor/msys_sdk --output /mnt/g/Code/MsYs/dist
```

Each `--overlay SOURCE=RELATIVE_DEST` source is resolved from the configured
workspace (absolute sources are also accepted). Destinations must be safe
package-relative paths; root `manifest.json`, `hashes.json`, and
`signature.optional`, traversal, symlink parents, and any overwrite of staged
content are rejected. VCS/cache files are filtered from overlay directories,
then one final `hashes.json` pass covers both source and vendored bytes. Archive
timestamps and modes remain reproducible under `SOURCE_DATE_EPOCH`.

Discover every language-neutral declaration and strictly validate it without
contacting a target:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package discover /mnt/g/Code/MsYs
```

HAL and Settings use the same package contract as every other language or UI
framework. Build them independently (each archive is self-contained and needs
no target package manager):

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package build /mnt/g/Code/MsYs/msys-hal --output /mnt/g/Code/MsYs/dist
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package build /mnt/g/Code/MsYs/msys-settings --output /mnt/g/Code/MsYs/dist
```

Projects normally use a root `manifest.json`. A repository with exactly one
canonical `manifests/*.json` is also staged correctly as a root package
manifest; use `--manifest RELATIVE_PATH` when a repository intentionally has
multiple choices.

For the normal developer loop, build, verify, upload, and ask the running
install agent to atomically commit a package in one command:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package deliver /mnt/g/Code/MsYs/msys-settings --output /mnt/g/Code/MsYs/dist --force
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package deliver /mnt/g/Code/MsYs/msys-hal --output /mnt/g/Code/MsYs/dist --force
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package deliver /mnt/g/Code/MsYs/msys-apps --output /mnt/g/Code/MsYs/dist --force
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package deliver /mnt/g/Code/MsYs/msys-settings --format maf --output /mnt/g/Code/MsYs/dist --force
```

`package deliver` is shorthand for the existing build plus
`install-archive` flow. `install-archive` itself now requires complete
`hashes.json` coverage and validates the archive locally before opening any
upload or install request. The verified file is staged under
`$MSYS_STATE_DIR/updates/staged-rpc`, and Tools sends its archive SHA-256,
package id, and version to `role:install-agent.install_archive`. A zero exit
status therefore means the typed terminal reply had
`schema=msys.install-agent-result.v1` and `ok=true`; merely publishing an event
is not treated as success.

HAL, Native Shell, and X11 policy delivery adds a target-native gate. Run
`sync --repo REPOSITORY --full-sync` first. The atomic target build executes
the ELF self-check, version probe, or loader probe and writes a marker binding
the package id, manifest version, relative path, and ELF SHA-256. Delivery
refuses a missing, stale, or mismatched marker, downloads that exact target ELF
into a temporary package copy, leaves the workstation source untouched, and
checks the ELF hash again inside the finished archive before contacting the
install agent. A newer manifest therefore cannot silently ship an older cached
AArch64 binary.

`package validate` accepts a package directory, a standalone `manifest.json`,
or a tar/zip/MAF archive and identifies the container by content. Add
`--require-content-hashes` when validating an update artifact. `.maf` is the
MSYS filename alias for the same deterministic gzip-compressed tar bytes built
as `.tar.gz`; it does not relax archive SHA-256, complete `hashes.json`, or
safe-extraction checks. `package build` copies the source into a temporary staging directory,
omits common VCS/cache files, writes `hashes.json` there, and verifies the final
archive through the same safe extraction path used on the device. It therefore
does not add or rewrite `hashes.json` in the source tree. Existing versioned
artifacts are not overwritten unless `--force` is explicit.

A source repository may add a UTF-8 `.msys-packageignore` containing exact,
root-relative files or directories (one per line, optional trailing `/`). It is
intended for source-only trees such as `tests/` or separately installable
`examples/`; globbing, absolute paths, `..`, backslashes, control characters,
and excluding the root `manifest.json` are rejected. The ignore file itself is
not shipped. Ignoring a file referenced by `@package` still fails final package
validation.

The default remains `<package-id>-<version>.tar.gz` for compatibility. Explicit
`--format maf` writes `<package-id>-<version>.maf`; an explicit output filename
must agree with the selected format. The default index path is
`<repository>/index.json`. `SOURCE_DATE_EPOCH` (or
`--source-date-epoch`) controls reproducible archive timestamps.

Upload a manually built archive to the running install agent with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev install-archive /mnt/g/Code/MsYs/dist/org.example.app-1.0.0.tar.gz
wsl ssh -i /home/luorix/.ssh/msys-dev-ed25519 root@192.168.1.215 "cat /opt/msys-state/registry/installed.json"
```

The old local-on-device directory event has no reliable request/reply contract.
It is available only for explicit compatibility work and warns that completion
cannot be confirmed:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev install-dir /opt/some-msys-package --legacy-events
```

For a repository index generated by `msys-install make-index`, check or apply a
verified remote update with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev update-trust generate --private ~/.config/msys-dev/update-publisher.private.json --public /mnt/g/Code/MsYs/dist/update-publisher.public.json
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev update-trust sign-index /mnt/g/Code/MsYs/dist/index.json --private ~/.config/msys-dev/update-publisher.private.json --sequence 1 --expires 2027-01-01T00:00:00Z
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev update-trust install-public /mnt/g/Code/MsYs/dist/update-publisher.public.json
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev check-update https://updates.example/msys/index.json
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev apply-update https://updates.example/msys/index.json --package org.example.app
```

Publisher signing uses Ed25519 through the existing OpenSSL 1.1.1+ EVP API and
canonical JSON; it installs no apt/pip dependency and fails closed when the
backend is unavailable. The
private document is created mode `0600`, is read only by the local
`generate`/`sign-index` commands, and is never accepted by `install-public` or
included in an upload command. The target receives only the strict public-key
document and stores it under
`$MSYS_STATE_DIR/trust/update-publishers.json`. Key ids are the full SHA-256 of
the raw public key. Signed indexes carry an RFC3339 UTC `expires` value and a
publisher-managed positive `sequence`; the target persists the highest
accepted source sequence and rejects rollback or same-sequence content
replacement.

HTTP/HTTPS update sources now require a trusted, unexpired signature. Local
path and `file://` sources also require one by default. For an intentionally
unsigned local development repository only, use `check-update ...
--allow-unsigned` or `apply-update ... --allow-unsigned`. The flag cannot make
HTTP/HTTPS unsigned, and cannot bypass a malformed or invalid signature.

Remote apply requires both the archive SHA-256 from the index and complete
`hashes.json` coverage inside the package. The updater stages and atomically
commits it, then `msysd` reloads the committed registry without a reboot.
`check-update`, `apply-update`, registry, install, uninstall, and rollback
commands use typed role RPC by default. Partial apply results with `ok=false`,
RPC errors, and health-check rollback all return nonzero. `--legacy-events` is
the explicit fire-and-forget compatibility mode; it is never selected
automatically.

Remove a package from the active catalog without deleting its immutable
version tree with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package uninstall org.example.app
```

The command calls `role:install-agent.uninstall({package})` and waits for the
typed terminal result. Before pointers move, Core validates the complete
catalog with that package removed. On success `current.json` is absent and the
old current becomes `previous.json`; the version directory remains intact, so
`package rollback` can restore it. A Core reload/readiness failure restores the
old registry automatically. The running install agent refuses to uninstall
its own package.

Roll back the current package pointer to its previously committed version with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package rollback org.example.app
```

Rollback is delivered to the running `install-agent`; it uses the same
preflight, Core reload, critical health, and failure-recovery transaction as an
install. The command waits for that terminal reply and fails when no compatible
previous version can be activated. Progress and terminal events remain
available for observers, but are no longer used as command acknowledgement.
