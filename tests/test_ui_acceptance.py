from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.remote_ui_acceptance import (
    DEFAULT_COMPONENTS,
    P0UIAcceptanceError,
    collect_process_memory,
    probe_thumbnail,
    read_dirty_stats,
    run_p0_ui_acceptance,
)


def returned(payload: dict) -> dict:
    return {
        "welcome": {},
        "response": {"type": "return", "id": 1, "payload": payload},
    }


class FakeP0Runtime:
    def __init__(self, *, initially_running: set[str] | None = None) -> None:
        self.identities = {
            "org.msys.apps:notes": "org.msys.apps.notes",
            "org.msys.apps:calculator": "org.msys.apps.calculator",
            "org.msys.apps:device-info": "org.msys.apps.device-info",
            "org.msys.settings:main": "org.msys.settings",
        }
        self.running = set(initially_running or {"org.msys.settings:main"})
        self.foreground = [
            component
            for component in ("org.msys.settings:main", *DEFAULT_COMPONENTS)
            if component in self.running
        ]
        self.task_visible = False
        self.toast_visible = False
        self.calls: list[tuple[str, str, dict]] = []

    def descriptors(self) -> list[dict]:
        return [
            {
                "id": component,
                "lifecycle": "manual",
                "launchable": True,
                "state": "ready" if component in self.running else "declared",
                "windowing": {
                    "identity": {
                        "app_id": identity,
                        "x11_wm_class": identity,
                    }
                },
            }
            for component, identity in self.identities.items()
        ]

    def focus(self, component: str) -> None:
        self.running.add(component)
        self.foreground = [
            component,
            *(item for item in self.foreground if item != component),
        ]
        self.task_visible = False

    def stop(self, component: str) -> None:
        self.running.discard(component)
        self.foreground = [item for item in self.foreground if item != component]

    def windows(self) -> list[dict]:
        result = []
        for component in self.foreground:
            if component not in self.running:
                continue
            result.append(
                {
                    "id": "msys.x11-window.v1:" + component,
                    "component": component,
                    "identity": self.identities[component],
                    "role": "application",
                    "kind": "application",
                    "state": "visible" if component == self.foreground[0] else "minimized",
                    "geometry": {"x": 0, "y": 42, "width": 320, "height": 396},
                    "thumbnail": "/tmp/msys-main/window-thumbnails/"
                    + component.replace(":", "-")
                    + ".ppm",
                }
            )
        if self.task_visible:
            result.insert(
                0,
                {
                    "id": "msys.x11-window.v1:recents",
                    "identity": "org.msys.shell.task-switcher",
                    "role": "task-switcher",
                    "kind": "overlay",
                    "state": "visible",
                },
            )
        if self.toast_visible:
            result.insert(
                0,
                {
                    "id": "msys.x11-window.v1:toast",
                    "identity": "org.msys.shell.native.notifications",
                    "role": "notification-presenter",
                    "kind": "overlay",
                    "state": "visible",
                },
            )
        return result

    def tasks(self) -> list[dict]:
        return [
            {
                "component": component,
                "id": "msys.x11-window.v1:" + component,
                "title": component,
                "thumbnail": "/tmp/msys-main/window-thumbnails/"
                + component.replace(":", "-")
                + ".ppm",
            }
            for component in self.foreground
            if component in self.running
        ]

    def sleep(self, seconds: float) -> None:
        if seconds >= 3:
            self.toast_visible = False

    def __call__(
        self,
        _runtime: str,
        target: str,
        method: str,
        payload: dict,
        **_kwargs: object,
    ) -> dict:
        self.calls.append((target, method, dict(payload)))
        if target == "msys.core" and method == "list_components":
            return returned({"components": self.descriptors()})
        if target == "msys.core" and method == "foreground_stack":
            return returned(
                {"windows": [{"component": item} for item in self.foreground]}
            )
        if target == "msys.core" and method == "start":
            component = str(payload["component"])
            self.focus(component)
            return returned(
                {
                    "component": component,
                    "state": "ready",
                    "activation": {"ok": True},
                }
            )
        if target == "msys.core" and method == "stop":
            component = str(payload["component"])
            self.stop(component)
            return returned({"component": component, "state": "stopped"})
        if target == "msys.core" and method == "broadcast":
            self.toast_visible = True
            return returned({"ok": True})
        if target == "role:window-manager" and method == "get_layout":
            return returned(
                {
                    "ok": True,
                    "schema": "msys.layout.effective.v1",
                    "profile": "mobile",
                    "orientation": "portrait",
                    "screen": {"width": 320, "height": 480},
                    "insets": {"top": 42, "right": 0, "bottom": 42, "left": 0},
                    "workarea": {"x": 0, "y": 42, "width": 320, "height": 396},
                    "display_consistent": True,
                }
            )
        if target == "role:window-manager" and method == "recents":
            return returned({"schema": "msys.window-list.v1", "windows": self.windows()})
        if target == "role:window-manager" and method == "navigation_action":
            if self.task_visible:
                self.task_visible = False
                return returned({"ok": True, "dismissed": "task-switcher"})
            if self.foreground:
                closed = self.foreground[0]
                self.stop(closed)
                return returned({"ok": True, "closed_component": closed})
            return returned({"ok": True, "destination": "home"})
        if target == "role:window-manager" and method == "home":
            return returned({"ok": True, "role": "launcher"})
        if target == "role:task-switcher" and method == "show":
            self.task_visible = True
            return returned({"ok": True, "visible": True})
        if target == "role:task-switcher" and method == "hide":
            self.task_visible = False
            return returned({"ok": True, "visible": False})
        if target == "role:task-switcher" and method == "list_recents":
            return returned(
                {
                    "schema": "msys.native-recents-list.v1",
                    "state": "ready",
                    "tasks": self.tasks(),
                    "count": len(self.tasks()),
                }
            )
        if target == "role:task-switcher" and method == "activate_task":
            self.focus(str(payload["component"]))
            return returned({"ok": True, "queued": True})
        if target == "role:task-switcher" and method == "close_task":
            self.stop(str(payload["component"]))
            return returned({"ok": True, "queued": True})
        raise AssertionError(f"unexpected call: {target} {method} {payload}")


