from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


DISPLAY_SESSION_SCHEMA = "msys.display-session.v1"
DISPLAY_PATTERN = re.compile(r"^:[0-9]+(?:\.[0-9]+)?$")
MAX_STATE_BYTES = 64 * 1024
COORDINATE_MAX = 32767
DURATION_MIN_MS = 40
DURATION_MAX_MS = 5000
WINDOW_LIST_MAX_BYTES = 1024 * 1024
WINDOW_LIST_MAX_WINDOWS = 512
ROLE_CANDIDATE_MAX = 4
ROLE_FALLBACK_IDENTITIES = {
    "navigation-bar": (
        "org.msys.shell.native.navigation-pill",
        "org.msys.shell.navigation",
        "org.msys.shell.navigation-pill",
    ),
}


class X11DebugError(ValueError):
    """A synthetic X11 gesture request is unsafe or cannot be routed."""


def _valid_display(value: str) -> str:
    if DISPLAY_PATTERN.fullmatch(value) is None:
        raise X11DebugError("DISPLAY must use the local X11 form :N or :N.S")
    return value


def resolve_display(runtime_dir: Path, explicit: str | None = None) -> str:
    """Resolve DISPLAY from the active session, with an explicit debug override."""

    if explicit is not None:
        return _valid_display(explicit)
    runtime_dir = runtime_dir.expanduser()
    if not runtime_dir.is_absolute() or ".." in runtime_dir.parts:
        raise X11DebugError("runtime directory must be an absolute path without '..'")
    state_path = runtime_dir / "display-session.json"
    try:
        metadata = state_path.lstat()
    except OSError as exc:
        raise X11DebugError(
            f"active display session is unavailable at {state_path}; use --display only for recovery"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise X11DebugError(f"display session is not a regular file: {state_path}")
    if metadata.st_size > MAX_STATE_BYTES:
        raise X11DebugError(f"display session exceeds {MAX_STATE_BYTES} bytes")
    try:
        document: Any = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise X11DebugError(f"cannot read active display session: {exc}") from exc
    if not isinstance(document, dict):
        raise X11DebugError("display session must be a JSON object")
    if document.get("schema") != DISPLAY_SESSION_SCHEMA or document.get("state") != "ready":
        raise X11DebugError("display session is not a ready msys.display-session.v1 document")
    return _valid_display(str(document.get("display", "")))


def _bounded_text(value: str, name: str, limit: int) -> str:
    if not value or len(value) > limit or any(ord(character) < 32 for character in value):
        raise X11DebugError(f"{name} must be 1-{limit} printable characters")
    return value


def _coordinate(value: int) -> str:
    if isinstance(value, bool) or not 0 <= value <= COORDINATE_MAX:
        raise X11DebugError(f"coordinates must be between 0 and {COORDINATE_MAX}")
    return str(value)


def native_arguments(args: argparse.Namespace) -> list[str]:
    identity_value = getattr(args, "identity", None)
    window_value = getattr(args, "window", None)
    title_value = getattr(args, "title", None)
    role_value = getattr(args, "role", None)
    if role_value is not None:
        raise X11DebugError("role selector must be resolved before native arguments")
    if window_value is not None:
        if identity_value is not None or title_value is not None:
            raise X11DebugError("--window cannot be combined with --identity or --title")
        identity_value, title_value = window_value
    identity = (
        _bounded_text(str(identity_value), "identity", 255)
        if identity_value is not None
        else None
    )
    title = (
        _bounded_text(str(title_value), "title", 512)
        if title_value is not None
        else None
    )
    if args.gesture == "tap":
        if identity is None:
            raise X11DebugError("tap requires --identity")
        selector = "--debug-click-window" if title is not None else "--debug-click-identity"
        values = [selector, identity]
        if title is not None:
            values.append(title)
        return [*values, _coordinate(args.x), _coordinate(args.y)]
    duration_ms = int(args.duration_ms)
    if not DURATION_MIN_MS <= duration_ms <= DURATION_MAX_MS:
        raise X11DebugError(
            f"duration must be {DURATION_MIN_MS}-{DURATION_MAX_MS} milliseconds"
        )
    if identity is not None and title is not None:
        values = ["--debug-swipe-window", identity, title]
    elif identity is not None:
        values = ["--debug-swipe-identity", identity]
    elif title is not None:
        values = ["--debug-swipe-title", title]
    else:
        raise X11DebugError("swipe requires --identity, --title, or --window")
    return [
        *values,
        _coordinate(args.x1),
        _coordinate(args.y1),
        _coordinate(args.x2),
        _coordinate(args.y2),
        str(duration_ms),
    ]


def _role_identity_candidates(
    binary: Path,
    role: str,
    environment: dict[str, str],
) -> tuple[str, ...]:
    """Resolve a visible role through X11 policy, with a bounded old-helper fallback."""

    role = _bounded_text(role, "role", 64)
    fallback = ROLE_FALLBACK_IDENTITIES.get(role)
    if fallback is None:
        raise X11DebugError(f"unsupported window role: {role}")
    try:
        completed = subprocess.run(
            [str(binary), "--list-windows"],
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    output = completed.stdout
    if (
        completed.returncode != 0
        or not isinstance(output, str)
        or len(output.encode("utf-8", errors="replace")) > WINDOW_LIST_MAX_BYTES
    ):
        return fallback
    try:
        document: Any = json.loads(output)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(document, dict) or document.get("schema") != "msys.window-list.v1":
        return fallback
    windows = document.get("windows")
    if not isinstance(windows, list) or len(windows) > WINDOW_LIST_MAX_WINDOWS:
        raise X11DebugError("native window list is malformed or exceeds its bound")

    candidates: list[str] = []
    for window in windows:
        if (
            not isinstance(window, dict)
            or window.get("role") != role
            or window.get("state") != "visible"
        ):
            continue
        identity_value = window.get("identity")
        if not isinstance(identity_value, str):
            continue
        try:
            identity = _bounded_text(identity_value, "identity", 255)
        except X11DebugError:
            continue
        if identity not in candidates:
            candidates.append(identity)
        if len(candidates) == ROLE_CANDIDATE_MAX:
            break
    if not candidates:
        raise X11DebugError(f"no visible X11 window provides role {role}")
    return tuple(candidates)


def native_argument_candidates(
    args: argparse.Namespace,
    binary: Path,
    environment: dict[str, str],
) -> tuple[list[str], ...]:
    role_value = getattr(args, "role", None)
    if role_value is None:
        return (native_arguments(args),)
    if (
        getattr(args, "identity", None) is not None
        or getattr(args, "title", None) is not None
        or getattr(args, "window", None) is not None
    ):
        raise X11DebugError("--role cannot be combined with --identity, --title, or --window")
    candidates = _role_identity_candidates(binary, str(role_value), environment)
    commands: list[list[str]] = []
    for identity in candidates:
        resolved = argparse.Namespace(**vars(args))
        resolved.role = None
        resolved.identity = identity
        commands.append(native_arguments(resolved))
    return tuple(commands)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="route a bounded synthetic gesture to the active MSYS X11 session"
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--display")
    sub = parser.add_subparsers(dest="gesture", required=True)
    tap = sub.add_parser("tap")
    tap.add_argument("x", type=int)
    tap.add_argument("y", type=int)
    tap.add_argument("--identity")
    tap.add_argument("--title")
    tap.add_argument("--role", choices=sorted(ROLE_FALLBACK_IDENTITIES))
    swipe = sub.add_parser("swipe")
    swipe.add_argument("x1", type=int)
    swipe.add_argument("y1", type=int)
    swipe.add_argument("x2", type=int)
    swipe.add_argument("y2", type=int)
    swipe.add_argument("--duration-ms", type=int, required=True)
    swipe.add_argument("--identity")
    swipe.add_argument("--title")
    swipe.add_argument("--window", nargs=2, metavar=("IDENTITY", "TITLE"))
    swipe.add_argument("--role", choices=sorted(ROLE_FALLBACK_IDENTITIES))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        display = resolve_display(Path(args.runtime_dir), args.display)
        binary = Path(args.binary)
        if not binary.is_absolute() or binary.is_symlink() or not binary.is_file():
            raise X11DebugError(f"native policy helper is not a regular absolute file: {binary}")
        if not os.access(binary, os.X_OK):
            raise X11DebugError(f"native policy helper is not executable: {binary}")
        environment = {**os.environ, "DISPLAY": display}
        commands = native_argument_candidates(args, binary, environment)
        result = 3
        for command_arguments in commands:
            completed = subprocess.run(
                [str(binary), *command_arguments],
                env=environment,
            )
            result = int(completed.returncode)
            if result != 3:
                break
        return result
    except X11DebugError as exc:
        print(f"msys-x11-debug: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
