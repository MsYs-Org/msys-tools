from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .remote_ctl import call
from .remote_screenshot import capture_screenshot
from .remote_ui_acceptance import collect_process_memory, read_dirty_stats, validate_layout
from .remote_x11_debug import resolve_display


SCHEMA = "msys.settings-smoke.v1"
COMPONENT = "org.msys.settings:main"
IDENTITY = "org.msys.settings"
PRESENT_PROPERTY = "_MSYS_LVGL_LAST_PRESENT"
PRESENT_RE = re.compile(r"=\s*([0-9]+(?:\s*,\s*[0-9]+){7})\s*$")
XID_RE = re.compile(r"^0x[0-9a-fA-F]+$")


class SettingsSmokeError(RuntimeError):
    pass


RpcCallable = Callable[..., dict[str, Any]]
PresentCallable = Callable[[str], tuple[int, ...]]
TapCallable = Callable[[int, int], None]
SleepCallable = Callable[[float], None]


def _payload(result: object, step: str) -> dict[str, Any]:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict) or response.get("type") != "return":
        code = response.get("code") if isinstance(response, dict) else "BAD_RESPONSE"
        raise SettingsSmokeError(f"{step} failed: {code}")
    payload = response.get("payload", {})
    if not isinstance(payload, dict):
        raise SettingsSmokeError(f"{step} returned a non-object payload")
    return dict(payload)


def parse_present(value: str) -> tuple[int, ...]:
    match = PRESENT_RE.search(value.strip())
    if match is None:
        raise SettingsSmokeError(f"invalid {PRESENT_PROPERTY} value")
    values = tuple(int(item.strip()) for item in match.group(1).split(","))
    if len(values) != 8:
        raise SettingsSmokeError(f"invalid {PRESENT_PROPERTY} field count")
    return values