def fake_thumbnail(path: str, runtime_dir: str) -> dict:
    if not path.startswith(runtime_dir + "/window-thumbnails/"):
        raise P0UIAcceptanceError("bad fake thumbnail path")
    return {
        "path": path,
        "format": "P6",
        "width": 120,
        "height": 90,
        "bytes": 32414,
        "sha256": "a" * 64,
    }


def fake_memory(runtime_dir: str, components: tuple[str, ...]) -> dict:
    return {
        "available": True,
        "collected_after_test_apps_ready": True,
        "unit": "KiB",
        "runtime_dir": runtime_dir,
        "components": list(components),
        "processes": [
            {
                "kind": "core",
                "component": "msys.core",
                "available": True,
                "pids": [10],
                "rss_kib": 12000,
                "pss_kib": 9000,
            }
        ],
    }


class P0UIAcceptanceTests(unittest.TestCase):
    def test_complete_route_restores_original_manual_set_and_foreground(self) -> None:
        runtime = FakeP0Runtime()
        status, document = run_p0_ui_acceptance(
            "/tmp/msys-main",
            rpc_call=runtime,
            sleep=runtime.sleep,
            thumbnail_probe=fake_thumbnail,
            memory_probe=fake_memory,
            display_log="/missing/old-sink.log",
        )

        self.assertEqual(status, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["restored"])
        self.assertEqual(runtime.running, {"org.msys.settings:main"})
        self.assertEqual(runtime.foreground, ["org.msys.settings:main"])
        self.assertEqual(
            set(document["checks"]),
            {
                "layout",
                "applications",
                "memory",
                "recents",
                "activate",
                "close",
                "back_recents",
                "back_application",
                "toast",
            },
        )
        self.assertFalse(document["dirty_stats"]["available"])
        self.assertTrue(document["dirty_stats"]["evidence_only"])

    def test_failure_still_restores_apps_that_were_running_before_test(self) -> None:
        original = {"org.msys.settings:main", "org.msys.apps:device-info"}
        runtime = FakeP0Runtime(initially_running=original)

        def fail_calculator(path: str, runtime_dir: str) -> dict:
            if "calculator" in path:
                raise P0UIAcceptanceError("thumbnail probe failed")
            return fake_thumbnail(path, runtime_dir)

        status, document = run_p0_ui_acceptance(
            "/tmp/msys-main",
            rpc_call=runtime,
            sleep=runtime.sleep,
            thumbnail_probe=fail_calculator,
            memory_probe=fake_memory,
            display_log="/missing/old-sink.log",
        )

        self.assertEqual(status, 1)
        self.assertFalse(document["ok"])
        self.assertTrue(document["restored"])
        self.assertEqual(runtime.running, original)
        self.assertEqual(set(runtime.foreground), original)
        self.assertIn("thumbnail probe failed", document["error"])

    def test_visible_overlay_aborts_before_starting_or_stopping_apps(self) -> None:
        runtime = FakeP0Runtime()
        runtime.task_visible = True
        before = set(runtime.running)

        status, document = run_p0_ui_acceptance(
            "/tmp/msys-main",
            rpc_call=runtime,
            sleep=runtime.sleep,
            thumbnail_probe=fake_thumbnail,
            display_log="/missing/old-sink.log",
        )

        self.assertEqual(status, 1)
        self.assertTrue(document["restored"])
        self.assertEqual(runtime.running, before)
        self.assertFalse(
            any(method in {"start", "stop"} for _target, method, _payload in runtime.calls)
        )

    def test_thumbnail_probe_requires_a_real_bounded_runtime_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            thumbnails = runtime / "window-thumbnails"
            thumbnails.mkdir()
            thumbnail = thumbnails / "window.ppm"
            thumbnail.write_bytes(b"P6\n2 1\n255\n" + bytes((1, 2, 3, 4, 5, 6)))

            evidence = probe_thumbnail(str(thumbnail), str(runtime))
            outside = runtime.parent / "outside.ppm"
            with self.assertRaisesRegex(P0UIAcceptanceError, "escapes"):
                probe_thumbnail(str(outside), str(runtime))

        self.assertEqual(evidence["format"], "P6")
        self.assertEqual((evidence["width"], evidence["height"]), (2, 1))
        self.assertEqual(len(evidence["sha256"]), 64)

    def test_latest_dirty_stats_record_is_evidence_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "live.log"
            log.write_text(
                "dirty_stats frame=1 sent_frames=1 zero_damage=0 full_refreshes=1 "
                "large_refreshes=1 sent_pixels=153600 last_sent_pixels=153600 last_rects=1\n"
                "noise\n"
                "dirty_stats frame=30 sent_frames=4 zero_damage=26 full_refreshes=1 "
                "large_refreshes=1 sent_pixels=154200 last_sent_pixels=200 last_rects=2\n",
                encoding="utf-8",
            )

            evidence = read_dirty_stats(str(log))

        self.assertTrue(evidence["available"])
        self.assertTrue(evidence["evidence_only"])
        self.assertEqual(evidence["frame"], 30)
        self.assertEqual(evidence["zero_damage"], 26)
        self.assertEqual(evidence["last_rects"], 2)

    def test_memory_evidence_sums_smaps_and_marks_missing_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            for pid, component, rss, pss in (
                (100, "", 10000, 7000),
                (101, "org.msys.shell.native:desktop-shell", 6000, 3000),
                (102, "org.msys.apps:notes", 8000, 5000),
            ):
                directory = proc / str(pid)
                directory.mkdir()
                directory.joinpath("environ").write_bytes(
                    (f"MSYS_COMPONENT_ID={component}\0".encode() if component else b"")
                )
                directory.joinpath("smaps_rollup").write_text(
                    f"Rss: {rss} kB\nPss: {pss} kB\n",
                    encoding="utf-8",
                )

            evidence = collect_process_memory(
                "/tmp/msys-main",
                DEFAULT_COMPONENTS,
                proc_root=proc,
                core_pids=[100],
            )

        by_component = {item["component"]: item for item in evidence["processes"]}
        self.assertEqual(by_component["msys.core"]["pss_kib"], 7000)
        self.assertEqual(
            by_component["org.msys.shell.native:desktop-shell"]["rss_kib"],
            6000,
        )
        self.assertEqual(by_component["org.msys.apps:notes"]["pss_kib"], 5000)
        self.assertFalse(by_component["org.msys.hal.linux:native-manager"]["available"])
        self.assertEqual(
            by_component["org.msys.hal.linux:native-manager"]["reason"],
            "process not found",
        )


