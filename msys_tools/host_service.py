from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


MANAGED_MARKER = "# Managed by msys-dev host-service v1"
HOOK_BEGIN = "# >>> MSYS HOST SERVICE >>>"
HOOK_END = "# <<< MSYS HOST SERVICE <<<"
BACKENDS = ("auto", "sysv", "openrc", "rc-local", "hook")
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class HostServiceError(RuntimeError):
    """Host-service configuration or installation is invalid."""


def quote_sh(value: str) -> str:
    return shlex.quote(value)


def remote_path(value: str, field: str) -> str:
    if not value or any(ord(character) < 32 for character in value):
        raise HostServiceError(f"{field} must be a non-empty single-line path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise HostServiceError(f"{field} must be an absolute path without '..': {value}")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class HostServiceSpec:
    root: str
    python: str
    runtime_dir: str = "/run/msys/main"
    state_dir: str = "/opt/msys-state"
    log_file: str = "/tmp/msysd.log"
    profile: str = "mobile-spi"
    release_root: str | None = None

    def __post_init__(self) -> None:
        for field in ("root", "python", "runtime_dir", "state_dir", "log_file"):
            object.__setattr__(self, field, remote_path(getattr(self, field), field))
        if self.release_root is not None:
            release_root = remote_path(self.release_root, "release_root").rstrip("/")
            if release_root == "":
                release_root = "/"
            if release_root == "/":
                raise HostServiceError("release_root must not be the filesystem root")
            object.__setattr__(self, "release_root", release_root)
            expected_root = f"{release_root}/current"
            if self.root != expected_root:
                raise HostServiceError(
                    f"release service root must be the current pointer {expected_root}"
                )
        if not PROFILE_PATTERN.fullmatch(self.profile):
            raise HostServiceError(f"invalid profile name: {self.profile!r}")

    @property
    def service_dir(self) -> str:
        if self.release_root is not None:
            return f"{self.release_root}/service"
        return f"{self.root.rstrip('/')}/.service"

    @property
    def launcher(self) -> str:
        return f"{self.service_dir}/msys-service"

    @property
    def state_file(self) -> str:
        return f"{self.service_dir}/installation.state"

    @property
    def config_dir(self) -> str:
        return f"{self.root.rstrip('/')}/msys-core/examples/config"


def integration_path(backend: str, hook: str | None = None) -> str:
    if backend == "sysv" or backend == "openrc":
        if hook is not None:
            raise HostServiceError(f"--hook is not valid with backend {backend}")
        return "/etc/init.d/msys"
    if backend == "rc-local":
        if hook is not None:
            raise HostServiceError("--hook is not valid with backend rc-local")
        return "/etc/rc.local"
    if backend == "hook":
        if hook is None:
            raise HostServiceError("backend hook requires --hook /absolute/startup-file")
        return remote_path(hook, "hook")
    raise HostServiceError(f"backend must be explicitly resolved, got {backend!r}")


def select_backend(requested: str, detected: Iterable[str]) -> str:
    if requested not in BACKENDS:
        raise HostServiceError(f"unsupported host-service backend: {requested}")
    available = list(dict.fromkeys(detected))
    if requested != "auto":
        return requested
    for candidate in ("openrc", "sysv", "rc-local"):
        if candidate in available:
            return candidate
    raise HostServiceError(
        "no supported host startup mechanism was detected; select --backend hook --hook PATH"
    )


def detection_command() -> str:
    return """set -eu
if { test -d /run/openrc || test -e /run/openrc/softlevel; } && test -x /sbin/openrc-run; then
    echo openrc
fi
if test -d /etc/init.d; then
    echo sysv
fi
if test -f /etc/rc.local && test ! -L /etc/rc.local; then
    echo rc-local
fi
"""


def parse_detection(output: str) -> list[str]:
    return [line for line in output.splitlines() if line in BACKENDS and line != "auto"]


def render_launcher(spec: HostServiceSpec) -> str:
    defaults = {
        "MSYS_ROOT_DEFAULT": spec.root,
        "MSYS_PYTHON_DEFAULT": spec.python,
        "MSYS_RUNTIME_DEFAULT": spec.runtime_dir,
        "MSYS_STATE_DEFAULT": spec.state_dir,
        "MSYS_LOG_DEFAULT": spec.log_file,
        "MSYS_PROFILE_DEFAULT": spec.profile,
        "MSYS_CONFIG_DEFAULT": spec.config_dir,
        "MSYS_RELEASE_ROOT_DEFAULT": spec.release_root or "",
    }
    assignments = "\n".join(
        f"{name}={quote_sh(value)}" for name, value in defaults.items()
    )
    return f"""#!/bin/sh
{MANAGED_MARKER}
set -eu

{assignments}
MSYS_ROOT=${{MSYS_ROOT:-$MSYS_ROOT_DEFAULT}}
MSYS_PYTHON=${{MSYS_PYTHON:-$MSYS_PYTHON_DEFAULT}}
MSYS_RUNTIME_DIR=${{MSYS_RUNTIME_DIR:-$MSYS_RUNTIME_DEFAULT}}
MSYS_STATE_DIR=${{MSYS_STATE_DIR:-$MSYS_STATE_DEFAULT}}
MSYS_LOG_FILE=${{MSYS_LOG_FILE:-$MSYS_LOG_DEFAULT}}
MSYS_PROFILE=${{MSYS_PROFILE:-$MSYS_PROFILE_DEFAULT}}
MSYS_CONFIG_DIR=${{MSYS_CONFIG_DIR:-$MSYS_CONFIG_DEFAULT}}
MSYS_RELEASE_ROOT=${{MSYS_RELEASE_ROOT:-$MSYS_RELEASE_ROOT_DEFAULT}}
PID_FILE="$MSYS_RUNTIME_DIR/msysd.pid"

resolve_start_root() {{
    if test -n "$MSYS_RELEASE_ROOT"; then
        expected="$MSYS_RELEASE_ROOT/current"
        if test "$MSYS_ROOT" != "$expected" || test ! -L "$MSYS_ROOT"; then
            echo "formal MSYS root must be the current release pointer: $expected" >&2
            return 1
        fi
        resolved=$(CDPATH= cd "$MSYS_ROOT" 2>/dev/null && pwd -P) || {{
            echo "cannot resolve current MSYS release: $MSYS_ROOT" >&2
            return 1
        }}
        relative=${{resolved#"$MSYS_RELEASE_ROOT/releases/"}}
        if test "$relative" = "$resolved" || test -z "$relative"; then
            echo "current MSYS pointer is outside $MSYS_RELEASE_ROOT/releases" >&2
            return 1
        fi
        case "$relative" in */*)
            echo "current MSYS pointer does not name one release: $resolved" >&2
            return 1
        esac
        MSYS_ROOT=$resolved
        MSYS_PYTHON="$MSYS_ROOT/.runtime/python/bin/python3"
        MSYS_CONFIG_DIR="$MSYS_ROOT/msys-core/examples/config"
    fi
    MSYS_PLATFORM_PYTHONPATH="$MSYS_ROOT/msys-sdk"
    export MSYS_PLATFORM_PYTHONPATH
    PYTHONDONTWRITEBYTECODE=1
    export PYTHONDONTWRITEBYTECODE
    MALLOC_ARENA_MAX="${{MALLOC_ARENA_MAX:-2}}"
    export MALLOC_ARENA_MAX
    # A fixed threshold prevents glibc's dynamic trim heuristic from retaining
    # transient manifest/catalog parsing heaps for the supervisor lifetime.
    # msysd consumes this variable before it constructs component environments.
    MALLOC_TRIM_THRESHOLD_="${{MALLOC_TRIM_THRESHOLD_:-262144}}"
    export MALLOC_TRIM_THRESHOLD_
}}

read_pid() {{
    pid=
    test -r "$PID_FILE" || return 1
    IFS= read -r pid < "$PID_FILE" || return 1
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    return 0
}}

owns_pid() {{
    test -r "/proc/$pid/cmdline" || return 0
    command -v tr >/dev/null 2>&1 || return 0
    command_line=$(tr '\\000' ' ' < "/proc/$pid/cmdline")
    case "$command_line" in *msys_core.msysd*) return 0 ;; *) return 1 ;; esac
}}

is_running() {{
    read_pid || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    owns_pid
}}

find_external_msysd() {{
    external_pid=
    command -v tr >/dev/null 2>&1 || return 1
    for command_file in /proc/[0-9]*/cmdline; do
        test -r "$command_file" || continue
        candidate=${{command_file#/proc/}}
        candidate=${{candidate%/cmdline}}
        case "$candidate" in ''|*[!0-9]*|$$) continue ;; esac
        command_line=$(tr '\000' ' ' < "$command_file")
        case "$command_line" in
            *msys_core.msysd*"$MSYS_RUNTIME_DIR"*)
                external_pid=$candidate
                return 0
                ;;
        esac
    done
    return 1
}}

start_service() {{
    if is_running; then
        echo "msysd already running (pid $pid)"
        return 0
    fi
    if find_external_msysd; then
        echo "refusing to start a duplicate msysd; unmanaged instance pid $external_pid uses $MSYS_RUNTIME_DIR" >&2
        return 4
    fi
    resolve_start_root || return $?
    rm -f "$PID_FILE"
    if test ! -x "$MSYS_PYTHON"; then
        echo "isolated Python is missing: $MSYS_PYTHON" >&2
        return 1
    fi
    log_directory=${{MSYS_LOG_FILE%/*}}
    test -n "$log_directory" || log_directory=/
    mkdir -p "$MSYS_RUNTIME_DIR" "$log_directory"
    trap '' HUP
    (
        cd "$MSYS_ROOT"
        export MSYS_STATE_DIR
        export MSYS_PLATFORM_PYTHONPATH
        export PYTHONPATH="$MSYS_ROOT/msys-core:$MSYS_ROOT/msys-sdk:$MSYS_ROOT/msys-shell-pyside:$MSYS_ROOT/msys-x11-session:$MSYS_ROOT/msys-hal:$MSYS_ROOT/msys-input-touch/files/app:$MSYS_ROOT/msys-install"
        set -- -m msys_core.msysd --foreground \\
            --config "$MSYS_CONFIG_DIR" \\
            --runtime-dir "$MSYS_RUNTIME_DIR" \\
            --profile "$MSYS_PROFILE"
        native_shell_manifest="$MSYS_ROOT/msys-shell-native/manifest.json"
        shell_manifest="$MSYS_ROOT/msys-shell-pyside/manifest.json"
        hal_manifest="$MSYS_ROOT/msys-hal/manifest.json"
        x11_session_manifest="$MSYS_ROOT/msys-x11-session/manifest.json"
        ch347_manifest="$MSYS_ROOT/msys-openstick-ch347/manifest.json"
        input_manifest="$MSYS_ROOT/msys-input-touch/manifest.json"
        install_manifest="$MSYS_ROOT/msys-install/manifest.json"
        if test -f "$native_shell_manifest"; then
            set -- "$@" --manifest "$native_shell_manifest"
        fi
        if test -f "$shell_manifest"; then
            set -- "$@" --manifest "$shell_manifest"
        fi
        if test -f "$hal_manifest"; then
            set -- "$@" --manifest "$hal_manifest"
        fi
        if test -f "$x11_session_manifest"; then
            set -- "$@" --manifest "$x11_session_manifest"
        fi
        if test -f "$ch347_manifest"; then
            set -- "$@" --manifest "$ch347_manifest"
        fi
        if test -f "$input_manifest"; then
            set -- "$@" --manifest "$input_manifest"
        fi
        if test -f "$install_manifest"; then
            set -- "$@" --manifest "$install_manifest"
        fi
        if command -v setsid >/dev/null 2>&1; then
            exec setsid "$MSYS_PYTHON" "$@"
        fi
        exec "$MSYS_PYTHON" "$@"
    ) >> "$MSYS_LOG_FILE" 2>&1 < /dev/null &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "msysd failed to start; see $MSYS_LOG_FILE" >&2
        return 1
    fi
    echo "started msysd (pid $pid)"
}}

stop_service() {{
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "msysd is not running"
        return 0
    fi
    kill -TERM "$pid"
    remaining=15
    while kill -0 "$pid" 2>/dev/null && test "$remaining" -gt 0; do
        sleep 1
        remaining=$((remaining - 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "msysd did not stop within 15 seconds (pid $pid)" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    echo "stopped msysd"
}}

status_service() {{
    if is_running; then
        echo "msysd is running (pid $pid)"
        return 0
    fi
    if find_external_msysd; then
        echo "msysd is running outside the host-service launcher (pid $external_pid)"
        return 4
    fi
    echo "msysd is stopped"
    return 3
}}

case "${{1:-}}" in
    start) start_service ;;
    stop) stop_service ;;
    restart) stop_service && start_service ;;
    status) status_service ;;
    *) echo "usage: $0 {{start|stop|restart|status}}" >&2; exit 2 ;;
esac
"""


def render_sysv(spec: HostServiceSpec) -> str:
    launcher = quote_sh(spec.launcher)
    return f"""#!/bin/sh
{MANAGED_MARKER}
### BEGIN INIT INFO
# Provides:          msys
# Required-Start:    $local_fs $network
# Required-Stop:     $local_fs $network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: MSYS user-space session
### END INIT INFO
set -eu
case "${{1:-}}" in
    start|stop|restart|status) exec {launcher} "$1" ;;
    *) echo "usage: $0 {{start|stop|restart|status}}" >&2; exit 2 ;;
esac
"""


def render_openrc(spec: HostServiceSpec) -> str:
    launcher = quote_sh(spec.launcher)
    return f"""#!/sbin/openrc-run
{MANAGED_MARKER}
description="MSYS user-space session"

depend() {{
    need localmount
    after networking
}}

start() {{
    ebegin "Starting MSYS"
    {launcher} start
    eend $?
}}

stop() {{
    ebegin "Stopping MSYS"
    {launcher} stop
    eend $?
}}

status() {{
    {launcher} status
}}
"""


def render_state(spec: HostServiceSpec, backend: str, integration: str) -> str:
    values = {
        "backend": backend,
        "integration": integration,
        "launcher": spec.launcher,
        "root": spec.root,
        "layout": "release" if spec.release_root is not None else "development",
        "release_root": spec.release_root or "",
    }
    return MANAGED_MARKER + "\n" + "".join(
        f"{key}={value}\n" for key, value in values.items()
    )


def parse_state(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != MANAGED_MARKER:
        raise HostServiceError("host-service state is not managed by msys-dev")
    result: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or not key:
            raise HostServiceError("invalid host-service state file")
        result[key] = value
    if result.get("backend") not in BACKENDS or result.get("backend") == "auto":
        raise HostServiceError("invalid backend in host-service state")
    for key in ("integration", "launcher", "root"):
        if key not in result:
            raise HostServiceError(f"host-service state is missing {key}")
    return result


def managed_file_test(path: str) -> str:
    path = remote_path(path, "managed file")
    marker = quote_sh(MANAGED_MARKER)
    return f"""test -f {quote_sh(path)} && test ! -L {quote_sh(path)} || exit 1
managed=0
while IFS= read -r line; do
    if test "$line" = {marker}; then managed=1; break; fi
done < {quote_sh(path)}
test "$managed" -eq 1
"""


def hook_block_test(path: str) -> str:
    path = remote_path(path, "hook")
    return f"""set -eu
test -f {quote_sh(path)} && test ! -L {quote_sh(path)} || exit 1
begin=0
end=0
inside=0
valid=1
while IFS= read -r line; do
    if test "$line" = {quote_sh(HOOK_BEGIN)}; then
        test "$inside" -eq 0 || valid=0
        begin=$((begin + 1))
        inside=1
    fi
    if test "$line" = {quote_sh(HOOK_END)}; then
        test "$inside" -eq 1 || valid=0
        end=$((end + 1))
        inside=0
    fi
done < {quote_sh(path)}
test "$valid" -eq 1 && test "$inside" -eq 0 && test "$begin" -eq 1 && test "$end" -eq 1
"""


def regular_file_test(path: str) -> str:
    path = remote_path(path, "regular file")
    return f"test -f {quote_sh(path)} && test ! -L {quote_sh(path)}"


def hook_marker_presence_test(path: str) -> str:
    path = remote_path(path, "hook")
    return f"""set -eu
test -f {quote_sh(path)} && test ! -L {quote_sh(path)} || exit 1
found=0
while IFS= read -r line; do
    if test "$line" = {quote_sh(HOOK_BEGIN)} || test "$line" = {quote_sh(HOOK_END)}; then
        found=1
    fi
done < {quote_sh(path)}
test "$found" -eq 1
"""


def integration_binding_test(backend: str, integration: str, launcher: str) -> str:
    """Return a POSIX-shell test for an integration bound to this launcher.

    The managed marker establishes MSYS ownership, while the exact generated
    command establishes ownership by one particular development or release
    layout.  Both checks are required: /etc/init.d/msys is shared by all MSYS
    layouts and a marker alone must never authorize a cross-layout overwrite
    or removal.
    """

    integration = remote_path(integration, "integration")
    launcher = remote_path(launcher, "launcher")
    if backend == "sysv":
        expected = f'    start|stop|restart|status) exec {quote_sh(launcher)} "$1" ;;'
        return managed_file_test(integration) + f"""
matches=0
while IFS= read -r line; do
    if test "$line" = {quote_sh(expected)}; then matches=$((matches + 1)); fi
done < {quote_sh(integration)}
test "$matches" -eq 1
"""
    if backend == "openrc":
        expected_start = f"    {quote_sh(launcher)} start"
        expected_stop = f"    {quote_sh(launcher)} stop"
        expected_status = f"    {quote_sh(launcher)} status"
        return managed_file_test(integration) + f"""
start_matches=0
stop_matches=0
status_matches=0
while IFS= read -r line; do
    if test "$line" = {quote_sh(expected_start)}; then start_matches=$((start_matches + 1)); fi
    if test "$line" = {quote_sh(expected_stop)}; then stop_matches=$((stop_matches + 1)); fi
    if test "$line" = {quote_sh(expected_status)}; then status_matches=$((status_matches + 1)); fi
done < {quote_sh(integration)}
test "$start_matches" -eq 1 && test "$stop_matches" -eq 1 && test "$status_matches" -eq 1
"""
    if backend in {"rc-local", "hook"}:
        expected = f"{quote_sh(launcher)} start >/dev/null 2>&1 || :"
        return hook_block_test(integration) + f"""
matches=0
inside=0
while IFS= read -r line; do
    if test "$line" = {quote_sh(HOOK_BEGIN)}; then inside=1; continue; fi
    if test "$line" = {quote_sh(HOOK_END)}; then inside=0; continue; fi
    if test "$inside" -eq 1 && test "$line" = {quote_sh(expected)}; then
        matches=$((matches + 1))
    fi
done < {quote_sh(integration)}
test "$matches" -eq 1
"""
    raise HostServiceError(f"unsupported host-service backend: {backend}")


def validate_state_binding(spec: HostServiceSpec, state: dict[str, str]) -> None:
    """Reject persisted metadata copied from or describing another layout."""

    expected = {
        "root": spec.root,
        "launcher": spec.launcher,
        "layout": "release" if spec.release_root is not None else "development",
        "release_root": spec.release_root or "",
    }
    for field in ("root", "launcher"):
        if state.get(field) != expected[field]:
            raise HostServiceError(
                f"host-service state {field} belongs to another layout: "
                f"{state.get(field, '<missing>')}"
            )
    # layout/release_root were added after the first state format.  Continue to
    # accept old state files, but validate these fields whenever they exist.
    for field in ("layout", "release_root"):
        if field in state and state[field] != expected[field]:
            raise HostServiceError(
                f"host-service state {field} belongs to another layout: "
                f"{state[field]}"
            )


def prerequisite_command(backend: str, integration: str) -> str:
    integration = remote_path(integration, "integration")
    if backend == "openrc":
        return """set -eu
test -x /sbin/openrc-run || { echo '/sbin/openrc-run is missing' >&2; exit 1; }
command -v rc-update >/dev/null 2>&1 || { echo 'rc-update is missing' >&2; exit 1; }
test -d /etc/init.d
"""
    if backend == "sysv":
        return "test -d /etc/init.d || { echo '/etc/init.d is missing' >&2; exit 1; }"
    parent = PurePosixPath(integration).parent.as_posix()
    message = quote_sh(f"hook parent is missing: {parent}")
    return f"test -d {quote_sh(parent)} || {{ echo {message} >&2; exit 1; }}"


def atomic_install_command(incoming: str, destination: str, mode: str) -> str:
    incoming = remote_path(incoming, "incoming")
    destination = remote_path(destination, "destination")
    parent = PurePosixPath(destination).parent.as_posix()
    marker = quote_sh(MANAGED_MARKER)
    return f"""set -eu
incoming={quote_sh(incoming)}
destination={quote_sh(destination)}
if test -e "$destination" || test -L "$destination"; then
    test -f "$destination" && test ! -L "$destination" || {{ echo "refusing non-regular destination: $destination" >&2; exit 3; }}
    managed=0
    while IFS= read -r line; do
        if test "$line" = {marker}; then managed=1; break; fi
    done < "$destination"
    test "$managed" -eq 1 || {{ echo "refusing to overwrite unmanaged file: $destination" >&2; exit 3; }}
fi
mkdir -p {quote_sh(parent)}
temporary="$destination.msys-new.$$"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
cp "$incoming" "$temporary"
chmod {mode} "$temporary"
mv -f "$temporary" "$destination"
trap - EXIT HUP INT TERM
"""


def managed_remove_command(path: str) -> str:
    path = remote_path(path, "managed file")
    return f"""set -eu
path={quote_sh(path)}
if test ! -e "$path" && test ! -L "$path"; then exit 0; fi
{managed_file_test(path)}
rm -f "$path"
"""


def hook_edit_command(path: str, launcher: str, *, install: bool) -> str:
    path = remote_path(path, "hook")
    launcher = remote_path(launcher, "launcher")
    parent = PurePosixPath(path).parent.as_posix()
    install_flag = "1" if install else "0"
    launch_line = f"{quote_sh(launcher)} start >/dev/null 2>&1 || :"
    return f"""set -eu
target={quote_sh(path)}
begin={quote_sh(HOOK_BEGIN)}
end={quote_sh(HOOK_END)}
install_block={install_flag}
if test ! -e "$target" && test "$install_block" -eq 0; then exit 0; fi
if test -e "$target" || test -L "$target"; then
    test -f "$target" && test ! -L "$target" || {{ echo "refusing non-regular hook: $target" >&2; exit 3; }}
fi
mkdir -p {quote_sh(parent)}
temporary="$target.msys-new.$$"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
emit_block() {{
    printf '%s\n' "$begin" {quote_sh(launch_line)} "$end"
}}
if test ! -e "$target"; then
    printf '%s\n' '#!/bin/sh' > "$temporary"
    emit_block >> "$temporary"
    chmod 755 "$temporary"
else
    cp -p "$target" "$temporary"
    : > "$temporary"
    first=1
    found=0
    skipping=0
    while IFS= read -r line || test -n "$line"; do
        if test "$line" = "$begin"; then
            test "$skipping" -eq 0 || {{ echo "nested MSYS hook marker" >&2; exit 3; }}
            found=1
            skipping=1
            continue
        fi
        if test "$line" = "$end"; then
            test "$skipping" -eq 1 || {{ echo "unmatched MSYS hook marker" >&2; exit 3; }}
            skipping=0
            continue
        fi
        test "$skipping" -eq 1 && continue
        if test "$first" -eq 1; then
            case "$line" in
                '#!'*) printf '%s\n' "$line"; test "$install_block" -eq 0 || emit_block ;;
                *) test "$install_block" -eq 0 || emit_block; printf '%s\n' "$line" ;;
            esac
            first=0
        else
            printf '%s\n' "$line"
        fi
    done < "$target" > "$temporary"
    test "$skipping" -eq 0 || {{ echo "unterminated MSYS hook marker" >&2; exit 3; }}
    if test "$install_block" -eq 0 && test "$found" -eq 0; then
        rm -f "$temporary"
        trap - EXIT HUP INT TERM
        exit 0
    fi
    if test "$first" -eq 1 && test "$install_block" -eq 1; then emit_block > "$temporary"; fi
    chmod u+x "$temporary"
fi
mv -f "$temporary" "$target"
trap - EXIT HUP INT TERM
"""


def enable_command(backend: str) -> str:
    if backend == "openrc":
        return """set -eu
command -v rc-update >/dev/null 2>&1 || { echo 'rc-update is required for OpenRC registration' >&2; exit 1; }
rc-update add msys default
"""
    if backend != "sysv":
        return ":"
    return """set -eu
if command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d msys defaults
elif command -v chkconfig >/dev/null 2>&1; then
    chkconfig --add msys
else
    for item in '0 K01' '1 K01' '2 S99' '3 S99' '4 S99' '5 S99' '6 K01'; do
        set -- $item
        directory="/etc/rc$1.d"
        link="$directory/$2msys"
        mkdir -p "$directory"
        if test -e "$link" || test -L "$link"; then
            test -L "$link" && test "$(readlink "$link")" = '../init.d/msys' || {
                echo "refusing to replace existing SysV link: $link" >&2
                exit 3
            }
        else
            ln -s ../init.d/msys "$link"
        fi
    done
fi
"""


def disable_command(backend: str) -> str:
    if backend == "openrc":
        return """set -eu
if command -v rc-update >/dev/null 2>&1; then rc-update del msys default || :; fi
"""
    if backend != "sysv":
        return ":"
    return """set -eu
if command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d -f msys remove
elif command -v chkconfig >/dev/null 2>&1; then
    chkconfig --del msys || :
else
    for link in /etc/rc0.d/K01msys /etc/rc1.d/K01msys /etc/rc2.d/S99msys /etc/rc3.d/S99msys /etc/rc4.d/S99msys /etc/rc5.d/S99msys /etc/rc6.d/K01msys; do
        if test -L "$link" && test "$(readlink "$link")" = '../init.d/msys'; then rm -f "$link"; fi
    done
fi
"""


def enabled_test_command(backend: str) -> str:
    if backend == "openrc":
        return """command -v rc-update >/dev/null 2>&1 &&
rc-update show default 2>/dev/null | grep -Eq '(^|[[:space:]])msys([[:space:]]|$)'
"""
    if backend != "sysv":
        return ":"
    return """found=0
for link in /etc/rc2.d/S*msys /etc/rc3.d/S*msys /etc/rc4.d/S*msys /etc/rc5.d/S*msys; do
    if test -L "$link" && test "$(readlink "$link")" = '../init.d/msys'; then found=1; fi
done
test "$found" -eq 1
"""


def dry_run_summary(
    action: str,
    spec: HostServiceSpec,
    backend: str,
    integration: str,
    *,
    start_now: bool = False,
) -> dict[str, object]:
    changes: list[str] = []
    if action == "install":
        changes.extend([f"write {spec.launcher}", f"write {spec.state_file}"])
        if backend in {"sysv", "openrc"}:
            changes.append(f"write {integration}")
            changes.append(f"enable {backend} service msys")
        else:
            changes.append(f"insert managed block into {integration}")
        if start_now:
            changes.append(f"run {spec.launcher} start")
    else:
        changes.append(f"run {spec.launcher} stop if managed")
        if backend in {"sysv", "openrc"}:
            changes.append(f"disable {backend} service msys")
            changes.append(f"remove managed {integration}")
        else:
            changes.append(f"remove managed block from {integration}")
        changes.extend([f"remove managed {spec.state_file}", f"remove managed {spec.launcher}"])
    return {
        "dry_run": True,
        "action": action,
        "backend": backend,
        "integration": integration,
        "launcher": spec.launcher,
        "changes": changes,
    }


def dry_run_text(spec: HostServiceSpec, backend: str) -> str:
    rendered = render_openrc(spec) if backend == "openrc" else render_sysv(spec)
    if backend not in {"openrc", "sysv"}:
        rendered = f"{HOOK_BEGIN}\n{quote_sh(spec.launcher)} start >/dev/null 2>&1 || :\n{HOOK_END}\n"
    return json.dumps(
        {"launcher": render_launcher(spec), "integration": rendered},
        indent=2,
        ensure_ascii=False,
    )
