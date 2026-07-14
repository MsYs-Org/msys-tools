from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable

from .remote_ctl import call
from .remote_lifecycle import runtime_processes


SCHEMA = "msys.p0-ui-acceptance.v1"
DEFAULT_COMPONENTS = (
    "org.msys.apps:notes",
    "org.msys.apps:calculator",
    "org.msys.apps:device-info",
)
OVERLAY_ROLES = frozenset(
    {
        "control-center",
        "input-method",
        "intent-chooser",
        "notification-center",
        "notification-presenter",
        "screen-shield",
        "task-switcher",
        "transition-presenter",
    }
)
DIRTY_STATS = re.compile(
    r"dirty_stats\s+frame=(\d+)\s+sent_frames=(\d+)\s+"
    r"zero_damage=(\d+)\s+full_refreshes=(\d+)\s+"
    r"large_refreshes=(\d+)\s+sent_pixels=(\d+)\s+"
    r"last_sent_pixels=(\d+)\s+last_rects=(\d+)"
)


class P0UIAcceptanceError(RuntimeError):
    """One P0 UI acceptance invariant failed."""


RpcCallable = Callable[..., dict[str, Any]]
SleepCallable = Callable[[float], None]
ThumbnailProbe = Callable[[str, str], dict[str, Any]]
MemoryProbe = Callable[[str, tuple[str, ...]], dict[str, Any]]


def _payload(result: object, step: str) -> dict[str, Any]:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict) or response.get("type") != "return":
        code = response.get("code") if isinstance(response, dict) else "BAD_RESPONSE"
        message = response.get("message") if isinstance(response, dict) else "non-object response"
        raise P0UIAcceptanceError(f"{step} failed: {code}: {message}")
    payload = response.get("payload", {})
    if not isinstance(payload, dict):
        raise P0UIAcceptanceError(f"{step} returned a non-object payload")
    return dict(payload)


def _records(payload: dict[str, Any], key: str, step: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise P0UIAcceptanceError(f"{step} returned an invalid {key} list")
    return [dict(item) for item in value if isinstance(item, dict)]


def _running_manual(items: list[dict[str, Any]]) -> set[str]:
    return {
        str(item["id"])
        for item in items
        if item.get("lifecycle") == "manual"
        and item.get("state") == "ready"
        and isinstance(item.get("id"), str)
    }


def _window(items: list[dict[str, Any]], component: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("component") == component), None)


def _visible_role(items: list[dict[str, Any]], role: str) -> bool:
    return any(item.get("role") == role and item.get("state") == "visible" for item in items)


def validate_layout(payload: dict[str, Any]) -> dict[str, Any]:
    screen, insets, workarea = (
        payload.get("screen"),
        payload.get("insets"),
        payload.get("workarea"),
    )
    if not all(isinstance(item, dict) for item in (screen, insets, workarea)):
        raise P0UIAcceptanceError("layout is missing screen/insets/workarea")
    assert isinstance(screen, dict) and isinstance(insets, dict) and isinstance(workarea, dict)
    values = [
        screen.get("width"),
        screen.get("height"),
        insets.get("top"),
        insets.get("right"),
        insets.get("bottom"),
        insets.get("left"),
        workarea.get("x"),
        workarea.get("y"),
        workarea.get("width"),
        workarea.get("height"),
    ]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        raise P0UIAcceptanceError("layout contains non-integer geometry")
    expected = {
        "x": insets["left"],
        "y": insets["top"],
        "width": screen["width"] - insets["left"] - insets["right"],
        "height": screen["height"] - insets["top"] - insets["bottom"],
    }
    if workarea != expected or expected["width"] <= 0 or expected["height"] <= 0:
        raise P0UIAcceptanceError(f"invalid effective workarea: {workarea!r}")
    if payload.get("display_consistent") is not True:
        raise P0UIAcceptanceError("layout does not match the active display session")
    return {
        "schema": payload.get("schema"),
        "profile": payload.get("profile"),
        "orientation": payload.get("orientation"),
        "screen": screen,
        "insets": insets,
        "workarea": workarea,
        "display_consistent": True,
    }


