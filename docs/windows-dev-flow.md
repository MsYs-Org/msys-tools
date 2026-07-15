# Windows Development Flow

The recommended development loop is:

1. Edit code on Windows in `G:\Code\MsYs`.
2. Run `msys-dev sync` to upload repositories to the Linux device.
3. Run `msys-dev run` to start a foreground-style `msysd` under `nohup`.
4. Run `msys-dev tail` to stream logs.
5. Run `msys-dev stop` before replacing low-level display or input providers.

This is a development workflow only. Production devices can launch the generated
shell script from BusyBox init, OpenRC, runit, cron `@reboot`, or a custom board
startup script. MSYS itself does not call those systems.

## Fast Windows entry point

At the workspace root, `msys.ps1` removes the repeated WSL/PYTHONPATH
boilerplate. The fastest repeated development loop is one persistent WSL shell,
which also reuses the existing SSH transport:

When local PowerShell policy blocks `.ps1` files, use `.\msys.cmd` with the
same arguments. It uses a process-only execution-policy bypass, preserves the
argument tail and exit code, and does not modify the user's policy.

```powershell
.\msys.cmd setup --target root@192.168.1.215 --remote /opt/msys-dev `
  --runtime-dir /tmp/msys-main --state-dir /opt/msys-state `
  --profile desktop-spi --ssh-key /home/luorix/.ssh/msys-dev-ed25519 `
  --ssh-control-persist 2h
.\msys.cmd key
.\msys.cmd connect
.\msys.cmd fast --repo msys-settings
.\msys.cmd shell
# Inside the resulting [msys-dev] prompt:
cd msys-shell-native
mq
mqs
mqshot ../artifacts/shell.png
msys debug
msys debug --follow
```

For a running immutable SysV release, use `fast`/`q` as the default edit loop:

```powershell
.\msys.cmd fast --repo msys-settings
.\msys.cmd q --repo msys-settings --screenshot artifacts\settings.png --force
.\msys.cmd fast --repo msys-settings --deliver
.\msys.cmd fast --repo my-app --deliver `
  --overlay msys-sdk/msys_sdk=files/app/msys_sdk
```

Screenshot output paths belong to the workstation: the Windows wrapper maps
bare relative, `.\`/`..\`, and drive-absolute forms into WSL paths. An already
absolute Linux path is preserved, so direct Linux callers keep their existing
path semantics.

The wrapper automatically starts its loopback-only WSL broker for `fast` and
reuses it for later calls. Without `--deliver`, the Python command syncs the
selected source and collects a bounded health/debug bundle in one SSH execution: current
release, critical component state/version/path when exposed by Core,
disk/memory, recent warning/error lines, and an optional PNG. `--json` emits
the same bounded summary as structured data; it never embeds manifests or the
full isolation document.

Default `fast` does not start or restart `msysd` and clearly reports that a
source-only sync did not modify the formal live release. `--run` is required
to start a stopped development runtime. `--deliver` is also explicit and
reuses the normal MAF/install-agent transaction. Pure MAF repositories build
directly from the local workspace without a redundant remote source upload;
target-native HAL/Shell/X11 repositories still sync and build first. Repeated
`--repo` values deliver in order, and `--full-sync` explicitly refreshes all
remote source trees. Core/tools still require the release flow, while install-agent
self-update requires the documented external/offline installer CLI. From the
persistent shell, `mf` infers the current `msys-*` repository, including
`msys-notes`, `msys-calculator`, and `msys-device-info`.

`--overlay SOURCE=RELATIVE_DEST` is repeatable. If none is supplied for the
canonical Settings, Notes, Calculator, Device Info, or Input repositories,
`fast` adds `msys-sdk/msys_sdk=files/app/msys_sdk` automatically. Explicit
pre-split `msys-apps` delivery keeps this compatibility behavior. An explicit
overlay list always wins.

A bare `\.\msys.cmd q` at the workspace root is diagnostic-only. Repository
inference happens only below an `msys-*` directory, so a status check cannot
silently expand into a full-workspace sync or native Core rebuild.

Use `.\msys.cmd accept` for a read-only release/runtime acceptance snapshot.
It automatically uses the same persistent broker policy as `fast` and returns
component version/state groups (Settings, Apps, Input, Shell, display), display
session, windows, resources, and bounded warning/error evidence in one SSH
execution. Add `--screenshot [PATH]`, repeat `--expect-window role=desktop` (or
`component=`, `identity=`, `title=`), use `--strict-logs` only when recent log
matches must fail the run, and add `--json` for automation. It performs no
sync, lifecycle action, installation, or synthetic input.

`quick` (also accepted as `deploy`) is a stateless composition of existing
operations: atomic sync and its necessary native builds, followed by the normal
run/wait-ready path. It deliberately skips `doctor` in the fast path. Add
`--safe` when the full doctor gate is wanted, `--status` to inspect an existing
runtime instead of starting one, and `--screenshot [PATH]` for a final capture:

```powershell
.\msys.cmd quick --repo msys-settings --status `
  --screenshot .\dist\settings.png --force
.\msys.cmd quick --repo msys-settings --safe
```

