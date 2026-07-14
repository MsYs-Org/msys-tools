"""Read-only, target-side runtime acceptance snapshot.

This helper deliberately performs no start, stop, install, or input action.  It
is run by :mod:`msys_tools.acceptance` inside the same SSH process that may also
capture a screenshot, so a developer gets one coherent point-in-time report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from .remote_ctl import call
from .remote_lifecycle import runtime_status


SCHEMA = "msys.runtime-acceptance.v1"
DISPLAY_PATTERN = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
LOG_PATTERN = re.compile(
    r"error|warning|failed|failure|oom|quarantine|traceback|bad file descriptor",
    re.IGNORECASE,
)
MAX_LOG_BYTES = 512 * 1024
MAX_LOG_LINES = 1000
DAEMON_SESSION_PATTERN = re.compile(r"^msysd: public control socket(?:\s|$)")
ISOLATION_AUDIT_PREFIX = "msysd: isolation "

CATEGORY_PREFIXES: dict[str, tuple[str, ...]] = {
    "settings": ("org.msys.settings:",),
    "apps": ("org.msys.apps:",),
    "input": ("org.msys.input.touch:",),
    "shell": ("org.msys.shell.native:", "org.msys.shell.pyside:"),
    "display": ("org.msys.openstick.ch347:", "org.msys.x11.session:"),
}
WINDOW_FIELDS = frozenset({"component", "identity", "role", "title"})


class AcceptanceError(RuntimeError):
    """A read-only acceptance probe could not produce trustworthy evidence."""


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    if details:
        issue["details"] = details
    return issue


def _rpc_payload(
    runtime_dir: Path,
    target: str,
    method: str,
    *,
    timeout: float = 3.0,
) -> dict[str, Any]:
    result = call(
        str(runtime_dir),
        target,
        method,
        {},
        timeout=timeout,
        idempotent=True,
    )
    response = result.get("response")
    if not isinstance(response, dict) or response.get("type") != "return":
        raise AcceptanceError(
            f"{target}.{method} returned "
            f"{response.get('code', 'an error') if isinstance(response, dict) else 'invalid data'}"
        )
    payload = response.get("payload")
    if not isinstance(payload, dict):
        raise AcceptanceError(f"{target}.{method} returned a non-object payload")
    return payload


def _component_summary(component: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": component.get("id"),
        "state": component.get("state", "unknown"),
        "lifecycle": component.get("lifecycle"),
        "version": component.get("package_version", component.get("version")),
        "path": component.get(
            "path",
            component.get("effective_path", component.get("package_root")),
        ),
    }


def classify_components(
    components: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    categories = {name: [] for name in CATEGORY_PREFIXES}
    issues: list[dict[str, Any]] = []
    for component in components:
        component_id = component.get("id")
        if not isinstance(component_id, str):
            continue
        for category, prefixes in CATEGORY_PREFIXES.items():
            if component_id.startswith(prefixes):
                summary = _component_summary(component)
                categories[category].append(summary)
                break
    for category, records in categories.items():
        records.sort(key=lambda item: str(item.get("id")))
        if not records:
            issues.append(
                _issue(
                    "COMPONENT_CATEGORY_MISSING",
                    f"no {category} component is installed",
                    category=category,
                )
            )
    return categories, issues


def _valid_geometry(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("width"), int)
        and not isinstance(value.get("width"), bool)
        and value["width"] > 0
        and isinstance(value.get("height"), int)
        and not isinstance(value.get("height"), bool)
        and value["height"] > 0
    )


def validate_display_session(
    session: object,
    components: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(session, dict):
        return [_issue("DISPLAY_SESSION_MISSING", "display session is unavailable")]
    issues: list[dict[str, Any]] = []
    display = session.get("display")
    provider = session.get("provider")
    if not isinstance(display, str) or DISPLAY_PATTERN.fullmatch(display) is None:
        issues.append(_issue("DISPLAY_SESSION_INVALID", "DISPLAY is missing or invalid"))
    if not isinstance(provider, str) or not provider:
        issues.append(_issue("DISPLAY_SESSION_INVALID", "display provider is missing"))
    if not _valid_geometry(session.get("geometry")):
        issues.append(_issue("DISPLAY_SESSION_INVALID", "display geometry is invalid"))
    by_id = {
        item.get("id"): item
        for item in components
        if isinstance(item.get("id"), str)
    }
    if isinstance(provider, str):
        record = by_id.get(provider)
        if record is None:
            issues.append(
                _issue(
                    "DISPLAY_PROVIDER_MISSING",
                    f"active display provider {provider} is not installed",
                    provider=provider,
                )
            )
        elif record.get("state") != "ready":
            issues.append(
                _issue(
                    "DISPLAY_PROVIDER_NOT_READY",
                    f"active display provider {provider} is {record.get('state')}",
                    provider=provider,
                    state=record.get("state"),
                )
            )
    return issues


def _load_display_session(runtime_dir: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = _rpc_payload(runtime_dir, "role:window-manager", "get_display_session")
        session = payload.get("display_session")
        if payload.get("ok") is True and isinstance(session, dict):
            return session, "window-manager"
    except (OSError, AcceptanceError, ValueError, json.JSONDecodeError):
        pass
    path = runtime_dir / "display-session.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "unavailable"
    return (value if isinstance(value, dict) else None), "runtime-file"


def parse_window_expectation(value: str) -> tuple[str, str]:
    field, separator, expected = value.partition("=")
    if separator != "=" or field not in WINDOW_FIELDS or not expected or len(expected) > 512:
        raise ValueError(
            "window expectation must be component=..., identity=..., role=..., or title=..."
        )
    return field, expected


def inspect_windows(
    runtime_dir: Path,
    expectations: Iterable[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    try:
        payload = _rpc_payload(runtime_dir, "role:window-manager", "list_windows")
        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list):
            raise AcceptanceError("window manager returned a malformed window list")
        windows = [item for item in raw_windows if isinstance(item, dict)]
        available = True
        error = None
    except (OSError, AcceptanceError, ValueError, json.JSONDecodeError) as exc:
        windows = []
        available = False
        error = str(exc)[:512]
        issues.append(_issue("WINDOW_LIST_UNAVAILABLE", error))

    key_windows = [
        item
        for item in windows
        if str(item.get("component") or "").startswith("org.msys.shell.")
        or item.get("role")
        in {"launcher", "navigation-bar", "status-bar", "desktop"}
    ]
    if available and not key_windows:
        issues.append(
            _issue("KEY_WINDOW_MISSING", "no declared shell/launcher window exists")
        )

    checks: list[dict[str, Any]] = []
    for text in expectations:
        field, expected = parse_window_expectation(text)
        matches = [item for item in windows if item.get(field) == expected]
        checks.append(
            {
                "expectation": text,
                "matched": bool(matches),
                "window_ids": [item.get("id") for item in matches if item.get("id")],
            }
        )
        if available and not matches:
            issues.append(
                _issue(
                    "EXPECTED_WINDOW_MISSING",
                    f"no window matches {text}",
                    expectation=text,
                )
            )

    compact_windows = [
        {
            key: item.get(key)
            for key in ("id", "component", "identity", "role", "kind", "state", "title")
            if item.get(key) is not None
        }
        for item in windows
    ]
    return {
        "available": available,
        "error": error,
        "count": len(windows),
        "key_window_count": len(key_windows),
        "checks": checks,
        "items": compact_windows,
    }, issues


def _is_normal_isolation_audit(line: str) -> bool:
    if not line.startswith(ISOLATION_AUDIT_PREFIX):
        return False
    fields = line.split()
    if "failure=fail-closed" not in fields or "degraded=False" not in fields:
        return False
    remainder = line.replace("failure=fail-closed", "", 1).replace(
        "degraded=False", "", 1
    )
    return LOG_PATTERN.search(remainder) is None


def recent_log_events(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LOG_BYTES), os.SEEK_SET)
            raw = handle.read(MAX_LOG_BYTES)
    except OSError as exc:
        return [f"<cannot read {path}: {exc}>"]
    bounded_lines = raw.decode("utf-8", "replace").splitlines()[-MAX_LOG_LINES:]
    session_start = next(
        (
            index
            for index in range(len(bounded_lines) - 1, -1, -1)
            if DAEMON_SESSION_PATTERN.match(bounded_lines[index])
        ),
        None,
    )
    if session_start is not None:
        bounded_lines = bounded_lines[session_start:]
    matches = [
        line
        for line in bounded_lines
        if LOG_PATTERN.search(line) and not _is_normal_isolation_audit(line)
    ]
    return matches[-lines:]


def resources() -> dict[str, int | str | None]:
    result: dict[str, int | str | None] = {
        "disk_available_kib": None,
        "disk_used_percent": None,
        "memory_total_kib": None,
        "memory_available_kib": None,
        "swap_used_kib": None,
    }
    try:
        disk = os.statvfs("/")
        available = disk.f_bavail * disk.f_frsize // 1024
        total = disk.f_blocks * disk.f_frsize // 1024
        used = max(0, total - disk.f_bfree * disk.f_frsize // 1024)
        result["disk_available_kib"] = available
        result["disk_used_percent"] = (
            f"{int((used * 100 + max(total, 1) // 2) / max(total, 1))}%"
        )
    except OSError:
        pass
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                token = value.strip().split()[0]
                memory[key] = int(token)
    except (OSError, UnicodeError, ValueError, IndexError):
        memory = {}
    result["memory_total_kib"] = memory.get("MemTotal")
    result["memory_available_kib"] = memory.get("MemAvailable")
    if "SwapTotal" in memory and "SwapFree" in memory:
        result["swap_used_kib"] = max(0, memory["SwapTotal"] - memory["SwapFree"])
    return result


def current_release() -> str | None:
    try:
        target = os.readlink("/opt/msys/current")
    except OSError:
        return None
    return Path(target).name or None


def collect(
    runtime_dir: Path,
    log_file: Path,
    *,
    lines: int = 80,
    strict_logs: bool = False,
    expect_windows: Iterable[str] = (),
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve(strict=False)
    status = runtime_status(runtime_dir)
    issues = list(status.get("issues", []))
    components: list[dict[str, Any]] = []
    try:
        payload = _rpc_payload(runtime_dir, "msys.core", "list_components")
        raw_components = payload.get("components")
        if not isinstance(raw_components, list):
            raise AcceptanceError("Core returned a malformed component list")
        components = [item for item in raw_components if isinstance(item, dict)]
    except (OSError, AcceptanceError, ValueError, json.JSONDecodeError) as exc:
        issues.append(_issue("COMPONENT_LIST_UNAVAILABLE", str(exc)[:512]))

    categories, component_issues = classify_components(components)
    issues.extend(component_issues)
    display_session, display_source = _load_display_session(runtime_dir)
    issues.extend(validate_display_session(display_session, components))
    window_report, window_issues = inspect_windows(runtime_dir, expect_windows)
    issues.extend(window_issues)
    log_events = recent_log_events(log_file, lines)
    if strict_logs and log_events:
        issues.append(
            _issue(
                "RECENT_ERROR_LOGS",
                f"{len(log_events)} recent warning/error log lines matched",
                count=len(log_events),
            )
        )
    return {
        "schema": SCHEMA,
        "ok": not issues,
        "release": current_release(),
        "runtime": {
            "directory": str(runtime_dir),
            "healthy": status.get("healthy") is True,
            "pids": status.get("processes", {}).get("pids", []),
            "issues": status.get("issues", []),
        },
        "components": categories,
        "display": {
            "source": display_source,
            "session": display_session,
        },
        "windows": window_report,
        "resources": resources(),
        "recent_warnings_errors": log_events,
        "strict_logs": strict_logs,
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m msys_tools.remote_acceptance")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--logs", type=int, default=80)
    parser.add_argument("--strict-logs", action="store_true")
    parser.add_argument("--expect-window", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.logs <= 1000:
        parser.error("--logs must be between 0 and 1000")
    try:
        for value in args.expect_window:
            parse_window_expectation(value)
        report = collect(
            Path(args.runtime_dir),
            Path(args.log_file),
            lines=args.logs,
            strict_logs=args.strict_logs,
            expect_windows=args.expect_window,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "schema": SCHEMA,
            "ok": False,
            "issues": [_issue("ACCEPTANCE_PROBE_FAILED", str(exc)[:512])],
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
