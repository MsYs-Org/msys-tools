"""Host-side, one-SSH runtime acceptance report.

The target probe lives in :mod:`msys_tools.remote_acceptance`.  This module
owns the transport envelope, bounded archive validation, optional screenshot
extraction, and human/JSON rendering so the main development CLI only needs a
small argument adapter.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol


SCHEMA = "msys.runtime-acceptance.v1"
ENVELOPE_SCHEMA = "msys.acceptance-envelope.v1"
SCREENSHOT_SCHEMA = "msys.debug-screenshot.v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 64 * 1024 * 1024
DISPLAY_PATTERN = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
WINDOW_FIELDS = frozenset({"component", "identity", "role", "title"})


class CompletedBytes(Protocol):
    returncode: int
    stdout: bytes
    stderr: bytes


Transport = Callable[[str, str], CompletedBytes]


@dataclass(frozen=True, slots=True)
class AcceptanceConfig:
    remote: str
    remote_python: str
    runtime_dir: str
    log_file: str
    lines: int = 80
    strict_logs: bool = False
    expect_windows: tuple[str, ...] = ()
    screenshot: Path | None = None
    display: str | None = None
    backend: str = "auto"
    timeout: float = 45.0
    force: bool = False
    json_output: bool = False


class AcceptanceHostError(ValueError):
    """The requested acceptance run or returned evidence is invalid."""


def _quote_sh(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _remote_path(value: str, name: str, *, allow_root: bool = False) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or (not allow_root and path == PurePosixPath("/"))
        or ".." in path.parts
        or any(ord(character) < 32 for character in value)
    ):
        raise AcceptanceHostError(
            f"{name} must be a non-root absolute POSIX path without '..'"
        )
    return value


def _window_expectation(value: str) -> str:
    field, separator, expected = value.partition("=")
    if (
        separator != "="
        or field not in WINDOW_FIELDS
        or not expected
        or len(expected) > 512
        or "\0" in expected
    ):
        raise AcceptanceHostError(
            "window expectation must be component=..., identity=..., role=..., or title=..."
        )
    return value


def _prepare_output(path: Path | None, force: bool) -> Path | None:
    if path is None:
        return None
    output = path.expanduser()
    if output.is_symlink():
        raise AcceptanceHostError(f"refusing a symlink screenshot path: {output}")
    output = output.resolve()
    if output.exists() and output.is_dir():
        raise AcceptanceHostError("screenshot output must be a regular file path")
    if output.exists() and not force:
        raise AcceptanceHostError(
            f"screenshot output already exists (use --force): {output}"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AcceptanceHostError(
            f"cannot create screenshot output directory: {exc}"
        ) from exc
    return output


def validate_config(config: AcceptanceConfig) -> Path | None:
    _remote_path(config.remote, "remote development directory")
    _remote_path(config.remote_python, "remote Python")
    _remote_path(config.runtime_dir, "runtime directory")
    _remote_path(config.log_file, "log file")
    if not 0 <= config.lines <= 1000:
        raise AcceptanceHostError("log lines must be between 0 and 1000")
    if config.backend not in {"auto", "scrot", "ffmpeg"}:
        raise AcceptanceHostError("screenshot backend must be auto, scrot, or ffmpeg")
    if config.display is not None and DISPLAY_PATTERN.fullmatch(config.display) is None:
        raise AcceptanceHostError("DISPLAY must use the local X11 form :N or :N.S")
    if not 0 < config.timeout <= 600:
        raise AcceptanceHostError("timeout must be greater than zero and at most 600 seconds")
    for value in config.expect_windows:
        _window_expectation(value)
    return _prepare_output(config.screenshot, config.force)


def build_remote_command(config: AcceptanceConfig, token: str) -> tuple[str, list[str], str]:
    """Build the read-only target command and its exact archive member list."""

    work = f"/tmp/msys-acceptance-{token}"
    remote_png = f"/tmp/msys-screenshot-{token}.png"
    environment = (
        f"PYTHONDONTWRITEBYTECODE=1 "
        f"PYTHONPATH={_quote_sh(config.remote + '/msys-tools')}"
    )
    probe_argv = [
        config.remote_python,
        "-m",
        "msys_tools.remote_acceptance",
        "--runtime-dir",
        config.runtime_dir,
        "--log-file",
        config.log_file,
        "--logs",
        str(config.lines),
    ]
    if config.strict_logs:
        probe_argv.append("--strict-logs")
    for expectation in config.expect_windows:
        probe_argv.extend(["--expect-window", expectation])

    commands = [
        "set -u",
        "umask 077",
        f"work={_quote_sh(work)}",
        f"png={_quote_sh(remote_png)}",
        'rm -rf "$work"',
        'mkdir -p "$work" || exit 2',
        'trap \'rm -rf "$work"; rm -f "$png"\' EXIT HUP INT TERM',
        "acceptance=0",
        (
            environment
            + " "
            + " ".join(_quote_sh(value) for value in probe_argv)
            + ' >"$work/acceptance.json" 2>&1 || acceptance=$?'
        ),
        "shot=0",
    ]
    members = ["meta.json", "acceptance.json"]
    if config.screenshot is not None:
        screenshot_argv = [
            config.remote_python,
            "-m",
            "msys_tools.remote_screenshot",
            "--runtime-dir",
            config.runtime_dir,
            "--output",
            remote_png,
            "--backend",
            config.backend,
            "--timeout",
            f"{config.timeout:g}",
        ]
        if config.display is not None:
            screenshot_argv.extend(["--display", config.display])
        commands.extend(
            [
                (
                    environment
                    + " "
                    + " ".join(_quote_sh(value) for value in screenshot_argv)
                    + ' >"$work/screenshot.json" 2>&1 || shot=$?'
                ),
                'if test "$shot" -eq 0; then mv "$png" "$work/screenshot.png" || shot=$?; fi',
                'test -f "$work/screenshot.png" || : >"$work/screenshot.png"',
            ]
        )
        members.extend(["screenshot.json", "screenshot.png"])
    commands.extend(
        [
            (
                "printf '{\"schema\":\"msys.acceptance-envelope.v1\"," 
                "\"acceptance_status\":%s,\"screenshot_status\":%s}\\n' "
                '"$acceptance" "$shot" >"$work/meta.json"'
            ),
            'tar -cf - -C "$work" ' + " ".join(_quote_sh(name) for name in members),
            "archive=$?",
            'if test "$archive" -ne 0; then exit "$archive"; fi',
            'if test "$acceptance" -ne 0; then exit "$acceptance"; fi',
            'exit "$shot"',
        ]
    )
    return "; ".join(commands), members, remote_png


def _member(archive: tarfile.TarFile, name: str, maximum: int) -> bytes:
    try:
        entry = archive.getmember(name)
    except KeyError as exc:
        raise AcceptanceHostError(f"acceptance bundle is missing {name}") from exc
    if not entry.isfile() or entry.name != name or not 0 <= entry.size <= maximum:
        raise AcceptanceHostError(f"acceptance bundle contains an invalid {name}")
    handle = archive.extractfile(entry)
    if handle is None:
        raise AcceptanceHostError(f"acceptance bundle cannot read {name}")
    data = handle.read(maximum + 1)
    if len(data) != entry.size:
        raise AcceptanceHostError(f"acceptance bundle has a truncated {name}")
    return data


def _json(data: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceHostError(f"acceptance bundle has invalid {name}") from exc
    if not isinstance(value, dict):
        raise AcceptanceHostError(f"acceptance bundle {name} must contain an object")
    return value


def _status(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise AcceptanceHostError(f"acceptance bundle has invalid {name}")
    return value


def _install_screenshot(output: Path, png: bytes, force: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".part", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(png)
        if output.exists() and not force:
            raise AcceptanceHostError(
                f"screenshot output already exists (use --force): {output}"
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def decode_bundle(
    payload: bytes,
    expected_members: list[str],
    remote_png: str,
    *,
    screenshot_output: Path | None,
    force: bool,
) -> tuple[dict[str, Any], int, int]:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            names = archive.getnames()
            if sorted(names) != sorted(expected_members) or len(names) != len(set(names)):
                raise AcceptanceHostError(
                    "acceptance bundle contains unexpected archive members"
                )
            meta = _json(_member(archive, "meta.json", 4096), "meta.json")
            if meta.get("schema") != ENVELOPE_SCHEMA:
                raise AcceptanceHostError("acceptance bundle metadata has an invalid schema")
            acceptance_status = _status(meta.get("acceptance_status"), "acceptance status")
            screenshot_status = _status(meta.get("screenshot_status"), "screenshot status")
            report = _json(
                _member(archive, "acceptance.json", MAX_REPORT_BYTES),
                "acceptance.json",
            )
            if report.get("schema") != SCHEMA or not isinstance(report.get("ok"), bool):
                raise AcceptanceHostError("target returned an invalid acceptance report")

            if screenshot_output is not None:
                details = _json(
                    _member(archive, "screenshot.json", 64 * 1024),
                    "screenshot.json",
                )
                if screenshot_status == 0:
                    png = _member(
                        archive, "screenshot.png", MAX_SCREENSHOT_BYTES
                    )
                    if (
                        details.get("schema") != SCREENSHOT_SCHEMA
                        or details.get("ok") is not True
                        or details.get("path") != remote_png
                        or details.get("size") != len(png)
                        or not png.startswith(PNG_SIGNATURE)
                    ):
                        raise AcceptanceHostError(
                            "acceptance bundle contains an invalid screenshot"
                        )
                    _install_screenshot(screenshot_output, png, force)
                    report["screenshot"] = {
                        "path": str(screenshot_output),
                        "display": details.get("display"),
                        "backend": details.get("backend"),
                        "bytes": len(png),
                    }
                else:
                    report["screenshot"] = {
                        "ok": False,
                        "error": str(details.get("error") or details.get("message") or "capture failed")[:512],
                    }
            return report, acceptance_status, screenshot_status
    except tarfile.TarError as exc:
        raise AcceptanceHostError(f"invalid acceptance archive: {exc}") from exc


def _text(value: object, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def render_text(report: dict[str, Any]) -> str:
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    resources = report.get("resources") if isinstance(report.get("resources"), dict) else {}
    display = report.get("display") if isinstance(report.get("display"), dict) else {}
    session = display.get("session") if isinstance(display.get("session"), dict) else {}
    windows = report.get("windows") if isinstance(report.get("windows"), dict) else {}
    lines = [
        f"accept: ok={str(report.get('ok') is True).lower()} release={_text(report.get('release'), 'unknown')}",
        (
            f"runtime: healthy={str(runtime.get('healthy') is True).lower()} "
            f"pids={','.join(str(item) for item in runtime.get('pids', [])) or '-'}"
        ),
        (
            "resources: "
            f"disk_free={_text(resources.get('disk_available_kib'), '?')}KiB "
            f"disk_used={_text(resources.get('disk_used_percent'), '?')} "
            f"mem_available={_text(resources.get('memory_available_kib'), '?')}KiB "
            f"swap_used={_text(resources.get('swap_used_kib'), '?')}KiB"
        ),
        (
            f"display: source={_text(display.get('source'))} "
            f"name={_text(session.get('display'))} provider={_text(session.get('provider'))}"
        ),
        (
            f"windows: available={str(windows.get('available') is True).lower()} "
            f"count={_text(windows.get('count'), '0')} key={_text(windows.get('key_window_count'), '0')}"
        ),
        "components:",
    ]
    categories = report.get("components")
    if not isinstance(categories, dict):
        categories = {}
    for category in ("settings", "apps", "input", "shell", "display"):
        records = categories.get(category)
        if not isinstance(records, list) or not records:
            lines.append(f"  {category}: missing")
            continue
        lines.append(f"  {category}:")
        for item in records:
            if isinstance(item, dict):
                lines.append(
                    f"    {_text(item.get('id'))} state={_text(item.get('state'), 'unknown')} "
                    f"version={_text(item.get('version'))} path={_text(item.get('path'))}"
                )
    checks = windows.get("checks")
    if isinstance(checks, list) and checks:
        lines.append("window checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(
                    f"  {check.get('expectation', '-')} matched="
                    f"{str(check.get('matched') is True).lower()}"
                )
    screenshot = report.get("screenshot")
    if isinstance(screenshot, dict):
        if screenshot.get("path"):
            lines.append(
                f"screenshot: {_text(screenshot.get('path'))} "
                f"display={_text(screenshot.get('display'))} "
                f"backend={_text(screenshot.get('backend'))} "
                f"bytes={_text(screenshot.get('bytes'))}"
            )
        elif screenshot.get("ok") is False:
            lines.append(f"screenshot: failed: {_text(screenshot.get('error'))}")
    events = report.get("recent_warnings_errors")
    if isinstance(events, list) and events:
        lines.append("recent warnings/errors:")
        lines.extend(f"  {str(event)}" for event in events)
    issues = report.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("issues:")
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(
                    f"  {_text(issue.get('code'), 'UNKNOWN')}: {_text(issue.get('message'), '')}"
                )
            else:
                lines.append(f"  {issue}")
    return "\n".join(lines)


def run(config: AcceptanceConfig, transport: Transport) -> int:
    """Collect, validate, and render one runtime acceptance snapshot."""

    completed: CompletedBytes | None = None
    try:
        output = validate_config(config)
        token = secrets.token_hex(16)
        command, members, remote_png = build_remote_command(config, token)
        completed = transport(command, "<one-pass read-only runtime acceptance>")
        report, acceptance_status, screenshot_status = decode_bundle(
            completed.stdout,
            members,
            remote_png,
            screenshot_output=output,
            force=config.force,
        )
    except (AcceptanceHostError, OSError) as exc:
        print(f"accept: {exc}", file=sys.stderr)
        if completed is not None:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            if error:
                print(error, file=sys.stderr)
            return completed.returncode or 2
        return 2

    if config.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))

    if completed.returncode not in {0, acceptance_status, screenshot_status}:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        if error:
            print(error, file=sys.stderr)
        return completed.returncode or 1
    return acceptance_status or screenshot_status