After the first successful atomic sync, a deterministic source fingerprint
lets later calls skip an unchanged repository's upload and target-native build.
Staging-directory preparation, all selected markers, and `rsync` capability are
handled in one SSH probe. Use
`--full-sync` only when repairing a manually modified remote development tree.

It never stops or restarts a running runtime. The default `run` already waits
for readiness, so no redundant status request is added. `--status --screenshot`
returns health and the PNG through one bounded SSH report.

`debug` executes the runtime health snapshot and log tail in one SSH command;
`debug --follow` keeps that same connection open. Like `fast`, `accept`, and
`ui-accept`, `.\msys.cmd debug` starts or reuses the loopback-only local broker
by default, so repeated snapshots do not recreate WSL. To manage it explicitly:

```powershell
.\msys.cmd broker start
.\msys.cmd debug          # Auto reuses the explicitly started broker
.\msys.cmd broker status
.\msys.cmd broker stop
```

Auto never starts a broker on its own. `fast`/`q`, `quick`/`deploy`, `accept`,
`ui-accept`, and `debug` select On automatically; `-Broker On` starts/requires one, while
`-Broker Off` always selects one-shot WSL. The broker binds only to
`127.0.0.1` and is token-gated from a state file under the current user's
`%LOCALAPPDATA%\MSYS\dev-brokers`; it does not expose a device service or accept
shell text, and its secret is not placed in a WSL command line. Each request
starts an isolated `msys_tools.dev` child with a validated JSON argument array,
so command behavior is unchanged while the Windows-to-WSL startup cost
disappears after explicit startup. It exits after four idle hours and remains
off until explicitly started again (or a command is run with `-Broker On`).

`key` and `connect` intentionally remain one-shot interactive WSL commands so
an SSH password prompt can reach the terminal. If localhost forwarding is
unavailable on a particular machine, use `-Broker Off` (or set
`MSYS_DEV_BROKER=Off`) for the exact prior compatibility behavior.

For an interactive Linux command prompt, use the persistent WSL shell:

```powershell
.\msys.cmd shell
# then at [msys-dev] ...:
m debug
cd msys-shell-native
mq
mqs
mqshot ../artifacts/shell.png
m screenshot ./artifacts/home.png
```

The shell's `m`/`msys` function calls the same `msys_tools.dev` module but does
not recreate a WSL command process for each invocation. `mq` also infers the
repository from the current `msys-*` directory; `mqs` adds status-only mode and
`mqshot` adds a screenshot. It still uses the configured SSH ControlMaster,
and `m connect` can warm that master before starting a debugging session.
Windows absolute paths and explicit `.\`/`..\` paths are translated
automatically. Use `.\msys.cmd` if a local PowerShell
execution policy blocks `.ps1`; it applies only a process-local bypass.

The direct `wsl env ... python3 -m msys_tools.dev ...` examples later in this
document are retained for Linux automation. In everyday PowerShell work, use
`.\msys.cmd` followed by the same MSYS arguments instead; e.g.
`.\msys.cmd screenshot .\artifacts\openstick.png`. Do not retype the WSL
prefix for each debug command.

## Direct CLI form

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

Configuration is stored at `~/.config/msys-dev/config.json` in the environment
running the tool. When using WSL from PowerShell, that means the WSL user's home
directory.

Persist the complete development context once, including the workspace and
dedicated SSH transport. Values are written atomically and subsequent commands
reuse one OpenSSH control connection:

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

`doctor` uses one read-only remote shell probe, so an initial password prompt
cannot repeat once for every capability. A target system Python and rsync are
optional; only the private runtime supervises MSYS. `make` and `cc` are marked
build-required and are fatal when absent because source synchronization builds
native programs in remote staging. No package manager is called.

The current OpenStick acceptance profile is `desktop-spi`. For that profile,
`doctor` also requires Bash, `xdpyinfo`, an available Xorg or Xvfb executable,
and isolated-Python `import tkinter`. It reports Xorg/Xvfb availability
separately. The native X11 policy and CH347 provider are reported as
`workspace-sync` artifacts, while `/root/x11display` scripts, library, and
binaries are reported as `x11display-sync` artifacts. Run both sync paths on a
new target before expecting the final check to pass. Changing the persisted
profile changes the checks but does not reinstall a component package:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev doctor --profile desktop-spi
```