def xprop_present(display: str, xid: str) -> tuple[int, ...]:
    if XID_RE.fullmatch(xid) is None:
        raise SettingsSmokeError("Settings window has no valid native XID")
    try:
        result = subprocess.run(
            ["xprop", "-display", display, "-id", xid, "-notype", PRESENT_PROPERTY],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SettingsSmokeError(f"cannot read LVGL presentation state: {exc}") from exc
    if result.returncode != 0:
        raise SettingsSmokeError(
            "cannot read LVGL presentation state: " + result.stderr.strip()[-300:]
        )
    if "not found" in result.stdout.lower():
        raise SettingsSmokeError(
            f"{PRESENT_PROPERTY} is missing; rebuild Settings against msys-ui-lvgl 0.3.6+"
        )
    return parse_present(result.stdout)


def _wait_stable(
    read: Callable[[], tuple[int, ...]],
    timeout: float,
    sleep: SleepCallable,
    *,
    quiet: float = 0.3,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout
    latest = read()
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        sleep(0.08)
        current = read()
        if current != latest:
            latest = current
            unchanged_since = time.monotonic()
        elif time.monotonic() - unchanged_since >= quiet:
            return current
    raise SettingsSmokeError("Settings LVGL surface did not settle")


def _wait_present_after(
    read: Callable[[], tuple[int, ...]],
    count: int,
    timeout: float,
    sleep: SleepCallable,
) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = read()
        if current[7] > count:
            return current
        sleep(0.05)
    raise SettingsSmokeError("touch produced no completed LVGL frame")


def _default_tap(policy_binary: str, display: str) -> TapCallable:
    binary = Path(policy_binary)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise SettingsSmokeError(f"X11 policy helper is unavailable: {binary}")

    def tap(x: int, y: int) -> None:
        result = subprocess.run(
            [str(binary), "--debug-click-identity", IDENTITY, str(x), str(y)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
            env={**os.environ, "DISPLAY": display},
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-300:]
            raise SettingsSmokeError(f"Settings touch injection failed: {detail}")

    return tap


def _window(payload: dict[str, Any]) -> dict[str, Any] | None:
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return None
    return next(
        (
            dict(item)
            for item in windows
            if isinstance(item, dict) and item.get("component") == COMPONENT
        ),
        None,
    )


def run_settings_smoke(
    runtime_dir: str,
    *,
    timeout: float = 12.0,
    display: str | None = None,
    policy_binary: str = "/opt/msys-dev/msys-x11-session/bin/msys-x11-policy",
    display_log: str = "/tmp/ch347_dirty_usb_x11/live.log",
    capture: bool = False,
    rpc_call: RpcCallable | None = None,
    present_reader: PresentCallable | None = None,
    tapper: TapCallable | None = None,
    sleep: SleepCallable = time.sleep,
) -> tuple[int, dict[str, Any]]:
    caller = rpc_call or call
    operations: list[dict[str, Any]] = []
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "component": COMPONENT,
        "operations": operations,
    }

    def rpc(target: str, method: str, payload: dict[str, Any], step: str) -> dict[str, Any]:
        response = caller(
            runtime_dir,
            target,
            method,
            payload,
            timeout=timeout,
            idempotent=method in {"get_layout", "recents", "list_windows"},
        )
        operations.append({"step": step, "target": target, "method": method})
        return _payload(response, step)

    try:
        if not 0 < timeout <= 120:
            raise SettingsSmokeError("timeout must be greater than zero and at most 120 seconds")
        layout = validate_layout(rpc("role:window-manager", "get_layout", {}, "layout"))
        expected = {"x": 0, "y": 42, "width": 320, "height": 396}
        screen = layout.get("screen")
        if not isinstance(screen, dict) or (
            screen.get("width"), screen.get("height")
        ) != (320, 480):
            raise SettingsSmokeError(f"expected a 320x480 display, got {layout.get('screen')!r}")
        if layout.get("workarea") != expected:
            raise SettingsSmokeError(f"expected workarea {expected!r}, got {layout.get('workarea')!r}")
        document["layout"] = layout

        started = rpc("msys.core", "start", {"component": COMPONENT}, "start")
        if started.get("state") != "ready" or isinstance(started.get("activation_error"), dict):
            raise SettingsSmokeError("Settings LVGL component did not become ready")

        deadline = time.monotonic() + timeout
        window: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            window = _window(rpc("role:window-manager", "recents", {}, "window"))
            if window is not None and window.get("state") == "visible":
                break
            sleep(0.08)
        if window is None or window.get("state") != "visible":
            raise SettingsSmokeError("Settings LVGL window did not become visible")
        if window.get("identity") != IDENTITY or window.get("role") != "application":
            raise SettingsSmokeError("Settings window identity or role is wrong")
        if window.get("geometry") != expected:
            raise SettingsSmokeError(f"Settings window geometry is wrong: {window.get('geometry')!r}")
        xid = str(window.get("native_id") or "")
        document["window"] = {
            key: window.get(key)
            for key in ("native_id", "identity", "component", "role", "geometry", "state")
        }

        active_display = resolve_display(Path(runtime_dir), display)
        read = (
            (lambda: present_reader(xid))
            if present_reader is not None
            else (lambda: xprop_present(active_display, xid))
        )
        tap = tapper or _default_tap(policy_binary, active_display)

        home = _wait_stable(read, timeout, sleep)
        tap(72, 132)
        opened = _wait_present_after(read, home[7], timeout, sleep)
        detail = _wait_stable(read, timeout, sleep)
        tap(20, 20)
        returned = _wait_present_after(read, detail[7], timeout, sleep)
        final = _wait_stable(read, timeout, sleep)
        if final[7] <= home[7]:
            raise SettingsSmokeError("secondary-page route did not complete")

        dirty_before = read_dirty_stats(display_log)
        idle_before = read()
        sleep(1.15)
        idle_after = read()
        dirty_after = read_dirty_stats(display_log)
        idle_ok = idle_before == idle_after
        if not idle_ok:
            raise SettingsSmokeError("idle Settings page submitted another LVGL frame")
        dirty_ok = True
        if dirty_before.get("available") and dirty_after.get("available"):
            dirty_ok = all(
                dirty_after.get(key) == dirty_before.get(key)
                for key in ("full_refreshes", "large_refreshes")
            )
            if not dirty_ok:
                raise SettingsSmokeError("idle interval added a large/full SPI refresh")

        document["frames"] = {
            "home": list(home),
            "detail_first": list(opened),
            "detail_settled": list(detail),
            "home_first": list(returned),
            "home_settled": list(final),
            "open_present_delta": detail[7] - home[7],
            "back_present_delta": final[7] - detail[7],
            "animated": detail[7] - home[7] > 1 or final[7] - detail[7] > 1,
            "idle_unchanged": idle_ok,
        }
        document["spi_dirty"] = {
            "large_full_unchanged": dirty_ok,
            "before": dirty_before,
            "after": dirty_after,
        }
        document["memory"] = collect_process_memory(runtime_dir, (COMPONENT,))

        if capture:
            with tempfile.TemporaryDirectory(prefix="msys-settings-smoke-") as temporary:
                output = Path(temporary) / "settings.png"
                details = capture_screenshot(output, active_display, timeout=min(15.0, timeout))
                png = output.read_bytes()
            document["screenshot"] = {
                "backend": details.get("backend"),
                "bytes": len(png),
                "png_base64": base64.b64encode(png).decode("ascii"),
            }
        document["ok"] = True
        return 0, document
    except (SettingsSmokeError, OSError, ValueError, RuntimeError) as exc:
        document["error"] = str(exc)
        return 1, document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="run one compact Settings LVGL smoke route")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--display")
    parser.add_argument("--policy-binary", required=True)
    parser.add_argument("--display-log", default="/tmp/ch347_dirty_usb_x11/live.log")
    parser.add_argument("--capture", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status, document = run_settings_smoke(
        args.runtime_dir,
        timeout=args.timeout,
        display=args.display,
        policy_binary=args.policy_binary,
        display_log=args.display_log,
        capture=args.capture,
    )
    print(json.dumps(document, separators=(",", ":"), ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
