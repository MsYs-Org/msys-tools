from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import sys
from typing import Any

from .remote_x11_debug import X11DebugError, resolve_display


FONT_PROBE_SCHEMA = "msys.font-probe.v1"
DEFAULT_FAMILY = "Noto Sans CJK SC"
DEFAULT_SAMPLE = "设置应用中文"


class FontProbeError(RuntimeError):
    """The isolated GUI runtime could not complete its font check."""


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


def _x11_ink_metrics(
    display_name: str,
    window: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Read a black probe window and count pixels changed by white glyphs."""

    library_name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        x11 = ctypes.CDLL(library_name)
    except OSError as exc:
        raise FontProbeError(f"cannot load {library_name}: {exc}") from exc
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XSync.restype = ctypes.c_int
    x11.XGetImage.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
    ]
    x11.XGetImage.restype = ctypes.POINTER(_XImage)
    x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    x11.XDestroyImage.restype = ctypes.c_int
    display = x11.XOpenDisplay(display_name.encode("utf-8"))
    if not display:
        raise FontProbeError(f"cannot open X11 display {display_name}")
    image: ctypes.POINTER(_XImage) | None = None
    try:
        x11.XSync(display, 0)
        image = x11.XGetImage(
            display,
            ctypes.c_ulong(int(window)),
            0,
            0,
            ctypes.c_uint(int(width)),
            ctypes.c_uint(int(height)),
            ctypes.c_ulong(-1).value,
            2,  # ZPixmap
        )
        if not image or not image.contents.data:
            raise FontProbeError("XGetImage did not return probe pixels")
        metadata = image.contents
        bytes_per_pixel = max(1, (int(metadata.bits_per_pixel) + 7) // 8)
        bytes_per_line = int(metadata.bytes_per_line)
        if (
            bytes_per_line < int(width) * bytes_per_pixel
            or bytes_per_line * int(height) > 16 * 1024 * 1024
        ):
            raise FontProbeError("XGetImage returned invalid pixel geometry")
        raw = ctypes.string_at(metadata.data, bytes_per_line * int(height))
        ink_pixels = 0
        left, top = int(width), int(height)
        right = bottom = -1
        for y in range(int(height)):
            row = y * bytes_per_line
            for x in range(int(width)):
                start = row + x * bytes_per_pixel
                if any(raw[start : start + bytes_per_pixel]):
                    ink_pixels += 1
                    left = min(left, x)
                    top = min(top, y)
                    right = max(right, x)
                    bottom = max(bottom, y)
        return {
            "ink_pixels": ink_pixels,
            "ink_bbox": (
                [left, top, right + 1, bottom + 1]
                if ink_pixels > 0
                else None
            ),
            "bits_per_pixel": int(metadata.bits_per_pixel),
        }
    finally:
        if image:
            x11.XDestroyImage(image)
        x11.XCloseDisplay(display)


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
    catalog = report.get("font_catalog", {})
    families = catalog.get("families") if isinstance(catalog, dict) else None
    if (
        not isinstance(families, list)
        or not families
        or all(str(name).strip().casefold() in {"fixed", "systemfixedfont"}
               for name in families)
    ):
        issues.append("FONT_CATALOG_FIXED_ONLY")
    if not isinstance(catalog, dict) or catalog.get("requested_present") is not True:
        issues.append("REQUESTED_FAMILY_UNAVAILABLE")
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
    raster = report.get("raster_probe", {})
    if (
        not isinstance(raster, dict)
        or not isinstance(raster.get("ink_pixels"), int)
        or int(raster.get("ink_pixels", 0)) <= 0
        or not isinstance(raster.get("ink_bbox"), list)
    ):
        issues.append("CJK_SAMPLE_HAS_NO_INK")
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


def _mapped_tk_library_paths() -> list[str]:
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
        if name.startswith("libtcl") or name.startswith("libtk"):
            selected.add(path)
    return sorted(selected)


def _tcl_global(root: Any, name: str) -> str:
    try:
        return str(root.tk.globalgetvar(name))
    except Exception:
        return ""


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
        available_families = sorted(
            {str(name).strip() for name in tkfont.families(root=root) if str(name).strip()},
            key=str.casefold,
        )
        requested_present = family.casefold() in {
            name.casefold() for name in available_families
        }
        canvas_width = max(64, min(2048, int(requested.measure(sample)) + 20))
        canvas_height = max(32, min(256, int(requested.metrics("linespace")) + 20))
        canvas = tk.Canvas(
            root,
            width=canvas_width,
            height=canvas_height,
            background="#000000",
            borderwidth=0,
            highlightthickness=0,
        )
        canvas.pack()
        text_item = canvas.create_text(
            10,
            canvas_height // 2,
            text=sample,
            font=requested,
            fill="#ffffff",
            anchor="w",
        )
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
        root.overrideredirect(True)
        root.geometry(f"{canvas_width}x{canvas_height}+0+0")
        root.deiconify()
        root.update()
        raster_probe = _x11_ink_metrics(
            display,
            int(canvas.winfo_id()),
            int(canvas.winfo_width()),
            int(canvas.winfo_height()),
        )
        raster_probe["layout_bbox"] = list(canvas.bbox(text_item) or ()) or None
        root.withdraw()
        report: dict[str, Any] = {
            "schema": FONT_PROBE_SCHEMA,
            "display": display,
            "windowing_system": str(root.tk.call("tk", "windowingsystem")),
            "tcl_version": str(root.tk.call("info", "patchlevel")),
            "tk_version": str(root.tk.call("package", "provide", "Tk")),
            "runtime": {
                "python_executable": str(Path(sys.executable).resolve()),
                "python_prefix": str(Path(sys.prefix).resolve()),
                "tcl_library": str(root.tk.call("info", "library")),
                "tk_library": _tcl_global(root, "tk_library"),
                "mapped_tk_libraries": _mapped_tk_library_paths(),
                "environment": {
                    name: os.environ[name]
                    for name in (
                        "TCL_LIBRARY",
                        "TK_LIBRARY",
                        "LD_LIBRARY_PATH",
                        "FONTCONFIG_FILE",
                        "FONTCONFIG_PATH",
                        "MSYS_UI_FONT_FAMILY",
                    )
                    if name in os.environ
                },
            },
            "requested_font": {
                "family": family,
                "pixel_size": size,
                "sample": sample,
                "actual": dict(requested.actual()),
                "sample_width": int(requested.measure(sample)),
                "glyph_widths": [int(requested.measure(char)) for char in sample],
                "line_space": int(requested.metrics("linespace")),
            },
            "font_catalog": {
                "count": len(available_families),
                "families": available_families[:256],
                "requested_present": requested_present,
            },
            "raster_probe": raster_probe,
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
