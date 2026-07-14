# Tcl/Tk 9 Xft experiment (OpenStick, 2026-07-13)

This is an investigation record, not a package change or deployment plan. All
target work used read-only inspection plus short-lived files under `/tmp`; no
`apt`, `pip`, system service, release pointer, or installed MSYS package was
changed.

## Result

The board has a working Xft stack and CJK font, but the isolated Python's
bundled Tk was built without Xft. Rebuilding only `_tkinter` is not the right
fix. A small Xft-to-`PhotoImage` bridge is immediately feasible and was
successfully demonstrated, but it is a selective rendering workaround rather
than a replacement for a full Xft-enabled Tk.

For a durable, all-widget result, rebuild the complete isolated Python runtime
from the locked `python-build-standalone` recipe with Tcl/Tk 9 and Xft support.
Do not drop an arbitrary `libtk` or `_tkinter` into the current runtime.

## Measured target facts

Target: Debian 11, AArch64, Linux 5.15; X11 display `:24` exposes `RENDER`,
`XFIXES`, and `XInputExtension`.

| Item | Observation |
| --- | --- |
| Python | `/opt/msys/current/.runtime/python/bin/python3`, CPython 3.10.20 |
| Tcl/Tk ABI | `_tkinter` reports Tcl 9.0/Tk 9.0; `info patchlevel` is 9.0.3 |
| `_tkinter` dependencies | `libtcl9.0.so` and `libtcl9tk9.0.so` |
| Bundled libraries | `libtcl9.0.so` 2.4 MiB, `libtcl9tk9.0.so` 3.1 MiB; runtime is 87 MiB |
| Runtime lookup | Python's `$ORIGIN/../lib` RPATH loads both Tcl/Tk libraries from the isolated runtime |
| Tk Xft evidence | no Xft/Fontconfig/FreeType symbols or dynamic dependencies in `libtcl9tk9.0.so` |
| Existing graphics ABI | `libXft.so.2`, `libfontconfig.so.1`, `libfreetype.so.6`, `libX11.so.6`, and `libXrender.so.1` are present |
| Fonts | `fc-match 'sans:lang=zh-cn'` selects `Noto Sans CJK SC` |
| Build environment | GCC/G++/make exist, but no `pkg-config`, Xft/Fontconfig/FreeType/Tcl/Tk headers, unversioned Xft/Fontconfig/FreeType linker names, or Tcl/Tk sources |

The decisive functional probe was:

```python
from tkinter import font
requested = font.Font(family="Noto Sans CJK SC", size=25)
print(requested.actual())
```

On the target it returned `{'family': 'fixed', 'size': 10, ...}`. A temporary
Tk `Label` screenshot showed missing Chinese glyphs and the bitmap fallback.
This is stronger evidence than merely checking for a system Xft library.

## `/tmp` visual prototypes

Two temporary probe windows were created on `:24`, captured, then removed.

| Probe | Result |
| --- | --- |
| Ordinary Tk `Label` requesting Noto CJK | Fell back to `fixed 10`; Chinese did not render correctly. |
| One Xft-to-Tk `PhotoImage` label | Matched `Noto Sans CJK SC:...:pixelsize=25:antialias=true:hinting=true`; Chinese was anti-aliased. |
| Full 320×480 bridge sample | Rendered CJK cards correctly; no resident helper process remained. |

Captured artifacts are intentionally outside packages:

- `dist/tk-xft-probe-current.png` — ordinary Tk fallback;
- `dist/tk-xft-minimal-probe.png` — one anti-aliased Xft-in-Tk label;
- `dist/tk-xft-bridge-probe.png` — full bridge sample.

Single-run timing/RSS measurements are directional rather than a memory
contract:

| Measurement | Value |
| --- | ---: |
| ordinary Tk import/root/label/update | 527 ms, 23.4 MiB RSS |
| Xft bridge construction after ordinary Tk | 212 ms |
| first 25 px CJK raster render | 198 ms |
| ordinary Tk + bridge + one image | 937 ms total, 33.0 MiB RSS |
| one-image cache estimate | 31,044 bytes |
| full bridge demo | 28.1 → 46.8 MiB RSS, +18.7 MiB; 78,416-byte image cache |

The larger RSS increase is mostly loading Fontconfig/Xft/FreeType and Python
objects, not the tiny pixel cache. It must be measured again under the final
screen/session workload before being accepted on a low-memory board.

## Why `_tkinter` replacement is risky

