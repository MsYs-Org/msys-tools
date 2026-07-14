from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any

from .remote_ctl import call


STATUS_SCHEMA = "msys.runtime-status.v1"
STOP_SCHEMA = "msys.runtime-stop.v1"
MAX_LOG_BYTES = 64 * 1024
MAX_LOG_LINES = 120


def _command_line(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _runtime_argument(argv: list[str]) -> str | None:
    for index, value in enumerate(argv):
        if value == "--runtime-dir" and index + 1 < len(argv):
            return value_or_none(argv[index + 1])
        if value.startswith("--runtime-dir="):
            return value_or_none(value.split("=", 1)[1])
    return None


def value_or_none(value: str) -> str | None:
    return value if value else None


def _normalise_runtime_argument(value: str | None) -> str | None:
    """Lexically normalise an absolute runtime argument for exact matching."""
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


def runtime_processes(runtime_dir: Path) -> list[int]:
    expected = _normalise_runtime_argument(str(runtime_dir))
    if expected is None:
        return []
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        argv = _command_line(int(entry.name))
        if not argv:
            continue
        if not any(
            argv[index] == "-m" and argv[index + 1] == "msys_core.msysd"
            for index in range(len(argv) - 1)
        ):
            continue
        if _normalise_runtime_argument(_runtime_argument(argv)) == expected:
            matches.append(int(entry.name))
    return sorted(matches)


def _socket_kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _socket_accepts(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if details:
        result["details"] = details
    return result


def _response_payload(result: dict[str, Any], operation: str) -> dict[str, Any]:
    response = result.get("response")
    if not isinstance(response, dict) or response.get("type") != "return":
        raise RuntimeError(f"Core {operation} RPC returned an error: {response!r}")
    payload = response.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Core {operation} RPC returned a non-object payload")
    return payload


def _critical_component_issues(
    components: list[dict[str, Any]],
    roles: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    by_id = {
        str(component.get("id")): component
        for component in components
        if isinstance(component, dict) and isinstance(component.get("id"), str)
    }
    preferred_by_role = {
        str(role.get("role")): role.get("preferred")
        for role in roles
        if isinstance(role, dict) and isinstance(role.get("role"), str)
    }
    critical: set[str] = set()
    issues: list[dict[str, Any]] = []

    for component_id, component in by_id.items():
        if component.get("lifecycle") not in {"background", "session"}:
            continue
        provided_roles = [
            str(item.get("name"))
            for item in component.get("provides", [])
            if isinstance(item, dict)
            and item.get("kind") == "role"
            and item.get("exclusive") is True
            and isinstance(item.get("name"), str)
        ]
        if not provided_roles or any(
            preferred_by_role.get(role_name) == component_id
            for role_name in provided_roles
        ):
            critical.add(component_id)

    for role in roles:
        if not isinstance(role, dict):
            continue
        preferred = role.get("preferred")
        if not isinstance(preferred, str) or preferred not in critical:
            continue
        if role.get("active") != preferred:
            issues.append(
                _issue(
                    "ROLE_NOT_ACTIVE",
                    f"critical role {role.get('role')} is not leased by its preferred provider",
                    role=role.get("role"),
                    preferred=preferred,
                    active=role.get("active"),
                )
            )

    for component_id in sorted(critical):
        component = by_id[component_id]
        if component.get("state") != "ready":
            issues.append(
                _issue(
                    "COMPONENT_NOT_READY",
                    f"critical component {component_id} is {component.get('state')}",
                    component=component_id,
                    state=component.get("state"),
                )
            )
    return sorted(critical), issues


def runtime_status(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve(strict=False)
    control = runtime_dir / "control.sock"
    pids = runtime_processes(runtime_dir)
    socket_kind = _socket_kind(control)
    issues: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    roles: list[dict[str, Any]] = []
    critical: list[str] = []
    rpc_status = "unavailable"

    if not pids:
        issues.append(_issue("DAEMON_NOT_RUNNING", "no msysd process owns this runtime"))
    elif len(pids) > 1:
        issues.append(
            _issue("MULTIPLE_DAEMONS", "multiple msysd processes use this runtime", pids=pids)
        )
    if socket_kind != "socket":
        issues.append(
            _issue(
                "CONTROL_SOCKET_MISSING" if socket_kind == "missing" else "CONTROL_PATH_INVALID",
                f"control path is {socket_kind}",
                path=str(control),
                kind=socket_kind,
            )
        )
    else:
        try:
            components_payload = _response_payload(
                call(str(runtime_dir), "msys.core", "list_components", {}, timeout=0.75),
                "list_components",
            )
            roles_payload = _response_payload(
                call(str(runtime_dir), "msys.core", "list_roles", {}, timeout=0.75),
                "list_roles",
            )
            raw_components = components_payload.get("components")
            raw_roles = roles_payload.get("roles")
            if not isinstance(raw_components, list) or not isinstance(raw_roles, list):
                raise RuntimeError("Core readiness RPC returned malformed collections")
            components = [item for item in raw_components if isinstance(item, dict)]
            roles = [item for item in raw_roles if isinstance(item, dict)]
            critical, critical_issues = _critical_component_issues(components, roles)
            issues.extend(critical_issues)
            rpc_status = "ready"
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            rpc_status = "failed"
            issues.append(_issue("CORE_RPC_FAILED", str(exc)[:512]))

    state_counts: dict[str, int] = {}
    for component in components:
        state = str(component.get("state", "unknown"))
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "schema": STATUS_SCHEMA,
        "runtime_dir": str(runtime_dir),
        "healthy": not issues,
        "processes": {"pids": pids, "count": len(pids)},
        "control_socket": {"path": str(control), "kind": socket_kind},
        "core_rpc": rpc_status,
        "critical_components": critical,
        "component_states": state_counts,
        "issues": issues,
    }


def _tail_log(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LOG_BYTES), os.SEEK_SET)
            data = handle.read(MAX_LOG_BYTES)
    except OSError as exc:
        return [f"<cannot read {path}: {exc}>"]
    return data.decode("utf-8", "replace").splitlines()[-MAX_LOG_LINES:]


def wait_ready(
    runtime_dir: Path,
    timeout: float,
    poll_interval: float,
    log_file: Path | None,
) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    latest = runtime_status(runtime_dir)
    while not latest["healthy"] and time.monotonic() < deadline:
        pids = latest.get("processes", {}).get("pids", [])
        if not pids and any(
            issue.get("code") == "DAEMON_NOT_RUNNING" for issue in latest.get("issues", [])
        ):
            # The launcher may not have exec'd Python on the very first poll.
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        else:
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        latest = runtime_status(runtime_dir)
    latest["wait_seconds"] = timeout
    if latest["healthy"]:
        return 0, latest
    latest["log_file"] = str(log_file) if log_file is not None else None
    latest["log_tail"] = _tail_log(log_file)
    return 1, latest


def prepare_run(runtime_dir: Path) -> tuple[int, dict[str, Any]]:
    runtime_dir = runtime_dir.resolve(strict=False)
    control = runtime_dir / "control.sock"
    pids = runtime_processes(runtime_dir)
    socket_kind = _socket_kind(control)
    ready = not pids and socket_kind == "missing"
    result = {
        "schema": "msys.runtime-prepare.v1",
        "runtime_dir": str(runtime_dir),
        "ready": ready,
        "process_pids": pids,
        "control_socket": socket_kind,
    }
    if not ready:
        result["message"] = (
            "runtime is already active or contains stale state; run msys-dev stop "
            "for this runtime before starting"
        )
    return (0 if ready else 1), result


def stop_runtime(runtime_dir: Path, timeout: float, poll_interval: float) -> tuple[int, dict[str, Any]]:
    runtime_dir = runtime_dir.resolve(strict=False)
    control = runtime_dir / "control.sock"
    initial_pids = runtime_processes(runtime_dir)
    signalled: list[int] = []
    for pid in initial_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return 1, {
                "schema": STOP_SCHEMA,
                "runtime_dir": str(runtime_dir),
                "stopped": False,
                "signalled": signalled,
                "error": str(exc),
            }

    deadline = time.monotonic() + timeout
    remaining = runtime_processes(runtime_dir)
    while remaining and time.monotonic() < deadline:
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        remaining = runtime_processes(runtime_dir)

    cleaned_stale_socket = False
    socket_kind = _socket_kind(control)
    if not remaining and socket_kind == "socket" and not _socket_accepts(control):
        try:
            control.unlink()
            cleaned_stale_socket = True
            socket_kind = "missing"
        except OSError as exc:
            return 1, {
                "schema": STOP_SCHEMA,
                "runtime_dir": str(runtime_dir),
                "stopped": False,
                "signalled": signalled,
                "remaining_pids": remaining,
                "error": f"cannot remove stale runtime socket: {exc}",
            }

    stopped = not remaining and socket_kind == "missing"
    result: dict[str, Any] = {
        "schema": STOP_SCHEMA,
        "runtime_dir": str(runtime_dir),
        "stopped": stopped,
        "already_stopped": not initial_pids and _socket_kind(control) == "missing" and not cleaned_stale_socket,
        "signalled": signalled,
        "remaining_pids": remaining,
        "control_socket": socket_kind,
        "cleaned_stale_socket": cleaned_stale_socket,
    }
    if not stopped:
        if remaining:
            result["error"] = "msysd did not exit before the stop timeout"
        elif socket_kind == "socket":
            result["error"] = "control socket is still accepting connections"
        else:
            result["error"] = "control path is not a removable stale socket"
    return (0 if stopped else 1), result


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0 or number > 600:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 600 seconds")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m msys_tools.remote_lifecycle")
    parser.add_argument("action", choices=["prepare", "wait-ready", "status", "stop"])
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--timeout", type=_positive_float, default=30.0)
    parser.add_argument("--poll-interval", type=_positive_float, default=0.2)
    parser.add_argument("--log-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    if not runtime_dir.is_absolute() or runtime_dir == Path("/") or ".." in runtime_dir.parts:
        print("runtime directory must be a non-root absolute path without '..'", file=sys.stderr)
        return 2
    if args.action == "prepare":
        status, result = prepare_run(runtime_dir)
    elif args.action == "wait-ready":
        status, result = wait_ready(
            runtime_dir,
            args.timeout,
            args.poll_interval,
            Path(args.log_file) if args.log_file else None,
        )
    elif args.action == "stop":
        status, result = stop_runtime(runtime_dir, args.timeout, args.poll_interval)
    else:
        result = runtime_status(runtime_dir)
        status = 0 if result["healthy"] else 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
