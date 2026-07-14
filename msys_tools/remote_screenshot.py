from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .remote_x11_debug import X11DebugError, resolve_display


SCREENSHOT_SCHEMA = "msys.debug-screenshot.v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 64 * 1024 * 1024
REMOTE_OUTPUT_PATTERN = re.compile(
    r"^/tmp/msys-screenshot-[a-f0-9]{32}\.png$"
)
DIMENSIONS_PATTERN = re.compile(r"dimensions:\s+([0-9]+)x([0-9]+)\s+pixels")
FFMPEG_DRAW_MOUSE_UNSUPPORTED = (
    "unrecognized option",
    "option not found",
    "no such option",
    "unknown option",
    "does not exist",
)


class ScreenshotError(RuntimeError):
    """A remote screenshot could not be captured safely."""


def _bounded_error(value: object, limit: int = 1000) -> str:
    text = str(value).strip()
    return text[-limit:] if text else "capture command failed without diagnostics"


def _png_size(path: Path) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ScreenshotError(f"capture output is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ScreenshotError("capture output is not a regular file")
    if not 8 <= metadata.st_size <= MAX_SCREENSHOT_BYTES:
        raise ScreenshotError(
            f"capture output size {metadata.st_size} is outside 8..{MAX_SCREENSHOT_BYTES} bytes"
        )
    try:
        with path.open("rb") as handle:
            signature = handle.read(len(PNG_SIGNATURE))
    except OSError as exc:
        raise ScreenshotError(f"cannot read capture output: {exc}") from exc
    if signature != PNG_SIGNATURE:
        raise ScreenshotError("capture backend did not produce a PNG file")
    return int(metadata.st_size)


def _truncate_output(path: Path) -> None:
    try:
        with path.open("wb"):
            pass
        path.chmod(0o600)
    except OSError as exc:
        raise ScreenshotError(f"cannot prepare capture output: {exc}") from exc


def _cleanup_scrot_alternates(path: Path) -> None:
    """Remove only scrot's bounded NAME_000.png non-overwrite siblings."""

    stem = re.escape(path.name.removesuffix(".png"))
    candidate_pattern = re.compile(rf"^{stem}_[0-9]{{3}}\.png$")
    try:
        candidates = list(path.parent.iterdir())
    except OSError as exc:
        raise ScreenshotError(f"cannot inspect capture temporary directory: {exc}") from exc
    for candidate in candidates:
        if candidate_pattern.fullmatch(candidate.name) is None:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            raise ScreenshotError(f"cannot remove scrot alternate output: {exc}") from exc


def _display_dimensions(display: str, timeout: float) -> str | None:
    executable = shutil.which("xdpyinfo")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-display", display],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, min(3.0, timeout)),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = DIMENSIONS_PATTERN.search(result.stdout)
    if match is None:
        return None
    width, height = (int(match.group(1)), int(match.group(2)))
    if not (1 <= width <= 32768 and 1 <= height <= 32768):
        return None
    return f"{width}x{height}"


def _capture_command(
    backend: str,
    executable: str,
    output: Path,
    display: str,
    timeout: float,
    *,
    hide_cursor: bool = True,
) -> list[str]:
    if backend == "scrot":
        return [executable, "--overwrite", "--silent", str(output)]
    dimensions = _display_dimensions(display, timeout)
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
    ]
    if dimensions is not None:
        command.extend(["-video_size", dimensions])
    # x11grab otherwise composites the current X cursor into a root capture.
    # It is an input option, so it must remain between ``-f x11grab`` and the
    # matching ``-i DISPLAY`` argument.
    if hide_cursor:
        command.extend(["-draw_mouse", "0"])
    # Debian 11's ffmpeg/image2 combination can report a successful PNG frame
    # while leaving an existing regular output file at zero bytes.  Stream the
    # single PNG through stdout and let this process own the reserved file.
    command.extend([
        "-i", display,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
    ])
    return command


def _ffmpeg_draw_mouse_is_unsupported(diagnostics: object) -> bool:
    """Identify old x11grab builds which do not expose ``draw_mouse``.

    Debian 11's packaged ffmpeg supports the option, but some small board
    images ship an older/static build.  Retry just this known compatibility
    failure without the option rather than making screenshots unavailable.
    """
    text = str(diagnostics).lower()
    return "draw_mouse" in text and any(
        marker in text for marker in FFMPEG_DRAW_MOUSE_UNSUPPORTED
    )


