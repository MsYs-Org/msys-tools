from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .remote_x11_debug import X11DebugError, resolve_display


FONT_PROBE_SCHEMA = "msys.font-probe.v1"
DEFAULT_FAMILY = "Noto Sans CJK SC"
DEFAULT_SAMPLE = "设置应用中文"


class FontProbeError(RuntimeError):
    """The isolated GUI runtime could not complete its font check."""


def assess_report(report: dict[str, Any]) -> list[str]:
    """Return stable machine-readable reasons a Tk font probe is unhealthy."""

    issues: list[str] = []
    if report.get("windowing_system") != "x11":
        issues.append("WINDOWING_NOT_X11")
    libraries = report.get("mapped_font_libraries")
    if not isinstance(libraries, list) or not any(
        "xft" in str(name).casefold() for name in libraries
    ):
        # This deliberately rejects a BDF/PCF renamed to look like Noto.  A
        # future fully static Xft runtime must expose signed build metadata and
        # an equivalent backend proof instead of weakening this live gate.
        issues.append("XFT_BACKEND_NOT_LOADED")
    actual = report.get("requested_font", {}).get("actual", {})
    family = str(actual.get("family", "")).strip().casefold()
    if not family or family in {"fixed", "systemfixedfont"}:
        issues.append("BITMAP_FIXED_FALLBACK")
    width = report.get("requested_font", {}).get("sample_width")
    if not isinstance(width, int) or width <= 0:
        issues.append("CJK_SAMPLE_HAS_NO_ADVANCE")
    glyph_widths = report.get("requested_font", {}).get("glyph_widths")
    if (
        not isinstance(glyph_widths, list)
        or not glyph_widths
        or any(not isinstance(value, int) or value <= 0 for value in glyph_widths)
    ):
        issues.append("CJK_GLYPH_MISSING")
    controls = report.get("controls")
    if not isinstance(controls, dict) or any(
        not isinstance(value, int) or value <= 0 for value in controls.values()
    ):
        issues.append("TK_WIDGET_PROBE_FAILED")
    return issues


def _memory_kib() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/self/smaps_rollup").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            name, separator, rest = line.partition(":")
            if separator and name in {"Pss", "Private_Clean", "Private_Dirty"}:
                token = rest.strip().split(maxsplit=1)[0]
                values[name.lower()] = int(token)
    except (OSError, ValueError):
        pass
    return values


def _mapped_font_libraries() -> list[str]:
    selected: set[str] = set()
    try:
        lines = Path("/proc/self/maps").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []
    for line in lines:
        path = line.rsplit(maxsplit=1)[-1] if "/" in line else ""
        name = Path(path).name.casefold()
        if any(marker in name for marker in ("xft", "fontconfig", "freetype")):
            selected.add(Path(path).name)
    return sorted(selected)


def probe_fonts(
    display: str,
    *,
    family: str = DEFAULT_FAMILY,
    size: int = 16,
    sample: str = DEFAULT_SAMPLE,
) -> dict[str, Any]:
    """Exercise real Tk widgets, then report the selected face and CJK metrics."""

    os.environ["DISPLAY"] = display
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk
    except (ImportError, RuntimeError) as exc:
        raise FontProbeError(f"tkinter import failed: {exc}") from exc

    root: Any = None
    try:
        root = tk.Tk()
        root.withdraw()
        # Negative Tk sizes are pixels.  A fixed pixel contract avoids the
        # target's 75-DPI Fontconfig default disagreeing with X11's 96-DPI
        # screen geometry and makes cross-toolkit screenshots comparable.
        requested = tkfont.Font(root=root, family=family, size=-size)
        label = ttk.Label(root, text=sample, font=requested)
        button = ttk.Button(root, text=sample)
        entry = ttk.Entry(root)
        entry.insert(0, sample)
        text = tk.Text(root, width=12, height=2)
        text.insert("1.0", sample)
        tree = ttk.Treeview(root, columns=("value",), show="headings", height=1)
        tree.heading("value", text=sample)
        tree.insert("", "end", values=(sample,))
        widgets = {
            "label": label,
            "button": button,
            "entry": entry,
            "text": text,
            "treeview": tree,
        }
        root.update_idletasks()
        report: dict[str, Any] = {
            "schema": FONT_PROBE_SCHEMA,
            "display": display,
            "windowing_system": str(root.tk.call("tk", "windowingsystem")),
            "tcl_version": str(root.tk.call("info", "patchlevel")),
            "tk_version": str(root.tk.call("package", "provide", "Tk")),
            "requested_font": {
                "family": family,
                "pixel_size": size,
                "sample": sample,
                "actual": dict(requested.actual()),
                "sample_width": int(requested.measure(sample)),
                "glyph_widths": [int(requested.measure(char)) for char in sample],
                "line_space": int(requested.metrics("linespace")),
            },
            "named_fonts": {
                name: dict(tkfont.nametofont(name, root=root).actual())
                for name in (
                    "TkDefaultFont",
                    "TkTextFont",
                    "TkMenuFont",
                    "TkHeadingFont",
                    "TkFixedFont",
                )
            },
            "controls": {
                name: int(widget.winfo_reqwidth()) for name, widget in widgets.items()
            },
            "mapped_font_libraries": _mapped_font_libraries(),
            "memory_kib": _memory_kib(),
        }
        issues = assess_report(report)
        report["ok"] = not issues
        report["issues"] = issues
        return report
    except (OSError, RuntimeError, tk.TclError, ValueError) as exc:
        raise FontProbeError(f"Tk font probe failed: {exc}") from exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="verify anti-aliased CJK-capable Tk rendering on an MSYS display"
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--display")
    parser.add_argument("--family", default=DEFAULT_FAMILY)
    parser.add_argument("--size", type=int, default=16)
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 6 <= args.size <= 96:
            raise FontProbeError("size must be between 6 and 96 pixels")
        if not args.family.strip() or len(args.family) > 128:
            raise FontProbeError("family must be a non-empty name of at most 128 characters")
        if not args.sample or len(args.sample) > 128:
            raise FontProbeError("sample must contain 1..128 characters")
        display = resolve_display(Path(args.runtime_dir), args.display)
        report = probe_fonts(
            display,
            family=args.family.strip(),
            size=args.size,
            sample=args.sample,
        )
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if report["ok"] else 3
    except (FontProbeError, X11DebugError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"schema": FONT_PROBE_SCHEMA, "ok": False, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