For the current Python reference runtime the device needs Python, but it should
be isolated inside the MSYS development directory:

```text
/opt/msys-dev/.runtime/python/bin/python3
```

The easiest path is automatic bootstrap:

```powershell
python -m msys_tools.dev runtime bootstrap
```

This downloads `astral-sh/python-build-standalone` for
`aarch64-unknown-linux-gnu`, uploads it over SSH, extracts it to a temporary
directory, and atomically swaps `.runtime/python`.

To only fetch the archive:

```powershell
python -m msys_tools.dev runtime fetch
```

To install a manually prepared archive, its root should contain `bin/python3`:

```powershell
python -m msys_tools.dev runtime install --archive .\runtime\python-aarch64.tar.gz
```

If you already have a runtime directory locally, package it with:

```powershell
python -m msys_tools.dev runtime make --source .\runtime\python --output .\runtime\python-aarch64.tar.gz
```

The future native `msysd` will remove Python as a supervisor dependency, but UI
providers and development tools may still use isolated language runtimes.

## Remote update versus dev sync

`msys-dev sync` is for trusted developer iteration. It may overwrite the remote
development directory. Each repository is first uploaded to `.sync/NAME.new`,
then swapped into place while retaining `.NAME.previous`; failed transfers do
not erase the last complete tree. The default set includes `msys-hal`,
`msys-settings`, `msys-notes`, `msys-calculator`, `msys-device-info`, and the
replaceable `msys-input-touch` provider as well as core, shell, X11, installer,
SDK, contracts, and tools.

For `msys-x11-session`, activation additionally requires `make all` to succeed
inside `.sync/msys-x11-session.new` and produce an executable
`bin/msys-x11-policy`. The build uses an already provisioned compiler/Xlib
development environment and never invokes a package manager. Failure leaves
the previous X11 repository active.

The external OpenStick capture/CH347 source tree is deployed independently and
is also compiled on the target before activation:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev sync-x11display --local x11display --destination /root/x11display
```

The command forces a clean staging build and verifies the five Makefile runtime
targets before swapping directories. A failure keeps `/root/x11display`
unchanged and removes `/root/x11display.new` plus the incoming archive. This
prevents a newer capture source, including first-frame fixes, from being
deployed beside an older bundled `aarch64` binary.

A persisted custom `repos` list remains an intentional subset. Older setups
must include `msys-x11-session` explicitly, or use `config unset repos` to
restore the complete default. `MSYS_DEV_REPOS` can temporarily override that
list with comma-separated repository names.

The same default workspace root is used by `package discover`, so the split
application manifests are found and strictly validated with the system package
manifests:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev package discover
```

At launch, repository-owned declarations are supplied with repeatable
`msysd --manifest` arguments. Shell, HAL, the canonical X11 session, and the
legacy OpenStick CH347 provider can therefore evolve in their own Git
repositories without copying their manifests into core. The canonical X11
manifest is loaded before the board-specific CH347 declaration, and the
host-service launcher uses the same manifest list.

`sync` only stages source code. Split application manifests are never passed to
canonical `msysd` startup. Deliver them through the installer registry so lifecycle,
version history, integrity hashes, and rollback remain consistent.

Runtime remote update is handled by `msys-install` and the `update-agent`. That
path verifies package manifests and commits versions into `$MSYS_STATE_DIR`.

## Start a normal application project

