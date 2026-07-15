from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .acceptance import AcceptanceConfig, run as run_acceptance
from .app_flow import AppFlowError, TEMPLATES, create_app, select_app_component
from .package_flow import (
    PackageFlowError,
    build_index,
    build_package,
    discover_manifests,
    generate_update_signing_key,
    inspect_update_public_key,
    parse_overlay_spec,
    resolve_source_manifest,
    sign_update_index_file,
    validate_package,
)
from .fallback_manifests import (
    FallbackManifestError,
    check_fallback_manifests,
    generate_fallback_manifests,
)
from .host_service import (
    BACKENDS as HOST_SERVICE_BACKENDS,
    HostServiceError,
    HostServiceSpec,
    atomic_install_command,
    detection_command,
    disable_command as host_disable_command,
    dry_run_summary,
    dry_run_text,
    enabled_test_command,
    enable_command as host_enable_command,
    hook_block_test,
    hook_edit_command,
    hook_marker_presence_test,
    integration_binding_test,
    integration_path,
    managed_file_test,
    managed_remove_command,
    parse_detection,
    parse_state,
    prerequisite_command,
    regular_file_test,
    render_launcher,
    render_openrc,
    render_state,
    render_sysv,
    select_backend,
    validate_state_binding,
)


DEFAULT_REPOS = [
    "msys-contracts",
    "msys-core",
    "msys-sdk",
    "msys-shell-native",
    "msys-shell-pyside",
    "msys-x11-session",
    "msys-hal",
    "msys-audio",
    "msys-settings",
    "msys-notes",
    "msys-calculator",
    "msys-device-info",
    "msys-input-touch",
    "msys-openstick-ch347",
    "msys-install",
    "msys-tools",
]

CONFIG_PATH = Path(os.environ.get("MSYS_DEV_CONFIG", "~/.config/msys-dev/config.json")).expanduser()
CONTROL_PATH = Path(os.environ.get("MSYS_DEV_SSH_CONTROL", "~/.ssh/msys-dev-%r@%h:%p")).expanduser()
DEFAULT_KEY_PATH = Path(os.environ.get("MSYS_DEV_SSH_KEY", "~/.ssh/msys-dev-ed25519")).expanduser()
DEFAULT_REMOTE_PYTHON_REL = ".runtime/python/bin/python3"
DEFAULT_RUNTIME_CACHE = Path(os.environ.get("MSYS_DEV_RUNTIME_CACHE", "~/.cache/msys-dev/runtime")).expanduser()
PYTHON_STANDALONE_RELEASES_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
PYTHON_STANDALONE_RELEASE_DOWNLOAD = "https://github.com/astral-sh/python-build-standalone/releases/download"
DEFAULT_PYTHON_STANDALONE_VERSION = "3.10.20"
DEFAULT_PYTHON_STANDALONE_TAG = "20260623"
DEFAULT_SSH_CONTROL_PERSIST = "10m"
CONTROL_PERSIST_PATTERN = re.compile(r"^(?:yes|no|[0-9]+[smhd]?)$")
REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SYNC_FINGERPRINT_PATTERN = re.compile(r"^[a-f0-9]{64}$")
SYNC_FINGERPRINT_PREFIX = "__MSYS_SYNC_V1__"
SYNC_FINGERPRINT_MARKER = ".msys-dev-source.sha256"
SYNC_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
)
X11_DISPLAY_PATTERN = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
REMOTE_SCREENSHOT_PATTERN = re.compile(
    r"^/tmp/msys-screenshot-[a-f0-9]{32}\.png$"
)
CALL_FIELD_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 64 * 1024 * 1024
STABLE_WINDOW_ID_PREFIX = "msys.x11-window.v1:"
POINTER_COORDINATE_MAX = 32767
SWIPE_DURATION_MIN_MS = 40
SWIPE_DURATION_MAX_MS = 5000
DEFAULT_RUN_TIMEOUT = 45.0
DEFAULT_STOP_TIMEOUT = 20.0
DEFAULT_RPC_TIMEOUT = 120.0
DEFAULT_RELEASE_HEALTH_TIMEOUT = 90.0
INSTALL_AGENT_RESULT_SCHEMA = "msys.install-agent-result.v1"
SHIELD_CONTROL_SCHEMA = "msys.shield-control.v1"
SHIELD_STATUS_SCHEMA = "msys.screen-shield.status.v1"
DOCTOR_PROBE_PREFIX = "__MSYS_DOCTOR_V2__"
DEFAULT_X11DISPLAY_REMOTE = "/root/x11display"
DEFAULT_SYSTEM_RELEASE_ROOT = "/opt/msys"
FAST_DELIVERY_RELEASE_INPUTS = frozenset({"msys-core", "msys-tools"})
FAST_DELIVERY_SDK_REPOSITORIES = frozenset(
    {
        "msys-settings",
        "msys-notes",
        "msys-calculator",
        "msys-device-info",
        "msys-input-touch",
        # Explicit compatibility delivery for pre-split workspaces.
        "msys-apps",
    }
)
DEFAULT_VISUAL_SMOKE_COMPONENT = "org.msys.calculator:calculator"
FAST_DELIVERY_SDK_OVERLAY = "msys-sdk/msys_sdk=files/app/msys_sdk"
X11DISPLAY_RUNTIME_BINARIES = (
    "bin/ch347_dirty_usb_sink",
    "bin/ch347_st7796_test",
    "bin/ch347_irq_test",
    "bin/ch347_app_gate",
    "bin/xdamage_shm_capture",
)


@dataclass(slots=True)
class Context:
    root: Path
    target: str
    remote: str
    remote_python: str
    ssh_key: Path | None = DEFAULT_KEY_PATH
    ssh_control_path: Path = CONTROL_PATH
    ssh_control_persist: str = DEFAULT_SSH_CONTROL_PERSIST


TARGET_NATIVE_MARKER_SCHEMA = "msys.target-native-artifact.v1"
TARGET_NATIVE_MARKER_NAME = ".msys-target-native.json"


@dataclass(frozen=True, slots=True)
class TargetNativeArtifactSpec:
    package_id: str
    repository: str
    relative_path: str
    probe: str
    runtime_inventory_path: str | None = None


TARGET_NATIVE_ARTIFACTS = {
    "org.msys.hal.linux": TargetNativeArtifactSpec(
        package_id="org.msys.hal.linux",
        repository="msys-hal",
        relative_path="files/bin/msys-hal-native",
        probe="self-check",
    ),
    "org.msys.shell.native": TargetNativeArtifactSpec(
        package_id="org.msys.shell.native",
        repository="msys-shell-native",
        relative_path="bin/msys-shell-native",
        probe="version",
    ),
    "org.msys.x11.session": TargetNativeArtifactSpec(
        package_id="org.msys.x11.session",
        repository="msys-x11-session",
        relative_path="bin/msys-x11-policy",
        probe="build-probe",
    ),
    "org.msys.audio.bluez": TargetNativeArtifactSpec(
        package_id="org.msys.audio.bluez",
        repository="msys-audio",
        relative_path="files/runtime/aarch64/bin/msys-hci-bootstrap",
        probe="hci-bootstrap",
        runtime_inventory_path="files/runtime/aarch64/runtime.json",
    ),
}
TARGET_NATIVE_OPTIONAL_ARTIFACTS = {
    "org.msys.audio.bluez": (
        TargetNativeArtifactSpec(
            package_id="org.msys.audio.bluez",
            repository="msys-audio",
            relative_path=(
                "files/runtime/aarch64/bin/msys-audio-manager-native"
            ),
            probe="audio-manager-native",
            runtime_inventory_path="files/runtime/aarch64/runtime.json",
        ),
    ),
}
TARGET_NATIVE_REPOSITORIES = {
    spec.repository: spec for spec in TARGET_NATIVE_ARTIFACTS.values()
}


def _target_native_package_specs(package_id: str) -> tuple[TargetNativeArtifactSpec, ...]:
    primary = TARGET_NATIVE_ARTIFACTS.get(package_id)
    if primary is None:
        return ()
    return (primary, *TARGET_NATIVE_OPTIONAL_ARTIFACTS.get(package_id, ()))