`_tkinter.cpython-310-aarch64-linux-gnu.so` was built for the standalone
runtime's Tcl 9 integration and is linked to the unusual pair
`libtcl9.0.so` + `libtcl9tk9.0.so`. The current
`python-build-standalone` build scripts deliberately remove dependency shared
objects, statically link X11 into Tk, and copy a custom Tcl/Tk layout into the
Python distribution. CPython 3.10 also carries the project's Tcl 9 backport
patch for `_tkinter`.

Upstream Tk normally installs a `libtk9.0.so`, so a separately built library
would not necessarily have the same filename, link closure, build flags,
stubs, private headers, script layout, or symbol visibility as the existing
`libtcl9tk9.0.so`. A filename symlink may appear to load but is not an ABI
validation strategy. Replacing only `_tkinter` has the inverse problem: it
would still bind to the old non-Xft Tk implementation.

## Build options without a target package manager

### A. Dynamic Tk using the board's versioned libraries — feasible, not preferred

Vendor a **build-only** AArch64 sysroot containing the exact headers and
linker metadata for X11/Xrender/Xft/Fontconfig/FreeType. Configure Tk 9.0.3
with `--enable-xft`; its upstream configure script checks `xft-config` or
`pkg-config`, `X11/Xft/Xft.h`, `XftFontOpen`, and `FcFontSort`. Where only
versioned board libraries exist, the private build can use explicit linker
inputs such as `-l:libXft.so.2`, rather than invoking a package manager.

Runtime payload could be close to one replacement Tk shared object because the
board already provides Xft/Fontconfig/FreeType. It is nevertheless tied to this
specific Debian ABI (`libXft.so.2`, `libfontconfig.so.1`, `libfreetype.so.6`),
and does not solve the standalone library-name/layout mismatch. It should be
treated only as a throwaway compatibility experiment.

### B. Rebuild the entire standalone runtime with static Xft — recommended long term

Fork or pin the exact `astral-sh/python-build-standalone` source/tag used for
the current 3.10.20 runtime, retain its Tcl 9 backport patches, and extend its
dependency graph before running its existing `build-tk.sh` recipe. The target
never builds this; a reproducible workstation/CI build produces a complete
private runtime archive that is verified and atomically staged through the
normal release process later.

In addition to Tcl 9.0.3/Tk 9.0.3 and the existing static X11/Xau/XCB inputs,
the private build sysroot needs at least:

- Xft headers/library;
- Fontconfig headers/library and its Expat dependency;
- FreeType headers/library and its libpng/zlib/Brotli dependencies;
- Xrender headers/library plus the existing X11/Xext/XCB header chain;
- matching `.pc`/`xft-config` metadata, or explicit configure cache/flags.

This route preserves `_tkinter`/Tcl/Tk co-build compatibility and avoids a
runtime dependency on the board's versioned libraries. It increases the
runtime payload and build graph; exact compressed size cannot be claimed until
the locked build is produced. Fontconfig configuration/cache behavior, CJK
font availability, cold-start latency, and RSS must be acceptance-tested on
the board.

### C. Keep the Xft bridge — recommended short term

The bridge uses only Python standard-library `ctypes` plus existing board
`libX11`/`libXft`; no headers, wheels, helper process, or runtime replacement
is needed. It can draw high-value static CJK text in a Canvas/Label as a Tk
`PhotoImage`, with a deterministic fallback to normal Tk/BDF text if Xft is
unavailable.

Its limitations are structural: it rasterizes text, so native Tk `Entry`,
`Text`, selection, cursor, IME composition, accessibility, and automatic
widget reflow are not repaired. Every dynamic string/style/scale change needs
rasterization and caching. It is therefore appropriate for headings, cards,
navigation labels, and a staged visual fix—not as a universal input-widget
font backend.

## Decision

Use the Xft bridge (or the existing X server BDF fallback) for the immediate
visible-label problem. Do **not** hot-replace `_tkinter` or
`libtcl9tk9.0.so` in the released runtime.

Start a separate, pinned full-runtime build project only if all Tk widgets,
especially editable/input widgets, must render anti-aliased CJK. Its gate
should require all of the following before any package integration:

1. exact Tcl/Tk 9.0 ABI and Python 3.10 `_tkinter` smoke tests;
2. `font.actual()` selects a real CJK family instead of `fixed`;
3. screenshots for CJK labels, Button/Listbox/Canvas, Entry/Text, and IME;
4. cold/warm startup and RSS measurements on the 391 MiB board;
5. no target package-manager call, no write outside the private staged
   runtime, and a tested rollback path.