def probe_thumbnail(path: str, runtime_dir: str) -> dict[str, Any]:
    candidate, runtime = Path(path), Path(runtime_dir)
    if not candidate.is_absolute() or not runtime.is_absolute():
        raise P0UIAcceptanceError("thumbnail and runtime paths must be absolute")
    try:
        Path(os.path.realpath(candidate)).relative_to(Path(os.path.realpath(runtime)))
    except ValueError as exc:
        raise P0UIAcceptanceError("thumbnail path escapes the runtime directory") from exc
    try:
        metadata = candidate.lstat()
        data = candidate.read_bytes()
    except OSError as exc:
        raise P0UIAcceptanceError(f"thumbnail is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise P0UIAcceptanceError("thumbnail must be a regular non-symlink file")
    if not 16 <= len(data) <= 4 * 1024 * 1024:
        raise P0UIAcceptanceError("thumbnail size is outside the accepted bound")
    header = re.match(rb"P6\n([1-9][0-9]{0,3}) ([1-9][0-9]{0,3})\n255\n", data)
    if header is None:
        raise P0UIAcceptanceError("thumbnail is not a canonical P6 PPM")
    width, height = int(header.group(1)), int(header.group(2))
    if len(data) - header.end() != width * height * 3:
        raise P0UIAcceptanceError("thumbnail pixel payload length is invalid")
    return {
        "path": path,
        "format": "P6",
        "width": width,
        "height": height,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_dirty_stats(log_file: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "evidence_only": True,
        "path": log_file,
    }
    try:
        with open(log_file, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 128 * 1024))
            lines = handle.read(128 * 1024).decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        result["reason"] = f"unavailable: {exc}"
        return result
    names = (
        "frame",
        "sent_frames",
        "zero_damage",
        "full_refreshes",
        "large_refreshes",
        "sent_pixels",
        "last_sent_pixels",
        "last_rects",
    )
    for line in reversed(lines):
        match = DIRTY_STATS.search(line)
        if match:
            result.update(
                available=True,
                line=line[-512:],
                **{name: int(value) for name, value in zip(names, match.groups())},
            )
            return result
    result["reason"] = "no dirty_stats record"
    return result


def collect_process_memory(
    runtime_dir: str,
    test_components: tuple[str, ...],
    *,
    proc_root: Path = Path("/proc"),
    core_pids: list[int] | None = None,
) -> dict[str, Any]:
    """Read bounded RSS/PSS evidence without exposing process environments."""

    component_pids: dict[str, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        return {"available": False, "reason": str(exc), "processes": []}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            with (entry / "environ").open("rb") as handle:
                environment = handle.read(256 * 1024)
        except OSError:
            continue
        component = next(
            (
                item.split(b"=", 1)[1].decode("utf-8", errors="replace")
                for item in environment.split(b"\0")
                if item.startswith(b"MSYS_COMPONENT_ID=")
            ),
            "",
        )
        if component:
            component_pids.setdefault(component, []).append(int(entry.name))

    core_error: str | None = None
    if core_pids is None:
        try:
            core_pids = runtime_processes(Path(runtime_dir))
        except OSError as exc:
            core_pids = []
            core_error = str(exc)
    wanted = [
        ("core", "msys.core", list(core_pids)),
        (
            "native_shell",
            "org.msys.shell.native:desktop-shell",
            component_pids.get("org.msys.shell.native:desktop-shell", []),
        ),
        (
            "native_hal",
            "org.msys.hal.linux:native-manager",
            component_pids.get("org.msys.hal.linux:native-manager", []),
        ),
        *(
            ("test_app", component, component_pids.get(component, []))
            for component in test_components
        ),
    ]
    processes = []
    for kind, component, pids in wanted:
        members = []
        errors = []
        for pid in sorted(set(pids)):
            try:
                text = (proc_root / str(pid) / "smaps_rollup").read_text(
                    encoding="utf-8", errors="replace"
                )
                values = {
                    key: int(match.group(1))
                    for key in ("Rss", "Pss")
                    if (
                        match := re.search(
                            rf"(?m)^{key}:\s+([0-9]+)\s+kB\s*$", text
                        )
                    )
                }
                if set(values) != {"Rss", "Pss"}:
                    raise ValueError("missing Rss/Pss fields")
                members.append(
                    {"pid": pid, "rss_kib": values["Rss"], "pss_kib": values["Pss"]}
                )
            except (OSError, ValueError) as exc:
                errors.append({"pid": pid, "error": str(exc)})
        row: dict[str, Any] = {
            "kind": kind,
            "component": component,
            "available": bool(members),
            "pids": sorted(set(pids)),
            "members": members,
        }
        if members:
            row["rss_kib"] = sum(item["rss_kib"] for item in members)
            row["pss_kib"] = sum(item["pss_kib"] for item in members)
        if errors:
            row["errors"] = errors
        if not pids:
            row["reason"] = (
                f"runtime process lookup unavailable: {core_error}"
                if kind == "core" and core_error
                else "process not found"
            )
        elif not members:
            row["reason"] = "smaps_rollup unavailable"
        processes.append(row)
    return {
        "available": any(item["available"] for item in processes),
        "collected_after_test_apps_ready": True,
        "unit": "KiB",
        "processes": processes,
    }


def run_p0_ui_acceptance(
    runtime_dir: str,
    *,
    components: tuple[str, ...] = DEFAULT_COMPONENTS,
    timeout: float = 12.0,
    display_log: str = "/tmp/ch347_dirty_usb_x11/live.log",
    rpc_call: RpcCallable | None = None,
    sleep: SleepCallable = time.sleep,
    thumbnail_probe: ThumbnailProbe = probe_thumbnail,
    memory_probe: MemoryProbe = collect_process_memory,
) -> tuple[int, dict[str, Any]]:
    caller = rpc_call or call
    operations: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    cleanup: list[dict[str, Any]] = []
    original_running: set[str] = set()
    original_foreground: list[str] = []
    started: set[str] = set()
    mutated = toast_pending = False
    restored = True
    error: str | None = None

    def rpc(target: str, method: str, payload: dict[str, Any], step: str) -> dict[str, Any]:
        result = caller(
            runtime_dir,
            target,
            method,
            payload,
            timeout=timeout,
            idempotent=method
            in {"foreground_stack", "get_layout", "list_components", "list_recents", "recents"},
        )
        response = result.get("response") if isinstance(result, dict) else None
        operations.append(
            {
                "step": step,
                "target": target,
                "method": method,
                "ok": isinstance(response, dict) and response.get("type") == "return",
            }
        )
        return _payload(result, step)

    def inventory(step: str) -> list[dict[str, Any]]:
        return _records(rpc("msys.core", "list_components", {}, step), "components", step)

    def windows(step: str) -> list[dict[str, Any]]:
        return _records(rpc("role:window-manager", "recents", {}, step), "windows", step)

    def wait(step: str, fetch: Callable[[], Any], ready: Callable[[Any], bool]) -> Any:
        attempts = max(1, min(120, int(timeout * 10)))
        operation_start = len(operations)
        for attempt in range(attempts):
            value = fetch()
            if ready(value):
                final = dict(operations[-1])
                final["attempts"] = attempt + 1
                operations[operation_start:] = [final]
                return value
            if attempt + 1 < attempts:
                sleep(0.1)
        operations[operation_start:] = [
            {"step": step, "ok": False, "attempts": attempts}
        ]
        raise P0UIAcceptanceError(f"{step} did not reach its expected state")

    def stopped(items: list[dict[str, Any]], component: str) -> bool:
        return next(
            (
                item.get("state") not in {"ready", "starting"}
                for item in items
                if item.get("id") == component
            ),
            False,
        )

    try:
        if len(components) < 3 or len(set(components)) != len(components):
            raise P0UIAcceptanceError("at least three unique P0 components are required")
        before = inventory("preflight.components")
        descriptors = {str(item.get("id")): item for item in before}
        for component in components:
            item = descriptors.get(component)
            if not item or item.get("lifecycle") != "manual" or item.get("launchable") is not True:
                raise P0UIAcceptanceError(f"required launchable manual app is missing: {component}")
            if item.get("state") not in {"declared", "ready", "stopped"}:
                raise P0UIAcceptanceError(f"required app is transitional/unhealthy: {component}")
        original_running = _running_manual(before)
        original_foreground = [
            str(item["component"])
            for item in _records(
                rpc("msys.core", "foreground_stack", {}, "preflight.foreground"),
                "windows",
                "preflight.foreground",
            )
            if isinstance(item.get("component"), str)
        ]
        before_windows = windows("preflight.windows")
        visible_overlays = [
            str(item.get("role") or item.get("title") or item.get("id"))
            for item in before_windows
            if item.get("state") == "visible"
            and (item.get("kind") == "overlay" or item.get("role") in OVERLAY_ROLES)
        ]
        if visible_overlays:
            raise P0UIAcceptanceError(f"dismiss visible overlays first: {visible_overlays}")
        layout = validate_layout(
            rpc("role:window-manager", "get_layout", {}, "layout.workarea")
        )
        checks["layout"] = layout
        workarea = layout["workarea"]
        mutated = True

        application_checks = []
        for component in components:
            if component not in original_running:
                started.add(component)
            reply = rpc("msys.core", "start", {"component": component}, f"start.{component}")
            if reply.get("state") != "ready" or isinstance(reply.get("activation_error"), dict):
                raise P0UIAcceptanceError(f"application failed to start: {component}")
            snapshot = wait(
                f"window.{component}",
                lambda component=component: windows(f"window.{component}"),
                lambda items, component=component: bool(
                    (item := _window(items, component))
                    and item.get("state") == "visible"
                    and item.get("thumbnail")
                ),
            )
            item = _window(snapshot, component)
            if item is None:
                raise P0UIAcceptanceError(f"application window disappeared: {component}")
            identity = descriptors[component].get("windowing", {}).get("identity", {})
            expected = {
                str(value).casefold()
                for value in (
                    identity.get("app_id") if isinstance(identity, dict) else None,
                    identity.get("x11_wm_class") if isinstance(identity, dict) else None,
                )
                if isinstance(value, str) and value
            }
            if str(item.get("identity") or "").casefold() not in expected:
                raise P0UIAcceptanceError(f"window identity mismatch: {component}")
            if item.get("role") != "application" or item.get("kind") != "application":
                raise P0UIAcceptanceError(f"window role/kind mismatch: {component}")
            if item.get("geometry") != workarea:
                raise P0UIAcceptanceError(f"window does not occupy workarea: {component}")
            application_checks.append(
                {
                    "component": component,
                    "identity": item.get("identity"),
                    "window_id": item.get("id"),
                    "geometry": item.get("geometry"),
                    "thumbnail": thumbnail_probe(str(item["thumbnail"]), runtime_dir),
                }
            )
        checks["applications"] = application_checks
        checks["memory"] = memory_probe(runtime_dir, components)

        current = windows("recents.canonical")
        if any(_window(current, component) is None for component in components):
            raise P0UIAcceptanceError("canonical Recents is missing a P0 application")
        rpc("role:task-switcher", "show", {}, "recents.show")

        def task_list(step: str) -> list[dict[str, Any]]:
            payload = rpc("role:task-switcher", "list_recents", {}, step)
            if payload.get("state") != "ready":
                return []
            return _records(payload, "tasks", step)

        tasks = wait(
            "recents.cards",
            lambda: task_list("recents.cards"),
            lambda items: all(_window(items, component) for component in components),
        )
        wait(
            "recents.visible",
            lambda: windows("recents.visible"),
            lambda items: _visible_role(items, "task-switcher"),
        )
        cards = []
        for component in components:
            item = _window(tasks, component)
            if item is None or not isinstance(item.get("thumbnail"), str):
                raise P0UIAcceptanceError(f"Recents card has no thumbnail: {component}")
            cards.append(
                {
                    "component": component,
                    "id": item.get("id"),
                    "thumbnail": thumbnail_probe(str(item["thumbnail"]), runtime_dir),
                }
            )
        checks["recents"] = {"count": len(tasks), "cards": cards, "visible": True}

        activate_component = components[0]
        rpc(
            "role:task-switcher",
            "activate_task",
            {"component": activate_component},
            "recents.activate",
        )
        wait(
            "recents.activate",
            lambda: windows("recents.activate"),
            lambda items: bool(
                (item := _window(items, activate_component))
                and item.get("state") == "visible"
                and not _visible_role(items, "task-switcher")
            ),
        )
        checks["activate"] = {"component": activate_component}

        close_component = components[1]
        rpc("role:task-switcher", "show", {}, "recents.show-for-close")
        wait(
            "recents.ready-for-close",
            lambda: task_list("recents.ready-for-close"),
            lambda items: all(_window(items, component) for component in components),
        )
        rpc(
            "role:task-switcher",
            "close_task",
            {"component": close_component},
            "recents.close",
        )
        wait(
            "recents.close",
            lambda: inventory("recents.close"),
            lambda items: stopped(items, close_component),
        )
        if _window(windows("recents.after-close"), close_component):
            raise P0UIAcceptanceError("closed Recents task remains present")
        checks["close"] = {"component": close_component}

        rpc(
            "role:window-manager",
            "navigation_action",
            {"action": "back", "input": "debug"},
            "recents.back",
        )
        wait(
            "recents.back",
            lambda: windows("recents.back"),
            lambda items: not _visible_role(items, "task-switcher"),
        )
        checks["back_recents"] = {"visible": False}

        back_component = components[2]
        rpc("msys.core", "start", {"component": back_component}, "back.focus")
        back_focus_windows = wait(
            "back.focus",
            lambda: windows("back.focus"),
            lambda items: bool(
                (item := _window(items, back_component)) and item.get("state") == "visible"
            ),
        )
        input_method_before = next(
            (
                {
                    key: item.get(key)
                    for key in ("id", "identity", "role", "kind", "state")
                    if item.get(key) is not None
                }
                for item in back_focus_windows
                if item.get("role") == "input-method"
                and item.get("state") == "visible"
            ),
            None,
        )

        def back_evidence(payload: dict[str, Any]) -> dict[str, Any]:
            return {
                key: payload.get(key)
                for key in (
                    "ok",
                    "dismissed",
                    "closed_component",
                    "destination",
                    "reason",
                    "window_id",
                )
                if payload.get(key) is not None
            }

        first_back = rpc(
            "role:window-manager",
            "navigation_action",
            {"action": "back", "input": "debug"},
            "back.close",
        )
        back_actions = [back_evidence(first_back)]
        if first_back.get("dismissed") is not None:
            if first_back.get("dismissed") != "input-method":
                raise P0UIAcceptanceError(
                    "back.close dismissed an unexpected overlay: "
                    f"{first_back.get('dismissed')}"
                )
            wait(
                "back.input-method-hidden",
                lambda: windows("back.input-method-hidden"),
                lambda items: not any(
                    item.get("role") == "input-method"
                    and item.get("state") == "visible"
                    for item in items
                ),
            )
            second_back = rpc(
                "role:window-manager",
                "navigation_action",
                {"action": "back", "input": "debug"},
                "back.close-application",
            )
            back_actions.append(back_evidence(second_back))
        wait(
            "back.close",
            lambda: inventory("back.close"),
            lambda items: stopped(items, back_component),
        )
        checks["back_application"] = {
            "component": back_component,
            "input_method_before": input_method_before,
            "actions": back_actions,
        }

        toast_pending = True
        rpc(
            "msys.core",
            "broadcast",
            {
                "topic": "msys.role.notification-presenter",
                "payload": {"message": "MSYS P0 bounded toast", "timeout_ms": 700},
            },
            "toast.broadcast",
        )
        wait(
            "toast.visible",
            lambda: windows("toast.visible"),
            lambda items: _visible_role(items, "notification-presenter"),
        )
        sleep(3.4)
        if _visible_role(windows("toast.expired"), "notification-presenter"):
            raise P0UIAcceptanceError("notification toast exceeded its bounded lifetime")
        toast_pending = False
        checks["toast"] = {"requested_timeout_ms": 700, "waited_ms": 3400}
    except (OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
    finally:
        cleanup_errors: list[str] = []

        def clean(target: str, method: str, payload: dict[str, Any], step: str) -> None:
            try:
                rpc(target, method, payload, step)
                cleanup.append({"step": step, "ok": True})
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup.append({"step": step, "ok": False, "error": str(exc)})
                cleanup_errors.append(f"{step}: {exc}")

        if mutated:
            clean("role:task-switcher", "hide", {}, "cleanup.hide-recents")
            if toast_pending:
                sleep(3.4)
            try:
                running_now = _running_manual(inventory("cleanup.before"))
                running_known = True
            except (OSError, RuntimeError, ValueError) as exc:
                running_now = set()
                running_known = False
                cleanup_errors.append(f"cleanup.before: {exc}")
            stop_candidates = started & running_now if running_known else started
            for component in sorted(stop_candidates):
                clean("msys.core", "stop", {"component": component}, f"cleanup.stop.{component}")
            for component in sorted(original_running - running_now):
                clean("msys.core", "start", {"component": component}, f"cleanup.start.{component}")
            if original_foreground:
                for component in reversed(original_foreground):
                    clean(
                        "msys.core",
                        "start",
                        {"component": component},
                        f"cleanup.foreground.{component}",
                    )
            else:
                clean("role:window-manager", "home", {}, "cleanup.home")
            try:
                after = _running_manual(inventory("cleanup.after"))
                if after != original_running:
                    cleanup_errors.append(
                        f"manual set mismatch: before={sorted(original_running)} "
                        f"after={sorted(after)}"
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup_errors.append(f"cleanup.after: {exc}")
            if cleanup_errors:
                restored = False
                error = error or "cleanup failed: " + "; ".join(cleanup_errors)

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": error is None and restored,
        "components": list(components),
        "baseline": {
            "manual_running": sorted(original_running),
            "foreground": original_foreground,
        },
        "checks": checks,
        "operations": operations,
        "cleanup": cleanup,
        "restored": restored,
        "dirty_stats": read_dirty_stats(display_log),
    }
    if error:
        document["error"] = error
    return (0 if document["ok"] else 1), document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="run one reversible multi-app P0 UI acceptance route"
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--display-log", default="/tmp/ch347_dirty_usb_x11/live.log")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (0 < args.timeout <= 120):
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": "invalid timeout"}, indent=2))
        return 2
    status, document = run_p0_ui_acceptance(
        args.runtime_dir,
        timeout=args.timeout,
        display_log=args.display_log,
    )
    print(json.dumps(document, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