Create an application from PowerShell through WSL so executable modes and the
same persisted target configuration are retained:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev app new `
  /mnt/g/Code/MsYs/hello-msys `
  --id org.example.hello `
  --name "Hello MSYS" `
  --template tk
```

Choose `tk`, `python`, `c`, `cpp`, `qt`, or `electron`. Creation is local and
refuses every existing destination, including an empty directory. Package and
component ids are checked before anything is written. Each project includes:

- a strict application manifest and stable X11 identity;
- an explicit `files/` installed-package boundary;
- `msys.i18n.catalog.v1` complete English and base Chinese messages plus
  `x-msys-i18n` `app.name`/`app.summary` presentation metadata (add only the
  differing keys if a real `zh-CN` regional overlay is needed);
- a working framework-specific localization example;
- offline build/runtime instructions and no automatic dependency download.

The generated localization code consistently samples
`MSYS_LOCALE > LC_ALL > LC_MESSAGES > LANG`, normalizes POSIX values such as
`zh_Hans_CN.UTF-8`, and merges each available parent locale before its partial
child overlay. Tk/Python, Qt, and Electron pages wrap long content and scroll
vertically when the target window is small.

Tk/Python use the isolated MSYS development interpreter directly for the fast
loop. C/C++ require the target-ABI `files/bin/app`; Qt also requires its private
libraries/xcb plugin under `files/runtime/qt`; Electron requires a complete
runtime under `files/runtime/electron`. Until those declared artifacts exist,
strict validation fails cleanly instead of launching a host fallback.

With the development daemon and install agent running, validate, build, upload,
atomically install and start the exact application component with:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev app run `
  /mnt/g/Code/MsYs/hello-msys
```

Useful forms include `--no-start`, `--component main`, `--output
/mnt/g/Code/MsYs/dist`, `--format maf`, and repeatable package overlays. The
command delegates to the existing validate/build/install-archive/start paths;
it does not add the generated project to repository sync or formal releases.

## Package release loop

Keep `msys-install` beside `msys-tools` in the workspace (or set
`MSYS_INSTALL_SOURCE`). The developer CLI imports that source directly, so no
host package-manager installation is needed:

```powershell
# Strict msys.manifest.v1 and package-tree validation
python -m msys_tools.dev package validate G:\Code\MsYs\my-package

# Staged hashes.json plus verified, reproducible tar.gz
python -m msys_tools.dev package build G:\Code\MsYs\my-package --output G:\Code\MsYs\dist

# Verified msys.update-index.v1 for a static update repository
python -m msys_tools.dev package index G:\Code\MsYs\dist --base-url https://updates.example/msys/
```

Scan the whole workspace and validate every discovered `msys.manifest.v1`
declaration with the same zero-dependency contract implementation:

```powershell
python -m msys_tools.dev package discover G:\Code\MsYs
```

HAL and Settings are real packages and follow exactly the same delivery path:

```powershell
python -m msys_tools.dev package build G:\Code\MsYs\msys-hal --output G:\Code\MsYs\dist
python -m msys_tools.dev package build G:\Code\MsYs\msys-settings --output G:\Code\MsYs\dist
```

When a development daemon and install agent are already running, the combined
command is convenient:

```powershell
python -m msys_tools.dev package deliver G:\Code\MsYs\msys-settings --output G:\Code\MsYs\dist --force
python -m msys_tools.dev package deliver G:\Code\MsYs\msys-calculator --output G:\Code\MsYs\dist --force
```

It builds a reproducible archive, requires complete content hashes, verifies it
locally, uploads it through the multiplexed SSH transport, and performs a typed
`role:install-agent.install_archive` request. The archive is staged only under
`$MSYS_STATE_DIR/updates/staged-rpc`; SHA-256, package id, and version are bound
into the request. It never invokes apt, pip, or another target package manager.
`install-archive` performs the same local verification even when the archive
was built separately. Typed install/update/rollback replies are terminal and
`ok=false` is nonzero. Old best-effort topics require explicit
`--legacy-events` and are never an automatic fallback.

The normal project flow invokes these commands through WSL, as shown in the
top-level README, so executable mode bits from Linux-built payloads are
preserved. `package build` does not modify the source package.

After publishing the archive and `index.json`, use `check-update` and
`apply-update`. If an applied version must be reverted, ask the running
install agent to atomically swap its version pointers:

```powershell
python -m msys_tools.dev package rollback org.example.app
```

This is a runtime package rollback, not a source-tree or development `sync`
rollback.

To acceptance-test the real package-level `previous.json` pointer without
leaving the older version selected, use one guarded round trip:

```powershell
.\msys.cmd package roundtrip org.example.app
```

The command records the exact current version, path, artifact SHA-256, and
content SHA-256; rolls back to the real previous package; then rolls back again
and requires every recorded current field to match. Once the first transition
may have happened, failure paths still attempt the restoring rollback. A failed
recovery is reported without a blind retry because rollback itself is
intentionally non-idempotent.

## Native touch-path debugging

The remote gesture commands call the native `msys-x11-policy` debug interface;
they do not install or invoke `xdotool`. `DISPLAY` is read from the active
`display-session.json`, while the configured target, remote root, runtime
directory, key, `ControlPath`, and `ControlPersist` are all reused from the
persistent WSL configuration.

```powershell
# Active Native or PySide navigation-bar role
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 160 34 160 5 --duration-ms 220