def _run_ffmpeg_capture(
    command: list[str], output: Path, display: str, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    with output.open("wb") as capture:
        return subprocess.run(
            command,
            check=False,
            text=False,
            stdout=capture,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={**os.environ, "DISPLAY": display},
        )


def capture_screenshot(
    output: Path,
    display: str,
    *,
    backend: str = "auto",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Capture one root-frame PNG, preferring scrot and falling back to ffmpeg."""

    if backend not in {"auto", "scrot", "ffmpeg"}:
        raise ScreenshotError("backend must be auto, scrot, or ffmpeg")
    requested = ("scrot", "ffmpeg") if backend == "auto" else (backend,)
    available = [
        (name, executable)
        for name in requested
        if (executable := shutil.which(name)) is not None
    ]
    if not available:
        names = " or ".join(requested)
        raise ScreenshotError(
            f"no screenshot backend is available ({names}); provision a static target binary"
        )

    failures: list[str] = []
    for name, executable in available:
        _cleanup_scrot_alternates(output)
        _truncate_output(output)
        command = _capture_command(name, executable, output, display, timeout)
        try:
            if name == "ffmpeg":
                completed = _run_ffmpeg_capture(command, output, display, timeout)
                if (
                    completed.returncode != 0
                    and _ffmpeg_draw_mouse_is_unsupported(completed.stderr)
                ):
                    # Retain capture availability on a legacy x11grab.  It is
                    # deliberately a narrow retry: a regular capture failure
                    # must still be reported instead of being obscured.
                    _truncate_output(output)
                    command = _capture_command(
                        name,
                        executable,
                        output,
                        display,
                        timeout,
                        hide_cursor=False,
                    )
                    completed = _run_ffmpeg_capture(
                        command, output, display, timeout
                    )
            else:
                completed = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    env={**os.environ, "DISPLAY": display},
                )
        except subprocess.TimeoutExpired:
            _cleanup_scrot_alternates(output)
            failures.append(f"{name}: timed out after {timeout:g}s")
            continue
        except OSError as exc:
            _cleanup_scrot_alternates(output)
            failures.append(f"{name}: {_bounded_error(exc)}")
            continue
        if completed.returncode != 0:
            _cleanup_scrot_alternates(output)
            diagnostics = completed.stderr or completed.stdout
            failures.append(
                f"{name}: rc={completed.returncode} {_bounded_error(diagnostics)}"
            )
            continue
        try:
            size = _png_size(output)
        except ScreenshotError as exc:
            _cleanup_scrot_alternates(output)
            failures.append(f"{name}: {exc}")
            continue
        _cleanup_scrot_alternates(output)
        return {
            "schema": SCREENSHOT_SCHEMA,
            "ok": True,
            "display": display,
            "backend": name,
            "path": str(output),
            "size": size,
        }
    raise ScreenshotError("; ".join(failures))


def _create_output(path: Path) -> None:
    if REMOTE_OUTPUT_PATTERN.fullmatch(str(path)) is None:
        raise ScreenshotError(
            "output must be an unguessable /tmp/msys-screenshot-<32 hex>.png path"
        )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ScreenshotError(f"cannot reserve capture output: {exc}") from exc
    os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="capture one PNG from the active MSYS X11 display"
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--display")
    parser.add_argument("--backend", choices=["auto", "scrot", "ffmpeg"], default="auto")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    reserved = False
    try:
        if not (0 < args.timeout <= 120):
            raise ScreenshotError("timeout must be greater than zero and at most 120 seconds")
        display = resolve_display(Path(args.runtime_dir), args.display)
        _create_output(output)
        reserved = True
        result = capture_screenshot(
            output,
            display,
            backend=args.backend,
            timeout=args.timeout,
        )
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (ScreenshotError, X11DebugError, OSError, ValueError) as exc:
        if reserved:
            output.unlink(missing_ok=True)
            try:
                _cleanup_scrot_alternates(output)
            except ScreenshotError as cleanup_exc:
                exc = ScreenshotError(f"{exc}; cleanup failed: {cleanup_exc}")
        print(json.dumps({
            "schema": SCREENSHOT_SCHEMA,
            "ok": False,
            "error": str(exc),
        }, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