def run_local(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(argv, check=check, text=True)


def ssh_base_args(ctx: Context | None = None) -> list[str]:
    control_path = ctx.ssh_control_path if ctx is not None else CONTROL_PATH
    persist = ctx.ssh_control_persist if ctx is not None else DEFAULT_SSH_CONTROL_PERSIST
    key_path = ctx.ssh_key if ctx is not None else DEFAULT_KEY_PATH
    args = [
        "ssh",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPersist={persist}",
        "-o", f"ControlPath={control_path}",
    ]
    if key_path is not None and key_path.exists():
        args.extend(["-i", str(key_path)])
    return args


def rsync_ssh_command(ctx: Context) -> str:
    return shlex.join(ssh_base_args(ctx))


def scp_base_args(ctx: Context | None = None) -> list[str]:
    control_path = ctx.ssh_control_path if ctx is not None else CONTROL_PATH
    persist = ctx.ssh_control_persist if ctx is not None else DEFAULT_SSH_CONTROL_PERSIST
    key_path = ctx.ssh_key if ctx is not None else DEFAULT_KEY_PATH
    args = [
        "scp",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPersist={persist}",
        "-o", f"ControlPath={control_path}",
    ]
    if key_path is not None and key_path.exists():
        args.extend(["-i", str(key_path)])
    return args


def ssh(ctx: Context, command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_local([*ssh_base_args(ctx), ctx.target, command], check=check)


def ssh_capture(
    ctx: Context,
    command: str,
    *,
    display_command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+ ssh " + ctx.target + " " + (display_command or command), flush=True)
    return subprocess.run(
        [*ssh_base_args(ctx), ctx.target, command],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def ssh_capture_bytes(
    ctx: Context,
    command: str,
    *,
    display_command: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Capture a binary-safe SSH reply without merging diagnostics into it."""

    print("+ ssh " + ctx.target + " " + (display_command or command), flush=True)
    return subprocess.run(
        [*ssh_base_args(ctx), ctx.target, command],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def quote_sh(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def repo_paths(root: Path, repos: list[str]) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    missing: list[str] = []
    for name in repos:
        if REPOSITORY_NAME_PATTERN.fullmatch(name) is None or name in {".", ".."}:
            missing.append(f"{name} (invalid name)")
            continue
        path = root / name
        if path.is_dir():
            paths.append(path)
        else:
            missing.append(name)
    return paths, missing


def normalize_repositories(repos: list[str]) -> list[str]:
    """Normalize CLI/config/environment repository lists without reordering them."""
    return list(dict.fromkeys(name.strip() for name in repos if name.strip()))


def repository_fingerprint(root: Path) -> str:
    """Hash exactly the source shape transferred by the repository sync path."""

    digest = hashlib.sha256()
    root = root.resolve()

    def record(kind: bytes, relative: str, payload: bytes = b"") -> None:
        encoded = relative.replace(os.sep, "/").encode("utf-8", "surrogateescape")
        digest.update(kind)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            if name in SYNC_EXCLUDED_DIRECTORIES:
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                record(b"L", relative, os.readlink(path).encode("utf-8", "surrogateescape"))
            else:
                record(b"D", relative)
                kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            if (
                name in SYNC_EXCLUDED_DIRECTORIES
                or name.endswith(".pyc")
                or name == SYNC_FINGERPRINT_MARKER
            ):
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                record(b"L", relative, os.readlink(path).encode("utf-8", "surrogateescape"))
                continue
            stat_result = path.stat()
            executable = b"1" if stat_result.st_mode & 0o111 else b"0"
            record(b"F", relative, executable + stat_result.st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
    return digest.hexdigest()


def remote_sync_probe(
    ctx: Context,
    repositories: list[str],
) -> tuple[bool, dict[str, str]]:
    """Prepare sync storage and read all active markers in one SSH call."""

    sync_root = f"{ctx.remote}/.sync"
    commands = [
        f"mkdir -p {quote_sh(ctx.remote)} {quote_sh(sync_root)}",
        "if command -v rsync >/dev/null 2>&1; then rsync=1; else rsync=0; fi",
        f"printf '{SYNC_FINGERPRINT_PREFIX}\\trsync\\t%s\\n' \"$rsync\"",
    ]
    for name in repositories:
        marker = f"{ctx.remote}/{name}/{SYNC_FINGERPRINT_MARKER}"
        commands.append(
            f"value=$(cat {quote_sh(marker)} 2>/dev/null || true); "
            f"printf '{SYNC_FINGERPRINT_PREFIX}\\t%s\\t%s\\n' "
            f"{quote_sh(name)} \"$value\""
        )
    result = ssh_capture(
        ctx,
        "; ".join(commands),
        display_command="<probe repository fingerprints and rsync once>",
    )
    remote: dict[str, str] = {}
    has_rsync = False
    if result.returncode != 0:
        return has_rsync, remote
    for line in (result.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] != SYNC_FINGERPRINT_PREFIX:
            continue
        name, value = fields[1:]
        if name == "rsync":
            has_rsync = value == "1"
        elif name in repositories and SYNC_FINGERPRINT_PATTERN.fullmatch(value):
            remote[name] = value
    return has_rsync, remote


def selected_sync_repositories(
    requested: list[str] | None,
    config: dict[str, Any],
) -> list[str]:
    """Resolve one shared sync selection for ``sync`` and ``quick``."""

    configured = config.get("repos")
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        configured = DEFAULT_REPOS
    else:
        configured = normalize_repositories(configured)
    environment = os.environ.get("MSYS_DEV_REPOS")
    selected = (
        requested
        or (normalize_repositories(environment.split(",")) if environment else None)
        or configured
    )
    return normalize_repositories(selected)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items()}


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{CONFIG_PATH.name}.", suffix=".tmp", dir=CONFIG_PATH.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def command_config(args: argparse.Namespace) -> int:
    data = load_config()
    if args.config_command == "show":
        print(json.dumps(data, indent=2))
        print(f"config_path={CONFIG_PATH}")
        return 0
    if args.config_command == "set":
        if args.root:
            data["root"] = str(Path(args.root).expanduser().resolve())
        if args.target:
            data["target"] = args.target
        if args.remote:
            data["remote"] = args.remote
        if args.runtime_dir:
            data["runtime_dir"] = args.runtime_dir
        if args.log_file:
            data["log_file"] = args.log_file
        if args.state_dir:
            data["state_dir"] = args.state_dir
        if args.profile:
            data["profile"] = args.profile
        if args.remote_python:
            data["remote_python"] = args.remote_python
        if args.ssh_key:
            data["ssh_key"] = str(Path(args.ssh_key).expanduser())
        if args.ssh_control_path:
            data["ssh_control_path"] = str(Path(args.ssh_control_path).expanduser())
        if args.ssh_control_persist:
            if CONTROL_PERSIST_PATTERN.fullmatch(args.ssh_control_persist) is None:
                print(
                    "config: ssh_control_persist must be yes, no, or a duration such as 10m",
                    file=sys.stderr,
                )
                return 2
            data["ssh_control_persist"] = args.ssh_control_persist
        if args.repos:
            repos = normalize_repositories(args.repos)
            invalid = [
                name
                for name in repos
                if REPOSITORY_NAME_PATTERN.fullmatch(name) is None
                or name in {".", ".."}
            ]
            if invalid:
                print(
                    "config: invalid repository name(s): " + ", ".join(invalid),
                    file=sys.stderr,
                )
                return 2
            data["repos"] = repos
        save_config(data)
        print(f"saved {CONFIG_PATH}")
        return 0
    if args.config_command == "unset":
        for key in args.keys:
            data.pop(key.replace("-", "_"), None)
        save_config(data)
        print(f"saved {CONFIG_PATH}")
        return 0
    raise ValueError(args.config_command)


def command_fallback_manifests(root: Path, *, check: bool) -> int:
    try:
        if check:
            differences = check_fallback_manifests(root)
            if differences:
                for difference in differences:
                    print(
                        f"fallback-manifests: {difference.path}: {difference.reason}",
                        file=sys.stderr,
                    )
                    if difference.diff:
                        print(difference.diff, file=sys.stderr)
                print(
                    f"fallback-manifests: {len(differences)} fallback manifest(s) are stale; "
                    "run without --check to regenerate",
                    file=sys.stderr,
                )
                return 1
            print("fallback-manifests: all canonical fallbacks are up to date")
            return 0

        paths = generate_fallback_manifests(root)
        for path in paths:
            print(f"generated {path}")
        return 0
    except FallbackManifestError as exc:
        print(f"fallback-manifests: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"fallback-manifests: cannot update fallback manifests: {exc}", file=sys.stderr)
        return 2


def _doctor_display_mode(root: Path, profile: str) -> str:
    """Infer the configured display architecture from its local profile."""
    providers: list[str] = []
    if REPOSITORY_NAME_PATTERN.fullmatch(profile) is not None:
        profile_path = (
            root
            / "msys-core"
            / "examples"
            / "config"
            / "profiles"
            / f"{profile}.json"
        )
        try:
            document = json.loads(profile_path.read_text(encoding="utf-8"))
            roles = document.get("roles", {}) if isinstance(document, dict) else {}
            configured = roles.get("display-output", []) if isinstance(roles, dict) else []
            if isinstance(configured, list):
                providers = [str(item).lower() for item in configured]
        except (OSError, json.JSONDecodeError):
            pass
    profile_text = profile.lower()
    provider_text = " ".join(providers)
    if "ch347" in provider_text or "spi" in provider_text or "spi" in profile_text:
        return "spi"
    if "hdmi" in provider_text or "hdmi" in profile_text:
        return "hdmi"
    if providers or any(token in profile_text for token in ("desktop", "mobile", "x11")):
        return "x11"
    return "headless"


def command_doctor(ctx: Context, profile: str = "mobile-spi") -> int:
    failed_required = False
    display_mode = _doctor_display_mode(ctx.root, profile)
    print(f"config: {CONFIG_PATH}")
    print(f"workspace: {ctx.root}")
    print(f"target: {ctx.target}")
    print(f"remote root: {ctx.remote}")
    print(f"isolated Python: {ctx.remote_python}")
    print(f"profile: {profile} (display={display_mode})")

    for executable in ("ssh", "scp", "tar"):
        path = shutil.which(executable)
        marker = "ok" if path else "missing"
        print(f"[{marker}] local:{executable}" + (f" {path}" if path else ""))
        failed_required |= path is None
    rsync_path = shutil.which("rsync")
    print(
        f"[{'ok' if rsync_path else 'optional-missing'}] local:rsync"
        + (f" {rsync_path}" if rsync_path else " (tar/scp fallback will be used)")
    )
    if ctx.ssh_key is None:
        print("[optional-missing] ssh-key (password authentication may be used)")
    elif ctx.ssh_key.is_file():
        print(f"[ok] ssh-key {ctx.ssh_key}")
    else:
        print(f"[optional-missing] ssh-key {ctx.ssh_key} (run `msys-dev setup-key`)")

    paths, missing_repos = repo_paths(ctx.root, DEFAULT_REPOS)
    if missing_repos:
        failed_required = True
        print(f"[missing] workspace repositories: {', '.join(missing_repos)}")
    else:
        print(f"[ok] workspace repositories: {len(paths)}")
    try:
        manifest_report = discover_manifests(ctx.root, ctx.root)
        invalid = [row for row in manifest_report["manifests"] if not row["valid"]]
        if invalid:
            failed_required = True
            print(
                f"[invalid] manifests: {len(invalid)}/{manifest_report['count']} invalid"
            )
            for row in invalid:
                print(f"  {row['path']}: {row['error']}")
        else:
            print(f"[ok] manifests: {manifest_report['count']} strictly validated")
    except PackageFlowError as exc:
        failed_required = True
        print(f"[invalid] manifests: {exc}")

    if failed_required and not shutil.which("ssh"):
        print("doctor: local prerequisites are missing", file=sys.stderr)
        return 2

    isolated_python = quote_sh(ctx.remote_python)
    remote_root = quote_sh(ctx.remote)
    native_policy = quote_sh(f"{ctx.remote}/msys-x11-session/bin/msys-x11-policy")
    native_shell = quote_sh(
        f"{ctx.remote}/msys-shell-native/bin/msys-shell-native"
    )
    native_hal = quote_sh(f"{ctx.remote}/msys-hal/files/bin/msys-hal-native")
    native_core_lite = quote_sh(
        f"{ctx.remote}/msys-core/native/build/msysd-native-lite"
    )
    ch347_provider_script = quote_sh(
        f"{ctx.remote}/msys-x11-session/scripts/msys_ch347_x11_provider.sh"
    )
    x11display_root = DEFAULT_X11DISPLAY_REMOTE
    ch347_start_script = quote_sh(
        f"{x11display_root}/scripts/start_ch347_dirty_usb_x11.sh"
    )
    ch347_stop_script = quote_sh(
        f"{x11display_root}/scripts/stop_ch347_dirty_usb_x11.sh"
    )
    ch347_library = quote_sh(f"{x11display_root}/ch347/libch347spi.so")
    ch347_binaries = " ".join(
        quote_sh(f"{x11display_root}/{relative}")
        for relative in X11DISPLAY_RUNTIME_BINARIES
    )
    prefix = DOCTOR_PROBE_PREFIX
    script = f"""set +e
emit() {{ printf '{prefix}|%s|%s|%s\\n' "$1" "$2" "$3"; }}
check_command() {{
    check_name=$1
    check_executable=$2
    check_value=$(command -v "$check_executable" 2>/dev/null)
    if test -n "$check_value"; then emit "$check_name" ok "$check_value"; else emit "$check_name" missing ""; fi
}}
check_executable_file() {{
    check_name=$1
    check_path=$2
    if test -f "$check_path" && test -x "$check_path" && test ! -L "$check_path"; then
        emit "$check_name" ok "$check_path"
    else
        emit "$check_name" missing "$check_path"
    fi
}}
check_regular_file() {{
    check_name=$1
    check_path=$2
    if test -f "$check_path" && test -r "$check_path" && test ! -L "$check_path"; then
        emit "$check_name" ok "$check_path"
    else
        emit "$check_name" missing "$check_path"
    fi
}}
check_command sh sh
check_command tar tar
check_command cp cp
check_command mv mv
check_command uname uname
check_command bash bash
check_command xdpyinfo xdpyinfo
check_command x-server-xorg Xorg
check_command x-server-xvfb Xvfb
check_command rsync rsync
check_command system-python3 python3
check_command system-python python
check_command native-build-make make
check_command native-build-cc cc
check_command native-build-cxx c++
xorg_value=$(command -v Xorg 2>/dev/null || true)
xvfb_value=$(command -v Xvfb 2>/dev/null || true)
if test -n "$xorg_value"; then
    if test -n "$xvfb_value"; then
        emit x-server ok "selected=Xorg Xorg=$xorg_value Xvfb=$xvfb_value"
    else
        emit x-server ok "selected=Xorg Xorg=$xorg_value Xvfb=missing"
    fi
elif test -n "$xvfb_value"; then
    emit x-server ok "selected=Xvfb Xorg=missing Xvfb=$xvfb_value"
else
    emit x-server missing "Xorg=missing Xvfb=missing"
fi
if test -x {isolated_python} && python_version=$({isolated_python} --version 2>&1); then
    emit isolated-python ok "$python_version"
    if {isolated_python} -c 'import tkinter' >/dev/null 2>&1; then
        tk_version=$({isolated_python} -c 'import tkinter; print(tkinter.TkVersion)' 2>/dev/null)
        emit isolated-python-tkinter ok "Tk $tk_version"
    else
        emit isolated-python-tkinter missing "import tkinter failed"
    fi
else
    emit isolated-python missing {isolated_python}
    emit isolated-python-tkinter missing "isolated Python unavailable"
fi
if test -x {native_policy} && test -f {native_policy} && test ! -L {native_policy}; then
    emit x11-policy ok {native_policy}
else
    emit x11-policy missing {native_policy}
fi
check_executable_file native-shell {native_shell}
check_executable_file native-hal {native_hal}
check_executable_file native-core-lite {native_core_lite}
check_executable_file ch347-provider-script {ch347_provider_script}
check_executable_file ch347-start-script {ch347_start_script}
check_executable_file ch347-stop-script {ch347_stop_script}
check_regular_file ch347-library {ch347_library}
ch347_binary_error=""
for artifact in {ch347_binaries}; do
    if ! test -f "$artifact" || ! test -x "$artifact" || test -L "$artifact"; then
        ch347_binary_error="$artifact"
        break
    fi
done
if test -z "$ch347_binary_error"; then
    emit ch347-runtime-binaries ok "5 verified under {x11display_root}/bin"
else
    emit ch347-runtime-binaries missing "$ch347_binary_error"
fi
if command -v uname >/dev/null 2>&1; then
    machine=$(uname -m 2>/dev/null)
    kernel=$(uname -sr 2>/dev/null)
    emit architecture ok "$machine"
    emit kernel ok "$kernel"
fi
if test -d {remote_root}; then
    if test -w {remote_root}; then emit remote-root ok writable; else emit remote-root readonly not-writable; fi
else
    emit remote-root absent will-be-created-by-sync
fi
"""
    result = ssh_capture(ctx, script, display_command="<single doctor capability probe>")
    required_remote = {"sh", "tar", "cp", "mv", "uname", "isolated-python"}
    build_required_remote = {
        "native-build-make",
        "native-build-cc",
        "native-build-cxx",
    }
    profile_required_remote: set[str] = set()
    deployment_required_remote: set[str] = set()
    deployment_stages = {
        "x11-policy": "workspace-sync",
        "native-shell": "workspace-sync",
        "native-hal": "workspace-sync",
        "native-core-lite": "workspace-sync",
        "ch347-provider-script": "workspace-sync",
        "ch347-start-script": "x11display-sync",
        "ch347-stop-script": "x11display-sync",
        "ch347-library": "x11display-sync",
        "ch347-runtime-binaries": "x11display-sync",
    }
    if display_mode != "headless":
        profile_required_remote.update(
            {"bash", "xdpyinfo", "x-server", "isolated-python-tkinter"}
        )
        deployment_required_remote.add("x11-policy")
        if profile != "kiosk-spi":
            deployment_required_remote.add("native-shell")
    deployment_required_remote.add("native-hal")
    if display_mode == "spi":
        deployment_required_remote.update(
            {
                "ch347-provider-script",
                "ch347-start-script",
                "ch347-stop-script",
                "ch347-library",
                "ch347-runtime-binaries",
            }
        )
    expected_required = (
        required_remote
        | build_required_remote
        | profile_required_remote
        | deployment_required_remote
    )
    seen: set[str] = set()
    statuses: dict[str, str] = {}
    diagnostics: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith(prefix + "|"):
            diagnostics.append(line)
            continue
        _, name, status, detail = line.split("|", 3)
        seen.add(name)
        statuses[name] = status
        ok = status == "ok" or (name == "remote-root" and status == "absent")
        required = name in expected_required
        if required and not ok:
            failed_required = True
        if ok:
            marker = "ok"
        elif name in build_required_remote:
            marker = "build-required"
        elif name in profile_required_remote:
            marker = "profile-required"
        elif name in deployment_required_remote:
            marker = "deploy-required"
        elif name in required_remote:
            marker = "missing"
        else:
            marker = "optional-missing"
        suffix = f" {detail}" if detail else ""
        if name in build_required_remote:
            suffix += " stage=source-build"
        elif name in deployment_stages:
            suffix += f" stage={deployment_stages[name]}"
        elif name in profile_required_remote:
            suffix += f" profile={profile}"
        print(f"[{marker}] target:{name}{suffix}")
    if result.returncode != 0 or not expected_required.issubset(seen):
        failed_required = True
        print(f"[failed] SSH probe exited with status {result.returncode}")
    if diagnostics:
        print("SSH diagnostics:")
        for line in diagnostics:
            print(f"  {line}")
    if failed_required:
        print("doctor: required development capability is missing", file=sys.stderr)
        if statuses.get("isolated-python") != "ok":
            print(
                "doctor: bootstrap the private runtime with `msys-dev runtime bootstrap`; "
                "the target package manager is not required",
                file=sys.stderr,
            )
        missing_build = sorted(
            name for name in build_required_remote if statuses.get(name) != "ok"
        )
        if missing_build:
            print(
                "doctor: source synchronization requires target make and cc; "
                "provision the development image before sync (MSYS will not invoke "
                "a package manager)",
                file=sys.stderr,
            )
        missing_profile = sorted(
            name for name in profile_required_remote if statuses.get(name) != "ok"
        )
        if missing_profile:
            print(
                f"doctor: profile {profile} is missing runtime capabilities: "
                + ", ".join(missing_profile),
                file=sys.stderr,
            )
        if statuses.get("x11-policy") != "ok" and "x11-policy" in deployment_required_remote:
            print(
                "doctor: native X11 policy is missing; `msys-dev sync --repo "
                "msys-x11-session` builds it in the remote staging tree before activation",
                file=sys.stderr,
            )
        missing_workspace = sorted(
            name
            for name in deployment_required_remote
            if deployment_stages.get(name) == "workspace-sync"
            and statuses.get(name) != "ok"
        )
        if missing_workspace and missing_workspace != ["x11-policy"]:
            print(
                "doctor: workspace-sync artifacts are missing: "
                + ", ".join(missing_workspace)
                + "; run `msys-dev sync --repo msys-x11-session`",
                file=sys.stderr,
            )
        missing_x11display = sorted(
            name
            for name in deployment_required_remote
            if deployment_stages.get(name) == "x11display-sync"
            and statuses.get(name) != "ok"
        )
        if missing_x11display:
            print(
                "doctor: x11display-sync artifacts are missing: "
                + ", ".join(missing_x11display)
                + "; run `msys-dev sync-x11display` after provisioning build tools",
                file=sys.stderr,
            )
        return 1
    print(
        f"doctor: ready profile={profile} display={display_mode} "
        "(one multiplexed SSH session)"
    )
    return 0


def command_setup_key(ctx: Context, key_path: Path) -> int:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key = key_path.with_suffix(key_path.suffix + ".pub") if key_path.suffix else Path(str(key_path) + ".pub")
    if not key_path.exists():
        run_local(["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "msys-dev"])
    if not public_key.exists():
        run_local(["ssh-keygen", "-y", "-f", str(key_path)], check=True)
        public_key.write_text(
            subprocess.check_output(["ssh-keygen", "-y", "-f", str(key_path)], text=True),
            encoding="utf-8",
        )
    key_text = public_key.read_text(encoding="utf-8").strip()
    command = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qxF {quote_sh(key_text)} ~/.ssh/authorized_keys 2>/dev/null "
        f"|| echo {quote_sh(key_text)} >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    ssh(ctx, command)
    print(f"installed public key from {public_key}")
    return 0


def command_ssh_reset(ctx: Context) -> int:
    run_local([*ssh_base_args(ctx), "-O", "exit", ctx.target], check=False)
    return 0


def command_ssh_warm(ctx: Context) -> int:
    """Start (or reuse) the configured SSH control master.

    This is deliberately separate from every normal command: callers that want
    a responsive Windows development loop can authenticate once up front, then
    let ControlPersist carry all later sync/control/tail commands.  ``-N``
    prevents a shell from being opened on the target and ``-f`` backgrounds the
    master only after authentication has completed successfully.
    """
    check = run_local([*ssh_base_args(ctx), "-O", "check", ctx.target], check=False)
    if check.returncode == 0:
        print("ssh: existing MSYS control connection is ready")
        return 0
    run_local([*ssh_base_args(ctx), "-M", "-N", "-f", ctx.target])
    print(
        "ssh: MSYS control connection is ready "
        f"(persists for {ctx.ssh_control_persist} after its last use)"
    )
    return 0


def command_runtime_status(ctx: Context, remote_python: str) -> int:
    ssh(ctx, (
        f"test -x {quote_sh(remote_python)} "
        f"&& {quote_sh(remote_python)} --version "
        f"|| (echo missing isolated runtime: {quote_sh(remote_python)}; exit 1)"
    ))
    return 0


def command_runtime_install(ctx: Context, archive: Path, remote_python: str) -> int:
    if not archive.exists():
        print(f"archive not found: {archive}", file=sys.stderr)
        return 2
    python_path = PurePosixPath(remote_python)
    if not python_path.is_absolute() or ".." in python_path.parts:
        print("remote Python must be an absolute POSIX path without '..'", file=sys.stderr)
        return 2
    try:
        python_path.relative_to(PurePosixPath(ctx.remote))
    except ValueError:
        print(
            f"remote Python must stay inside the isolated development root {ctx.remote}",
            file=sys.stderr,
        )
        return 2
    runtime_root = python_path.parent.parent.as_posix()
    if PurePosixPath(runtime_root) in {PurePosixPath("/"), PurePosixPath(ctx.remote)}:
        print("remote Python path does not contain a safe private runtime directory", file=sys.stderr)
        return 2
    tmp_remote = f"{ctx.remote}/.runtime/incoming/{archive.name}"
    ssh(ctx, f"mkdir -p {quote_sh(ctx.remote + '/.runtime/incoming')} {quote_sh(runtime_root)}")
    run_local([
        *scp_base_args(ctx),
        str(archive),
        f"{ctx.target}:{tmp_remote}",
    ])
    ssh(ctx, (
        f"rm -rf {quote_sh(runtime_root)}.new && "
        f"mkdir -p {quote_sh(runtime_root)}.new && "
        f"tar -xf {quote_sh(tmp_remote)} -C {quote_sh(runtime_root)}.new --strip-components=1 && "
        f"test -x {quote_sh(runtime_root)}.new/bin/python3 && "
        f"rm -rf {quote_sh(runtime_root)}.old && "
        f"if test -d {quote_sh(runtime_root)}; then mv {quote_sh(runtime_root)} {quote_sh(runtime_root)}.old; fi && "
        f"mv {quote_sh(runtime_root)}.new {quote_sh(runtime_root)} && "
        f"{quote_sh(remote_python)} --version && "
        f"rm -f {quote_sh(tmp_remote)}"
    ))
    return 0


def http_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "msys-dev/0.1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch JSON from {url}: {last_error}") from last_error


def download_file(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "msys-dev/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as handle:
        total = int(response.headers.get("Content-Length", "0") or 0)
        done = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if total:
                percent = int(done * 100 / total)
                print(f"\rdownload {percent:3d}% {done // (1024 * 1024)} MiB", end="", flush=True)
    if total:
        print()
    tmp.replace(output)


def select_python_standalone_asset(version_prefix: str, arch: str) -> dict[str, Any]:
    releases = http_json(PYTHON_STANDALONE_RELEASES_API)
    if not isinstance(releases, list):
        raise RuntimeError("unexpected GitHub releases response")
    needle = f"{arch}-unknown-linux-gnu-install_only_stripped.tar.gz"
    for release in releases:
        if release.get("prerelease") or release.get("draft"):
            continue
        for asset in release.get("assets", []):
            name = str(asset.get("name", ""))
            if "freethreaded" in name:
                continue
            if not name.endswith(needle):
                continue
            if version_prefix and not name.startswith(f"cpython-{version_prefix}."):
                continue
            url = asset.get("browser_download_url")
            if not url:
                continue
            return {
                "name": name,
                "url": url,
                "release": release.get("tag_name", ""),
                "size": asset.get("size", 0),
            }
    raise RuntimeError(f"no python-build-standalone asset found for version={version_prefix} arch={arch}")


def standalone_asset_name(version_prefix: str, arch: str, tag: str) -> str:
    # The exact micro version is encoded in the asset name. For direct tag mode
    # callers should pass a full prefix such as 3.13.5. API mode can use 3.13.
    return f"cpython-{version_prefix}+{tag}-{arch}-unknown-linux-gnu-install_only_stripped.tar.gz"


def command_runtime_fetch(
    version_prefix: str,
    arch: str,
    cache_dir: Path,
    asset_url: str | None = None,
    tag: str | None = None,
) -> Path:
    if asset_url:
        name = asset_url.rsplit("/", 1)[-1]
        asset = {"name": name, "url": asset_url, "release": "manual-url", "size": 0}
    elif tag:
        name = standalone_asset_name(version_prefix, arch, tag)
        asset = {
            "name": name,
            "url": f"{PYTHON_STANDALONE_RELEASE_DOWNLOAD}/{tag}/{name}",
            "release": tag,
            "size": 0,
        }
    else:
        asset = select_python_standalone_asset(version_prefix, arch)
    output = cache_dir / asset["name"]
    meta = output.with_suffix(output.suffix + ".json")
    if output.exists() and output.stat().st_size == int(asset.get("size") or output.stat().st_size):
        print(f"cached {output}")
    else:
        print(f"fetching {asset['name']} from release {asset['release']}")
        download_file(asset["url"], output)
    meta.write_text(json.dumps(asset, indent=2), encoding="utf-8")
    return output


def command_runtime_bootstrap(
    ctx: Context,
    version_prefix: str,
    arch: str,
    cache_dir: Path,
    asset_url: str | None = None,
    tag: str | None = None,
) -> int:
    archive = command_runtime_fetch(version_prefix, arch, cache_dir, asset_url=asset_url, tag=tag)
    return command_runtime_install(ctx, archive, ctx.remote_python)


def command_runtime_make(source: Path, output: Path) -> int:
    python_path = source / "bin" / "python3"
    if not python_path.exists():
        print(f"source does not look like a Python runtime: missing {python_path}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    run_local(["tar", "-czf", str(output), "-C", str(source.parent), source.name])
    print(output)
    return 0


def _package_source_identity(
    source: Path,
    manifest_path: Path | None = None,
) -> tuple[str, str, Path]:
    manifest = resolve_source_manifest(source, manifest_path)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        package = document["package"]
        package_id = package["id"]
        version = package["version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PackageFlowError(
            f"cannot read native package identity from {manifest}: {exc}"
        ) from exc
    if not isinstance(package_id, str):
        raise PackageFlowError(f"package id must be a string: {package_id!r}")
    if (
        not isinstance(version, str)
        or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            version,
        )
    ):
        raise PackageFlowError(
            f"native package has an invalid semantic version: {version!r}"
        )
    return package_id, version, manifest


def _target_native_source_identity(
    source: Path,
    spec: TargetNativeArtifactSpec,
    manifest_path: Path | None = None,
) -> tuple[str, str, Path]:
    package_id, version, manifest = _package_source_identity(source, manifest_path)
    if package_id != spec.package_id:
        raise PackageFlowError(
            f"native repository {spec.repository} must declare {spec.package_id}, "
            f"got {package_id!r}"
        )
    return package_id, version, manifest


def _remote_native_sha256(ctx: Context, binary: str) -> str:
    result = ssh_capture(
        ctx,
        "set -eu; command -v sha256sum >/dev/null 2>&1; "
        f"test -x {quote_sh(binary)}; test -f {quote_sh(binary)}; "
        f"test ! -L {quote_sh(binary)}; sha256sum {quote_sh(binary)}",
        display_command=f"<hash target-native artifact {PurePosixPath(binary).name}>",
    )
    if result.returncode != 0:
        _print_completed_output(result, error=True)
        raise PackageFlowError(f"cannot hash target-native artifact: {binary}")
    match = re.search(r"(?m)^([0-9a-fA-F]{64})(?:\s|$)", result.stdout)
    if match is None:
        raise PackageFlowError(f"target returned an invalid native artifact hash: {binary}")
    return match.group(1).lower()


def _probe_remote_native_binary(
    ctx: Context,
    binary: str,
    spec: TargetNativeArtifactSpec,
    expected_version: str,
) -> dict[str, Any]:
    before = _remote_native_sha256(ctx, binary)
    command_prefix = (
        f"test -x {quote_sh(binary)} && test -f {quote_sh(binary)} "
        f"&& test ! -L {quote_sh(binary)} && "
    )
    if spec.probe == "self-check":
        result = ssh_capture(
            ctx,
            command_prefix + f"{quote_sh(binary)} --self-check",
            display_command=f"<run {spec.repository} native self-check>",
        )
        if result.returncode != 0:
            _print_completed_output(result, error=True)
            raise PackageFlowError(
                f"target-native self-check failed for {spec.package_id}"
            )
        try:
            report = _decode_json_document(result.stdout)
        except ValueError as exc:
            raise PackageFlowError(
                f"target-native self-check returned invalid JSON for {spec.package_id}"
            ) from exc
        actual_version = report.get("version")
        if report.get("ok") is not True or actual_version != expected_version:
            raise PackageFlowError(
                f"target-native version mismatch for {spec.package_id}: manifest "
                f"{expected_version}, ELF self-check {actual_version!r}"
            )
        probe: dict[str, Any] = {
            "kind": "self-check",
            "version": expected_version,
        }
    elif spec.probe == "version":
        result = ssh_capture(
            ctx,
            command_prefix + f"{quote_sh(binary)} --version",
            display_command=f"<run {spec.repository} native version probe>",
        )
        if result.returncode != 0:
            _print_completed_output(result, error=True)
            raise PackageFlowError(
                f"target-native version probe failed for {spec.package_id}"
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if expected_version not in lines:
            actual = lines[-1] if lines else "<empty>"
            raise PackageFlowError(
                f"target-native version mismatch for {spec.package_id}: manifest "
                f"{expected_version}, ELF --version {actual!r}"
            )
        probe = {"kind": "version", "version": expected_version}
    elif spec.probe == "build-probe":
        result = ssh_capture(
            ctx,
            command_prefix + f"{quote_sh(binary)} --msys-build-probe",
            display_command=f"<run {spec.repository} target loader probe>",
        )
        if result.returncode != 64:
            _print_completed_output(result, error=True)
            raise PackageFlowError(
                f"target-native loader probe for {spec.package_id} returned "
                f"{result.returncode}, expected 64"
            )
        probe = {"kind": "build-probe", "status": 64}
    elif spec.probe == "hci-bootstrap":
        result = ssh_capture(
            ctx,
            command_prefix
            + f"{quote_sh(binary)} --self-test && "
            + f"{quote_sh(binary)} --build-probe",
            display_command=f"<run {spec.repository} native bootstrap checks>",
        )
        expected_lines = [
            "msys-hci-bootstrap self-test: ok",
            "msys-hci-bootstrap 1",
        ]
        actual_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or actual_lines != expected_lines:
            _print_completed_output(result, error=True)
            raise PackageFlowError(
                f"target-native bootstrap probe failed for {spec.package_id}"
            )
        probe = {
            "kind": "hci-bootstrap",
            "self_test": expected_lines[0],
            "build_probe": expected_lines[1],
        }
    elif spec.probe == "audio-manager-native":
        result = ssh_capture(
            ctx,
            command_prefix
            + f"{quote_sh(binary)} --self-check && "
            + f"{quote_sh(binary)} --build-probe",
            display_command=f"<run {spec.repository} native manager checks>",
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        try:
            report = _decode_json_document(lines[0]) if lines else {}
        except ValueError as exc:
            raise PackageFlowError(
                f"target-native audio manager returned invalid JSON for {spec.package_id}"
            ) from exc
        manager_version = report.get("version")
        expected_build_probe = (
            f"msys-audio-manager-native {manager_version} candidate"
        )
        rss_kib = report.get("rss_kib")
        if (
            result.returncode != 0
            or len(lines) != 2
            or report.get("ok") is not True
            or not isinstance(manager_version, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", manager_version) is None
            or report.get("stage") != "candidate"
            or report.get("production_default") is not False
            or isinstance(rss_kib, bool)
            or not isinstance(rss_kib, int)
            or rss_kib < 0
            or lines[1] != expected_build_probe
        ):
            _print_completed_output(result, error=True)
            raise PackageFlowError(
                f"target-native audio manager probe failed for {spec.package_id}"
            )
        probe = {
            "kind": "audio-manager-native",
            "version": manager_version,
            "stage": "candidate",
            "production_default": False,
            "build_probe": expected_build_probe,
        }
    else:
        raise PackageFlowError(f"unsupported target-native probe: {spec.probe}")
    after = _remote_native_sha256(ctx, binary)
    if after != before:
        raise PackageFlowError(
            f"target-native artifact changed during preflight: {spec.package_id}"
        )
    return {"sha256": before, "probe": probe}


def _record_target_native_artifact(
    ctx: Context,
    staging: str,
    spec: TargetNativeArtifactSpec,
    *,
    package_id: str,
    version: str,
) -> dict[str, Any]:
    return _record_target_native_artifacts(
        ctx,
        staging,
        (spec,),
        package_id=package_id,
        version=version,
    )


def _record_target_native_artifacts(
    ctx: Context,
    staging: str,
    specs: tuple[TargetNativeArtifactSpec, ...],
    *,
    package_id: str,
    version: str,
) -> dict[str, Any]:
    if not specs:
        raise PackageFlowError("target-native package has no artifact specification")
    artifacts = []
    seen_paths: set[str] = set()
    for spec in specs:
        if spec.package_id != package_id or spec.relative_path in seen_paths:
            raise PackageFlowError(
                f"invalid target-native artifact set for {package_id}"
            )
        seen_paths.add(spec.relative_path)
        checked = _probe_remote_native_binary(
            ctx,
            f"{staging}/{spec.relative_path}",
            spec,
            expected_version=version,
        )
        artifacts.append(
            {
                "path": spec.relative_path,
                "sha256": checked["sha256"],
                "probe": checked["probe"],
            }
        )
    primary = artifacts[0]
    marker = {
        "schema": TARGET_NATIVE_MARKER_SCHEMA,
        "package": package_id,
        "version": version,
        # Retain the original single-artifact fields so an old bootstrap-only
        # marker and current marker remain wire-compatible.
        **primary,
        "artifacts": artifacts,
    }
    marker_text = json.dumps(
        marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    marker_path = f"{staging}/{TARGET_NATIVE_MARKER_NAME}"
    ssh(
        ctx,
        "set -eu; umask 022; "
        f"printf '%s\n' {quote_sh(marker_text)} > {quote_sh(marker_path)}; "
        f"test -f {quote_sh(marker_path)}; test ! -L {quote_sh(marker_path)}",
    )
    return marker


def command_sync(
    ctx: Context,
    repos: list[str],
    *,
    force: bool = False,
    native_audio_manager: bool = False,
) -> int:
    paths, missing = repo_paths(ctx.root, repos)
    if missing:
        print("missing repositories: " + ", ".join(missing), file=sys.stderr)
        return 2
    if not paths:
        print("no repositories to sync", file=sys.stderr)
        return 2

    try:
        fingerprints = {path.name: repository_fingerprint(path) for path in paths}
    except OSError as exc:
        print(f"sync: cannot fingerprint repository sources: {exc}", file=sys.stderr)
        return 2

    sync_root = f"{ctx.remote}/.sync"
    remote_has_rsync, remote_fingerprints = remote_sync_probe(
        ctx, [path.name for path in paths]
    )
    local_has_rsync = bool(shutil.which("rsync"))

    selected_paths = []
    for path in paths:
        force_native_audio = native_audio_manager and path.name == "msys-audio"
        if (
            not force
            and not force_native_audio
            and remote_fingerprints.get(path.name) == fingerprints[path.name]
        ):
            print(f"unchanged {path.name} -> skip upload/build")
        else:
            selected_paths.append(path)
    if not selected_paths:
        print("sync: all selected repositories are current")
        return 0

    def finalise(name: str, staging: str, fingerprint: str) -> None:
        destination = f"{ctx.remote}/{name}"
        previous = f"{ctx.remote}/.{name}.previous"
        marker = f"{staging}/{SYNC_FINGERPRINT_MARKER}"
        ssh(ctx, (
            "set -eu; "
            f"test -d {quote_sh(staging)}; "
            f"printf '%s\\n' {quote_sh(fingerprint)} > {quote_sh(marker)}; "
            f"rm -rf {quote_sh(previous)}; "
            "moved_old=0; "
            f"if test -e {quote_sh(destination)}; then "
            f"mv {quote_sh(destination)} {quote_sh(previous)}; moved_old=1; fi; "
            f"if mv {quote_sh(staging)} {quote_sh(destination)}; then :; else "
            "status=$?; "
            f"if test \"$moved_old\" = 1 && test ! -e {quote_sh(destination)}; then "
            f"mv {quote_sh(previous)} {quote_sh(destination)}; fi; "
            "exit \"$status\"; fi"
        ))

    for path in selected_paths:
        staging = f"{sync_root}/{path.name}.new"
        ssh(ctx, f"rm -rf {quote_sh(staging)} && mkdir -p {quote_sh(staging)}")
        if local_has_rsync and remote_has_rsync:
            run_local([
                "rsync",
                "-az",
                "-e",
                rsync_ssh_command(ctx),
                "--delete",
                "--exclude", ".git/",
                "--exclude", "__pycache__/",
                "--exclude", ".pytest_cache/",
                "--exclude", ".mypy_cache/",
                "--exclude", ".ruff_cache/",
                "--exclude", "build/",
                "--exclude", "dist/",
                "--exclude", "*.pyc",
                str(path) + "/",
                f"{ctx.target}:{staging}/",
            ])
        else:
            with tempfile.TemporaryDirectory(prefix="msys-sync-") as temporary:
                archive = Path(temporary) / f"{path.name}.tar"
                run_local([
                    "tar",
                    "--exclude=.git",
                    "--exclude=__pycache__",
                    "--exclude=.pytest_cache",
                    "--exclude=.mypy_cache",
                    "--exclude=.ruff_cache",
                    "--exclude=build",
                    "--exclude=dist",
                    "--exclude=*.pyc",
                    "-cf", str(archive),
                    "-C", str(path),
                    ".",
                ])
                remote_archive = f"{sync_root}/{path.name}.tar"
                run_local([
                    *scp_base_args(ctx),
                    str(archive),
                    f"{ctx.target}:{remote_archive}",
                ])
                try:
                    ssh(ctx, (
                        f"tar -xf {quote_sh(remote_archive)} -C {quote_sh(staging)} && "
                        f"rm -f {quote_sh(remote_archive)}"
                    ))
                except Exception:
                    ssh(ctx, f"rm -f {quote_sh(remote_archive)}", check=False)
                    raise
        if path.name == "msys-sdk":
            static_library = f"{staging}/build/libmsys-mipc.a"
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1; "
                "command -v cc >/dev/null 2>&1; "
                "command -v ar >/dev/null 2>&1; "
                # Never reuse an uploaded workstation archive/object.  The
                # staging tree is target-built before it can replace the
                # active SDK consumed by Native Shell/HAL builds.
                "MAKEFLAGS= MFLAGS= make -j1 clean; "
                "MAKEFLAGS= MFLAGS= make -j1 "
                "CFLAGS='-Os -g0 -DNDEBUG -ffunction-sections -fdata-sections "
                "-std=c11 -Wall -Wextra -Wpedantic -Werror' all check; "
                f"test -f {quote_sh(static_library)}; "
                f"test ! -L {quote_sh(static_library)}",
                display_command="<build C SDK in atomic target staging tree>",
            )
            if build.returncode != 0:
                if build.stdout:
                    print(
                        build.stdout,
                        end="" if build.stdout.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                print(
                    "sync: target C SDK build failed; the previous remote "
                    "repository was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        elif path.name == "msys-core":
            native_core = f"{staging}/native/build/msysd-native-lite"
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1; "
                "command -v c++ >/dev/null 2>&1; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native clean; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native "
                "OPTIMIZE=-Os DEBUG_INFO=-g0 all; "
                f"test -x {quote_sh(native_core)}; "
                f"test -f {quote_sh(native_core)}; "
                f"test ! -L {quote_sh(native_core)}; "
                f"{quote_sh(native_core)} --version >/dev/null",
                display_command="<build native Core migration artifact in atomic staging tree>",
            )
            if build.returncode != 0:
                if build.stdout:
                    print(
                        build.stdout,
                        end="" if build.stdout.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                print(
                    "sync: native Core build failed; the previous remote repository "
                    "was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        elif path.name == "msys-shell-native":
            native_shell = f"{staging}/bin/msys-shell-native"
            sdk_root = f"{ctx.remote}/msys-sdk"
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1; "
                "if command -v cc >/dev/null 2>&1; then compiler=cc; "
                "elif command -v gcc >/dev/null 2>&1; then compiler=gcc; "
                "else echo 'native Shell build requires cc or gcc on the target' >&2; exit 127; fi; "
                f"test -f {quote_sh(sdk_root + '/include/msys/mipc.h')}; "
                "MAKEFLAGS= MFLAGS= make -j1 CC=\"$compiler\" clean; "
                "MAKEFLAGS= MFLAGS= make -j1 CC=\"$compiler\" "
                f"SDK_DIR={quote_sh(sdk_root)} "
                "CFLAGS='-Os -g0 -DNDEBUG -ffunction-sections -fdata-sections "
                "-Wl,--gc-sections -std=c11 -Wall -Wextra -Wpedantic -Werror' "
                "all test; "
                f"test -x {quote_sh(native_shell)}; "
                f"test -f {quote_sh(native_shell)}; "
                f"test ! -L {quote_sh(native_shell)}; "
                f"{quote_sh(native_shell)} --version >/dev/null",
                display_command="<build native X11 Shell in atomic staging tree>",
            )
            if build.returncode != 0:
                if build.stdout:
                    print(
                        build.stdout,
                        end="" if build.stdout.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                print(
                    "sync: native Shell build failed; the previous remote repository "
                    "was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        elif path.name == "msys-hal":
            native_hal = f"{staging}/files/bin/msys-hal-native"
            sdk_root = f"{ctx.remote}/msys-sdk"
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1; "
                "command -v cc >/dev/null 2>&1; "
                f"test -f {quote_sh(sdk_root + '/include/msys/mipc.h')}; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native clean; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native "
                f"MSYS_SDK_DIR={quote_sh(sdk_root)} "
                "CFLAGS='-Os -g0 -DNDEBUG -ffunction-sections -fdata-sections "
                "-Wl,--gc-sections -std=c11 -Wall -Wextra -Wpedantic -Werror' "
                "all check; "
                f"test -x {quote_sh(native_hal)}; "
                f"test -f {quote_sh(native_hal)}; "
                f"test ! -L {quote_sh(native_hal)}",
                display_command="<build native HAL in atomic staging tree>",
            )
            if build.returncode != 0:
                if build.stdout:
                    print(
                        build.stdout,
                        end="" if build.stdout.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                print(
                    "sync: native HAL build failed; the previous remote repository "
                    "was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        elif path.name == "msys-audio":
            audio_specs = [TARGET_NATIVE_REPOSITORIES["msys-audio"]]
            if native_audio_manager:
                audio_specs.extend(
                    TARGET_NATIVE_OPTIONAL_ARTIFACTS["org.msys.audio.bluez"]
                )
            bootstrap_spec = audio_specs[0]
            manager_spec = audio_specs[1] if len(audio_specs) == 2 else None
            native_audio = f"{staging}/{bootstrap_spec.relative_path}"
            runtime_root = f"{staging}/files/runtime/aarch64"
            runtime_inventory = f"{staging}/{bootstrap_spec.runtime_inventory_path}"
            sdk_root = f"{ctx.remote}/msys-sdk"
            inventory_update = """\
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
inventory_relative = sys.argv[2]
relatives = sys.argv[3:]
inventory = root.joinpath(*pathlib.PurePosixPath(inventory_relative).parts)
document = json.loads(inventory.read_text(encoding="utf-8"))
files = document.get("files")
if not isinstance(files, list):
    raise SystemExit("audio runtime inventory has no files list")
entries = []
for relative in relatives:
    binary = root.joinpath(*pathlib.PurePosixPath(relative).parts)
    entries.append({
        "path": relative,
        "size": binary.stat().st_size,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    })
selected = {entry["path"] for entry in entries}
document["files"] = sorted(
    [item for item in files if isinstance(item, dict) and item.get("path") not in selected]
    + entries,
    key=lambda item: item["path"],
)
temporary = inventory.with_name(inventory.name + ".tmp")
temporary.write_text(json.dumps(document, indent=2) + "\\n", encoding="utf-8")
temporary.replace(inventory)
verified = json.loads(inventory.read_text(encoding="utf-8"))
matches = [item for item in verified.get("files", []) if item.get("path") in selected]
if sorted(matches, key=lambda item: item["path"]) != sorted(entries, key=lambda item: item["path"]):
    raise SystemExit("audio runtime inventory verification failed")
"""
            manager_build = ""
            if manager_spec is not None:
                manager_audio = f"{staging}/{manager_spec.relative_path}"
                manager_build = (
                    f"test -f {quote_sh(sdk_root + '/include/msys/mipc.h')}; "
                    f"test -f {quote_sh(sdk_root + '/src/mipc.c')}; "
                    "MAKEFLAGS= MFLAGS= make -j1 -C native CC=\"$compiler\" "
                    f"MSYS_SDK_DIR={quote_sh(sdk_root)} manager check-manager; "
                    "MAKEFLAGS= MFLAGS= make -j1 -C native CC=\"$compiler\" "
                    f"MSYS_SDK_DIR={quote_sh(sdk_root)} "
                    f"DESTDIR={quote_sh(runtime_root)} install-manager; "
                    f"test -x {quote_sh(manager_audio)}; "
                    f"test -f {quote_sh(manager_audio)}; "
                    f"test ! -L {quote_sh(manager_audio)}; "
                )
            inventory_paths = " ".join(
                quote_sh(spec.relative_path) for spec in audio_specs
            )
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1; "
                "if command -v cc >/dev/null 2>&1; then compiler=cc; "
                "elif command -v gcc >/dev/null 2>&1; then compiler=gcc; "
                "else echo 'native audio bootstrap build requires cc or gcc on the target' >&2; exit 127; fi; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native CC=\"$compiler\" clean; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native CC=\"$compiler\" all check; "
                "MAKEFLAGS= MFLAGS= make -j1 -C native CC=\"$compiler\" "
                f"DESTDIR={quote_sh(runtime_root)} install; "
                f"test -x {quote_sh(native_audio)}; "
                f"test -f {quote_sh(native_audio)}; "
                f"test ! -L {quote_sh(native_audio)}; "
                f"{quote_sh(native_audio)} --self-test >/dev/null; "
                f"test \"$({quote_sh(native_audio)} --build-probe)\" = "
                "'msys-hci-bootstrap 1'; "
                + manager_build
                +
                f"test -f {quote_sh(runtime_inventory)}; "
                f"{quote_sh(ctx.remote_python)} -c {quote_sh(inventory_update)} "
                f"{quote_sh(staging)} "
                f"{quote_sh(str(bootstrap_spec.runtime_inventory_path))} "
                f"{inventory_paths}",
                display_command=(
                    "<build native audio bootstrap and opt-in manager in atomic staging tree>"
                    if manager_spec is not None
                    else "<build native audio bootstrap in atomic staging tree>"
                ),
            )
            if build.returncode != 0:
                if build.stdout:
                    print(
                        build.stdout,
                        end="" if build.stdout.endswith("\n") else "\n",
                        file=sys.stderr,
                    )
                print(
                    "sync: native audio target build failed; the previous remote "
                    "repository was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        elif path.name == "msys-x11-session":
            native_policy = f"{staging}/bin/msys-x11-policy"
            build = ssh_capture(
                ctx,
                "set -eu; "
                f"cd {quote_sh(staging)}; "
                "command -v make >/dev/null 2>&1 || { "
                "echo 'native policy build requires make on the target' >&2; exit 127; }; "
                # A workstation-built binary may be present in the uploaded
                # tree and newer than its sources.  It is never a valid target
                # build input, so force a clean target-native rebuild.
                "MAKEFLAGS= MFLAGS= make clean; "
                "MAKEFLAGS= MFLAGS= make all; "
                f"test -x {quote_sh(native_policy)}; "
                f"test -f {quote_sh(native_policy)}; "
                f"test ! -L {quote_sh(native_policy)}; "
                # Execute a display-independent invalid-option probe.  Exit
                # 64 proves that the kernel could load this target binary;
                # an architecture/loader mismatch returns 126/127 instead.
                "probe_status=0; "
                f"{quote_sh(native_policy)} --msys-build-probe >/dev/null 2>&1 "
                "|| probe_status=$?; "
                "test \"$probe_status\" -eq 64",
                display_command="<build remote msys-x11-policy in atomic staging tree>",
            )
            if build.returncode != 0:
                if build.stdout:
                    print(build.stdout, end="" if build.stdout.endswith("\n") else "\n", file=sys.stderr)
                print(
                    "sync: msys-x11-policy build failed; the previous remote repository "
                    "was left active",
                    file=sys.stderr,
                )
                return build.returncode or 1
        native_spec = TARGET_NATIVE_REPOSITORIES.get(path.name)
        if native_spec is not None:
            try:
                package_id, version, _manifest = _target_native_source_identity(
                    path, native_spec
                )
                native_specs = (native_spec,)
                if path.name == "msys-audio" and native_audio_manager:
                    native_specs = _target_native_package_specs(package_id)
                if len(native_specs) == 1:
                    _record_target_native_artifact(
                        ctx,
                        staging,
                        native_specs[0],
                        package_id=package_id,
                        version=version,
                    )
                else:
                    _record_target_native_artifacts(
                        ctx,
                        staging,
                        native_specs,
                        package_id=package_id,
                        version=version,
                    )
            except PackageFlowError as exc:
                print(
                    f"sync: target-native artifact preflight failed: {exc}; "
                    "the previous remote repository was left active",
                    file=sys.stderr,
                )
                return 2
        finalise(path.name, staging, fingerprints[path.name])
        print(f"synced {path.name} -> {ctx.target}:{ctx.remote}/{path.name}")
    return 0


def command_sync_x11display(ctx: Context, local_path: Path, remote_path: str) -> int:
    if not local_path.is_dir():
        print(f"x11display path not found: {local_path}", file=sys.stderr)
        return 2
    remote_posix = PurePosixPath(remote_path)
    if not remote_posix.is_absolute() or remote_posix == PurePosixPath("/") or ".." in remote_posix.parts:
        print("x11display destination must be a non-root absolute POSIX path", file=sys.stderr)
        return 2
    remote_parent = remote_posix.parent.as_posix()
    incoming = f"{remote_path}.incoming.tar"
    backup = f"{remote_path}.previous"
    staging = f"{remote_path}.new"
    runtime_artifacts = " ".join(
        quote_sh(f"{staging}/{relative}")
        for relative in X11DISPLAY_RUNTIME_BINARIES
    )
    with tempfile.TemporaryDirectory(prefix="msys-x11display-") as temporary:
        archive = Path(temporary) / "x11display-deploy.tar"
        run_local([
            "tar",
            "--exclude=.git",
            "--exclude=__pycache__",
            "-cf",
            str(archive),
            "-C", str(local_path),
            ".",
        ])
        try:
            ssh(ctx, f"mkdir -p {quote_sh(remote_parent)}")
            run_local([
                *scp_base_args(ctx),
                str(archive),
                f"{ctx.target}:{incoming}",
            ])
            ssh(ctx, (
                "set -eu; "
                f"rm -rf {quote_sh(staging)} && mkdir -p {quote_sh(staging)}; "
                f"tar -xf {quote_sh(incoming)} -C {quote_sh(staging)}; "
                f"test -f {quote_sh(staging + '/Makefile')}; "
                f"test ! -L {quote_sh(staging + '/Makefile')}; "
                f"test -f {quote_sh(staging + '/scripts/start_ch347_dirty_usb_x11.sh')}; "
                "command -v make >/dev/null 2>&1 || { "
                "echo 'x11display target build requires make' >&2; exit 127; }; "
                f"cd {quote_sh(staging)}; "
                "MAKEFLAGS= MFLAGS= make clean; "
                "MAKEFLAGS= MFLAGS= make all; "
                f"for artifact in {runtime_artifacts}; do "
                "test -f \"$artifact\"; test -x \"$artifact\"; "
                "test ! -L \"$artifact\"; done; "
                f"rm -f {quote_sh(incoming)}; "
                f"rm -rf {quote_sh(backup)}; moved_old=0; "
                f"if test -e {quote_sh(remote_path)}; then "
                f"mv {quote_sh(remote_path)} {quote_sh(backup)}; moved_old=1; fi; "
                f"if mv {quote_sh(staging)} {quote_sh(remote_path)}; then :; else "
                "status=$?; "
                f"if test \"$moved_old\" = 1 && test ! -e {quote_sh(remote_path)}; then "
                f"mv {quote_sh(backup)} {quote_sh(remote_path)}; fi; "
                "exit \"$status\"; fi"
            ))
        except Exception:
            ssh(
                ctx,
                f"rm -rf {quote_sh(staging)}; rm -f {quote_sh(incoming)}",
                check=False,
            )
            raise
    print(f"synced {local_path} -> {ctx.target}:{remote_path}")
    return 0


def _remote_lifecycle_command(
    ctx: Context,
    action: str,
    runtime_dir: str,
    *,
    timeout: float | None = None,
    log_file: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        f"{quote_sh(ctx.remote_python)} -m msys_tools.remote_lifecycle "
        f"{quote_sh(action)} --runtime-dir {quote_sh(runtime_dir)}"
    )
    if timeout is not None:
        command += f" --timeout {timeout:g}"
    if log_file is not None:
        command += f" --log-file {quote_sh(log_file)}"
    return ssh_capture(ctx, command)


def _print_completed_output(result: subprocess.CompletedProcess[str], *, error: bool = False) -> None:
    if result.stdout:
        print(
            result.stdout,
            end="" if result.stdout.endswith("\n") else "\n",
            file=sys.stderr if error else sys.stdout,
        )


def command_run(
    ctx: Context,
    profile: str,
    runtime_dir: str,
    log_file: str,
    remote_python: str,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> int:
    py_path = ":".join([
        f"{ctx.remote}/msys-core",
        f"{ctx.remote}/msys-sdk",
        f"{ctx.remote}/msys-shell-pyside",
        f"{ctx.remote}/msys-x11-session",
        f"{ctx.remote}/msys-hal",
        f"{ctx.remote}/msys-input-touch/files/app",
        f"{ctx.remote}/msys-install",
    ])
    source_config = f"{ctx.remote}/msys-core/examples/config"
    native_shell_manifest = f"{ctx.remote}/msys-shell-native/manifest.json"
    shell_manifest = f"{ctx.remote}/msys-shell-pyside/manifest.json"
    hal_root_manifest = f"{ctx.remote}/msys-hal/manifest.json"
    x11_session_manifest = f"{ctx.remote}/msys-x11-session/manifest.json"
    ch347_manifest = f"{ctx.remote}/msys-openstick-ch347/manifest.json"
    input_manifest = f"{ctx.remote}/msys-input-touch/manifest.json"
    install_manifest = f"{ctx.remote}/msys-install/manifest.json"
    native_policy = f"{ctx.remote}/msys-x11-session/bin/msys-x11-policy"
    native_shell = f"{ctx.remote}/msys-shell-native/bin/msys-shell-native"
    native_hal = f"{ctx.remote}/msys-hal/files/bin/msys-hal-native"
    preflight = ssh_capture(
        ctx,
        "set -eu; "
        f"test -x {quote_sh(remote_python)} || {{ "
        f"echo {quote_sh('missing isolated Python: ' + remote_python)} >&2; exit 78; }}; "
        f"test -x {quote_sh(native_policy)} && test -f {quote_sh(native_policy)} "
        f"&& test ! -L {quote_sh(native_policy)} || {{ "
        f"echo {quote_sh('missing native X11 policy: ' + native_policy)} >&2; "
        "echo 'run msys-dev sync --repo msys-x11-session to build it atomically' >&2; "
        "exit 78; }; "
        f"test -x {quote_sh(native_shell)} && test -f {quote_sh(native_shell)} "
        f"&& test ! -L {quote_sh(native_shell)} || {{ "
        f"echo {quote_sh('missing native Shell: ' + native_shell)} >&2; "
        "echo 'run msys-dev sync --repo msys-sdk --repo msys-shell-native' >&2; "
        "exit 78; }; "
        f"test -x {quote_sh(native_hal)} && test -f {quote_sh(native_hal)} "
        f"&& test ! -L {quote_sh(native_hal)} || {{ "
        f"echo {quote_sh('missing native HAL: ' + native_hal)} >&2; "
        "echo 'run msys-dev sync --repo msys-sdk --repo msys-hal' >&2; "
        "exit 78; }",
        display_command=(
            "<run preflight: isolated Python and target-native X11/Shell/HAL>"
        ),
    )
    if preflight.returncode != 0:
        _print_completed_output(preflight, error=True)
        return preflight.returncode or 1
    prepared = _remote_lifecycle_command(ctx, "prepare", runtime_dir)
    if prepared.returncode != 0:
        _print_completed_output(prepared, error=True)
        return prepared.returncode or 1
    runner = (
        "set -- -m msys_core.msysd --foreground "
        f"--config {quote_sh(source_config)} "
        f"--runtime-dir {quote_sh(runtime_dir)} "
        f"--profile {quote_sh(profile)}; "
        f"if test -f {quote_sh(native_shell_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(native_shell_manifest)}; fi; "
        f"if test -f {quote_sh(shell_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(shell_manifest)}; fi; "
        f"if test -f {quote_sh(hal_root_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(hal_root_manifest)}; fi; "
        f"if test -f {quote_sh(x11_session_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(x11_session_manifest)}; fi; "
        f"if test -f {quote_sh(ch347_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(ch347_manifest)}; fi; "
        f"if test -f {quote_sh(input_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(input_manifest)}; fi; "
        f"if test -f {quote_sh(install_manifest)}; then "
        f"set -- \"$@\" --manifest {quote_sh(install_manifest)}; fi; "
        f"export MSYS_PLATFORM_PYTHONPATH={quote_sh(ctx.remote + '/msys-sdk')}; "
        "export PYTHONDONTWRITEBYTECODE=1; "
        "export MALLOC_ARENA_MAX=\"${MALLOC_ARENA_MAX:-2}\"; "
        "export MALLOC_TRIM_THRESHOLD_=\"${MALLOC_TRIM_THRESHOLD_:-262144}\"; "
        f"export PYTHONPATH={quote_sh(py_path)}; "
        f"exec {quote_sh(remote_python)} \"$@\""
    )
    command = (
        f"mkdir -p {quote_sh(runtime_dir)} {quote_sh(PurePosixPath(log_file).parent.as_posix())} && "
        f"if test -S {quote_sh(runtime_dir + '/control.sock')}; then "
        f"echo {quote_sh('refusing to start a duplicate msysd; run msys-dev stop first')} >&2; "
        "exit 73; fi && "
        f"cd {quote_sh(ctx.remote)} && "
        f"test -x {quote_sh(native_policy)} && "
        f"if command -v setsid >/dev/null 2>&1; then "
        f"nohup setsid sh -c {quote_sh(runner)} > {quote_sh(log_file)} 2>&1 < /dev/null & "
        f"else "
        f"nohup sh -c {quote_sh(runner)} > {quote_sh(log_file)} 2>&1 < /dev/null & "
        f"fi; "
        f"echo $!"
    )
    started = ssh_capture(ctx, command, display_command="<start isolated msysd session>")
    if started.returncode != 0:
        _print_completed_output(started, error=True)
        return started.returncode or 1
    _print_completed_output(started)
    ready = _remote_lifecycle_command(
        ctx,
        "wait-ready",
        runtime_dir,
        timeout=timeout,
        log_file=log_file,
    )
    _print_completed_output(ready, error=ready.returncode != 0)
    return ready.returncode


def command_stop(
    ctx: Context,
    runtime_dir: str,
    timeout: float = DEFAULT_STOP_TIMEOUT,
) -> int:
    result = _remote_lifecycle_command(ctx, "stop", runtime_dir, timeout=timeout)
    _print_completed_output(result)
    return result.returncode


def command_tail(ctx: Context, log_file: str) -> int:
    ssh(ctx, f"tail -n 200 -f {quote_sh(log_file)}")
    return 0


def command_debug(
    ctx: Context,
    runtime_dir: str,
    log_file: str,
    *,
    lines: int = 80,
    follow: bool = False,
) -> int:
    """Print a runtime snapshot and recent log lines through one SSH session.

    ``status`` is intentionally run on the target, where it can query the
    local control socket without paying Windows/WSL/SSH startup costs for each
    individual diagnostic.  The shell continues to the log section even when
    the runtime is unhealthy, then returns the status result to automation.
    With ``follow`` the same SSH transport remains open for ``tail -f``.
    """
    if not 1 <= lines <= 1000:
        raise ValueError("debug log lines must be between 1 and 1000")
    lifecycle = " ".join(
        quote_sh(value)
        for value in (
            ctx.remote_python,
            "-m",
            "msys_tools.remote_lifecycle",
            "status",
            "--runtime-dir",
            runtime_dir,
        )
    )
    command = (
        "status=0; "
        "printf '%s\\n' '=== MSYS runtime snapshot ==='; "
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        f"{lifecycle} || status=$?; "
        f"printf '%s\\n' '=== msysd log (last {lines} lines) ==='; "
        f"tail -n {lines} {quote_sh(log_file)} 2>&1 || true; "
    )
    if follow:
        command += (
            "printf '%s\\n' '=== following msysd log (Ctrl+C to stop) ==='; "
            f"tail -n 0 -f {quote_sh(log_file)} 2>&1; "
        )
    command += "exit $status"
    completed = ssh(ctx, command, check=False)
    return completed.returncode


def command_status(ctx: Context, runtime_dir: str) -> int:
    result = _remote_lifecycle_command(ctx, "status", runtime_dir)
    _print_completed_output(result)
    return result.returncode


def remote_control_command(
    ctx: Context,
    runtime_dir: str,
    method: str,
    payload: dict[str, Any],
    response_only: bool = False,
    capture: bool = False,
    *,
    target: str = "msys.core",
    timeout: float | None = None,
    idempotent: bool = False,
    wait_display_migration: bool = False,
) -> subprocess.CompletedProcess[str] | int:
    payload_json = json.dumps(payload, separators=(",", ":"))
    py_path = f"{ctx.remote}/msys-tools"
    command = (
        f"PYTHONPATH={quote_sh(py_path)} "
        f"{quote_sh(ctx.remote_python)} -m msys_tools.remote_ctl "
        f"--runtime-dir {quote_sh(runtime_dir)} "
        f"--target {quote_sh(target)} "
        f"--method {quote_sh(method)} "
        f"--payload {quote_sh(payload_json)}"
    )
    if timeout is not None:
        command += f" --timeout {timeout:g}"
    if idempotent:
        command += " --idempotent"
    if wait_display_migration:
        command += " --wait-display-migration"
    if response_only:
        command += " --response-only"
    if capture:
        return ssh_capture(ctx, command)
    completed = ssh(ctx, command, check=False)
    return completed.returncode


def _decode_json_document(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for position, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((end, position, value))
    if not candidates:
        raise ValueError("remote command did not return a JSON object")
    # The root document spans more bytes than any nested object.  Position is
    # the tie-breaker so a later complete document wins over SSH diagnostics.
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def command_components(ctx: Context, runtime_dir: str, as_json: bool) -> int:
    if as_json:
        result = remote_control_command(
            ctx, runtime_dir, "list_components", {}, response_only=True
        )
        return result if isinstance(result, int) else result.returncode
    result = remote_control_command(ctx, runtime_dir, "list_components", {}, response_only=True, capture=True)
    if isinstance(result, int):
        return result
    if result.returncode != 0:
        print(result.stdout, end="")
        return result.returncode
    data = _decode_json_document(result.stdout)
    components = data.get("payload", {}).get("components", [])
    print(f"{'state':<12} {'runtime':<10} {'lifecycle':<12} component")
    for item in components:
        print(
            f"{item.get('state', ''):<12} "
            f"{item.get('runtime', ''):<10} "
            f"{item.get('lifecycle', ''):<12} "
            f"{item.get('id', '')}"
        )
    return 0


def command_roles(ctx: Context, runtime_dir: str, as_json: bool) -> int:
    if as_json:
        result = remote_control_command(
            ctx, runtime_dir, "list_roles", {}, response_only=True
        )
        return result if isinstance(result, int) else result.returncode
    result = remote_control_command(ctx, runtime_dir, "list_roles", {}, response_only=True, capture=True)
    if isinstance(result, int):
        return result
    if result.returncode != 0:
        print(result.stdout, end="")
        return result.returncode
    data = _decode_json_document(result.stdout)
    roles = data.get("payload", {}).get("roles", [])
    print(f"{'role':<26} {'active':<46} preferred")
    for item in roles:
        print(
            f"{item.get('role', ''):<26} "
            f"{str(item.get('active') or '-'):<46} "
            f"{item.get('preferred') or '-'}"
        )
        for candidate in item.get("candidates", []):
            marker = "*" if candidate.get("component") == item.get("active") else " "
            print(
                f"  {marker} {candidate.get('component', '')} "
                f"priority={candidate.get('priority', 0)} state={candidate.get('state', '')}"
            )
    return 0


def command_select_role(
    ctx: Context,
    runtime_dir: str,
    role: str,
    provider: str,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> int:
    return remote_control_command(
        ctx,
        runtime_dir,
        "select_role",
        {"role": role, "provider": provider},
        timeout=timeout,
        wait_display_migration=role == "display-output",
    )


def command_reset_role(
    ctx: Context,
    runtime_dir: str,
    role: str,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> int:
    return remote_control_command(
        ctx,
        runtime_dir,
        "reset_role",
        {"role": role},
        timeout=timeout,
        wait_display_migration=role == "display-output",
    )


def command_discover(
    ctx: Context,
    runtime_dir: str,
    kind: str | None,
    name: str | None,
) -> int:
    payload = {
        key: value
        for key, value in {"kind": kind, "name": name}.items()
        if value is not None
    }
    return remote_control_command(
        ctx,
        runtime_dir,
        "discover",
        payload,
        response_only=True,
        idempotent=True,
    )


def command_call(
    ctx: Context,
    runtime_dir: str,
    target: str,
    method: str,
    payload: dict[str, Any],
    timeout: float,
    idempotent: bool,
) -> int:
    return remote_control_command(
        ctx,
        runtime_dir,
        method,
        payload,
        target=target,
        timeout=timeout,
        idempotent=idempotent,
    )


def parse_call_payload(
    payload_json: str | None,
    fields: list[str],
) -> dict[str, Any]:
    """Decode one call payload without requiring shell-sensitive object JSON."""

    if payload_json is not None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--payload is not valid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("--payload must decode to a JSON object")
        return payload

    payload: dict[str, Any] = {}
    assigned_paths: set[tuple[str, ...]] = set()
    for field in fields:
        name, separator, encoded_value = field.partition("=")
        if not separator:
            raise ValueError(f"--field must use KEY=VALUE syntax: {field!r}")
        path = tuple(name.split("."))
        if not 1 <= len(path) <= 4:
            raise ValueError(f"--field path must contain 1 to 4 segments: {name!r}")
        if any(CALL_FIELD_SEGMENT_PATTERN.fullmatch(segment) is None for segment in path):
            raise ValueError(f"--field has an invalid key path: {name!r}")
        if path in assigned_paths:
            raise ValueError(f"--field leaf is repeated: {name}")
        conflict = next(
            (
                existing
                for existing in assigned_paths
                if path[: len(existing)] == existing
                or existing[: len(path)] == path
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(
                "--field scalar/object path conflict: "
                f"{'.'.join(conflict)} and {name}"
            )
        try:
            value = json.loads(encoded_value)
        except json.JSONDecodeError:
            # Bare text is the common Windows path. It deliberately needs no
            # nested quotes: --field id=network:wlan0 remains one stable argv.
            value = encoded_value
        destination = payload
        for segment in path[:-1]:
            child = destination.get(segment)
            if child is None:
                child = {}
                destination[segment] = child
            if not isinstance(child, dict):
                raise ValueError(
                    f"--field scalar/object path conflict at {segment!r}"
                )
            destination = child
        destination[path[-1]] = value
        assigned_paths.add(path)
    return payload


def command_start_component(ctx: Context, runtime_dir: str, component: str) -> int:
    return remote_control_command(ctx, runtime_dir, "start", {"component": component})


def command_app_run(
    ctx: Context,
    workspace: Path,
    package_dir: Path,
    output: Path,
    *,
    runtime_dir: str,
    state_dir: str,
    component: str | None = None,
    no_start: bool = False,
    force: bool = False,
    source_date_epoch: int | None = None,
    manifest_path: Path | None = None,
    artifact_format: str = "tar.gz",
    overlays: list[Any] | None = None,
) -> int:
    """Compose existing package and lifecycle operations for one app."""

    source = package_dir.expanduser().resolve()
    selected_overlays = overlays or []
    validation_target = (
        resolve_source_manifest(source, manifest_path)
        if selected_overlays
        else source
    )
    checked = validate_package(
        workspace,
        validation_target,
        manifest_path=None if selected_overlays else manifest_path,
    )
    selected_component = None
    if not no_start or component is not None:
        selected_component = select_app_component(
            workspace,
            source,
            component,
            manifest_path=manifest_path,
        )
    print(f"validated {checked['package']} {checked['version']}")
    built = build_package(
        workspace,
        source,
        output,
        force=force,
        source_date_epoch=source_date_epoch,
        manifest_path=manifest_path,
        artifact_format=artifact_format,
        overlays=selected_overlays,
    )
    print_json(built)
    installed = command_install_archive(
        ctx,
        runtime_dir,
        Path(built["artifact"]),
        state_dir=state_dir,
    )
    if installed != 0 or no_start:
        return installed
    assert selected_component is not None
    return command_start_component(ctx, runtime_dir, selected_component)


def command_activate(
    ctx: Context,
    runtime_dir: str,
    *,
    action: str | None,
    uri: str | None,
    mime: str | None,
    name: str | None,
    component: str | None,
) -> int:
    if action is None:
        if uri:
            action = "open-uri"
        elif mime:
            action = "open-mime"
        elif name:
            action = "settings-panel"
    payload = {
        key: value
        for key, value in {
            "action": action,
            "uri": uri,
            "mime": mime,
            "name": name,
            "component": component,
        }.items()
        if value is not None
    }
    return remote_control_command(ctx, runtime_dir, "activate", payload)


def command_stop_component(ctx: Context, runtime_dir: str, component: str) -> int:
    return remote_control_command(ctx, runtime_dir, "stop", {"component": component})


def command_broadcast(ctx: Context, runtime_dir: str, topic: str, payload: dict[str, Any]) -> int:
    return remote_control_command(ctx, runtime_dir, "broadcast", {"topic": topic, "payload": payload})


def _valid_shield_terminal(result: dict[str, Any], action: str) -> bool:
    if (
        result.get("schema") != SHIELD_CONTROL_SCHEMA
        or result.get("action") != action
        or result.get("role") != "screen-shield"
        or result.get("ok") is not True
        or not isinstance(result.get("provider_running"), bool)
    ):
        return False
    provider = result.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider):
        return False
    if action == "hide" and result["provider_running"] is False:
        return (
            result.get("already_hidden") is True
            and result.get("changed") is False
            and result.get("reason") == "provider-not-running"
        )
    status = result.get("status")
    return (
        result["provider_running"] is True
        and isinstance(provider, str)
        and bool(provider)
        and isinstance(status, dict)
        and status.get("schema") == SHIELD_STATUS_SCHEMA
        and status.get("visible") is (action == "show")
    )


def command_shield(
    ctx: Context,
    runtime_dir: str,
    action: str,
    *,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> int:
    command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        f"{quote_sh(ctx.remote_python)} -m msys_tools.remote_shield "
        f"{quote_sh(action)} --runtime-dir {quote_sh(runtime_dir)} "
        f"--timeout {timeout:g}"
    )
    completed = ssh_capture(
        ctx,
        command,
        display_command=f"<typed screen-shield {action}>",
    )
    try:
        result = _decode_json_document(completed.stdout)
    except ValueError as exc:
        if completed.stdout:
            _print_completed_output(completed, error=True)
        print(f"shield {action}: {exc}", file=sys.stderr)
        return completed.returncode or 1
    if completed.returncode != 0:
        print(json.dumps(result, indent=2, ensure_ascii=False), file=sys.stderr)
        print(
            f"shield {action}: remote helper exited with status "
            f"{completed.returncode}",
            file=sys.stderr,
        )
        return completed.returncode
    if not _valid_shield_terminal(result, action):
        print(json.dumps(result, indent=2, ensure_ascii=False), file=sys.stderr)
        print(
            f"shield {action}: remote helper returned an invalid terminal result",
            file=sys.stderr,
        )
        return 1
    print_json(result)
    return 0


def _legacy_event_warning(operation: str) -> None:
    print(
        f"warning: {operation} is using legacy best-effort events; a zero exit status "
        "does not confirm completion",
        file=sys.stderr,
    )


def _typed_agent_result(
    ctx: Context,
    runtime_dir: str,
    *,
    target: str,
    method: str,
    payload: dict[str, Any],
    operation: str,
    timeout: float = DEFAULT_RPC_TIMEOUT,
    emit_success: bool = True,
) -> tuple[int, dict[str, Any] | None]:
    completed = remote_control_command(
        ctx,
        runtime_dir,
        method,
        payload,
        response_only=True,
        capture=True,
        target=target,
        timeout=timeout,
    )
    if isinstance(completed, int):
        return completed, None
    try:
        response = _decode_json_document(completed.stdout)
    except ValueError as exc:
        if completed.stdout:
            _print_completed_output(completed, error=True)
        print(f"{operation}: {exc}", file=sys.stderr)
        return completed.returncode or 1, None
    if completed.returncode != 0:
        print(json.dumps(response, indent=2, ensure_ascii=False), file=sys.stderr)
        print(
            f"{operation}: remote control command exited with status "
            f"{completed.returncode}",
            file=sys.stderr,
        )
        return completed.returncode, None
    if response.get("type") != "return":
        print(json.dumps(response, indent=2, ensure_ascii=False), file=sys.stderr)
        return completed.returncode or 1, None
    result = response.get("payload")
    if not isinstance(result, dict):
        print(f"{operation}: agent returned a non-object result", file=sys.stderr)
        return 1, None
    if result.get("schema") != INSTALL_AGENT_RESULT_SCHEMA:
        print(
            f"{operation}: unexpected result schema {result.get('schema')!r}",
            file=sys.stderr,
        )
        return 1, None
    if result.get("operation") != operation:
        print(
            f"{operation}: agent reported operation {result.get('operation')!r}",
            file=sys.stderr,
        )
        return 1, None
    if emit_success:
        print_json(result)
    if result.get("ok") is not True:
        print(f"{operation}: agent completed with ok=false", file=sys.stderr)
        return 1, result
    return 0, result


def _typed_agent_request(
    ctx: Context,
    runtime_dir: str,
    *,
    target: str,
    method: str,
    payload: dict[str, Any],
    operation: str,
    timeout: float = DEFAULT_RPC_TIMEOUT,
) -> int:
    status, _result = _typed_agent_result(
        ctx,
        runtime_dir,
        target=target,
        method=method,
        payload=payload,
        operation=operation,
        timeout=timeout,
    )
    return status


def command_install_dir(
    ctx: Context,
    runtime_dir: str,
    package_dir: str,
    *,
    legacy_events: bool = False,
) -> int:
    if not legacy_events:
        print(
            "install-dir: typed remote installation accepts verified archives only; "
            "use install-archive/package deliver, or explicitly pass --legacy-events",
            file=sys.stderr,
        )
        return 2
    _legacy_event_warning("install_dir")
    return command_broadcast(ctx, runtime_dir, "msys.install.install_dir", {"path": package_dir})


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _delivery_artifact_suffix(path: Path) -> str:
    return ".maf" if path.name.lower().endswith(".maf") else ".tar.gz"


def command_install_archive(
    ctx: Context,
    runtime_dir: str,
    archive: Path,
    *,
    state_dir: str = "/opt/msys-state",
    legacy_events: bool = False,
) -> int:
    archive = archive.expanduser().resolve()
    try:
        details = validate_package(ctx.root, archive, require_content_hashes=True)
    except PackageFlowError as exc:
        print(f"install-archive: {exc}", file=sys.stderr)
        return 2
    print(
        f"verified {details['package']} {details['version']} "
        f"content={details['content_sha256']}"
    )
    archive_sha256 = _file_sha256(archive)
    if legacy_events:
        _legacy_event_warning("install_archive")
        remote_archive = f"{ctx.remote}/incoming/{archive.name}"
        ssh(ctx, f"mkdir -p {quote_sh(ctx.remote + '/incoming')}")
        run_local([*scp_base_args(ctx), str(archive), f"{ctx.target}:{remote_archive}"])
        return command_broadcast(
            ctx,
            runtime_dir,
            "msys.install.install_archive",
            {"path": remote_archive},
        )

    state_path = PurePosixPath(state_dir)
    if not state_path.is_absolute() or state_path == PurePosixPath("/") or ".." in state_path.parts:
        print("install-archive: state directory must be a non-root absolute path", file=sys.stderr)
        return 2
    incoming_dir = f"{ctx.remote}/.incoming/packages"
    artifact_suffix = _delivery_artifact_suffix(archive)
    incoming = f"{incoming_dir}/{archive_sha256}{artifact_suffix}"
    staged_dir = f"{state_path.as_posix()}/updates/staged-rpc"
    staged = f"{staged_dir}/{archive_sha256}{artifact_suffix}"
    ssh(ctx, f"mkdir -p {quote_sh(incoming_dir)} && rm -f {quote_sh(incoming)}")
    run_local([*scp_base_args(ctx), str(archive), f"{ctx.target}:{incoming}"])
    ssh(
        ctx,
        "set -eu; "
        f"mkdir -p -m 0700 {quote_sh(staged_dir)}; "
        f"test -d {quote_sh(staged_dir)}; test ! -L {quote_sh(staged_dir)}; "
        f"chmod 0700 {quote_sh(staged_dir)}; "
        f"rm -f {quote_sh(staged)}; mv {quote_sh(incoming)} {quote_sh(staged)}; "
        f"chmod 0600 {quote_sh(staged)}; test -f {quote_sh(staged)}; "
        f"test ! -L {quote_sh(staged)}",
    )
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="install_archive",
        operation="install_archive",
        payload={
            "path": staged,
            "sha256": archive_sha256,
            "package": details["package"],
            "version": details["version"],
            "remote": True,
            "require_sha256": True,
            "require_content_hashes": True,
        },
    )


def _load_target_native_marker(
    ctx: Context,
    spec: TargetNativeArtifactSpec,
    *,
    expected_version: str,
) -> dict[str, Any]:
    repository = f"{ctx.remote}/{spec.repository}"
    marker_path = f"{repository}/{TARGET_NATIVE_MARKER_NAME}"
    result = ssh_capture(
        ctx,
        f"test -f {quote_sh(marker_path)} && test ! -L {quote_sh(marker_path)} "
        f"&& cat {quote_sh(marker_path)}",
        display_command=f"<read {spec.repository} target-native marker>",
    )
    if result.returncode != 0:
        raise PackageFlowError(
            f"target-native marker is missing for {spec.package_id}; run "
            f"msys-dev sync --repo {spec.repository} --full-sync first"
        )
    try:
        marker = _decode_json_document(result.stdout)
    except ValueError as exc:
        raise PackageFlowError(
            f"target-native marker is invalid for {spec.package_id}"
        ) from exc
    expected_fields = {
        "schema": TARGET_NATIVE_MARKER_SCHEMA,
        "package": spec.package_id,
        "version": expected_version,
        "path": spec.relative_path,
    }
    for field, expected in expected_fields.items():
        if marker.get(field) != expected:
            raise PackageFlowError(
                f"target-native marker mismatch for {spec.package_id}: "
                f"{field} expected {expected!r}, got {marker.get(field)!r}"
            )
    declared_hash = marker.get("sha256")
    if not isinstance(declared_hash, str) or re.fullmatch(
        r"[0-9a-f]{64}", declared_hash
    ) is None:
        raise PackageFlowError(
            f"target-native marker has an invalid SHA-256 for {spec.package_id}"
        )
    checked = _probe_remote_native_binary(
        ctx,
        f"{repository}/{spec.relative_path}",
        spec,
        expected_version=expected_version,
    )
    if checked["sha256"] != declared_hash or checked["probe"] != marker.get("probe"):
        raise PackageFlowError(
            f"target-native marker no longer matches the target ELF for {spec.package_id}"
        )
    return marker


def _load_target_native_artifacts(
    ctx: Context,
    specs: tuple[TargetNativeArtifactSpec, ...],
    *,
    expected_version: str,
) -> tuple[tuple[TargetNativeArtifactSpec, dict[str, Any]], ...]:
    if not specs:
        return ()
    primary = specs[0]
    if any(
        spec.package_id != primary.package_id
        or spec.repository != primary.repository
        for spec in specs
    ):
        raise PackageFlowError("target-native artifact specifications disagree")
    repository = f"{ctx.remote}/{primary.repository}"
    marker_path = f"{repository}/{TARGET_NATIVE_MARKER_NAME}"
    result = ssh_capture(
        ctx,
        f"test -f {quote_sh(marker_path)} && test ! -L {quote_sh(marker_path)} "
        f"&& cat {quote_sh(marker_path)}",
        display_command=f"<read {primary.repository} target-native marker>",
    )
    if result.returncode != 0:
        raise PackageFlowError(
            f"target-native marker is missing for {primary.package_id}; run "
            f"msys-dev sync --repo {primary.repository} --full-sync first"
        )
    try:
        marker = _decode_json_document(result.stdout)
    except ValueError as exc:
        raise PackageFlowError(
            f"target-native marker is invalid for {primary.package_id}"
        ) from exc
    expected_fields = {
        "schema": TARGET_NATIVE_MARKER_SCHEMA,
        "package": primary.package_id,
        "version": expected_version,
    }
    for field, expected in expected_fields.items():
        if marker.get(field) != expected:
            raise PackageFlowError(
                f"target-native marker mismatch for {primary.package_id}: "
                f"{field} expected {expected!r}, got {marker.get(field)!r}"
            )
    raw_artifacts = marker.get("artifacts")
    if raw_artifacts is None:
        raw_artifacts = [
            {
                "path": marker.get("path"),
                "sha256": marker.get("sha256"),
                "probe": marker.get("probe"),
            }
        ]
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise PackageFlowError(
            f"target-native marker has no artifacts for {primary.package_id}"
        )
    if marker.get("artifacts") is not None:
        first = raw_artifacts[0]
        if not isinstance(first, dict) or any(
            marker.get(field) != first.get(field)
            for field in ("path", "sha256", "probe")
        ):
            raise PackageFlowError(
                f"target-native marker primary fields disagree for {primary.package_id}"
            )
    by_path: dict[str, dict[str, Any]] = {}
    for artifact in raw_artifacts:
        path = artifact.get("path") if isinstance(artifact, dict) else None
        declared_hash = artifact.get("sha256") if isinstance(artifact, dict) else None
        if (
            not isinstance(path, str)
            or path in by_path
            or not isinstance(declared_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", declared_hash) is None
        ):
            raise PackageFlowError(
                f"target-native marker has an invalid artifact for {primary.package_id}"
            )
        by_path[path] = artifact
    expected_by_path = {spec.relative_path: spec for spec in specs}
    unknown = sorted(set(by_path) - set(expected_by_path))
    if unknown:
        raise PackageFlowError(
            f"target-native marker has unknown artifacts for {primary.package_id}: "
            + ", ".join(unknown)
        )
    if primary.relative_path not in by_path:
        raise PackageFlowError(
            f"target-native marker is missing required {primary.relative_path}"
        )
    loaded = []
    for spec in specs:
        artifact = by_path.get(spec.relative_path)
        if artifact is None:
            continue
        checked = _probe_remote_native_binary(
            ctx,
            f"{repository}/{spec.relative_path}",
            spec,
            expected_version=expected_version,
        )
        if (
            checked["sha256"] != artifact["sha256"]
            or checked["probe"] != artifact.get("probe")
        ):
            raise PackageFlowError(
                f"target-native marker no longer matches {spec.relative_path}"
            )
        loaded.append((spec, artifact))
    return tuple(loaded)


def _update_target_native_runtime_inventory(
    package_root: Path,
    destination: Path,
    spec: TargetNativeArtifactSpec,
) -> None:
    if spec.runtime_inventory_path is None:
        return
    inventory = package_root.joinpath(
        *PurePosixPath(spec.runtime_inventory_path).parts
    )
    if inventory.is_symlink() or not inventory.is_file():
        raise PackageFlowError(
            f"native package source is missing a regular {spec.runtime_inventory_path}"
        )
    try:
        document = json.loads(inventory.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageFlowError(
            f"native runtime inventory is invalid for {spec.package_id}: {exc}"
        ) from exc
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list):
        raise PackageFlowError(
            f"native runtime inventory has no files list for {spec.package_id}"
        )
    entry = {
        "path": spec.relative_path,
        "size": destination.stat().st_size,
        "sha256": _file_sha256(destination),
    }
    retained = [
        item
        for item in files
        if isinstance(item, dict) and item.get("path") != spec.relative_path
    ]
    document["files"] = sorted(
        [*retained, entry], key=lambda item: str(item.get("path", ""))
    )
    inventory.write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    try:
        verified = json.loads(inventory.read_text(encoding="utf-8"))
        matches = [
            item
            for item in verified.get("files", [])
            if isinstance(item, dict) and item.get("path") == spec.relative_path
        ]
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise PackageFlowError(
            f"cannot verify native runtime inventory for {spec.package_id}: {exc}"
        ) from exc
    if matches != [entry]:
        raise PackageFlowError(
            f"native runtime inventory verification failed for {spec.package_id}"
        )


def _remove_target_native_runtime_inventory_entry(
    package_root: Path,
    spec: TargetNativeArtifactSpec,
) -> None:
    if spec.runtime_inventory_path is None:
        return
    inventory = package_root.joinpath(
        *PurePosixPath(spec.runtime_inventory_path).parts
    )
    if inventory.is_symlink() or not inventory.is_file():
        raise PackageFlowError(
            f"native package source is missing a regular {spec.runtime_inventory_path}"
        )
    try:
        document = json.loads(inventory.read_text(encoding="utf-8"))
        files = document.get("files") if isinstance(document, dict) else None
        if not isinstance(files, list):
            raise ValueError("files is not a list")
        document["files"] = [
            item
            for item in files
            if not isinstance(item, dict) or item.get("path") != spec.relative_path
        ]
        inventory.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackageFlowError(
            f"cannot prune native runtime inventory for {spec.package_id}: {exc}"
        ) from exc


@contextlib.contextmanager
def _prepared_target_native_package(
    ctx: Context,
    package_dir: Path,
    manifest_path: Path | None,
) -> Iterator[
    tuple[
        Path,
        Path,
        tuple[tuple[TargetNativeArtifactSpec, dict[str, Any]], ...],
    ]
]:
    unresolved_source = package_dir.expanduser()
    if unresolved_source.is_symlink():
        raise PackageFlowError(
            f"package root is not a real directory: {unresolved_source}"
        )
    source = unresolved_source.resolve()
    if not source.is_dir():
        raise PackageFlowError(f"package root is not a real directory: {source}")
    package_id, version, manifest = _package_source_identity(source, manifest_path)
    specs = _target_native_package_specs(package_id)
    if not specs:
        yield source, manifest, ()
        return

    artifacts = _load_target_native_artifacts(
        ctx, specs, expected_version=version
    )
    artifact_by_path = {
        spec.relative_path: (spec, marker) for spec, marker in artifacts
    }
    manifest_relative = manifest.relative_to(source)
    with tempfile.TemporaryDirectory(prefix=f"msys-native-{specs[0].repository}-") as temporary:
        temporary_root = Path(temporary)
        prepared = temporary_root / source.name
        shutil.copytree(
            source,
            prepared,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                "*.pyc",
                TARGET_NATIVE_MARKER_NAME,
            ),
            copy_function=shutil.copy2,
        )
        for spec in specs:
            destination = prepared.joinpath(*PurePosixPath(spec.relative_path).parts)
            if destination.is_symlink() or (
                destination.exists() and not destination.is_file()
            ):
                raise PackageFlowError(
                    f"native package source has a non-regular {spec.relative_path}"
                )
            selected = artifact_by_path.get(spec.relative_path)
            if selected is None:
                destination.unlink(missing_ok=True)
                _remove_target_native_runtime_inventory_entry(prepared, spec)
                continue
            _selected_spec, marker = selected
            destination.parent.mkdir(parents=True, exist_ok=True)
            downloaded = temporary_root / (
                "target-native-" + PurePosixPath(spec.relative_path).name
            )
            remote_binary = f"{ctx.remote}/{spec.repository}/{spec.relative_path}"
            try:
                run_local(
                    [
                        *scp_base_args(ctx),
                        f"{ctx.target}:{remote_binary}",
                        str(downloaded),
                    ]
                )
            except subprocess.CalledProcessError as exc:
                raise PackageFlowError(
                    f"cannot recover target-native ELF {spec.relative_path}"
                ) from exc
            if downloaded.is_symlink() or not downloaded.is_file():
                raise PackageFlowError(
                    f"target-native download is not a regular file: {spec.relative_path}"
                )
            actual = _file_sha256(downloaded)
            if actual != marker["sha256"]:
                raise PackageFlowError(
                    f"target-native ELF changed during download: {spec.relative_path}; "
                    f"expected {marker['sha256']}, got {actual}"
                )
            downloaded.chmod(downloaded.stat().st_mode | 0o111)
            os.replace(downloaded, destination)
            _update_target_native_runtime_inventory(prepared, destination, spec)
        yield prepared, prepared / manifest_relative, artifacts


def _archive_member_sha256(archive_path: Path, relative_path: str) -> str:
    wanted = PurePosixPath(relative_path).as_posix()
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            matches = []
            for member in archive.getmembers():
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name == wanted:
                    matches.append(member)
            if len(matches) != 1 or not matches[0].isfile():
                raise PackageFlowError(
                    f"built archive must contain one regular target-native {wanted}"
                )
            handle = archive.extractfile(matches[0])
            if handle is None:
                raise PackageFlowError(
                    f"cannot read target-native artifact from archive: {wanted}"
                )
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise PackageFlowError(
            f"cannot inspect built target-native archive {archive_path}: {exc}"
        ) from exc


def _archive_content_hash(archive_path: Path, relative_path: str) -> str:
    wanted = PurePosixPath(relative_path).as_posix()
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            matches = [
                member
                for member in archive.getmembers()
                if member.name.removeprefix("./") == "hashes.json"
            ]
            if len(matches) != 1 or not matches[0].isfile():
                raise PackageFlowError(
                    "built package must contain one regular hashes.json"
                )
            handle = archive.extractfile(matches[0])
            if handle is None:
                raise PackageFlowError("cannot read built package hashes.json")
            document = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise PackageFlowError(
            f"cannot inspect built package content hashes {archive_path}: {exc}"
        ) from exc
    files = document.get("files") if isinstance(document, dict) else None
    declared = files.get(wanted) if isinstance(files, dict) else None
    if not isinstance(declared, str) or re.fullmatch(r"[0-9a-f]{64}", declared) is None:
        raise PackageFlowError(
            f"built package content hashes do not cover target-native {wanted}"
        )
    return declared


def command_package_deliver(
    ctx: Context,
    workspace: Path,
    package_dir: Path,
    output: Path,
    *,
    runtime_dir: str,
    state_dir: str,
    force: bool,
    source_date_epoch: int | None,
    manifest_path: Path | None,
    artifact_format: str,
    overlays: list[Any],
    legacy_events: bool,
) -> int:
    with _prepared_target_native_package(
        ctx, package_dir, manifest_path
    ) as (prepared, prepared_manifest, native_artifacts):
        result = build_package(
            workspace,
            prepared,
            output,
            force=force,
            source_date_epoch=source_date_epoch,
            manifest_path=prepared_manifest,
            artifact_format=artifact_format,
            overlays=overlays,
        )
        if native_artifacts:
            verified_artifacts = []
            for native_spec, marker in native_artifacts:
                packaged_hash = _archive_member_sha256(
                    Path(result["artifact"]), native_spec.relative_path
                )
                content_hash = _archive_content_hash(
                    Path(result["artifact"]), native_spec.relative_path
                )
                if (
                    packaged_hash != marker["sha256"]
                    or content_hash != marker["sha256"]
                ):
                    raise PackageFlowError(
                        "built package did not retain and content-hash the verified "
                        f"target-native ELF {native_spec.relative_path}"
                    )
                verified_artifacts.append(
                    {
                        "path": native_spec.relative_path,
                        "sha256": marker["sha256"],
                        "probe": marker["probe"],
                    }
                )
            primary_spec, primary_marker = native_artifacts[0]
            result["target_native"] = {
                "repository": primary_spec.repository,
                "path": primary_spec.relative_path,
                "sha256": primary_marker["sha256"],
                "probe": primary_marker["probe"],
                "artifacts": verified_artifacts,
            }
        print_json(result)
        return command_install_archive(
            ctx,
            runtime_dir,
            Path(result["artifact"]),
            state_dir=state_dir,
            legacy_events=legacy_events,
        )


def command_registry(
    ctx: Context,
    runtime_dir: str,
    *,
    legacy_events: bool = False,
) -> int:
    if legacy_events:
        _legacy_event_warning("registry")
        return command_broadcast(ctx, runtime_dir, "msys.install.registry", {})
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="registry",
        payload={},
        operation="registry",
    )


def command_check_update(
    ctx: Context,
    runtime_dir: str,
    source: str,
    package: str | None = None,
    allow_downgrade: bool = False,
    allow_unsigned: bool = False,
    *,
    legacy_events: bool = False,
) -> int:
    payload: dict[str, Any] = {"source": source}
    if package:
        payload["package"] = package
    if allow_downgrade:
        payload["allow_downgrade"] = True
    if allow_unsigned:
        payload["allow_unsigned"] = True
    if legacy_events:
        _legacy_event_warning("check_updates")
        return command_broadcast(ctx, runtime_dir, "msys.update.check", payload)
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:update-agent",
        method="check_updates",
        payload=payload,
        operation="check_updates",
    )


def command_apply_update(
    ctx: Context,
    runtime_dir: str,
    source: str,
    package: str | None,
    allow_downgrade: bool,
    allow_unsigned: bool = False,
    *,
    legacy_events: bool = False,
) -> int:
    payload: dict[str, Any] = {
        "source": source,
        "allow_downgrade": allow_downgrade,
    }
    if package:
        payload["package"] = package
    if allow_unsigned:
        payload["allow_unsigned"] = True
    if legacy_events:
        _legacy_event_warning("apply_updates")
        return command_broadcast(ctx, runtime_dir, "msys.update.apply", payload)
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:update-agent",
        method="apply_updates",
        payload=payload,
        operation="apply_updates",
    )


def command_rollback(
    ctx: Context,
    runtime_dir: str,
    package: str,
    *,
    legacy_events: bool = False,
) -> int:
    if legacy_events:
        _legacy_event_warning("rollback")
        return command_broadcast(
            ctx,
            runtime_dir,
            "msys.install.rollback",
            {"package": package},
        )
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="rollback",
        payload={"package": package},
        operation="rollback",
    )


PACKAGE_SNAPSHOT_FIELDS = (
    "package",
    "version",
    "path",
    "artifact_sha256",
    "content_sha256",
)


def _package_registry_snapshot(
    ctx: Context,
    runtime_dir: str,
    package: str,
) -> tuple[int, dict[str, str] | None]:
    status, response = _typed_agent_result(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="registry",
        payload={},
        operation="registry",
        emit_success=False,
    )
    if status != 0 or response is None:
        return status or 1, None
    registry = response.get("result")
    records = registry.get("packages") if isinstance(registry, dict) else None
    if not isinstance(records, list):
        print("package roundtrip: install registry has no package list", file=sys.stderr)
        return 1, None
    matches = [
        item
        for item in records
        if isinstance(item, dict) and item.get("package") == package
    ]
    if len(matches) != 1:
        print(
            f"package roundtrip: expected one current record for {package!r}, "
            f"found {len(matches)}",
            file=sys.stderr,
        )
        return 2, None
    record = matches[0]
    snapshot: dict[str, str] = {}
    for field in PACKAGE_SNAPSHOT_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value or len(value) > 4096:
            print(
                f"package roundtrip: current record has invalid {field}",
                file=sys.stderr,
            )
            return 1, None
        if field.endswith("_sha256") and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            print(
                f"package roundtrip: current record has invalid {field}",
                file=sys.stderr,
            )
            return 1, None
        snapshot[field] = value
    return 0, snapshot


def command_package_roundtrip(
    ctx: Context,
    runtime_dir: str,
    package: str,
) -> int:
    """Swap to package previous and back, proving exact current restoration."""

    before_status, before = _package_registry_snapshot(ctx, runtime_dir, package)
    if before_status != 0 or before is None:
        return before_status or 1

    first_status, _first = _typed_agent_result(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="rollback",
        payload={"package": package},
        operation="rollback",
        emit_success=False,
    )
    middle_status, middle = _package_registry_snapshot(ctx, runtime_dir, package)
    transitioned = middle is not None and middle != before
    must_restore = first_status == 0 or transitioned

    recovery_status: int | None = None
    if must_restore:
        recovery_status, _recovery = _typed_agent_result(
            ctx,
            runtime_dir,
            target="role:install-agent",
            method="rollback",
            payload={"package": package},
            operation="rollback",
            emit_success=False,
        )

    final_status, final = _package_registry_snapshot(ctx, runtime_dir, package)
    restored = final is not None and final == before

    ok = (
        first_status == 0
        and middle_status == 0
        and transitioned
        and recovery_status == 0
        and final_status == 0
        and restored
    )
    report = {
        "schema": "msys.package-rollback-roundtrip.v1",
        "ok": ok,
        "package": package,
        "before": before,
        "previous": middle,
        "final": final,
        "rollback_status": first_status,
        "previous_observation_status": middle_status,
        "recovery_status": recovery_status,
        "final_observation_status": final_status,
        "transitioned": transitioned,
        "restored": restored,
    }
    print_json(report)
    if not restored:
        print(
            "package roundtrip: original current package was not proven restored; "
            "stop and inspect the install registry before another mutation",
            file=sys.stderr,
        )
    return (
        0
        if ok
        else first_status or middle_status or recovery_status or final_status or 1
    )


def command_uninstall(
    ctx: Context,
    runtime_dir: str,
    package: str,
    *,
    legacy_events: bool = False,
) -> int:
    if legacy_events:
        _legacy_event_warning("uninstall")
        return command_broadcast(
            ctx,
            runtime_dir,
            "msys.install.uninstall",
            {"package": package},
        )
    return _typed_agent_request(
        ctx,
        runtime_dir,
        target="role:install-agent",
        method="uninstall",
        payload={"package": package},
        operation="uninstall",
    )


def command_install_update_public_key(
    ctx: Context,
    public_key: Path,
    *,
    state_dir: str = "/opt/msys-state",
) -> int:
    public_key = public_key.expanduser().resolve()
    try:
        details = inspect_update_public_key(ctx.root, public_key)
    except PackageFlowError as exc:
        print(f"update-trust install-public: {exc}", file=sys.stderr)
        return 2
    state_path = PurePosixPath(state_dir)
    if (
        not state_path.is_absolute()
        or state_path == PurePosixPath("/")
        or ".." in state_path.parts
    ):
        print(
            "update-trust install-public: state directory must be a non-root absolute path",
            file=sys.stderr,
        )
        return 2
    digest = _file_sha256(public_key)
    incoming_dir = f"{ctx.remote}/.incoming/update-trust"
    incoming = f"{incoming_dir}/{digest}.public.json"
    ssh(ctx, f"mkdir -p {quote_sh(incoming_dir)} && rm -f {quote_sh(incoming)}")
    run_local([*scp_base_args(ctx), str(public_key), f"{ctx.target}:{incoming}"])
    command = (
        "set -eu; "
        f"cleanup() {{ rm -f {quote_sh(incoming)}; }}; trap cleanup EXIT; "
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-install:' + ctx.remote + '/msys-sdk')} "
        f"{quote_sh(ctx.remote_python)} -m msys_install.cli install-public-key "
        f"{quote_sh(incoming)} "
        f"--state-dir {quote_sh(state_path.as_posix())}"
    )
    completed = ssh(ctx, command, check=False)
    if completed.returncode == 0:
        print_json(
            {
                "installed": True,
                "key_id": details["key_id"],
                "public_key": str(public_key),
                "state_dir": state_path.as_posix(),
            }
        )
    return completed.returncode


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def command_wm(
    ctx: Context,
    runtime_dir: str,
    action: str,
    *,
    window_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> int:
    method_aliases = {
        "list": "list_windows",
        "focus": "focus_window",
        "minimize": "minimize_window",
        "move": "move_window",
        "resize": "resize_window",
        "move-resize": "move_resize_window",
        "close": "close_window",
    }
    method = method_aliases.get(action, action)
    window_actions = {
        "focus_window",
        "minimize_window",
        "move_window",
        "resize_window",
        "move_resize_window",
        "close_window",
    }
    supplied_geometry = {
        name: value
        for name, value in {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        }.items()
        if value is not None
    }
    if method not in window_actions:
        if window_id is not None or supplied_geometry:
            raise ValueError(
                f"wm {action} does not accept --window-id or geometry options"
            )
        if action == "recents":
            return remote_control_command(
                ctx,
                runtime_dir,
                "navigation_action",
                {"action": "recents", "input": "debug"},
                target="role:window-manager",
            )
        return remote_control_command(
            ctx,
            runtime_dir,
            method,
            {},
            target="role:window-manager",
            idempotent=method == "list_windows",
        )

    if (
        window_id is None
        or not window_id.startswith(STABLE_WINDOW_ID_PREFIX)
        or len(window_id) >= 192
        or any(ord(character) < 33 or ord(character) > 126 for character in window_id)
    ):
        raise ValueError(
            f"wm {action} requires a stable --window-id beginning with "
            f"{STABLE_WINDOW_ID_PREFIX}"
        )
    required_geometry = {
        "move_window": {"x", "y"},
        "resize_window": {"width", "height"},
        "move_resize_window": {"x", "y", "width", "height"},
    }.get(method, set())
    if set(supplied_geometry) != required_geometry:
        expected = (
            "no geometry options"
            if not required_geometry
            else " ".join(f"--{name}" for name in ("x", "y", "width", "height") if name in required_geometry)
        )
        raise ValueError(f"wm {action} requires {expected}")
    for name in ("x", "y"):
        value = supplied_geometry.get(name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -32768 <= value <= 32767
        ):
            raise ValueError(f"--{name} must be between -32768 and 32767")
    for name in ("width", "height"):
        value = supplied_geometry.get(name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 32767
        ):
            raise ValueError(f"--{name} must be between 1 and 32767")
    return remote_control_command(
        ctx,
        runtime_dir,
        method,
        {"window_id": window_id, **supplied_geometry},
        target="role:window-manager",
    )


def command_layout(
    ctx: Context,
    runtime_dir: str,
    action: str,
    *,
    profile: str | None = None,
    orientation: str | None = None,
    insets: str | None = None,
) -> int:
    if action == "show":
        return remote_control_command(
            ctx,
            runtime_dir,
            "get_layout",
            {},
            target="role:window-manager",
            idempotent=True,
        )
    payload = {
        key: value
        for key, value in {
            "profile": profile,
            "orientation": orientation,
            "insets": insets,
        }.items()
        if value is not None
    }
    return remote_control_command(
        ctx,
        runtime_dir,
        "set_layout",
        payload,
        target="role:window-manager",
    )


def _validate_pointer_target(
    identity: str | None,
    title: str | None,
    *,
    title_only: bool = False,
    role: str | None = None,
) -> None:
    if role is not None:
        if identity is not None or title is not None:
            raise ValueError("a pointer role cannot be combined with identity or title")
        if role != "navigation-bar":
            raise ValueError("unsupported pointer role")
        return
    if identity is None and (title is None or not title_only):
        raise ValueError("a window identity is required")
    if identity is not None and (
        not identity
        or len(identity) > 255
        or any(ord(character) < 32 for character in identity)
    ):
        raise ValueError("window identity must be 1-255 printable characters")
    if title is not None and (
        not title
        or len(title) > 512
        or any(ord(character) < 32 for character in title)
    ):
        raise ValueError("window title must be 1-512 printable characters")


def _validate_pointer_coordinates(*coordinates: int) -> None:
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= POINTER_COORDINATE_MAX
        for value in coordinates
    ):
        raise ValueError(
            f"pointer coordinates must be integers between 0 and {POINTER_COORDINATE_MAX}"
        )


def _x11_debug_command(
    ctx: Context,
    runtime_dir: str,
    display: str | None,
    arguments: list[str],
) -> int:
    if display is not None and X11_DISPLAY_PATTERN.fullmatch(display) is None:
        raise ValueError("DISPLAY must use the local X11 form :N or :N.S")
    binary = f"{ctx.remote}/msys-x11-session/bin/msys-x11-policy"
    argv = [
        ctx.remote_python,
        "-m",
        "msys_tools.remote_x11_debug",
        "--runtime-dir",
        runtime_dir,
        "--binary",
        binary,
    ]
    if display is not None:
        argv.extend(["--display", display])
    argv.extend(arguments)
    command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        + " ".join(quote_sh(value) for value in argv)
    )
    result = ssh(ctx, command, check=False)
    returncode = getattr(result, "returncode", 0)
    return returncode if isinstance(returncode, int) else 0


def command_tap(
    ctx: Context,
    identity: str | None,
    title: str | None,
    x: int,
    y: int,
    display: str | None = None,
    runtime_dir: str = "/run/msys/main",
    *,
    role: str | None = None,
) -> int:
    _validate_pointer_target(identity, title, role=role)
    _validate_pointer_coordinates(x, y)
    arguments = ["tap", str(x), str(y)]
    if role is not None:
        arguments.extend(["--role", role])
    elif identity is not None:
        arguments.extend(["--identity", identity])
    if title is not None:
        arguments.extend(["--title", title])
    return _x11_debug_command(ctx, runtime_dir, display, arguments)


def command_swipe(
    ctx: Context,
    identity: str | None,
    title: str | None,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int,
    display: str | None = None,
    runtime_dir: str = "/run/msys/main",
    *,
    role: str | None = None,
) -> int:
    _validate_pointer_target(identity, title, title_only=True, role=role)
    _validate_pointer_coordinates(x1, y1, x2, y2)
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not SWIPE_DURATION_MIN_MS <= duration_ms <= SWIPE_DURATION_MAX_MS
    ):
        raise ValueError(
            f"swipe duration must be {SWIPE_DURATION_MIN_MS}-"
            f"{SWIPE_DURATION_MAX_MS} milliseconds"
        )
    arguments = [
        "swipe",
        str(x1),
        str(y1),
        str(x2),
        str(y2),
        "--duration-ms",
        str(duration_ms),
    ]
    if role is not None:
        arguments.extend(["--role", role])
    elif identity is not None:
        arguments.extend(["--identity", identity])
    if title is not None:
        arguments.extend(["--title", title])
    return _x11_debug_command(ctx, runtime_dir, display, arguments)


def _commit_screenshot(
    temporary: Path,
    output: Path,
    *,
    force: bool,
) -> None:
    """Commit a verified local download without an accidental overwrite."""

    if force:
        os.replace(temporary, output)
        return
    try:
        os.link(temporary, output)
    except FileExistsError as exc:
        raise OSError(f"output already exists (use --force): {output}") from exc
    except OSError as exc:
        raise OSError(
            f"cannot atomically create screenshot output {output}: {exc}"
        ) from exc
    temporary.unlink()


def command_screenshot(
    ctx: Context,
    runtime_dir: str,
    output: Path,
    *,
    display: str | None,
    backend: str,
    timeout: float,
    force: bool,
) -> int:
    if display is not None and X11_DISPLAY_PATTERN.fullmatch(display) is None:
        print("screenshot: DISPLAY must use the local X11 form :N or :N.S", file=sys.stderr)
        return 2
    if backend not in {"auto", "scrot", "ffmpeg"}:
        print("screenshot: backend must be auto, scrot, or ffmpeg", file=sys.stderr)
        return 2
    output = output.expanduser()
    if output.is_symlink():
        print(f"screenshot: refusing a symlink output path: {output}", file=sys.stderr)
        return 2
    output = output.resolve()
    if output.exists() and output.is_dir():
        print(f"screenshot: output must be a regular file path: {output}", file=sys.stderr)
        return 2
    if output.exists() and not force:
        print(f"screenshot: output already exists (use --force): {output}", file=sys.stderr)
        return 2
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"screenshot: cannot create output directory: {exc}", file=sys.stderr)
        return 2

    remote_path = f"/tmp/msys-screenshot-{secrets.token_hex(16)}.png"
    if REMOTE_SCREENSHOT_PATTERN.fullmatch(remote_path) is None:
        print("screenshot: internal temporary path validation failed", file=sys.stderr)
        return 2
    argv = [
        ctx.remote_python,
        "-m",
        "msys_tools.remote_screenshot",
        "--runtime-dir",
        runtime_dir,
        "--output",
        remote_path,
        "--backend",
        backend,
        "--timeout",
        f"{timeout:g}",
    ]
    if display is not None:
        argv.extend(["--display", display])
    remote_command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        + " ".join(quote_sh(value) for value in argv)
    )

    local_temporary: Path | None = None
    status = 0
    details: dict[str, Any] | None = None
    error: str | None = None

    class ScreenshotAbort(Exception):
        pass

    try:
        captured = ssh_capture(ctx, remote_command)
        try:
            details = _decode_json_document(captured.stdout)
        except ValueError:
            details = None
        if captured.returncode != 0:
            remote_error = details.get("error") if isinstance(details, dict) else None
            error = str(remote_error or captured.stdout.strip() or "remote capture failed")
            status = captured.returncode or 1
            raise ScreenshotAbort
        if (
            not isinstance(details, dict)
            or details.get("schema") != "msys.debug-screenshot.v1"
            or details.get("ok") is not True
            or details.get("path") != remote_path
        ):
            error = "remote helper returned an invalid screenshot result"
            status = 2
            raise ScreenshotAbort
        remote_size = details.get("size")
        if (
            isinstance(remote_size, bool)
            or not isinstance(remote_size, int)
            or not 8 <= remote_size <= MAX_SCREENSHOT_BYTES
        ):
            error = "remote helper returned an invalid screenshot size"
            status = 2
            raise ScreenshotAbort

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".part", dir=output.parent
        )
        os.close(descriptor)
        local_temporary = Path(temporary_name)
        downloaded = run_local(
            [
                *scp_base_args(ctx),
                f"{ctx.target}:{remote_path}",
                str(local_temporary),
            ],
            check=False,
        )
        if downloaded.returncode != 0:
            error = f"scp download failed with exit status {downloaded.returncode}"
            status = downloaded.returncode or 1
            raise ScreenshotAbort
        try:
            actual_size = local_temporary.stat().st_size
            with local_temporary.open("rb") as handle:
                signature = handle.read(len(PNG_SIGNATURE))
        except OSError as exc:
            error = f"cannot validate downloaded image: {exc}"
            status = 2
            raise ScreenshotAbort
        if actual_size != remote_size:
            error = (
                f"downloaded size mismatch: remote={remote_size} local={actual_size}"
            )
            status = 2
            raise ScreenshotAbort
        if signature != PNG_SIGNATURE:
            error = "downloaded file is not a PNG image"
            status = 2
            raise ScreenshotAbort
        try:
            _commit_screenshot(local_temporary, output, force=force)
        except OSError as exc:
            error = str(exc)
            status = 2
            raise ScreenshotAbort
        local_temporary = None
    except ScreenshotAbort:
        pass
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        error = f"debug transport failed: {exc}"
        status = 2
    finally:
        if local_temporary is not None:
            local_temporary.unlink(missing_ok=True)
        try:
            cleanup = ssh(
                ctx,
                f"rm -f -- {quote_sh(remote_path)} && test ! -e {quote_sh(remote_path)}",
                check=False,
            )
            cleanup_status = getattr(cleanup, "returncode", 0)
            if cleanup_status != 0:
                cleanup_error = (
                    f"remote temporary file cleanup failed with exit status {cleanup_status}: "
                    f"{remote_path}"
                )
                print(f"screenshot: {cleanup_error}", file=sys.stderr)
                if status == 0:
                    status = cleanup_status or 1
        except OSError as exc:
            print(
                f"screenshot: cannot verify remote temporary file cleanup: {exc}",
                file=sys.stderr,
            )
            if status == 0:
                status = 2
    if error is not None:
        print(f"screenshot: {error}", file=sys.stderr)
    if status != 0:
        return status
    assert details is not None
    print(
        f"screenshot: saved {output} "
        f"(display={details.get('display')} backend={details.get('backend')} "
        f"bytes={details.get('size')})"
    )
    return 0


def screenshot_output(value: str | None) -> Path:
    """Resolve the shared explicit-or-timestamped workstation PNG path."""

    if value:
        return Path(value)
    return Path.cwd() / f"msys-screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"


def _fast_report_member(
    archive: tarfile.TarFile,
    name: str,
    *,
    maximum: int,
) -> bytes:
    """Read one bounded regular member from a trusted fast-report envelope."""

    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise ValueError(f"fast report is missing {name}") from exc
    if not member.isfile() or member.name != name or not 0 <= member.size <= maximum:
        raise ValueError(f"fast report contains an invalid {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise ValueError(f"fast report cannot read {name}")
    data = handle.read(maximum + 1)
    if len(data) != member.size:
        raise ValueError(f"fast report has a truncated {name}")
    return data


def command_fast_report(
    ctx: Context,
    runtime_dir: str,
    log_file: str,
    *,
    lines: int,
    screenshot: Path | None,
    display: str | None,
    backend: str,
    timeout: float,
    force: bool,
    json_output: bool = False,
    audio: bool = False,
) -> int:
    """Fetch health, logs, and an optional PNG through one SSH execution.

    A target-side temporary directory is archived to stdout and removed by a
    shell trap.  This avoids the usual status SSH + capture SSH + SCP + cleanup
    SSH sequence while keeping the existing remote helpers and package-free
    target contract.
    """

    if not 0 <= lines <= 1000:
        print("fast: log lines must be between 0 and 1000", file=sys.stderr)
        return 2
    if display is not None and X11_DISPLAY_PATTERN.fullmatch(display) is None:
        print("fast: DISPLAY must use the local X11 form :N or :N.S", file=sys.stderr)
        return 2
    if backend not in {"auto", "scrot", "ffmpeg"}:
        print("fast: backend must be auto, scrot, or ffmpeg", file=sys.stderr)
        return 2

    resolved_output: Path | None = None
    if screenshot is not None:
        resolved_output = screenshot.expanduser()
        if resolved_output.is_symlink():
            print(f"fast: refusing a symlink screenshot path: {resolved_output}", file=sys.stderr)
            return 2
        resolved_output = resolved_output.resolve()
        if resolved_output.exists() and resolved_output.is_dir():
            print("fast: screenshot output must be a regular file path", file=sys.stderr)
            return 2
        if resolved_output.exists() and not force:
            print(
                f"fast: screenshot output already exists (use --force): {resolved_output}",
                file=sys.stderr,
            )
            return 2
        try:
            resolved_output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"fast: cannot create screenshot directory: {exc}", file=sys.stderr)
            return 2

    token = secrets.token_hex(16)
    work = f"/tmp/msys-fast-report-{token}"
    remote_png = f"/tmp/msys-screenshot-{token}.png"
    lifecycle = " ".join(
        quote_sh(value)
        for value in (
            ctx.remote_python,
            "-m",
            "msys_tools.remote_lifecycle",
            "status",
            "--runtime-dir",
            runtime_dir,
        )
    )
    commands = [
        "set -u",
        "umask 077",
        f"work={quote_sh(work)}",
        f"png={quote_sh(remote_png)}",
        'rm -rf "$work"',
        'mkdir -p "$work" || exit 2',
        'trap \'rm -rf "$work"; rm -f "$png"\' EXIT HUP INT TERM',
        "health=0",
        (
            f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
            f"{lifecycle} >\"$work/status.txt\" 2>&1 || health=$?"
        ),
        (
            f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
            f"{quote_sh(ctx.remote_python)} -m msys_tools.remote_ctl "
            f"--runtime-dir {quote_sh(runtime_dir)} --method list_components "
            "--payload '{}' --response-only >\"$work/components.json\" 2>&1 || true"
        ),
        (
            "release=$(readlink -f /opt/msys/current 2>/dev/null || true); "
            "printf 'current_release=%s\\n' \"${release##*/}\" >\"$work/system.txt\"; "
            "df -Pk / 2>/dev/null | awk 'NR==2 {printf \"disk_available_kib=%s\\n"
            "disk_used_percent=%s\\n\", $4, $5}' >>\"$work/system.txt\"; "
            "awk '/^MemTotal:/ {printf \"memory_total_kib=%s\\n\", $2} "
            "/^MemAvailable:/ {printf \"memory_available_kib=%s\\n\", $2} "
            "/^SwapFree:/ {free=$2} /^SwapTotal:/ {total=$2} END "
            "{printf \"swap_used_kib=%s\\n\", total-free}' /proc/meminfo "
            ">>\"$work/system.txt\""
        ),
    ]
    members = ["meta.json", "status.txt", "components.json", "system.txt"]
    commands.append("audio=0")
    if audio:
        audio_argv = [
            ctx.remote_python,
            "-m",
            "msys_tools.remote_ctl",
            "--runtime-dir",
            runtime_dir,
            "--target",
            "role:audio-manager",
            "--method",
            "get_state",
            "--payload",
            "{}",
            "--response-only",
            "--timeout",
            "10",
            "--idempotent",
        ]
        commands.extend(
            [
                (
                    f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
                    + " ".join(quote_sh(value) for value in audio_argv)
                    + ' >"$work/audio.json" 2>&1 || audio=$?'
                ),
                (
                    "LC_ALL=C ps -ww -eo pid=,ppid=,rss=,vsz=,stat=,comm=,args= "
                    "2>/dev/null | awk 'index($7, \"/org.msys.audio.bluez/\") "
                    "|| ($6 ~ /^python/ && index($8, \"/org.msys.audio.bluez/\")) "
                    "{print}' "
                    '>"$work/audio-processes.txt" || true'
                ),
                (
                    "while read -r pid rest; do "
                    "test -r \"/proc/$pid/smaps_rollup\" || continue; "
                    "pss=$(awk '$1 == \"Pss:\" {print $2; exit}' "
                    "\"/proc/$pid/smaps_rollup\" 2>/dev/null); "
                    "case \"$pss\" in ''|*[!0-9]*) continue;; esac; "
                    "printf '%s %s\\n' \"$pid\" \"$pss\"; "
                    "done <\"$work/audio-processes.txt\" "
                    '>"$work/audio-memory.txt" || true'
                ),
                (
                    "LC_ALL=C ps -ww -eo pid=,ppid=,stat=,comm=,args= "
                    "2>/dev/null | awk '$4 == \"bluetoothd\" "
                    "&& index($5, \"/org.msys.audio.bluez/\") == 0 {print}' "
                    '>"$work/audio-host-conflicts.txt" || true'
                ),
            ]
        )
        members.extend(
            [
                "audio.json",
                "audio-processes.txt",
                "audio-memory.txt",
                "audio-host-conflicts.txt",
            ]
        )
    if lines:
        commands.append(
            f"tail -n 500 {quote_sh(log_file)} 2>/dev/null | "
            "awk '$0 ~ /^msysd: public control socket([[:space:]]|$)/ "
            "{n=0; next} "
            "tolower($0) ~ /error|warning|failed|failure|oom|quarantine|exited|traceback/ "
            "{lines[++n]=$0} "
            f"END {{start=n-{lines}+1; if (start<1) start=1; for (i=start;i<=n;i++) print lines[i]}}' "
            ">\"$work/log.txt\" || true"
        )
        members.append("log.txt")
        if audio:
            commands.append(
                f"tail -n 1000 {quote_sh(log_file)} 2>/dev/null | "
                "awk '$0 ~ /^msysd: public control socket([[:space:]]|$)/ "
                "{n=0; next} "
                "tolower($0) ~ /msys-audio|audio-manager|bluez|bluealsa|"
                "bluetoothd|squeezelite/ {lines[++n]=$0} "
                f"END {{start=n-{lines}+1; if (start<1) start=1; "
                "for (i=start;i<=n;i++) print lines[i]}' "
                '>"$work/audio-log.txt" || true'
            )
            members.append("audio-log.txt")
    commands.append("shot=0")
    if screenshot is not None:
        screenshot_argv = [
            ctx.remote_python,
            "-m",
            "msys_tools.remote_screenshot",
            "--runtime-dir",
            runtime_dir,
            "--output",
            remote_png,
            "--backend",
            backend,
            "--timeout",
            f"{timeout:g}",
        ]
        if display is not None:
            screenshot_argv.extend(["--display", display])
        commands.extend(
            [
                (
                    f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
                    + " ".join(quote_sh(value) for value in screenshot_argv)
                    + ' >"$work/screenshot.json" 2>&1 || shot=$?'
                ),
                'if test "$shot" -eq 0; then mv "$png" "$work/screenshot.png" || shot=$?; fi',
                'test -f "$work/screenshot.png" || : >"$work/screenshot.png"',
            ]
        )
        members.extend(["screenshot.json", "screenshot.png"])
    metadata = (
        "printf '{\"schema\":\"msys.fast-report.v1\","
        "\"health_status\":%s,\"screenshot_status\":%s,"
        "\"audio_status\":%s}\\n' "
        '"$health" "$shot" "$audio" >"$work/meta.json"'
        if audio
        else (
            "printf '{\"schema\":\"msys.fast-report.v1\","
            "\"health_status\":%s,\"screenshot_status\":%s}\\n' "
            '"$health" "$shot" >"$work/meta.json"'
        )
    )
    commands.extend(
        [
            metadata,
            "tar -cf - -C \"$work\" " + " ".join(quote_sh(name) for name in members),
            "archive=$?",
            'if test "$archive" -ne 0; then exit "$archive"; fi',
            'if test "$health" -ne 0; then exit "$health"; fi',
            'if test "$audio" -ne 0; then exit "$audio"; fi',
            'exit "$shot"',
        ]
    )
    completed = ssh_capture_bytes(
        ctx,
        "; ".join(commands),
        display_command="<one-pass health/log/screenshot report>",
    )

    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            names = archive.getnames()
            if sorted(names) != sorted(members) or len(names) != len(set(names)):
                raise ValueError("fast report contains unexpected archive members")
            meta = json.loads(
                _fast_report_member(archive, "meta.json", maximum=4096).decode("utf-8")
            )
            if not isinstance(meta, dict) or meta.get("schema") != "msys.fast-report.v1":
                raise ValueError("fast report metadata has an invalid schema")
            health_status = meta.get("health_status")
            screenshot_status = meta.get("screenshot_status")
            audio_status = meta.get("audio_status", 0)
            if (
                isinstance(health_status, bool)
                or not isinstance(health_status, int)
                or not 0 <= health_status <= 255
                or isinstance(screenshot_status, bool)
                or not isinstance(screenshot_status, int)
                or not 0 <= screenshot_status <= 255
                or isinstance(audio_status, bool)
                or not isinstance(audio_status, int)
                or not 0 <= audio_status <= 255
            ):
                raise ValueError("fast report metadata has invalid status values")
            status_text = _fast_report_member(
                archive, "status.txt", maximum=2 * 1024 * 1024
            ).decode("utf-8", errors="replace")
            status_document = _decode_json_document(status_text)
            components_text = _fast_report_member(
                archive, "components.json", maximum=2 * 1024 * 1024
            ).decode("utf-8", errors="replace")
            try:
                components_response = _decode_json_document(components_text)
            except ValueError:
                components_response = {}
            raw_components = (
                components_response.get("payload", {}).get("components", [])
                if components_response.get("type") == "return"
                else []
            )
            component_by_id = {
                str(item.get("id")): item
                for item in raw_components
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            critical_records = []
            for component_id in status_document.get("critical_components", []):
                if not isinstance(component_id, str):
                    continue
                component = component_by_id.get(component_id, {})
                critical_records.append(
                    {
                        "id": component_id,
                        "state": component.get("state", "unknown"),
                        "package_version": component.get(
                            "package_version", component.get("version")
                        ),
                        "path": component.get(
                            "path",
                            component.get(
                                "effective_path",
                                component.get("package_root", component.get("command")),
                            ),
                        ),
                    }
                )
            system_text = _fast_report_member(
                archive, "system.txt", maximum=64 * 1024
            ).decode("utf-8", errors="replace")
            system = {}
            for line in system_text.splitlines():
                key, separator, value = line.partition("=")
                if separator and key:
                    system[key] = value
            log_lines: list[str] = []
            if lines:
                log_text = _fast_report_member(
                    archive, "log.txt", maximum=8 * 1024 * 1024
                ).decode("utf-8", errors="replace")
                log_lines = log_text.splitlines()

            report = {
                "schema": "msys.fast-summary.v1",
                "healthy": status_document.get("healthy") is True,
                "current_release": system.get("current_release") or None,
                "issues": status_document.get("issues", []),
                "critical_components": critical_records,
                "resources": {
                    "disk_available_kib": system.get("disk_available_kib"),
                    "disk_used_percent": system.get("disk_used_percent"),
                    "memory_total_kib": system.get("memory_total_kib"),
                    "memory_available_kib": system.get("memory_available_kib"),
                    "swap_used_kib": system.get("swap_used_kib"),
                },
                "recent_warnings_errors": log_lines,
            }

            if audio:
                audio_text = _fast_report_member(
                    archive, "audio.json", maximum=2 * 1024 * 1024
                ).decode("utf-8", errors="replace")
                audio_response = _decode_json_document(audio_text)
                if audio_status == 0:
                    audio_payload = audio_response.get("payload")
                    if (
                        audio_response.get("type") != "return"
                        or not isinstance(audio_payload, dict)
                    ):
                        raise ValueError("audio role returned an invalid get_state response")
                else:
                    audio_payload = None
                audio_components = []
                for item in raw_components:
                    if not isinstance(item, dict):
                        continue
                    provided = item.get("provides")
                    has_audio_role = isinstance(provided, list) and any(
                        isinstance(entry, dict)
                        and entry.get("kind") == "role"
                        and entry.get("name") == "audio-manager"
                        for entry in provided
                    )
                    if item.get("id") == "org.msys.audio.bluez:audio-manager" or has_audio_role:
                        audio_components.append(item)
                process_text = _fast_report_member(
                    archive, "audio-processes.txt", maximum=1024 * 1024
                ).decode("utf-8", errors="replace")
                memory_text = _fast_report_member(
                    archive, "audio-memory.txt", maximum=64 * 1024
                ).decode("utf-8", errors="replace")
                host_conflict_lines = [
                    line
                    for line in _fast_report_member(
                        archive, "audio-host-conflicts.txt", maximum=64 * 1024
                    ).decode("utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
                pss_by_pid: dict[int, int] = {}
                for line in memory_text.splitlines():
                    fields = line.split()
                    if len(fields) != 2:
                        continue
                    try:
                        pid_value, pss_value = int(fields[0]), int(fields[1])
                    except ValueError:
                        continue
                    if pid_value > 0 and pss_value >= 0:
                        pss_by_pid[pid_value] = pss_value
                audio_log_lines: list[str] = []
                if lines:
                    audio_log_lines = _fast_report_member(
                        archive, "audio-log.txt", maximum=8 * 1024 * 1024
                    ).decode("utf-8", errors="replace").splitlines()
                audio_processes = []
                for line in process_text.splitlines():
                    fields = line.strip().split(None, 6)
                    if len(fields) < 6:
                        continue
                    arguments = fields[6] if len(fields) == 7 else ""
                    # The target-side ps predicate is authoritative. Keep this
                    # identical local check as a compatibility fence for an old
                    # helper/report so a host dbus-daemon can never inflate RSS.
                    if "/org.msys.audio.bluez/" not in arguments:
                        continue
                    try:
                        pid, ppid, rss_kib, vsz_kib = (
                            int(fields[0]),
                            int(fields[1]),
                            int(fields[2]),
                            int(fields[3]),
                        )
                    except ValueError:
                        continue
                    audio_processes.append(
                        {
                            "pid": pid,
                            "ppid": ppid,
                            "rss_kib": rss_kib,
                            "pss_kib": pss_by_pid.get(pid),
                            "vsz_kib": vsz_kib,
                            "state": fields[4],
                            "command": fields[5],
                            "args": arguments,
                        }
                    )
                report["audio"] = {
                    "registered": audio_status == 0,
                    "status": audio_status,
                    "components": audio_components,
                    "state": audio_payload,
                    "error": audio_response if audio_status != 0 else None,
                    "processes": audio_processes,
                    "host_conflicts": host_conflict_lines,
                    "recent_log": audio_log_lines,
                    "rss_total_kib": sum(
                        item["rss_kib"] for item in audio_processes
                    ),
                    "pss_total_kib": sum(
                        item["pss_kib"]
                        for item in audio_processes
                        if isinstance(item["pss_kib"], int)
                    ),
                }

            if screenshot is not None:
                capture_text = _fast_report_member(
                    archive, "screenshot.json", maximum=64 * 1024
                ).decode("utf-8", errors="replace")
                if screenshot_status != 0:
                    print(
                        "fast: screenshot failed: " + capture_text.strip(),
                        file=sys.stderr,
                    )
                else:
                    details = _decode_json_document(capture_text)
                    png = _fast_report_member(
                        archive, "screenshot.png", maximum=MAX_SCREENSHOT_BYTES
                    )
                    if (
                        details.get("schema") != "msys.debug-screenshot.v1"
                        or details.get("ok") is not True
                        or details.get("path") != remote_png
                        or details.get("size") != len(png)
                        or not png.startswith(PNG_SIGNATURE)
                    ):
                        raise ValueError("fast report contains an invalid screenshot")
                    assert resolved_output is not None
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{resolved_output.name}.",
                        suffix=".part",
                        dir=resolved_output.parent,
                    )
                    os.close(descriptor)
                    temporary = Path(temporary_name)
                    try:
                        temporary.write_bytes(png)
                        _commit_screenshot(temporary, resolved_output, force=force)
                    finally:
                        temporary.unlink(missing_ok=True)
                    print(
                        f"screenshot: saved {resolved_output} "
                        f"(display={details.get('display')} backend={details.get('backend')} "
                        f"bytes={details.get('size')})"
                    )
                    report["screenshot"] = {
                        "path": str(resolved_output),
                        "display": details.get("display"),
                        "backend": details.get("backend"),
                        "bytes": details.get("size"),
                    }

            if json_output:
                print_json(report)
            else:
                release = report["current_release"] or "unknown"
                print(
                    f"health: healthy={str(report['healthy']).lower()} "
                    f"current_release={release}"
                )
                resources = report["resources"]
                print(
                    "resources: "
                    f"disk_free={resources['disk_available_kib'] or '?'}KiB "
                    f"disk_used={resources['disk_used_percent'] or '?'} "
                    f"mem_available={resources['memory_available_kib'] or '?'}KiB "
                    f"swap_used={resources['swap_used_kib'] or '?'}KiB"
                )
                if critical_records:
                    print("critical components:")
                    for component in critical_records:
                        print(
                            f"  {component['id']} state={component['state']} "
                            f"version={component['package_version'] or '-'} "
                            f"path={component['path'] or '-'}"
                        )
                if report["issues"]:
                    print("issues:")
                    for issue in report["issues"]:
                        if isinstance(issue, dict):
                            print(
                                f"  {issue.get('code', 'UNKNOWN')}: "
                                f"{issue.get('message', '')}"
                            )
                if log_lines:
                    print("recent warnings/errors:")
                    for line in log_lines:
                        print(f"  {line}")
                if audio:
                    audio_summary = report["audio"]
                    state = audio_summary["state"] or {}
                    component = next(iter(audio_summary["components"]), {})
                    print(
                        "audio: "
                        f"registered={str(audio_summary['registered']).lower()} "
                        f"component={component.get('id', '-')} "
                        f"state={component.get('state', '-')} "
                        f"backend={state.get('backend', '-')} "
                        f"available={str(state.get('available') is True).lower()} "
                        f"reason={state.get('reason') or '-'}"
                    )
                    if audio_summary["error"] is not None:
                        error = audio_summary["error"]
                        print(
                            "audio error: "
                            f"{error.get('code', 'AUDIO_ROLE_UNAVAILABLE')}: "
                            f"{error.get('message', audio_text.strip())}"
                        )
                    if audio_summary["host_conflicts"]:
                        print(
                            "audio host conflicts: "
                            f"count={len(audio_summary['host_conflicts'])}"
                        )
                        for line in audio_summary["host_conflicts"]:
                            print(f"  {line}")
                    print(
                        "audio processes: "
                        f"count={len(audio_summary['processes'])} "
                        f"rss_total={audio_summary['rss_total_kib']}KiB "
                        f"pss_total={audio_summary['pss_total_kib']}KiB"
                    )
                    for process in audio_summary["processes"]:
                        pss = process["pss_kib"]
                        print(
                            f"  pid={process['pid']} rss={process['rss_kib']}KiB "
                            f"pss={str(pss) + 'KiB' if pss is not None else '-'} "
                            f"state={process['state']} command={process['command']}"
                        )
                    if audio_summary["recent_log"]:
                        print("audio recent log:")
                        for line in audio_summary["recent_log"]:
                            print(f"  {line}")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        remote_error = completed.stderr.decode("utf-8", errors="replace").strip()
        print(f"fast: invalid diagnostic bundle: {exc}", file=sys.stderr)
        if remote_error:
            print(remote_error, file=sys.stderr)
        return completed.returncode or 2

    if completed.returncode not in {health_status, screenshot_status, audio_status, 0}:
        return completed.returncode or 1
    return health_status or audio_status or screenshot_status


def command_fast(
    ctx: Context,
    repos: list[str],
    *,
    safe: bool,
    profile: str,
    runtime_dir: str,
    state_dir: str,
    log_file: str,
    run: bool,
    deliver: bool,
    lines: int,
    screenshot: Path | None,
    display: str | None,
    backend: str,
    timeout: float,
    force: bool,
    full_sync: bool,
    json_output: bool = False,
    overlays: list[Any] | None = None,
    audio: bool = False,
    native_audio_manager: bool = False,
) -> int:
    """Fast edit loop: sync only required sources, then deliver/run and report."""

    if deliver and not repos:
        print("fast: --deliver requires at least one --repo", file=sys.stderr)
        return 2
    if native_audio_manager and "msys-audio" not in repos:
        print(
            "fast: --native-audio-manager requires --repo msys-audio",
            file=sys.stderr,
        )
        return 2
    if deliver and overlays and len(repos) != 1:
        print(
            "fast: explicit --overlay is allowed only with exactly one --repo; "
            "a batch overlay has no unambiguous repository owner",
            file=sys.stderr,
        )
        return 2
    blocked_release_inputs = [
        repository for repository in repos if repository in FAST_DELIVERY_RELEASE_INPUTS
    ]
    if deliver and blocked_release_inputs:
        blocked = ", ".join(blocked_release_inputs)
        classification = (
            "is a formal release input"
            if len(blocked_release_inputs) == 1
            else "are formal release inputs"
        )
        print(
            f"fast: {blocked} {classification}; use the release compose/stage/"
            "activate flow instead of pretending a source sync is live deployment",
            file=sys.stderr,
        )
        return 2
    if safe:
        status = command_doctor(ctx, profile)
        if status != 0:
            return status
    sync_repositories = list(repos)
    if deliver and not full_sync:
        sync_repositories = [
            repository
            for repository in repos
            if repository in TARGET_NATIVE_REPOSITORIES
        ]
        skipped = [
            repository for repository in repos if repository not in sync_repositories
        ]
        if skipped:
            print(
                "fast: direct package delivery skips redundant remote source sync for "
                + ", ".join(skipped)
                + " (use --full-sync to update remote development sources too)"
            )
    if sync_repositories:
        sync_options: dict[str, Any] = {"force": full_sync}
        if native_audio_manager:
            sync_options["native_audio_manager"] = True
        status = command_sync(ctx, sync_repositories, **sync_options)
        if status != 0:
            return status
    elif not repos:
        print("fast: no repository selected; collecting diagnostics only")
    if repos and not deliver and not run:
        print(
            "fast: source synchronized only; the immutable formal current release was "
            "not changed (use --deliver for one installable package, or the release flow)"
        )

    if deliver:
        explicit_overlays = list(overlays or [])
        for position, repository in enumerate(repos, start=1):
            package_dir = (ctx.root / repository).resolve()
            try:
                selected_overlays = list(explicit_overlays)
                if (
                    not selected_overlays
                    and repository in FAST_DELIVERY_SDK_REPOSITORIES
                ):
                    selected_overlays.append(
                        parse_overlay_spec(ctx.root, FAST_DELIVERY_SDK_OVERLAY)
                    )
                    print(
                        "fast: automatically overlaying msys-sdk/msys_sdk -> "
                        f"files/app/msys_sdk for canonical {repository} delivery"
                    )
                manifest = resolve_source_manifest(package_dir)
                document = json.loads(manifest.read_text(encoding="utf-8-sig"))
                package_document = (
                    document.get("package") if isinstance(document, dict) else None
                )
                package_id = (
                    package_document.get("id")
                    if isinstance(package_document, dict)
                    else None
                )
                print(f"fast: delivering {position}/{len(repos)} {repository}")
                status = command_package_deliver(
                    ctx,
                    ctx.root,
                    package_dir,
                    ctx.root / "dist",
                    runtime_dir=runtime_dir,
                    state_dir=state_dir,
                    force=True,
                    source_date_epoch=None,
                    manifest_path=manifest,
                    artifact_format="maf",
                    overlays=selected_overlays,
                    legacy_events=False,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, PackageFlowError) as exc:
                print(f"fast: cannot deliver {repository}: {exc}", file=sys.stderr)
                return 2
            if status != 0:
                if package_id == "org.msys.core.install":
                    print(
                        "fast: install-agent self-update requires the external/offline "
                        "msys_install.cli install-archive transaction",
                        file=sys.stderr,
                    )
                return status
    elif run:
        status = command_run(
            ctx, profile, runtime_dir, log_file, ctx.remote_python, timeout
        )
        if status != 0:
            return status

    return command_fast_report(
        ctx,
        runtime_dir,
        log_file,
        lines=lines,
        screenshot=screenshot,
        display=display,
        backend=backend,
        timeout=timeout,
        force=force,
        json_output=json_output,
        audio=audio,
    )


def command_storage(
    ctx: Context,
    dev_root: str,
    state_dir: str,
    release_root: str,
    usb_root: str,
    *,
    apply: bool,
    no_archive: bool,
    json_output: bool,
    log_file: str = "/tmp/msysd.log",
) -> int:
    """Run one read-only plan or USB-backed cleanup transaction over one SSH."""

    if no_archive and not apply:
        print("storage: --no-archive requires explicit --apply", file=sys.stderr)
        return 2
    argv = [
        ctx.remote_python,
        "-m",
        "msys_tools.remote_storage",
        "--dev-root",
        dev_root,
        "--state-dir",
        state_dir,
        "--release-root",
        release_root,
        "--usb-root",
        usb_root,
        "--log-file",
        log_file,
    ]
    if apply:
        argv.append("--apply")
    if no_archive:
        argv.append("--no-archive")
    command = (
        "PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools:/opt/msys/current/msys-tools')} "
        + " ".join(quote_sh(value) for value in argv)
    )
    completed = ssh_capture(ctx, command, display_command="<one-pass storage plan/offload>")
    try:
        document = _decode_json_document(completed.stdout)
    except ValueError as exc:
        print(f"storage: invalid remote report: {exc}", file=sys.stderr)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
        return completed.returncode or 2
    if document.get("schema") != "msys.storage-report.v1":
        print("storage: remote report has an invalid schema", file=sys.stderr)
        return completed.returncode or 2
    if json_output:
        print_json(document)
    elif document.get("ok") is False:
        print(f"storage: failed: {document.get('error', 'unknown remote error')}")
    else:
        print(
            f"storage: mode={document.get('mode', 'unknown')} "
            f"candidates={document.get('candidate_count', 0)} "
            f"reclaimable={document.get('reclaimable_bytes', 0)}B"
        )
        root_fs = document.get("filesystems", {}).get("root", {})
        usb = document.get("usb", {})
        print(
            "space: "
            f"root_free={root_fs.get('free_bytes', '?')}B "
            f"usb_mounted={str(usb.get('mounted') is True).lower()} "
            f"usb_free={usb.get('free_bytes', '?')}B"
        )
        package_versions = document.get("package_versions", [])
        release_items = document.get("releases", {}).get("items", [])
        print(
            "inventory (read-only): "
            f"package_versions={len(package_versions) if isinstance(package_versions, list) else 0} "
            f"package_bytes={document.get('package_version_bytes', 0)}B "
            f"releases={len(release_items) if isinstance(release_items, list) else 0}"
        )
        for item in document.get("candidates", []):
            if isinstance(item, dict):
                print(
                    f"  {item.get('kind', 'unknown')} "
                    f"{item.get('allocated_bytes', 0)}B {item.get('path', '')}"
                )
        archive = document.get("archive")
        if isinstance(archive, dict):
            print(
                f"archive: {archive.get('path')} sha256={archive.get('archive_sha256')}"
            )
        if document.get("archive_skipped") is True:
            print("archive: explicitly skipped by --no-archive")
        for issue in document.get("issues", []):
            if isinstance(issue, dict):
                print(f"warning: {issue.get('code', 'STORAGE')}: {issue.get('message', '')}")
    return completed.returncode or (2 if document.get("ok") is False else 0)


def command_quick(
    ctx: Context,
    repos: list[str],
    *,
    safe: bool,
    profile: str,
    runtime_dir: str,
    log_file: str,
    status_only: bool,
    screenshot: Path | None,
    display: str | None,
    backend: str,
    timeout: float,
    force: bool,
    full_sync: bool = False,
    native_audio_manager: bool = False,
) -> int:
    """Compose the common edit/deploy loop without adding another service."""

    if safe:
        print("quick: running the explicit full doctor gate")
        status = command_doctor(ctx, profile)
        if status != 0:
            return status

    sync_options: dict[str, Any] = {}
    if full_sync:
        sync_options["force"] = True
    if native_audio_manager:
        sync_options["native_audio_manager"] = True
    status = command_sync(ctx, repos, **sync_options)
    if status != 0:
        return status

    if not status_only:
        status = command_run(
            ctx,
            profile,
            runtime_dir,
            log_file,
            ctx.remote_python,
            timeout,
        )
        if status != 0:
            return status

    if screenshot is not None:
        return command_fast_report(
            ctx,
            runtime_dir,
            log_file,
            lines=0,
            screenshot=screenshot,
            display=display,
            backend=backend,
            timeout=timeout,
            force=force,
        )
    if status_only:
        return command_status(ctx, runtime_dir)
    return 0


def command_font_doctor(
    ctx: Context,
    runtime_dir: str,
    *,
    python: str | None,
    display: str | None,
    family: str,
    size: int,
) -> int:
    if python is not None:
        python_path = PurePosixPath(python)
        if (
            not python_path.is_absolute()
            or python_path == PurePosixPath("/")
            or ".." in python_path.parts
            or any(ord(character) < 32 for character in python)
        ):
            print(
                "font-doctor: --python must be a non-root absolute POSIX path",
                file=sys.stderr,
            )
            return 2
    if display is not None and X11_DISPLAY_PATTERN.fullmatch(display) is None:
        print("font-doctor: DISPLAY must use the local X11 form :N or :N.S", file=sys.stderr)
        return 2
    probe_argv = [
        "-B",
        "-m",
        "msys_tools.remote_font_probe",
        "--runtime-dir",
        runtime_dir,
        "--family",
        family,
        "--size",
        str(size),
    ]
    if display is not None:
        probe_argv.extend(["--display", display])
    environment = (
        "export PYTHONDONTWRITEBYTECODE=1; "
        f"export PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')}; "
    )
    arguments = " ".join(quote_sh(value) for value in probe_argv)
    if python is not None:
        # An explicit candidate probe is authoritative, even when a formal
        # current release exists.
        command = environment + f"exec {quote_sh(python)} {arguments}"
    else:
        # Probe the interpreter of the Core which is actually supervising Tk
        # components.  A healthy formal candidate is irrelevant when the live
        # development Core launches applications through a different
        # core-font-only runtime.  Formal/development paths remain bounded
        # fallbacks for a stopped runtime.
        current_python = (
            f"{DEFAULT_SYSTEM_RELEASE_ROOT}/current/{DEFAULT_REMOTE_PYTHON_REL}"
        )
        lock_file = str(PurePosixPath(runtime_dir) / ".msysd.lock")
        pid_file = str(PurePosixPath(runtime_dir) / "msysd.pid")
        command = (
            environment
            + "active_python=''; msys_pid=''; "
            + f"for active_pid_file in {quote_sh(lock_file)} {quote_sh(pid_file)}; do "
            + "if test ! -r \"$active_pid_file\"; then continue; fi; "
            + "msys_pid=$(cat \"$active_pid_file\" 2>/dev/null || true); "
            + "case \"$msys_pid\" in ''|*[!0-9]*) msys_pid='' ;; esac; "
            + "if test -n \"$msys_pid\" && test -e \"/proc/$msys_pid/exe\" "
            + "&& tr '\\0' '\\n' <\"/proc/$msys_pid/cmdline\" "
            + "| grep -Fqx -- 'msys_core.msysd'; then "
            + "active_python=$(readlink -f \"/proc/$msys_pid/exe\" 2>/dev/null || true); "
            + "break; fi; done; "
            + "if test -n \"$active_python\" && test -x \"$active_python\"; then "
            + f"exec \"$active_python\" {arguments}; "
            + f"elif test -x {quote_sh(current_python)}; then "
            + f"exec {quote_sh(current_python)} {arguments}; "
            + f"elif test -x {quote_sh(ctx.remote_python)}; then "
            + f"exec {quote_sh(ctx.remote_python)} {arguments}; "
            + "else echo 'font-doctor: no usable isolated Python runtime' >&2; "
            + "exit 127; fi"
        )
    completed = ssh(ctx, command, check=False)
    return completed.returncode


def command_visual_smoke(
    ctx: Context,
    runtime_dir: str,
    component: str,
    *,
    timeout: float,
) -> int:
    if (
        not component
        or ":" not in component
        or len(component) > 255
        or any(ord(character) < 33 or ord(character) > 126 for character in component)
    ):
        print(
            "visual-smoke: component must be a package:component identifier",
            file=sys.stderr,
        )
        return 2
    argv = [
        ctx.remote_python,
        "-m",
        "msys_tools.remote_visual_smoke",
        "--runtime-dir",
        runtime_dir,
        "--component",
        component,
        "--timeout",
        f"{timeout:g}",
    ]
    command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        + " ".join(quote_sh(value) for value in argv)
    )
    result = ssh_capture(ctx, command)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return result.returncode


def command_ui_acceptance(
    ctx: Context,
    runtime_dir: str,
    *,
    timeout: float,
    display_log: str,
) -> int:
    if not 0 < timeout <= 120:
        print(
            "ui-accept: timeout must be greater than zero and at most 120 seconds",
            file=sys.stderr,
        )
        return 2
    for label, value in (("runtime directory", runtime_dir), ("display log", display_log)):
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path == PurePosixPath("/")
            or ".." in path.parts
            or len(value) > 1024
        ):
            print(f"ui-accept: {label} must be a non-root absolute target path", file=sys.stderr)
            return 2
    argv = [
        ctx.remote_python,
        "-B",
        "-m",
        "msys_tools.remote_ui_acceptance",
        "--runtime-dir",
        runtime_dir,
        "--timeout",
        f"{timeout:g}",
        "--display-log",
        display_log,
    ]
    command = (
        "PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools')} "
        + " ".join(quote_sh(value) for value in argv)
    )
    result = ssh_capture(ctx, command)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return result.returncode


def command_debug_env(ctx: Context, runtime_dir: str) -> int:
    ssh(ctx, (
        "set -x; "
        "uname -a; "
        f"test -x {quote_sh(ctx.remote_python)} && {quote_sh(ctx.remote_python)} --version; "
        f"ls -la {quote_sh(ctx.remote)}; "
        f"find {quote_sh(ctx.remote)} -maxdepth 2 -type f -name pyproject.toml -print; "
        f"ls -la {quote_sh(runtime_dir)} 2>/dev/null || true; "
        f"test -S {quote_sh(runtime_dir + '/control.sock')} && echo socket=ok || echo socket=missing; "
        "pgrep -af 'msys_core.msysd|msys_install|msys_shell' || true"
    ))
    return 0


def command_script(output: Path, remote: str) -> int:
    remote_default = quote_sh(remote)
    text = f"""#!/bin/sh
set -eu
MSYS_ROOT_DEFAULT={remote_default}
MSYS_ROOT="${{MSYS_ROOT:-$MSYS_ROOT_DEFAULT}}"
MSYS_RUNTIME_DIR="${{MSYS_RUNTIME_DIR:-/run/msys/main}}"
MSYS_PROFILE="${{MSYS_PROFILE:-mobile-spi}}"
MSYS_PYTHON="${{MSYS_PYTHON:-$MSYS_ROOT/.runtime/python/bin/python3}}"
export MSYS_PLATFORM_PYTHONPATH="${{MSYS_PLATFORM_PYTHONPATH:-$MSYS_ROOT/msys-sdk}}"
export PYTHONDONTWRITEBYTECODE=1
export MALLOC_ARENA_MAX="${{MALLOC_ARENA_MAX:-2}}"
export MALLOC_TRIM_THRESHOLD_="${{MALLOC_TRIM_THRESHOLD_:-262144}}"
export PYTHONPATH="$MSYS_ROOT/msys-core:$MSYS_ROOT/msys-sdk:$MSYS_ROOT/msys-shell-pyside:$MSYS_ROOT/msys-x11-session:$MSYS_ROOT/msys-hal:$MSYS_ROOT/msys-input-touch/files/app:$MSYS_ROOT/msys-install"
if test -S "$MSYS_RUNTIME_DIR/control.sock"; then
  echo "refusing to start a duplicate msysd; stop the existing session first" >&2
  exit 73
fi
mkdir -p "$MSYS_RUNTIME_DIR"
cd "$MSYS_ROOT"
set -- -m msys_core.msysd --foreground \\
  --config "$MSYS_ROOT/msys-core/examples/config" \\
  --runtime-dir "$MSYS_RUNTIME_DIR" \\
  --profile "$MSYS_PROFILE"
native_shell_manifest="$MSYS_ROOT/msys-shell-native/manifest.json"
shell_manifest="$MSYS_ROOT/msys-shell-pyside/manifest.json"
hal_manifest="$MSYS_ROOT/msys-hal/manifest.json"
x11_session_manifest="$MSYS_ROOT/msys-x11-session/manifest.json"
ch347_manifest="$MSYS_ROOT/msys-openstick-ch347/manifest.json"
input_manifest="$MSYS_ROOT/msys-input-touch/manifest.json"
install_manifest="$MSYS_ROOT/msys-install/manifest.json"
test ! -f "$native_shell_manifest" || set -- "$@" --manifest "$native_shell_manifest"
test ! -f "$shell_manifest" || set -- "$@" --manifest "$shell_manifest"
if test -f "$hal_manifest"; then
  set -- "$@" --manifest "$hal_manifest"
fi
test ! -f "$x11_session_manifest" || set -- "$@" --manifest "$x11_session_manifest"
if test -f "$ch347_manifest"; then
  set -- "$@" --manifest "$ch347_manifest"
fi
test ! -f "$input_manifest" || set -- "$@" --manifest "$input_manifest"
test ! -f "$install_manifest" || set -- "$@" --manifest "$install_manifest"
exec "$MSYS_PYTHON" "$@"
"""
    output.write_text(text, encoding="utf-8", newline="\n")
    print(output)
    return 0


def _normalise_remote_release_root(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("release root must be a non-root absolute POSIX path without '..'")
    return path.as_posix().rstrip("/")


def _normalise_remote_source_root(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("source root must be a non-root absolute POSIX path without '..'")
    return path.as_posix().rstrip("/")


def _normalise_release_entry(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or ".." in value
        or "/" in value
        or "\\" in value
        or REPOSITORY_NAME_PATTERN.fullmatch(value.lstrip(".")) is None
    ):
        raise ValueError(f"invalid top-level release entry: {value!r}")
    return value


def _normalise_release_mapping(value: str) -> str:
    name, separator, raw_path = value.partition("=")
    if not separator or not raw_path:
        raise ValueError("release mapping must use NAME=/absolute/path syntax")
    entry = _normalise_release_entry(name)
    path = _normalise_remote_source_root(raw_path)
    return f"{entry}={path}"


def command_release_compose(
    ctx: Context,
    release_root: str,
    release_id: str,
    baseline_release: str,
    workspace_root: str,
    output_root: str,
    entries: list[str],
    mafs: list[str],
    python_runtime: str | None = None,
) -> int:
    root = _normalise_remote_release_root(release_root)
    workspace = _normalise_remote_source_root(workspace_root)
    output = _normalise_remote_source_root(output_root)
    argv = [
        ctx.remote_python,
        "-B",
        "-m",
        "msys_tools.release_compose",
        release_id,
        "--release-root",
        root,
        "--baseline-release",
        baseline_release,
        "--workspace-root",
        workspace,
        "--output-root",
        output,
    ]
    if python_runtime is not None:
        argv.extend(
            ["--python-runtime", _normalise_remote_source_root(python_runtime)]
        )
    for mapping in entries:
        argv.extend(["--entry", _normalise_release_mapping(mapping)])
    for mapping in mafs:
        argv.extend(["--maf", _normalise_release_mapping(mapping)])
    command = (
        "PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-tools:' + ctx.remote + '/msys-install')} "
        + " ".join(quote_sh(item) for item in argv)
    )
    result = ssh_capture(ctx, command)
    _print_completed_output(result, error=result.returncode != 0)
    return result.returncode


def _remote_release_command(
    ctx: Context,
    release_root: str,
    action: str,
    arguments: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    root = _normalise_remote_release_root(release_root)
    argv = [
        "-B",
        "-m",
        "msys_install.release",
        "--release-root",
        root,
        action,
        *(arguments or []),
    ]
    command = (
        f"PYTHONPATH={quote_sh(ctx.remote + '/msys-install')} "
        f"PYTHONDONTWRITEBYTECODE=1 {quote_sh(ctx.remote_python)} "
        + " ".join(quote_sh(item) for item in argv)
    )
    return ssh_capture(ctx, command)


def _release_status_document(ctx: Context, release_root: str) -> dict[str, Any]:
    result = _remote_release_command(ctx, release_root, "status")
    if result.returncode != 0:
        _print_completed_output(result, error=True)
        raise RuntimeError("cannot read remote system-release status")
    try:
        return _decode_json_document(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"invalid remote system-release status: {exc}") from exc


def command_release_stage(
    ctx: Context,
    release_root: str,
    release_id: str,
    source_root: str,
    entries: list[str],
    *,
    keep: int,
    activate: bool,
    restart_service: bool,
    runtime_dir: str,
    log_file: str,
    health_timeout: float = DEFAULT_RELEASE_HEALTH_TIMEOUT,
) -> int:
    source = _normalise_remote_source_root(source_root)
    selected = list(dict.fromkeys(_normalise_release_entry(item) for item in entries))
    arguments = [release_id, "--source-root", source, "--keep", str(keep)]
    for entry in selected:
        arguments.extend(["--entry", entry])
    staged = _remote_release_command(ctx, release_root, "stage", arguments)
    _print_completed_output(staged, error=staged.returncode != 0)
    if staged.returncode != 0 or not activate:
        return staged.returncode
    return command_release_switch(
        ctx,
        release_root,
        "activate",
        release_id,
        restart_service=restart_service,
        runtime_dir=runtime_dir,
        log_file=log_file,
        health_timeout=health_timeout,
    )


def command_release_switch(
    ctx: Context,
    release_root: str,
    action: str,
    release_id: str | None,
    *,
    restart_service: bool,
    runtime_dir: str,
    log_file: str,
    health_timeout: float = DEFAULT_RELEASE_HEALTH_TIMEOUT,
) -> int:
    if action not in {"activate", "rollback"}:
        raise ValueError(f"unsupported release switch action: {action}")
    arguments = [release_id] if release_id is not None else []
    if not restart_service:
        result = _remote_release_command(ctx, release_root, action, arguments)
        _print_completed_output(result, error=result.returncode != 0)
        return result.returncode

    status = _release_status_document(ctx, release_root)
    before = status.get("current")
    if not isinstance(before, str) or not before:
        print("release: cannot health-check a switch without an active current release", file=sys.stderr)
        return 2
    root = _normalise_remote_release_root(release_root)
    baseline = _remote_release_command(ctx, root, "verify", [before])
    if baseline.returncode != 0:
        _print_completed_output(baseline, error=True)
        print(
            "release: current release "
            f"{before!r} failed verification; refusing to stop the service or switch pointers",
            file=sys.stderr,
        )
        return baseline.returncode or 1
    target = release_id if action == "activate" else status.get("previous")
    if not isinstance(target, str) or not target:
        print(
            "release: cannot health-check rollback without a previous release",
            file=sys.stderr,
        )
        return 2
    target_verification = _remote_release_command(ctx, root, "verify", [target])
    if target_verification.returncode != 0:
        _print_completed_output(target_verification, error=True)
        print(
            "release: target release "
            f"{target!r} failed verification; refusing to stop the service or switch pointers",
            file=sys.stderr,
        )
        return target_verification.returncode or 1
    launcher = f"{root}/service/msys-service"
    prerequisite = ssh_capture(
        ctx,
        f"test -f {quote_sh(launcher)} && test ! -L {quote_sh(launcher)} "
        f"&& test -x {quote_sh(launcher)}",
    )
    if prerequisite.returncode != 0:
        print(
            "release: formal host service is not installed; run host-service install "
            f"--release-root {root} first",
            file=sys.stderr,
        )
        return 2
    stopped = ssh_capture(ctx, f"{quote_sh(launcher)} stop")
    _print_completed_output(stopped, error=stopped.returncode != 0)
    if stopped.returncode != 0:
        return stopped.returncode

    switched = _remote_release_command(ctx, root, action, arguments)
    _print_completed_output(switched, error=switched.returncode != 0)
    if switched.returncode != 0:
        restarted = ssh_capture(ctx, f"{quote_sh(launcher)} start")
        _print_completed_output(restarted, error=restarted.returncode != 0)
        return switched.returncode

    started = ssh_capture(ctx, f"{quote_sh(launcher)} start")
    _print_completed_output(started, error=started.returncode != 0)
    ready = (
        _remote_lifecycle_command(
            ctx,
            "wait-ready",
            runtime_dir,
            timeout=health_timeout,
            log_file=log_file,
        )
        if started.returncode == 0
        else started
    )
    _print_completed_output(ready, error=ready.returncode != 0)
    if ready.returncode == 0:
        print_json(
            {
                "release_switch": action,
                "current": target,
                "service_restarted": True,
                "healthy": True,
            }
        )
        return 0

    # A failed health gate is rollback-biased: restore the exact pointer that
    # was current before the switch, then bring that known release back up.
    ssh_capture(ctx, f"{quote_sh(launcher)} stop")
    restored = _remote_release_command(ctx, root, "activate", [before])
    _print_completed_output(restored, error=restored.returncode != 0)
    if restored.returncode == 0:
        recovery_start = ssh_capture(ctx, f"{quote_sh(launcher)} start")
        _print_completed_output(recovery_start, error=recovery_start.returncode != 0)
    else:
        recovery_start = subprocess.CompletedProcess(
            [], restored.returncode or 1, stdout=""
        )
    if restored.returncode == 0 and recovery_start.returncode == 0:
        recovery_ready = _remote_lifecycle_command(
            ctx,
            "wait-ready",
            runtime_dir,
            timeout=health_timeout,
            log_file=log_file,
        )
    else:
        recovery_ready = recovery_start
    _print_completed_output(recovery_ready, error=recovery_ready.returncode != 0)
    print_json(
        {
            "release_switch": action,
            "healthy": False,
            "restored_release": before if restored.returncode == 0 else None,
            "restored_healthy": (
                restored.returncode == 0
                and recovery_start.returncode == 0
                and recovery_ready.returncode == 0
            ),
        }
    )
    return ready.returncode or 1


def command_release_simple(
    ctx: Context,
    release_root: str,
    action: str,
    arguments: list[str] | None = None,
) -> int:
    result = _remote_release_command(ctx, release_root, action, arguments)
    _print_completed_output(result, error=result.returncode != 0)
    return result.returncode


def detect_host_service_backends(ctx: Context) -> list[str]:
    result = ssh_capture(ctx, detection_command())
    if result.returncode != 0:
        raise HostServiceError(
            "cannot detect host startup mechanisms: " + result.stdout.strip()
        )
    return parse_detection(result.stdout)


def read_host_service_state(ctx: Context, spec: HostServiceSpec) -> dict[str, str] | None:
    begin = "__MSYS_HOST_STATE_BEGIN__"
    end = "__MSYS_HOST_STATE_END__"
    command = (
        f"test -f {quote_sh(spec.state_file)} && test ! -L {quote_sh(spec.state_file)} "
        "|| exit 4; "
        f"echo {begin}; "
        f"while IFS= read -r line; do printf '%s\\n' \"$line\"; done < {quote_sh(spec.state_file)}; "
        f"echo {end}"
    )
    result = ssh_capture(ctx, command)
    if result.returncode == 4:
        return None
    if result.returncode != 0:
        raise HostServiceError("cannot read host-service state: " + result.stdout.strip())
    start = result.stdout.find(begin + "\n")
    finish = result.stdout.find("\n" + end, start + len(begin))
    if start < 0 or finish < 0:
        raise HostServiceError("host-service state response is incomplete")
    state_text = result.stdout[start + len(begin) + 1 : finish]
    return parse_state(state_text)


def resolve_host_service(
    ctx: Context,
    spec: HostServiceSpec,
    requested: str,
    hook: str | None,
) -> tuple[str, str, dict[str, str] | None]:
    state = read_host_service_state(ctx, spec)
    if state is not None:
        validate_state_binding(spec, state)
    if requested == "auto" and hook is not None:
        raise HostServiceError("--hook requires the explicit --backend hook selection")
    if requested == "auto" and state is not None:
        return state["backend"], state["integration"], state
    if requested == "auto":
        backend = select_backend("auto", detect_host_service_backends(ctx))
        return backend, integration_path(backend), None

    backend = select_backend(requested, [])
    if state is not None:
        if state["backend"] != backend:
            raise HostServiceError(
                f"installed backend is {state['backend']}; uninstall it before selecting {backend}"
            )
        if hook is None:
            integration = state["integration"]
        else:
            integration = integration_path(backend, hook)
            if integration != state["integration"]:
                raise HostServiceError(
                    "installed hook path differs; uninstall the existing integration first"
                )
        return backend, integration, state
    return backend, integration_path(backend, hook), None


def remote_ownership(ctx: Context, path: str, test_command: str) -> str:
    marker = "__MSYS_HOST_OWNERSHIP__="
    command = (
        f"if test ! -e {quote_sh(path)} && test ! -L {quote_sh(path)}; then "
        f"echo {marker}absent; "
        f"elif ( {test_command} ); then echo {marker}managed; "
        f"else echo {marker}unmanaged; fi"
    )
    result = ssh_capture(ctx, command)
    if result.returncode != 0:
        raise HostServiceError("cannot inspect remote integration: " + result.stdout.strip())
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            return line[len(marker) :]
    raise HostServiceError("remote integration ownership response is incomplete")


def remote_boolean(ctx: Context, test_command: str) -> bool:
    marker = "__MSYS_HOST_BOOLEAN__="
    result = ssh_capture(
        ctx,
        f"if ( {test_command} ); then echo {marker}yes; else echo {marker}no; fi",
    )
    if result.returncode != 0:
        raise HostServiceError("cannot inspect remote service state: " + result.stdout.strip())
    for line in result.stdout.splitlines():
        if line == marker + "yes":
            return True
        if line == marker + "no":
            return False
    raise HostServiceError("remote service-state response is incomplete")


def remote_integration_ownership(
    ctx: Context,
    spec: HostServiceSpec,
    backend: str,
    integration: str,
) -> str:
    """Classify startup integration ownership without conflating layouts."""

    if backend in {"sysv", "openrc"}:
        owner = remote_ownership(ctx, integration, managed_file_test(integration))
    else:
        owner = remote_ownership(ctx, integration, hook_block_test(integration))
    if owner == "managed":
        if not remote_boolean(
            ctx,
            integration_binding_test(backend, integration, spec.launcher),
        ):
            return "foreign-managed"
        return "managed"
    if backend not in {"sysv", "openrc"} and owner == "unmanaged":
        # An ordinary regular hook is available for an MSYS block and remains
        # user-owned.  Symlinks/non-regular files and malformed MSYS markers
        # are not safe to edit.
        if not remote_boolean(ctx, regular_file_test(integration)):
            return "unmanaged"
        if remote_boolean(ctx, hook_marker_presence_test(integration)):
            return "malformed-managed"
        return "available"
    return owner


def host_service_status_issues(
    state: dict[str, str] | None,
    launcher_owner: str,
    integration_owner: str,
    enabled: bool,
) -> list[str]:
    issues: list[str] = []
    if state is None:
        issues.append("installation-state-absent")
    if launcher_owner != "managed":
        issues.append(f"launcher-{launcher_owner}")
    integration_issue = {
        "absent": "startup-integration-absent",
        "available": "startup-integration-absent",
        "foreign-managed": "startup-integration-bound-to-another-launcher",
        "malformed-managed": "startup-integration-markers-malformed",
        "unmanaged": "startup-integration-unmanaged",
    }.get(integration_owner)
    if integration_issue is not None:
        issues.append(integration_issue)
    if not enabled:
        issues.append("startup-integration-disabled")
    return issues


def upload_host_service_files(
    ctx: Context,
    spec: HostServiceSpec,
    files: dict[str, str],
) -> dict[str, str]:
    ssh(ctx, f"mkdir -p {quote_sh(spec.service_dir)}")
    nonce = f"{os.getpid()}-{time.time_ns()}"
    remote_files: dict[str, str] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="msys-host-service-") as temporary:
            local_root = Path(temporary)
            for label, content in files.items():
                local = local_root / label
                local.write_text(content, encoding="utf-8", newline="\n")
                remote = f"{spec.service_dir}/.{label}.{nonce}.incoming"
                run_local([*scp_base_args(ctx), str(local), f"{ctx.target}:{remote}"])
                remote_files[label] = remote
    except BaseException:
        cleanup_host_service_uploads(ctx, remote_files)
        raise
    return remote_files


def cleanup_host_service_uploads(
    ctx: Context, remote_files: dict[str, str]
) -> None:
    if not remote_files:
        return
    command = "rm -f " + " ".join(quote_sh(path) for path in remote_files.values())
    ssh(ctx, command, check=False)


def command_host_service_detect(ctx: Context) -> int:
    detected = detect_host_service_backends(ctx)
    print_json({"detected": detected, "auto": select_backend("auto", detected) if detected else None})
    return 0


def command_host_service_install(
    ctx: Context,
    spec: HostServiceSpec,
    backend_name: str,
    hook: str | None,
    *,
    dry_run: bool,
    start_now: bool,
) -> int:
    backend, integration, _state = resolve_host_service(
        ctx, spec, backend_name, hook
    )

    if spec.release_root is not None and _state is None and spec.root != ctx.remote:
        development_spec = HostServiceSpec(
            root=ctx.remote,
            python=ctx.remote_python,
            runtime_dir=spec.runtime_dir,
            state_dir=spec.state_dir,
            log_file=spec.log_file,
            profile=spec.profile,
        )
        development_state = read_host_service_state(ctx, development_spec)
        if development_state is not None:
            validate_state_binding(development_spec, development_state)
            raise HostServiceError(
                "a development-tree host service is still installed; uninstall it "
                "without --release-root before installing the formal release service"
            )

    launcher_owner = remote_ownership(
        ctx, spec.launcher, managed_file_test(spec.launcher)
    )
    if launcher_owner == "unmanaged":
        raise HostServiceError(f"refusing to overwrite unmanaged {spec.launcher}")
    integration_owner = remote_integration_ownership(
        ctx, spec, backend, integration
    )
    if integration_owner == "foreign-managed":
        raise HostServiceError(
            f"refusing to replace managed {integration}: it is bound to another "
            "host-service launcher; uninstall that layout first"
        )
    if integration_owner == "malformed-managed":
        raise HostServiceError(
            f"refusing to repair malformed MSYS markers in {integration} automatically"
        )
    if integration_owner == "unmanaged":
        raise HostServiceError(f"refusing to overwrite unmanaged {integration}")

    if dry_run:
        summary = dry_run_summary(
            "install", spec, backend, integration, start_now=start_now
        )
        summary["current"] = {
            "installation_state": "managed" if _state is not None else "absent",
            "launcher": launcher_owner,
            "startup_integration": integration_owner,
        }
        print_json(summary)
        print(dry_run_text(spec, backend))
        return 0

    if spec.release_root is not None:
        try:
            release_state = _release_status_document(ctx, spec.release_root)
        except RuntimeError as exc:
            raise HostServiceError(str(exc)) from exc
        current_release = release_state.get("current")
        if not isinstance(current_release, str) or not current_release:
            raise HostServiceError(
                f"formal release root has no active current release: {spec.release_root}"
            )
        verified = _remote_release_command(
            ctx, spec.release_root, "verify", [current_release]
        )
        if verified.returncode != 0:
            raise HostServiceError(
                "active formal release failed verification: "
                + verified.stdout.strip()
            )

    ssh(ctx, prerequisite_command(backend, integration))
    contents = {
        "launcher": render_launcher(spec),
        "state": render_state(spec, backend, integration),
    }
    if backend == "sysv":
        contents["integration"] = render_sysv(spec)
    elif backend == "openrc":
        contents["integration"] = render_openrc(spec)
    remote_files: dict[str, str] = {}
    try:
        remote_files = upload_host_service_files(ctx, spec, contents)
        ssh(
            ctx,
            atomic_install_command(remote_files["launcher"], spec.launcher, "755"),
        )
        if backend in {"sysv", "openrc"}:
            ssh(
                ctx,
                atomic_install_command(
                    remote_files["integration"], integration, "755"
                ),
            )
        else:
            ssh(ctx, hook_edit_command(integration, spec.launcher, install=True))
        ssh(
            ctx,
            atomic_install_command(remote_files["state"], spec.state_file, "600"),
        )
        if backend in {"sysv", "openrc"}:
            ssh(ctx, host_enable_command(backend))
        if start_now:
            ssh(ctx, f"{quote_sh(spec.launcher)} start")
    finally:
        cleanup_host_service_uploads(ctx, remote_files)
    print_json(
        {
            "installed": True,
            "backend": backend,
            "integration": integration,
            "launcher": spec.launcher,
            "started": start_now,
        }
    )
    return 0


def command_host_service_uninstall(
    ctx: Context,
    spec: HostServiceSpec,
    backend_name: str,
    hook: str | None,
    *,
    dry_run: bool,
) -> int:
    backend, integration, state = resolve_host_service(
        ctx, spec, backend_name, hook
    )
    launcher_owner = remote_ownership(
        ctx, spec.launcher, managed_file_test(spec.launcher)
    )
    if launcher_owner == "unmanaged":
        raise HostServiceError(f"refusing to execute or remove unmanaged {spec.launcher}")
    integration_owner = remote_integration_ownership(
        ctx, spec, backend, integration
    )
    if integration_owner == "foreign-managed":
        raise HostServiceError(
            f"refusing to disable managed {integration}: it is bound to another "
            "host-service launcher"
        )
    if integration_owner == "malformed-managed":
        raise HostServiceError(
            f"refusing to edit malformed MSYS markers in {integration}"
        )
    if integration_owner == "unmanaged":
        raise HostServiceError(f"refusing to disable unmanaged {integration}")

    if dry_run:
        summary = dry_run_summary("uninstall", spec, backend, integration)
        summary["current"] = {
            "installation_state": "managed" if state is not None else "absent",
            "launcher": launcher_owner,
            "startup_integration": integration_owner,
        }
        print_json(summary)
        return 0

    if launcher_owner == "managed":
        ssh(ctx, f"{quote_sh(spec.launcher)} stop", check=False)
    if backend in {"sysv", "openrc"}:
        if integration_owner == "managed":
            ssh(ctx, host_disable_command(backend))
            ssh(ctx, managed_remove_command(integration))
    else:
        if integration_owner == "managed":
            ssh(ctx, hook_edit_command(integration, spec.launcher, install=False))

    if state is not None:
        ssh(ctx, managed_remove_command(spec.state_file))
    if launcher_owner == "managed":
        ssh(ctx, managed_remove_command(spec.launcher))
    if state is not None or launcher_owner == "managed" or integration_owner == "managed":
        ssh(ctx, f"rmdir {quote_sh(spec.service_dir)} 2>/dev/null || :", check=False)
    print_json(
        {
            "installed": False,
            "backend": backend,
            "integration": integration,
        }
    )
    return 0


def command_host_service_status(
    ctx: Context,
    spec: HostServiceSpec,
    backend_name: str,
    hook: str | None,
) -> int:
    try:
        backend, integration, state = resolve_host_service(
            ctx, spec, backend_name, hook
        )
    except HostServiceError as exc:
        print_json({"installed": False, "message": str(exc)})
        return 4
    launcher_owner = remote_ownership(
        ctx, spec.launcher, managed_file_test(spec.launcher)
    )
    integration_owner = remote_integration_ownership(
        ctx, spec, backend, integration
    )
    enabled = (
        remote_boolean(ctx, enabled_test_command(backend))
        if backend in {"sysv", "openrc"}
        else integration_owner == "managed"
    )
    installed = (
        state is not None
        and launcher_owner == "managed"
        and integration_owner == "managed"
        and enabled
    )
    print_json(
        {
            "installed": installed,
            "backend": backend,
            "integration": integration,
            "expected_launcher": spec.launcher,
            "installation_state": "managed" if state is not None else "absent",
            "launcher": launcher_owner,
            "startup_integration": integration_owner,
            "enabled": enabled,
            "issues": host_service_status_issues(
                state, launcher_owner, integration_owner, enabled
            ),
        }
    )
    if not installed:
        return 4
    result = ssh_capture(ctx, f"{quote_sh(spec.launcher)} status")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return result.returncode


def _bounded_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not (0 < timeout <= 600):
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and at most 600 seconds"
        )
    return timeout


def _bounded_release_health_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "release health timeout must be a number"
        ) from exc
    if not 10 <= timeout <= 180:
        raise argparse.ArgumentTypeError(
            "release health timeout must be between 10 and 180 seconds"
        )
    return timeout


def _bounded_notify_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("notification timeout must be an integer") from exc
    if not 500 <= timeout <= 6000:
        raise argparse.ArgumentTypeError(
            "notification timeout must be between 500 and 6000 milliseconds"
        )
    return timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msys-dev")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root")
    common.add_argument("--target")
    common.add_argument("--remote")
    sub = parser.add_subparsers(dest="command", required=True)

    app = sub.add_parser(
        "app",
        help="create or validate, build, install and start an application",
    )
    app_sub = app.add_subparsers(dest="app_command", required=True)
    app_new = app_sub.add_parser(
        "new",
        parents=[common],
        help="create an offline MSYS application scaffold",
    )
    app_new.add_argument("path")
    app_new.add_argument("--id", required=True, dest="package_id")
    app_new.add_argument("--template", required=True, choices=TEMPLATES)
    app_new.add_argument("--name")
    app_new.add_argument("--version", default="0.1.0")
    app_new.add_argument("--component", default="main")
    app_run = app_sub.add_parser(
        "run",
        parents=[common],
        help="reuse package validation/delivery and start one exact component",
    )
    app_run.add_argument("path", nargs="?", default=".")
    app_run.add_argument("--output")
    app_run.add_argument("--component")
    app_run.add_argument("--no-start", action="store_true")
    app_run.add_argument("--force", action="store_true")
    app_run.add_argument(
        "--format",
        choices=["tar.gz", "maf"],
        default="tar.gz",
    )
    app_run.add_argument("--source-date-epoch", type=int)
    app_run.add_argument("--manifest")
    app_run.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="SOURCE=RELATIVE_DEST",
    )
    app_run.add_argument("--runtime-dir")
    app_run.add_argument("--state-dir")

    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("--target")
    config_set.add_argument("--root")
    config_set.add_argument("--remote")
    config_set.add_argument("--runtime-dir")
    config_set.add_argument("--log-file")
    config_set.add_argument("--state-dir")
    config_set.add_argument("--profile")
    config_set.add_argument("--remote-python")
    config_set.add_argument("--ssh-key")
    config_set.add_argument("--ssh-control-path")
    config_set.add_argument("--ssh-control-persist")
    config_set.add_argument("--repo", action="append", dest="repos")
    config_unset = config_sub.add_parser("unset")
    config_unset.add_argument(
        "keys",
        nargs="+",
        choices=[
            "root", "target", "remote", "runtime-dir", "log-file", "state-dir", "profile",
            "remote-python", "ssh-key", "ssh-control-path",
            "ssh-control-persist", "repos",
        ],
    )

    doctor = sub.add_parser("doctor", parents=[common])
    doctor.add_argument("--profile")

    setup_key = sub.add_parser("setup-key", parents=[common])
    setup_key.add_argument("--key")
    sub.add_parser("ssh-reset", parents=[common])
    sub.add_parser(
        "ssh-warm",
        parents=[common],
        help="authenticate once and keep the configured SSH control connection ready",
    )

    runtime = sub.add_parser("runtime", parents=[common])
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_sub.add_parser("status")
    runtime_fetch = runtime_sub.add_parser("fetch")
    runtime_fetch.add_argument("--version", default=DEFAULT_PYTHON_STANDALONE_VERSION)
    runtime_fetch.add_argument("--arch", default="aarch64")
    runtime_fetch.add_argument("--cache-dir", default=str(DEFAULT_RUNTIME_CACHE))
    runtime_fetch.add_argument("--asset-url")
    runtime_fetch.add_argument("--tag", default=DEFAULT_PYTHON_STANDALONE_TAG)
    runtime_bootstrap = runtime_sub.add_parser("bootstrap")
    runtime_bootstrap.add_argument("--version", default=DEFAULT_PYTHON_STANDALONE_VERSION)
    runtime_bootstrap.add_argument("--arch", default="aarch64")
    runtime_bootstrap.add_argument("--cache-dir", default=str(DEFAULT_RUNTIME_CACHE))
    runtime_bootstrap.add_argument("--asset-url")
    runtime_bootstrap.add_argument("--tag", default=DEFAULT_PYTHON_STANDALONE_TAG)
    runtime_install = runtime_sub.add_parser("install")
    runtime_install.add_argument("--archive", required=True)
    runtime_install.add_argument("--remote-python")
    runtime_make = runtime_sub.add_parser("make")
    runtime_make.add_argument("--source", required=True)
    runtime_make.add_argument("--output", required=True)

    sync = sub.add_parser("sync", parents=[common])
    sync.add_argument("--repo", action="append", dest="repos")
    sync.add_argument(
        "--full-sync",
        action="store_true",
        help="upload/rebuild even when the remote source fingerprint matches",
    )
    sync.add_argument(
        "--native-audio-manager",
        action="store_true",
        help=(
            "opt in to target-building the candidate audio manager with the "
            "already-synchronized remote msys-sdk"
        ),
    )

    quick = sub.add_parser(
        "quick",
        aliases=["deploy"],
        parents=[common],
        help="sync/build selected sources, then start and wait for the runtime",
    )
    quick.add_argument("--repo", action="append", dest="repos")
    quick.add_argument(
        "--full-sync",
        action="store_true",
        help="upload/rebuild even when the remote source fingerprint matches",
    )
    quick.add_argument("--native-audio-manager", action="store_true")
    quick.add_argument(
        "--safe",
        action="store_true",
        help="run the full doctor gate before synchronization",
    )
    quick.add_argument("--profile")
    quick.add_argument("--runtime-dir")
    quick.add_argument("--log-file")
    quick.add_argument(
        "--status",
        action="store_true",
        dest="status_only",
        help="query the existing runtime after sync instead of starting one",
    )
    quick.add_argument(
        "--screenshot",
        nargs="?",
        const="",
        metavar="PATH",
        help="capture after successful run/status; omit PATH for a timestamped PNG",
    )
    quick.add_argument("--display")
    quick.add_argument(
        "--backend", choices=["auto", "scrot", "ffmpeg"], default="auto"
    )
    quick.add_argument(
        "--timeout",
        type=_bounded_timeout,
        default=DEFAULT_RUN_TIMEOUT,
        help="run readiness and screenshot timeout (default: 45 seconds)",
    )
    quick.add_argument(
        "--force", action="store_true", help="replace an existing screenshot output"
    )

    fast = sub.add_parser(
        "fast",
        aliases=["q"],
        parents=[common],
        help=(
            "sync selected sources, optionally deliver/run explicitly, then fetch a "
            "compact health/log/screenshot bundle in one SSH execution"
        ),
    )
    fast.add_argument("--repo", action="append", dest="repos")
    fast.add_argument("--full-sync", action="store_true")
    fast.add_argument(
        "--native-audio-manager",
        action="store_true",
        help=(
            "opt in to target-building and delivering the candidate native audio "
            "manager; the default audio flow remains bootstrap-only"
        ),
    )
    fast.add_argument("--safe", action="store_true")
    fast.add_argument("--profile")
    fast.add_argument("--runtime-dir")
    fast.add_argument("--state-dir")
    fast.add_argument("--log-file")
    fast.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="SOURCE=DEST",
        help=(
            "add a package overlay for --deliver; repeat for multiple overlays "
            "(explicit overlays require exactly one repo; canonical Settings/Apps/"
            "Input default to their required SDK overlay)"
        ),
    )
    fast_action = fast.add_mutually_exclusive_group()
    fast_action.add_argument(
        "--run",
        action="store_true",
        help="explicitly start a stopped development msysd (never the default)",
    )
    fast_action.add_argument(
        "--deliver",
        action="store_true",
        help="build and transactionally install selected packages in repository order",
    )
    fast.add_argument(
        "--logs",
        type=int,
        default=80,
        metavar="LINES",
        help="maximum recent warning/error lines in the bundle (default: 80)",
    )
    fast.add_argument(
        "--no-logs", action="store_const", const=0, dest="logs"
    )
    fast.add_argument("--screenshot", nargs="?", const="", metavar="PATH")
    fast.add_argument("--display")
    fast.add_argument(
        "--backend", choices=["auto", "scrot", "ffmpeg"], default="auto"
    )
    fast.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)
    fast.add_argument("--force", action="store_true")
    fast.add_argument(
        "--json",
        action="store_true",
        help="emit the bounded structured summary instead of concise text",
    )
    fast.add_argument(
        "--audio",
        action="store_true",
        help=(
            "include audio-manager role state and private BlueZ/BlueALSA process RSS "
            "in the same one-SSH report"
        ),
    )

    audio_debug = sub.add_parser(
        "audio-debug",
        aliases=["audio-accept"],
        parents=[common],
        help=(
            "read-only one-SSH audio acceptance: release, component, role state, "
            "stack RSS, logs, and optional screenshot"
        ),
    )
    audio_debug.add_argument("--runtime-dir")
    audio_debug.add_argument("--log-file")
    audio_debug.add_argument(
        "--logs",
        type=int,
        default=80,
        metavar="LINES",
        help="maximum recent warning/error lines in the bundle (default: 80)",
    )
    audio_debug.add_argument("--no-logs", action="store_const", const=0, dest="logs")
    audio_debug.add_argument("--screenshot", nargs="?", const="", metavar="PATH")
    audio_debug.add_argument("--display")
    audio_debug.add_argument(
        "--backend", choices=["auto", "scrot", "ffmpeg"], default="auto"
    )
    audio_debug.add_argument(
        "--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT
    )
    audio_debug.add_argument("--force", action="store_true")
    audio_debug.add_argument(
        "--json",
        action="store_true",
        help="emit the bounded structured summary instead of concise text",
    )

    storage = sub.add_parser(
        "storage",
        aliases=["storage-clean"],
        parents=[common],
        help=(
            "inventory disk/USB/reclaimable data in one SSH; --apply archives to "
            "USB and verifies SHA-256 before deleting strict whitelist candidates"
        ),
    )
    storage.add_argument("--dev-root")
    storage.add_argument("--state-dir")
    storage.add_argument("--release-root", default=DEFAULT_SYSTEM_RELEASE_ROOT)
    storage.add_argument("--usb-root", default="/mnt/msys-usb")
    storage.add_argument("--log-file")
    storage.add_argument(
        "--apply",
        action="store_true",
        help="apply the reported whitelist cleanup after a verified USB archive",
    )
    storage.add_argument(
        "--no-archive",
        action="store_true",
        help="with --apply only, explicitly delete without creating a USB archive",
    )
    storage.add_argument("--json", action="store_true")

    accept = sub.add_parser(
        "accept",
        parents=[common],
        help=(
            "read-only runtime acceptance for UI components, windows, display, "
            "resources, logs, and an optional screenshot in one SSH execution"
        ),
    )
    accept.add_argument("--runtime-dir")
    accept.add_argument("--log-file")
    accept.add_argument(
        "--logs",
        type=int,
        default=80,
        metavar="LINES",
        help="maximum recent warning/error lines (default: 80)",
    )
    accept.add_argument("--no-logs", action="store_const", const=0, dest="logs")
    accept.add_argument(
        "--strict-logs",
        action="store_true",
        help="fail acceptance when the bounded log scan contains a warning/error",
    )
    accept.add_argument(
        "--expect-window",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="require an exact component, identity, role, or title window match",
    )
    accept.add_argument("--screenshot", nargs="?", const="", metavar="PATH")
    accept.add_argument("--display")
    accept.add_argument(
        "--backend", choices=["auto", "scrot", "ffmpeg"], default="auto"
    )
    accept.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)
    accept.add_argument("--force", action="store_true")
    accept.add_argument("--json", action="store_true")

    fallback_manifests = sub.add_parser(
        "fallback-manifests",
        parents=[common],
        help="generate Core development fallbacks from canonical manifests",
    )
    fallback_manifests.add_argument(
        "--check",
        action="store_true",
        help="report semantic drift without changing files",
    )

    sync_x11 = sub.add_parser(
        "sync-x11display",
        parents=[common],
        help="build x11display in remote staging and atomically activate it",
    )
    sync_x11.add_argument("--local", default="x11display")
    sync_x11.add_argument("--destination", default="/root/x11display")

    run_cmd = sub.add_parser("run", parents=[common])
    run_cmd.add_argument("--profile")
    run_cmd.add_argument("--runtime-dir")
    run_cmd.add_argument("--log-file")
    run_cmd.add_argument("--remote-python")
    run_cmd.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)

    stop = sub.add_parser("stop", parents=[common])
    stop.add_argument("--runtime-dir")
    stop.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_STOP_TIMEOUT)

    tail = sub.add_parser("tail", parents=[common])
    tail.add_argument("--log-file")

    debug = sub.add_parser(
        "debug",
        parents=[common],
        help="print one remote runtime snapshot and recent logs through one SSH session",
    )
    debug.add_argument("--runtime-dir")
    debug.add_argument("--log-file")
    debug.add_argument(
        "--lines",
        type=int,
        default=80,
        help="recent log lines to include (1-1000, default: 80)",
    )
    debug.add_argument(
        "--follow",
        action="store_true",
        help="continue following the log through the same SSH session",
    )

    status = sub.add_parser("status", parents=[common])
    status.add_argument("--runtime-dir")

    components = sub.add_parser("components", parents=[common])
    components.add_argument("--runtime-dir")
    components.add_argument("--json", action="store_true")

    roles = sub.add_parser("roles", parents=[common])
    roles.add_argument("--runtime-dir")
    roles.add_argument("--json", action="store_true")

    discover = sub.add_parser(
        "discover",
        parents=[common],
        help="discover installed mIPC interfaces and capabilities",
    )
    discover.add_argument("--kind", choices=["interface", "capability"])
    discover.add_argument("--name")
    discover.add_argument("--runtime-dir")

    call = sub.add_parser(
        "call",
        parents=[common],
        help="call a role, interface, exact component, or msys.core",
    )
    call.add_argument("call_target")
    call.add_argument("method")
    call_payload = call.add_mutually_exclusive_group()
    call_payload.add_argument(
        "--payload",
        help="complete JSON object (legacy; shell quoting rules apply)",
    )
    call_payload.add_argument(
        "--field",
        action="append",
        default=[],
        dest="call_fields",
        metavar="KEY=VALUE",
        help=(
            "add one payload field without object JSON; repeat as needed "
            "(dotted keys nest up to 4 levels; JSON literals are typed)"
        ),
    )
    call.add_argument("--timeout", type=float, default=30.0)
    call.add_argument("--idempotent", action="store_true")
    call.add_argument("--runtime-dir")

    role_cmd = sub.add_parser("role", parents=[common])
    role_cmd.add_argument("--runtime-dir")
    role_sub = role_cmd.add_subparsers(dest="role_command", required=True)
    role_select = role_sub.add_parser("select")
    role_select.add_argument("role")
    role_select.add_argument("provider")
    role_select.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)
    role_reset = role_sub.add_parser("reset")
    role_reset.add_argument("role")
    role_reset.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)

    start_component = sub.add_parser("start-component", parents=[common])
    start_component.add_argument("component")
    start_component.add_argument("--runtime-dir")

    activate = sub.add_parser("activate", parents=[common])
    activate.add_argument("--action")
    activate.add_argument("--uri")
    activate.add_argument("--mime")
    activate.add_argument("--name")
    activate.add_argument("--component")
    activate.add_argument("--runtime-dir")

    stop_component = sub.add_parser("stop-component", parents=[common])
    stop_component.add_argument("component")
    stop_component.add_argument("--runtime-dir")

    broadcast = sub.add_parser("broadcast", parents=[common])
    broadcast.add_argument("topic")
    broadcast.add_argument("--payload", default="{}")
    broadcast.add_argument("--runtime-dir")

    install_dir_cmd = sub.add_parser("install-dir", parents=[common])
    install_dir_cmd.add_argument("package_dir")
    install_dir_cmd.add_argument("--runtime-dir")
    install_dir_cmd.add_argument("--legacy-events", action="store_true")

    install_archive_cmd = sub.add_parser("install-archive", parents=[common])
    install_archive_cmd.add_argument("archive")
    install_archive_cmd.add_argument("--runtime-dir")
    install_archive_cmd.add_argument("--state-dir")
    install_archive_cmd.add_argument("--legacy-events", action="store_true")

    registry_cmd = sub.add_parser("registry", parents=[common])
    registry_cmd.add_argument("--runtime-dir")
    registry_cmd.add_argument("--legacy-events", action="store_true")

    update_cmd = sub.add_parser("check-update", parents=[common])
    update_cmd.add_argument("source")
    update_cmd.add_argument("--package")
    update_cmd.add_argument("--allow-downgrade", action="store_true")
    update_cmd.add_argument("--allow-unsigned", action="store_true")
    update_cmd.add_argument("--runtime-dir")
    update_cmd.add_argument("--legacy-events", action="store_true")

    apply_update_cmd = sub.add_parser("apply-update", parents=[common])
    apply_update_cmd.add_argument("source")
    apply_update_cmd.add_argument("--package")
    apply_update_cmd.add_argument("--allow-downgrade", action="store_true")
    apply_update_cmd.add_argument("--allow-unsigned", action="store_true")
    apply_update_cmd.add_argument("--runtime-dir")
    apply_update_cmd.add_argument("--legacy-events", action="store_true")

    update_trust = sub.add_parser(
        "update-trust",
        help="manage Ed25519 update publisher keys without uploading private keys",
    )
    update_trust_sub = update_trust.add_subparsers(
        dest="update_trust_command", required=True
    )
    trust_generate = update_trust_sub.add_parser("generate", parents=[common])
    trust_generate.add_argument("--private", required=True)
    trust_generate.add_argument("--public", required=True)
    trust_generate.add_argument("--force", action="store_true")
    trust_sign = update_trust_sub.add_parser("sign-index", parents=[common])
    trust_sign.add_argument("index")
    trust_sign.add_argument("--private", required=True)
    trust_sign.add_argument("--sequence", required=True, type=int)
    trust_sign.add_argument("--expires", required=True)
    trust_sign.add_argument("--output")
    trust_sign.add_argument("--force", action="store_true")
    trust_install = update_trust_sub.add_parser("install-public", parents=[common])
    trust_install.add_argument("public_key")
    trust_install.add_argument("--state-dir")

    package_cmd = sub.add_parser(
        "package",
        help="validate, build, deliver, uninstall, or roll back an MSYS package",
    )
    package_sub = package_cmd.add_subparsers(dest="package_command", required=True)

    package_validate = package_sub.add_parser(
        "validate",
        parents=[common],
        help="validate a manifest, package directory, or archive",
    )
    package_validate.add_argument("path")
    package_validate.add_argument("--require-content-hashes", action="store_true")
    package_validate.add_argument(
        "--manifest",
        help="manifest relative to a source directory (auto-detected by default)",
    )

    package_discover = package_sub.add_parser(
        "discover",
        parents=[common],
        help="discover and strictly validate language-neutral manifests",
    )
    package_discover.add_argument(
        "path",
        nargs="?",
        help="file or directory to scan (default: workspace root)",
    )

    package_build = package_sub.add_parser(
        "build",
        parents=[common],
        help="build a verified content-hashed tar.gz or MAF archive",
    )
    package_build.add_argument("package_dir")
    package_build.add_argument(
        "--output",
        help="archive path or directory (default: <package-parent>/dist)",
    )
    package_build.add_argument("--force", action="store_true")
    package_build.add_argument(
        "--format",
        choices=["tar.gz", "maf"],
        default="tar.gz",
        help="container filename format (default: tar.gz; MAF is deterministic tar+gzip)",
    )
    package_build.add_argument("--source-date-epoch", type=int)
    package_build.add_argument(
        "--manifest",
        help="manifest relative to a source directory (auto-detected by default)",
    )
    package_build.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="SOURCE=RELATIVE_DEST",
        help="vendor a file/tree into a new package-relative destination before hashing",
    )

    package_deliver = package_sub.add_parser(
        "deliver",
        parents=[common],
        help="build, verify, upload, and request atomic install of one source package",
    )
    package_deliver.add_argument("package_dir")
    package_deliver.add_argument("--output")
    package_deliver.add_argument("--force", action="store_true")
    package_deliver.add_argument(
        "--format",
        choices=["tar.gz", "maf"],
        default="tar.gz",
        help="container filename format (default: tar.gz)",
    )
    package_deliver.add_argument("--source-date-epoch", type=int)
    package_deliver.add_argument("--manifest")
    package_deliver.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="SOURCE=RELATIVE_DEST",
        help="vendor a file/tree into a new package-relative destination before hashing",
    )
    package_deliver.add_argument("--runtime-dir")
    package_deliver.add_argument("--state-dir")
    package_deliver.add_argument("--legacy-events", action="store_true")

    package_index = package_sub.add_parser(
        "index",
        parents=[common],
        help="build a verified msys.update-index.v1",
    )
    package_index.add_argument("repository")
    package_index.add_argument("--output")
    package_index.add_argument("--base-url")

    package_rollback = package_sub.add_parser(
        "rollback",
        parents=[common],
        help="request an atomic remote package rollback",
    )
    package_rollback.add_argument("package_id")
    package_rollback.add_argument("--runtime-dir")
    package_rollback.add_argument("--legacy-events", action="store_true")

    package_roundtrip = package_sub.add_parser(
        "roundtrip",
        parents=[common],
        help=(
            "accept the real package previous pointer by rolling back and then "
            "restoring the exact current version and hashes"
        ),
    )
    package_roundtrip.add_argument("package_id")
    package_roundtrip.add_argument("--runtime-dir")

    package_uninstall = package_sub.add_parser(
        "uninstall",
        parents=[common],
        help="request a health-checked remote package uninstall",
    )
    package_uninstall.add_argument("package_id")
    package_uninstall.add_argument("--runtime-dir")
    package_uninstall.add_argument("--legacy-events", action="store_true")

    release = sub.add_parser(
        "release",
        help="stage and atomically switch whole-system releases",
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_common = argparse.ArgumentParser(add_help=False, parents=[common])
    release_common.add_argument(
        "--release-root",
        default=DEFAULT_SYSTEM_RELEASE_ROOT,
        help="formal deployment root containing releases/current/service",
    )
    release_compose = release_sub.add_parser(
        "compose",
        parents=[release_common],
        help="compose an immutable stage source without switching the live release",
    )
    release_compose.add_argument("release_id")
    release_compose.add_argument("--baseline-release", required=True)
    release_compose.add_argument("--workspace-root")
    release_compose.add_argument("--output-root")
    release_compose.add_argument(
        "--python-runtime",
        help="complete target-built Tk/Xft Python runtime to embed in the release",
    )
    release_compose.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="NAME=REMOTE_PATH",
        help="override a synchronized source entry",
    )
    release_compose.add_argument(
        "--maf",
        action="append",
        default=[],
        metavar="NAME=REMOTE_MAF",
        help="map each built-in entry to one fully hashed MAF",
    )
    release_stage = release_sub.add_parser(
        "stage",
        parents=[release_common],
        help="copy an immutable release from the synchronized target workspace",
    )
    release_stage.add_argument("release_id")
    release_stage.add_argument("--source-root")
    release_stage.add_argument("--entry", action="append", dest="entries")
    release_stage.add_argument("--keep", type=int, default=3)
    release_stage.add_argument("--activate", action="store_true")
    release_stage.add_argument("--restart-service", action="store_true")
    release_stage.add_argument("--runtime-dir")
    release_stage.add_argument("--log-file")
    release_stage.add_argument(
        "--health-timeout",
        type=_bounded_release_health_timeout,
        default=DEFAULT_RELEASE_HEALTH_TIMEOUT,
        help="candidate and recovery readiness deadline in seconds (default: 90)",
    )
    release_activate = release_sub.add_parser(
        "activate",
        parents=[release_common],
        help="atomically select a verified release",
    )
    release_activate.add_argument("release_id")
    release_activate.add_argument("--restart-service", action="store_true")
    release_activate.add_argument("--runtime-dir")
    release_activate.add_argument("--log-file")
    release_activate.add_argument(
        "--health-timeout",
        type=_bounded_release_health_timeout,
        default=DEFAULT_RELEASE_HEALTH_TIMEOUT,
        help="candidate and recovery readiness deadline in seconds (default: 90)",
    )
    release_rollback = release_sub.add_parser(
        "rollback",
        parents=[release_common],
        help="atomically exchange current and previous releases",
    )
    release_rollback.add_argument("--restart-service", action="store_true")
    release_rollback.add_argument("--runtime-dir")
    release_rollback.add_argument("--log-file")
    release_rollback.add_argument(
        "--health-timeout",
        type=_bounded_release_health_timeout,
        default=DEFAULT_RELEASE_HEALTH_TIMEOUT,
        help="candidate and recovery readiness deadline in seconds (default: 90)",
    )
    release_sub.add_parser("status", parents=[release_common])
    release_verify = release_sub.add_parser("verify", parents=[release_common])
    release_verify.add_argument("release_id")
    release_repair = release_sub.add_parser(
        "repair-python-cache",
        parents=[release_common],
        help="preview or repair only digest-proven post-release CPython caches",
    )
    release_repair.add_argument("release_id")
    release_repair.add_argument("--apply", action="store_true")
    release_repair.add_argument(
        "--backup",
        help="absolute target path for the required cache backup archive",
    )
    release_prune = release_sub.add_parser("prune", parents=[release_common])
    release_prune.add_argument("--keep", type=int, default=3)
    release_sub.add_parser("recover", parents=[release_common])

    host_service = sub.add_parser(
        "host-service",
        help="install MSYS as a normal non-PID1 host service",
    )
    host_sub = host_service.add_subparsers(dest="host_service_command", required=True)
    host_sub.add_parser(
        "detect",
        parents=[common],
        help="detect supported non-systemd startup mechanisms",
    )
    host_common = argparse.ArgumentParser(add_help=False, parents=[common])
    host_common.add_argument("--backend", choices=HOST_SERVICE_BACKENDS, default="auto")
    host_common.add_argument("--hook", help="absolute startup hook for --backend hook")
    host_common.add_argument("--profile")
    host_common.add_argument("--runtime-dir")
    host_common.add_argument("--state-dir")
    host_common.add_argument("--log-file")
    host_common.add_argument("--remote-python")
    host_common.add_argument(
        "--release-root",
        help="launch /current from a formal release root instead of the development tree",
    )
    host_install = host_sub.add_parser(
        "install",
        parents=[host_common],
        help="install and enable the selected host startup integration",
    )
    host_install.add_argument("--start-now", action="store_true")
    host_install.add_argument("--dry-run", action="store_true")
    host_uninstall = host_sub.add_parser(
        "uninstall",
        parents=[host_common],
        help="disable and remove only managed host-service files",
    )
    host_uninstall.add_argument("--dry-run", action="store_true")
    host_sub.add_parser(
        "status",
        parents=[host_common],
        help="show installation and process status",
    )

    wm = sub.add_parser("wm", parents=[common])
    wm.add_argument(
        "action",
        choices=[
            "home",
            "back",
            "recents",
            "list",
            "list_windows",
            "close_active",
            "focus",
            "minimize",
            "move",
            "resize",
            "move-resize",
            "close",
        ],
    )
    wm.add_argument("--window-id")
    wm.add_argument("--x", type=int)
    wm.add_argument("--y", type=int)
    wm.add_argument("--width", type=int)
    wm.add_argument("--height", type=int)
    wm.add_argument("--runtime-dir")

    layout = sub.add_parser(
        "layout",
        parents=[common],
        help="inspect or switch the active X11 layout contract",
    )
    layout.add_argument("action", choices=["show", "set"])
    layout.add_argument("--profile", choices=["mobile", "kiosk", "desktop"])
    layout.add_argument("--orientation", choices=["auto", "portrait", "landscape"])
    layout.add_argument("--insets")
    layout.add_argument("--runtime-dir")

    tap = sub.add_parser(
        "tap",
        parents=[common],
        help="inject one X11 click for remote UI-path debugging",
    )
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    tap.add_argument(
        "--identity",
        help="select an explicit stable WM identity (default: the active navigation-bar role)",
    )
    tap.add_argument("--title")
    tap.add_argument("--display")
    tap.add_argument("--runtime-dir")

    swipe = sub.add_parser(
        "swipe",
        parents=[common],
        help="inject one native X11 swipe for remote UI-path debugging",
    )
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("--duration-ms", type=int, default=220)
    swipe.add_argument(
        "--identity",
        help="select by stable WM identity (default: the active navigation-bar role)",
    )
    swipe.add_argument(
        "--title",
        help="select a legacy window by title, or refine --identity to an exact window",
    )
    swipe.add_argument(
        "--window",
        nargs=2,
        metavar=("IDENTITY", "TITLE"),
        help="select one exact window by stable identity and title",
    )
    swipe.add_argument("--display")
    swipe.add_argument("--runtime-dir")

    screenshot = sub.add_parser(
        "screenshot",
        parents=[common],
        help="capture the active target X11 display and download one PNG",
    )
    screenshot.add_argument(
        "output",
        nargs="?",
        help="workstation PNG path (default: timestamped file in the current directory)",
    )
    screenshot.add_argument("--display")
    screenshot.add_argument(
        "--backend", choices=["auto", "scrot", "ffmpeg"], default="auto"
    )
    screenshot.add_argument("--timeout", type=_bounded_timeout, default=15.0)
    screenshot.add_argument("--force", action="store_true")
    screenshot.add_argument("--runtime-dir")

    font_doctor = sub.add_parser(
        "font-doctor",
        parents=[common],
        help="verify the selected target Python/Tk runtime renders CJK through Xft",
    )
    font_doctor.add_argument(
        "--python",
        dest="font_python",
        help=(
            "absolute target Python path (default: live Core interpreter, "
            "then formal current and configured development runtimes)"
        ),
    )
    font_doctor.add_argument("--display")
    font_doctor.add_argument("--family", default="Noto Sans CJK SC")
    font_doctor.add_argument("--size", type=int, default=16)
    font_doctor.add_argument("--runtime-dir")

    visual_smoke = sub.add_parser(
        "visual-smoke",
        parents=[common],
        help="exercise typed Home/start/Back/Recents from a clean Home session",
    )
    visual_smoke.add_argument(
        "component", nargs="?", default=DEFAULT_VISUAL_SMOKE_COMPONENT
    )
    visual_smoke.add_argument("--timeout", type=_bounded_timeout, default=12.0)
    visual_smoke.add_argument("--runtime-dir")

    ui_accept = sub.add_parser(
        "ui-accept",
        aliases=["p0-ui"],
        parents=[common],
        help=(
            "run one reversible single-SSH Notes/Calculator/Device Info P0 UI "
            "acceptance route"
        ),
    )
    ui_accept.add_argument("--timeout", type=_bounded_timeout, default=12.0)
    ui_accept.add_argument(
        "--display-log", default="/tmp/ch347_dirty_usb_x11/live.log"
    )
    ui_accept.add_argument("--runtime-dir")

    notify = sub.add_parser("notify", parents=[common])
    notify.add_argument("message")
    notify.add_argument("--timeout-ms", type=_bounded_notify_timeout, default=2500)
    notify.add_argument("--runtime-dir")

    shield = sub.add_parser(
        "shield",
        parents=[common],
        help="show or hide the selected typed screen-shield provider",
    )
    shield.add_argument("action", choices=["show", "hide"])
    shield.add_argument("--timeout", type=_bounded_timeout, default=DEFAULT_RUN_TIMEOUT)
    shield.add_argument("--runtime-dir")

    debug_env = sub.add_parser("debug-env", parents=[common])
    debug_env.add_argument("--runtime-dir")

    script = sub.add_parser("install-service-script", parents=[common])
    script.add_argument("output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "config":
        return command_config(args)

    config = load_config()
    root = Path(
        args.root
        or os.environ.get("MSYS_DEV_ROOT")
        or config.get("root")
        or Path.cwd()
    ).expanduser().resolve()
    target = str(
        args.target
        or os.environ.get("MSYS_DEV_TARGET")
        or config.get("target", "")
    )
    remote = str(
        args.remote
        or os.environ.get("MSYS_DEV_REMOTE")
        or config.get("remote", "/opt/msys-dev")
    )
    remote_python = (
        getattr(args, "remote_python", None)
        or os.environ.get("MSYS_DEV_REMOTE_PYTHON")
        or config.get("remote_python")
        or f"{remote}/{DEFAULT_REMOTE_PYTHON_REL}"
    )
    ssh_key_value = (
        os.environ.get("MSYS_DEV_SSH_KEY")
        or config.get("ssh_key")
        or str(DEFAULT_KEY_PATH)
    )
    ssh_key = Path(str(ssh_key_value)).expanduser() if ssh_key_value else None
    ssh_control_path = Path(str(
        os.environ.get("MSYS_DEV_SSH_CONTROL")
        or config.get("ssh_control_path")
        or CONTROL_PATH
    )).expanduser()
    ssh_control_persist = str(
        os.environ.get("MSYS_DEV_SSH_CONTROL_PERSIST")
        or config.get("ssh_control_persist")
        or DEFAULT_SSH_CONTROL_PERSIST
    )
    if CONTROL_PERSIST_PATTERN.fullmatch(ssh_control_persist) is None:
        parser.error("SSH ControlPersist must be yes, no, or a duration such as 10m")
    local_only = (
        args.command == "install-service-script"
        or args.command == "config"
        or args.command == "fallback-manifests"
        or (args.command == "runtime" and args.runtime_command in {"fetch", "make"})
        or (
            args.command == "update-trust"
            and args.update_trust_command in {"generate", "sign-index"}
        )
        or (
            args.command == "package"
            and args.package_command in {"validate", "discover", "build", "index"}
        )
        or (args.command == "app" and args.app_command == "new")
    )
    if not local_only and not target:
        parser.error("--target or MSYS_DEV_TARGET is required")
    remote_path = PurePosixPath(remote)
    if (
        not remote_path.is_absolute()
        or remote_path == PurePosixPath("/")
        or ".." in remote_path.parts
        or any(ord(character) < 32 for character in remote)
    ):
        parser.error("--remote must be a non-root absolute POSIX path without '..'")
    if not local_only:
        try:
            ssh_control_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            parser.error(f"cannot create SSH control directory: {exc}")
    ctx = Context(
        root=root,
        target=target,
        remote=remote,
        remote_python=str(remote_python),
        ssh_key=ssh_key,
        ssh_control_path=ssh_control_path,
        ssh_control_persist=ssh_control_persist,
    )

    if args.command == "app":
        try:
            if args.app_command == "new":
                result = create_app(
                    root,
                    Path(args.path),
                    template=args.template,
                    package_id=args.package_id,
                    name=args.name,
                    version=args.version,
                    component=args.component,
                )
                print_json(result)
                return 0
            package_dir = Path(args.path)
            output = (
                Path(args.output)
                if args.output
                else package_dir.expanduser().resolve().parent / "dist"
            )
            return command_app_run(
                ctx,
                root,
                package_dir,
                output,
                runtime_dir=args.runtime_dir
                or config.get("runtime_dir", "/run/msys/main"),
                state_dir=args.state_dir
                or config.get("state_dir", "/opt/msys-state"),
                component=args.component,
                no_start=args.no_start,
                force=args.force,
                source_date_epoch=args.source_date_epoch,
                manifest_path=Path(args.manifest) if args.manifest else None,
                artifact_format=args.format,
                overlays=[parse_overlay_spec(root, item) for item in args.overlay],
            )
        except (AppFlowError, PackageFlowError) as exc:
            print(f"app: {exc}", file=sys.stderr)
            return 2

    if args.command == "doctor":
        return command_doctor(
            ctx, str(args.profile or config.get("profile", "mobile-spi"))
        )
    if args.command == "setup-key":
        return command_setup_key(ctx, Path(args.key).expanduser() if args.key else (ctx.ssh_key or DEFAULT_KEY_PATH))
    if args.command == "ssh-reset":
        return command_ssh_reset(ctx)
    if args.command == "ssh-warm":
        return command_ssh_warm(ctx)
    if args.command == "runtime":
        if args.runtime_command == "status":
            return command_runtime_status(ctx, remote_python)
        if args.runtime_command == "fetch":
            command_runtime_fetch(
                args.version,
                args.arch,
                Path(args.cache_dir).expanduser(),
                asset_url=args.asset_url,
                tag=args.tag,
            )
            return 0
        if args.runtime_command == "bootstrap":
            return command_runtime_bootstrap(
                ctx,
                args.version,
                args.arch,
                Path(args.cache_dir).expanduser(),
                asset_url=args.asset_url,
                tag=args.tag,
            )
        if args.runtime_command == "install":
            install_python = args.remote_python or remote_python
            return command_runtime_install(ctx, Path(args.archive), install_python)
        if args.runtime_command == "make":
            return command_runtime_make(Path(args.source), Path(args.output))
    if args.command == "sync":
        return command_sync(
            ctx,
            selected_sync_repositories(args.repos, config),
            force=args.full_sync,
            native_audio_manager=args.native_audio_manager,
        )
    if args.command in {"quick", "deploy"}:
        return command_quick(
            ctx,
            selected_sync_repositories(args.repos, config),
            safe=args.safe,
            profile=str(args.profile or config.get("profile", "mobile-spi")),
            runtime_dir=str(
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
            ),
            log_file=str(args.log_file or config.get("log_file", "/tmp/msysd.log")),
            status_only=args.status_only,
            screenshot=(
                screenshot_output(args.screenshot)
                if args.screenshot is not None
                else None
            ),
            display=args.display,
            backend=args.backend,
            timeout=args.timeout,
            force=args.force,
            full_sync=args.full_sync,
            **(
                {"native_audio_manager": True}
                if args.native_audio_manager
                else {}
            ),
        )
    if args.command in {"fast", "q"}:
        # A bare fast/q is a cheap diagnostic.  The Windows wrapper and the
        # persistent shell add an explicit --repo when invoked from inside a
        # repository; never expand a root-level diagnostic into a full sync.
        fast_repositories = (
            selected_sync_repositories(args.repos, config) if args.repos else []
        )
        try:
            fast_overlays = [parse_overlay_spec(root, item) for item in args.overlay]
        except PackageFlowError as exc:
            print(f"fast: invalid package overlay: {exc}", file=sys.stderr)
            return 2
        return command_fast(
            ctx,
            fast_repositories,
            safe=args.safe,
            profile=str(args.profile or config.get("profile", "mobile-spi")),
            runtime_dir=str(
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
            ),
            state_dir=str(
                args.state_dir or config.get("state_dir", "/opt/msys-state")
            ),
            log_file=str(args.log_file or config.get("log_file", "/tmp/msysd.log")),
            run=args.run,
            deliver=args.deliver,
            lines=args.logs,
            screenshot=(
                screenshot_output(args.screenshot)
                if args.screenshot is not None
                else None
            ),
            display=args.display,
            backend=args.backend,
            timeout=args.timeout,
            force=args.force,
            full_sync=args.full_sync,
            json_output=args.json,
            overlays=fast_overlays,
            audio=args.audio,
            **(
                {"native_audio_manager": True}
                if args.native_audio_manager
                else {}
            ),
        )
    if args.command in {"audio-debug", "audio-accept"}:
        return command_fast_report(
            ctx,
            str(args.runtime_dir or config.get("runtime_dir", "/run/msys/main")),
            str(args.log_file or config.get("log_file", "/tmp/msysd.log")),
            lines=args.logs,
            screenshot=(
                screenshot_output(args.screenshot)
                if args.screenshot is not None
                else None
            ),
            display=args.display,
            backend=args.backend,
            timeout=args.timeout,
            force=args.force,
            json_output=args.json,
            audio=True,
        )
    if args.command in {"storage", "storage-clean"}:
        if args.no_archive and not args.apply:
            parser.error("storage --no-archive requires explicit --apply")
        try:
            storage_dev_root = _normalise_remote_source_root(
                str(args.dev_root or remote)
            )
            storage_state_dir = _normalise_remote_source_root(
                str(args.state_dir or config.get("state_dir", "/opt/msys-state"))
            )
            storage_release_root = _normalise_remote_release_root(args.release_root)
            storage_usb_root = _normalise_remote_source_root(args.usb_root)
            storage_log_file = _normalise_remote_source_root(
                str(args.log_file or config.get("log_file", "/tmp/msysd.log"))
            )
        except ValueError as exc:
            print(f"storage: {exc}", file=sys.stderr)
            return 2
        return command_storage(
            ctx,
            storage_dev_root,
            storage_state_dir,
            storage_release_root,
            storage_usb_root,
            apply=args.apply,
            no_archive=args.no_archive,
            json_output=args.json,
            log_file=storage_log_file,
        )
    if args.command == "accept":
        return run_acceptance(
            AcceptanceConfig(
                remote=ctx.remote,
                remote_python=ctx.remote_python,
                runtime_dir=str(
                    args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
                ),
                log_file=str(
                    args.log_file or config.get("log_file", "/tmp/msysd.log")
                ),
                lines=args.logs,
                strict_logs=args.strict_logs,
                expect_windows=tuple(args.expect_window),
                screenshot=(
                    screenshot_output(args.screenshot)
                    if args.screenshot is not None
                    else None
                ),
                display=args.display,
                backend=args.backend,
                timeout=args.timeout,
                force=args.force,
                json_output=args.json,
            ),
            lambda command, label: ssh_capture_bytes(
                ctx, command, display_command=label
            ),
        )
    if args.command == "fallback-manifests":
        return command_fallback_manifests(root, check=args.check)
    if args.command == "sync-x11display":
        local_path = Path(args.local)
        if not local_path.is_absolute():
            local_path = root / local_path
        return command_sync_x11display(ctx, local_path.resolve(), args.destination)
    if args.command == "run":
        runtime_dir = args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
        log_file = args.log_file or config.get("log_file", "/tmp/msysd.log")
        profile = args.profile or config.get("profile", "mobile-spi")
        return command_run(
            ctx,
            str(profile),
            str(runtime_dir),
            str(log_file),
            str(remote_python),
            args.timeout,
        )
    if args.command == "stop":
        return command_stop(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.timeout,
        )
    if args.command == "tail":
        return command_tail(ctx, args.log_file or config.get("log_file", "/tmp/msysd.log"))
    if args.command == "debug":
        try:
            return command_debug(
                ctx,
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                args.log_file or config.get("log_file", "/tmp/msysd.log"),
                lines=args.lines,
                follow=args.follow,
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "status":
        return command_status(ctx, args.runtime_dir or config.get("runtime_dir", "/run/msys/main"))
    if args.command == "components":
        return command_components(ctx, args.runtime_dir or config.get("runtime_dir", "/run/msys/main"), args.json)
    if args.command == "roles":
        return command_roles(ctx, args.runtime_dir or config.get("runtime_dir", "/run/msys/main"), args.json)
    if args.command == "discover":
        return command_discover(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.kind,
            args.name,
        )
    if args.command == "call":
        try:
            call_payload = parse_call_payload(args.payload, args.call_fields)
        except ValueError as exc:
            parser.error(f"call: {exc}")
        return command_call(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.call_target,
            args.method,
            call_payload,
            args.timeout,
            args.idempotent,
        )
    if args.command == "role":
        runtime_dir = args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
        if args.role_command == "select":
            return command_select_role(
                ctx, runtime_dir, args.role, args.provider, args.timeout
            )
        return command_reset_role(ctx, runtime_dir, args.role, args.timeout)
    if args.command == "start-component":
        return command_start_component(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.component,
        )
    if args.command == "activate":
        return command_activate(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            action=args.action,
            uri=args.uri,
            mime=args.mime,
            name=args.name,
            component=args.component,
        )
    if args.command == "stop-component":
        return command_stop_component(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.component,
        )
    if args.command == "broadcast":
        return command_broadcast(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.topic,
            json.loads(args.payload),
        )
    if args.command == "install-dir":
        return command_install_dir(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.package_dir,
            legacy_events=args.legacy_events,
        )
    if args.command == "install-archive":
        return command_install_archive(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            Path(args.archive),
            state_dir=args.state_dir or config.get("state_dir", "/opt/msys-state"),
            legacy_events=args.legacy_events,
        )
    if args.command == "registry":
        return command_registry(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            legacy_events=args.legacy_events,
        )
    if args.command == "check-update":
        return command_check_update(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.source,
            args.package,
            args.allow_downgrade,
            args.allow_unsigned,
            legacy_events=args.legacy_events,
        )
    if args.command == "apply-update":
        return command_apply_update(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.source,
            args.package,
            args.allow_downgrade,
            args.allow_unsigned,
            legacy_events=args.legacy_events,
        )
    if args.command == "update-trust":
        try:
            if args.update_trust_command == "generate":
                result = generate_update_signing_key(
                    root,
                    Path(args.private),
                    Path(args.public),
                    force=args.force,
                )
                print_json(result)
                return 0
            if args.update_trust_command == "sign-index":
                result = sign_update_index_file(
                    root,
                    Path(args.index),
                    Path(args.private),
                    sequence=args.sequence,
                    expires=args.expires,
                    output=Path(args.output) if args.output else None,
                    force=args.force,
                )
                print_json(result)
                return 0
            return command_install_update_public_key(
                ctx,
                Path(args.public_key),
                state_dir=args.state_dir
                or config.get("state_dir", "/opt/msys-state"),
            )
        except PackageFlowError as exc:
            print(f"update-trust: {exc}", file=sys.stderr)
            return 2
    if args.command == "package":
        try:
            if args.package_command == "validate":
                result = validate_package(
                    root,
                    Path(args.path),
                    require_content_hashes=args.require_content_hashes,
                    manifest_path=Path(args.manifest) if args.manifest else None,
                )
                print_json(result)
                return 0
            if args.package_command == "discover":
                result = discover_manifests(
                    root,
                    Path(args.path) if args.path else root,
                )
                print_json(result)
                return 0 if result["valid"] else 2
            if args.package_command == "build":
                package_dir = Path(args.package_dir)
                output = (
                    Path(args.output)
                    if args.output
                    else package_dir.expanduser().resolve().parent / "dist"
                )
                result = build_package(
                    root,
                    package_dir,
                    output,
                    force=args.force,
                    source_date_epoch=args.source_date_epoch,
                    manifest_path=Path(args.manifest) if args.manifest else None,
                    artifact_format=args.format,
                    overlays=[parse_overlay_spec(root, item) for item in args.overlay],
                )
                print_json(result)
                return 0
            if args.package_command == "deliver":
                package_dir = Path(args.package_dir)
                output = (
                    Path(args.output)
                    if args.output
                    else package_dir.expanduser().resolve().parent / "dist"
                )
                return command_package_deliver(
                    ctx,
                    root,
                    package_dir,
                    output,
                    runtime_dir=args.runtime_dir
                    or config.get("runtime_dir", "/run/msys/main"),
                    state_dir=args.state_dir
                    or config.get("state_dir", "/opt/msys-state"),
                    force=args.force,
                    source_date_epoch=args.source_date_epoch,
                    manifest_path=Path(args.manifest) if args.manifest else None,
                    artifact_format=args.format,
                    overlays=[parse_overlay_spec(root, item) for item in args.overlay],
                    legacy_events=args.legacy_events,
                )
            if args.package_command == "index":
                result = build_index(
                    root,
                    Path(args.repository),
                    Path(args.output) if args.output else None,
                    base_url=args.base_url,
                )
                print_json(result)
                return 0
            if args.package_command == "rollback":
                return command_rollback(
                    ctx,
                    args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                    args.package_id,
                    legacy_events=args.legacy_events,
                )
            if args.package_command == "roundtrip":
                return command_package_roundtrip(
                    ctx,
                    args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                    args.package_id,
                )
            return command_uninstall(
                ctx,
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                args.package_id,
                legacy_events=args.legacy_events,
            )
        except PackageFlowError as exc:
            print(f"package: {exc}", file=sys.stderr)
            return 2
    if args.command == "release":
        try:
            release_root = _normalise_remote_release_root(args.release_root)
            runtime_dir = getattr(args, "runtime_dir", None) or config.get(
                "runtime_dir", "/run/msys/main"
            )
            log_file = getattr(args, "log_file", None) or config.get(
                "log_file", "/tmp/msysd.log"
            )
            if args.release_command == "compose":
                return command_release_compose(
                    ctx,
                    release_root,
                    args.release_id,
                    args.baseline_release,
                    args.workspace_root or remote,
                    args.output_root or f"{remote}/release-sources",
                    args.entry,
                    args.maf,
                    args.python_runtime,
                )
            if args.release_command == "stage":
                if args.restart_service and not args.activate:
                    parser.error("release stage --restart-service requires --activate")
                keep = int(args.keep)
                if not 1 <= keep <= 100:
                    parser.error("release --keep must be between 1 and 100")
                return command_release_stage(
                    ctx,
                    release_root,
                    args.release_id,
                    args.source_root or remote,
                    args.entries or [".runtime", *DEFAULT_REPOS],
                    keep=keep,
                    activate=args.activate,
                    restart_service=args.restart_service,
                    runtime_dir=str(runtime_dir),
                    log_file=str(log_file),
                    health_timeout=args.health_timeout,
                )
            if args.release_command == "activate":
                return command_release_switch(
                    ctx,
                    release_root,
                    "activate",
                    args.release_id,
                    restart_service=args.restart_service,
                    runtime_dir=str(runtime_dir),
                    log_file=str(log_file),
                    health_timeout=args.health_timeout,
                )
            if args.release_command == "rollback":
                return command_release_switch(
                    ctx,
                    release_root,
                    "rollback",
                    None,
                    restart_service=args.restart_service,
                    runtime_dir=str(runtime_dir),
                    log_file=str(log_file),
                    health_timeout=args.health_timeout,
                )
            if args.release_command == "verify":
                return command_release_simple(
                    ctx, release_root, "verify", [args.release_id]
                )
            if args.release_command == "repair-python-cache":
                arguments = [args.release_id]
                if args.backup and not args.apply:
                    parser.error("release repair-python-cache --backup requires --apply")
                if args.apply:
                    arguments.append("--apply")
                if args.backup:
                    arguments.extend(
                        ["--backup", _normalise_remote_source_root(args.backup)]
                    )
                return command_release_simple(
                    ctx, release_root, "repair-python-cache", arguments
                )
            if args.release_command == "prune":
                keep = int(args.keep)
                if not 1 <= keep <= 100:
                    parser.error("release --keep must be between 1 and 100")
                return command_release_simple(
                    ctx, release_root, "prune", ["--keep", str(keep)]
                )
            return command_release_simple(ctx, release_root, args.release_command)
        except (RuntimeError, ValueError) as exc:
            print(f"release: {exc}", file=sys.stderr)
            return 2
    if args.command == "host-service":
        try:
            if args.host_service_command == "detect":
                return command_host_service_detect(ctx)
            formal_release_root = (
                _normalise_remote_release_root(args.release_root)
                if args.release_root
                else None
            )
            service_root = (
                f"{formal_release_root}/current" if formal_release_root else remote
            )
            service_python = (
                args.remote_python
                or (
                    f"{service_root}/{DEFAULT_REMOTE_PYTHON_REL}"
                    if formal_release_root
                    else remote_python
                )
            )
            spec = HostServiceSpec(
                root=service_root,
                python=service_python,
                runtime_dir=(
                    args.runtime_dir
                    or config.get("runtime_dir", "/run/msys/main")
                ),
                state_dir=args.state_dir or config.get("state_dir", "/opt/msys-state"),
                log_file=args.log_file or config.get("log_file", "/tmp/msysd.log"),
                profile=args.profile or config.get("profile", "mobile-spi"),
                release_root=formal_release_root,
            )
            if args.host_service_command == "install":
                return command_host_service_install(
                    ctx,
                    spec,
                    args.backend,
                    args.hook,
                    dry_run=args.dry_run,
                    start_now=args.start_now,
                )
            if args.host_service_command == "uninstall":
                return command_host_service_uninstall(
                    ctx,
                    spec,
                    args.backend,
                    args.hook,
                    dry_run=args.dry_run,
                )
            return command_host_service_status(
                ctx, spec, args.backend, args.hook
            )
        except HostServiceError as exc:
            print(f"host-service: {exc}", file=sys.stderr)
            return 2
    if args.command == "wm":
        try:
            return command_wm(
                ctx,
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                args.action,
                window_id=args.window_id,
                x=args.x,
                y=args.y,
                width=args.width,
                height=args.height,
            )
        except ValueError as exc:
            print(f"wm: {exc}", file=sys.stderr)
            return 2
    if args.command == "layout":
        if args.action == "set" and not any(
            value is not None
            for value in (args.profile, args.orientation, args.insets)
        ):
            parser.error("layout set requires --profile, --orientation, or --insets")
        return command_layout(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.action,
            profile=args.profile,
            orientation=args.orientation,
            insets=args.insets,
        )
    if args.command == "tap":
        try:
            tap_identity = args.identity
            tap_role = None
            if tap_identity is None and args.title is None:
                tap_role = "navigation-bar"
            elif tap_identity is None:
                # Preserve the previous --title spelling, which refined the
                # historical PySide navigation identity.
                tap_identity = "org.msys.shell.navigation"
            return command_tap(
                ctx,
                tap_identity,
                args.title,
                args.x,
                args.y,
                args.display,
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                role=tap_role,
            )
        except ValueError as exc:
            print(f"tap: {exc}", file=sys.stderr)
            return 2
    if args.command == "swipe":
        try:
            if args.window is not None and (
                args.identity is not None or args.title is not None
            ):
                raise ValueError("--window cannot be combined with --identity or --title")
            if args.window is not None:
                swipe_identity, swipe_title = args.window
                swipe_role = None
            elif args.identity is None and args.title is None:
                swipe_identity = None
                swipe_title = None
                swipe_role = "navigation-bar"
            else:
                swipe_identity = args.identity
                swipe_title = args.title
                swipe_role = None
            return command_swipe(
                ctx,
                swipe_identity,
                swipe_title,
                args.x1,
                args.y1,
                args.x2,
                args.y2,
                args.duration_ms,
                args.display,
                args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
                role=swipe_role,
            )
        except ValueError as exc:
            print(f"swipe: {exc}", file=sys.stderr)
            return 2
    if args.command == "screenshot":
        return command_screenshot(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            screenshot_output(args.output),
            display=args.display,
            backend=args.backend,
            timeout=args.timeout,
            force=args.force,
        )
    if args.command == "font-doctor":
        return command_font_doctor(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            python=args.font_python,
            display=args.display,
            family=args.family,
            size=args.size,
        )
    if args.command == "visual-smoke":
        return command_visual_smoke(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            args.component,
            timeout=args.timeout,
        )
    if args.command in {"ui-accept", "p0-ui"}:
        return command_ui_acceptance(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            timeout=args.timeout,
            display_log=args.display_log,
        )
    if args.command == "notify":
        return command_broadcast(
            ctx,
            args.runtime_dir or config.get("runtime_dir", "/run/msys/main"),
            "msys.role.notification-presenter",
            {"message": args.message, "timeout_ms": args.timeout_ms},
        )
    if args.command == "shield":
        runtime_dir = args.runtime_dir or config.get("runtime_dir", "/run/msys/main")
        return command_shield(
            ctx,
            runtime_dir,
            args.action,
            timeout=args.timeout,
        )
    if args.command == "debug-env":
        return command_debug_env(ctx, args.runtime_dir or config.get("runtime_dir", "/run/msys/main"))
    if args.command == "install-service-script":
        return command_script(Path(args.output), args.remote)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