# Any stable identity
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --identity org.example.app

# Identity-less legacy window title
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --title "Legacy App"

# One exact identity/title pair
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev swipe 10 300 10 40 --window org.example.app "Example App"
```

Coordinates must be in `0..32767` and `--duration-ms` in `40..5000`. Use
`--display :24` only as an explicit recovery override when the live display
session document is unavailable. With no selector, both `tap` and `swipe`
resolve the visible `_MSYS_WINDOW_ROLE=navigation-bar` window, so changing the
selected shell provider does not require changing the debug command. An
explicit `--identity`, `--title`, or `--window` selector bypasses that default.

## Screenshot and semantic visual smoke debugging

The window-manager CLI exposes stable handle-addressed actions directly, so
PowerShell callers do not have to escape a JSON payload:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm list
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm move --window-id msys.x11-window.v1:PID:GEN:TOKEN --x 20 --y 60
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev wm resize --window-id msys.x11-window.v1:PID:GEN:TOKEN --width 280 --height 360
```

`focus`, `minimize`, `move-resize`, and `close` use the same
`--window-id`. Tools validates the stable prefix and geometry bounds locally;
X11 policy then validates the generation-bearing handle before mutation.

Capture the active X11 session to a workstation-visible path without adding a
target package-manager dependency:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev screenshot /mnt/g/Code/MsYs/artifacts/openstick.png
```

The helper resolves the selected display from
`/tmp/msys-main/display-session.json` (normally `:24` on OpenStick), prefers an
already provisioned `scrot`, and falls back to an already provisioned
`ffmpeg -f x11grab`. `--backend scrot|ffmpeg` selects one explicitly and
`--display :24` is a recovery override. Capture never installs either binary.
The random remote temporary file is quoted, downloaded with the configured SSH
key/ControlMaster transport, validated as PNG, and removed on every exit path.

To exercise navigation semantics rather than pointer coordinates, first close
all user applications and overlays so Home is clean, then run:

```powershell
wsl env PYTHONPATH=/mnt/g/Code/MsYs/msys-tools python3 -m msys_tools.dev visual-smoke
```

The omitted component defaults to `org.msys.calculator:calculator`. A board
that has no split application components yet may use the legacy calculator ID;
the result marks that compatibility selection explicitly.

This uses only typed Core/window-manager calls for Home, start, Back, and
Recents. It refuses a running test component or non-empty user-window state;
after mutation it cleans up only the component it started and restores Home,
including on a failed intermediate assertion.

## Persistent startup without systemd

Once the development session is stable, install the same workspace as an
ordinary host service. This is independent of `sync` and does not make MSYS the
machine's init process:

```powershell
python -m msys_tools.dev host-service detect
python -m msys_tools.dev host-service install --backend auto --dry-run
python -m msys_tools.dev stop
python -m msys_tools.dev host-service install --backend sysv --start-now
python -m msys_tools.dev host-service status
```

Choose `openrc`, `rc-local`, or `hook --hook /absolute/file` instead when that
matches the board. The dry-run still performs read-only SSH detection but makes
no remote filesystem or service changes. Full behavior and safety rules are in
[host-service.md](host-service.md).