class P0UIHostCommandTests(unittest.TestCase):
    def test_host_runs_exactly_one_remote_helper_over_one_ssh(self) -> None:
        context = dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
        )
        completed = subprocess.CompletedProcess([], 0, stdout='{"ok":true}\n')
        with mock.patch.object(dev, "ssh_capture", return_value=completed) as capture:
            status = dev.command_ui_acceptance(
                context,
                "/tmp/msys-main",
                timeout=20,
                display_log="/tmp/ch347_dirty_usb_x11/live.log",
            )

        self.assertEqual(status, 0)
        capture.assert_called_once()
        command = capture.call_args.args[1]
        self.assertEqual(command.count("msys_tools.remote_ui_acceptance"), 1)
        self.assertIn("'/tmp/msys-main'", command)
        self.assertIn("'/tmp/ch347_dirty_usb_x11/live.log'", command)
        for forbidden in ("xdotool", "remote_screenshot", "install-archive"):
            self.assertNotIn(forbidden, command)

    def test_host_rejects_unsafe_remote_paths_before_ssh(self) -> None:
        context = dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
        )
        with mock.patch.object(dev, "ssh_capture") as capture:
            status = dev.command_ui_acceptance(
                context,
                "/tmp/../other",
                timeout=12,
                display_log="/tmp/live.log",
            )
        self.assertEqual(status, 2)
        capture.assert_not_called()

    def test_cli_alias_uses_configured_runtime_and_display_log(self) -> None:
        with (
            mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/msys-dev.json")),
            mock.patch.object(dev, "command_ui_acceptance", return_value=0) as command,
        ):
            status = dev.main(
                [
                    "p0-ui",
                    "--timeout",
                    "20",
                    "--display-log",
                    "/tmp/custom-live.log",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(command.call_args.args[1], "/run/msys/main")
        self.assertEqual(
            command.call_args.kwargs,
            {"timeout": 20.0, "display_log": "/tmp/custom-live.log"},
        )


if __name__ == "__main__":
    unittest.main()
