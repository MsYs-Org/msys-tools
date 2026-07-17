from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools import remote_settings_smoke as smoke


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SettingsRemoteSmokeTests(unittest.TestCase):
    def test_smoke_targets_the_single_production_component(self) -> None:
        self.assertEqual(smoke.COMPONENT, "org.msys.settings:main")

    def test_present_property_has_eight_fields_and_monotonic_count(self) -> None:
        value = smoke.parse_present(
            "_MSYS_LVGL_LAST_PRESENT = 0, 0, 320, 396, 3, 126720, 0, 17\n"
        )
        self.assertEqual(value[-1], 17)
        self.assertEqual(value[2:4], (320, 396))

    def test_smoke_uses_typed_activation_and_back_not_screen_coordinates(self) -> None:
        source = Path(smoke.__file__).read_text(encoding="utf-8")
        self.assertIn('"settings-panel"', source)
        self.assertIn('"navigation_back"', source)
        self.assertNotIn('"--debug-click-identity"', source)

    def test_one_route_checks_detail_back_idle_and_effective_workarea(self) -> None:
        clock = FakeClock()
        stage = 0

        def rpc(_runtime, target, method, payload, **_kwargs):
            nonlocal stage
            if target == "role:window-manager" and method == "get_layout":
                result = {
                    "schema": "msys.layout.v1",
                    "profile": "mobile",
                    "orientation": "portrait",
                    "screen": {"width": 320, "height": 480},
                    "insets": {"top": 42, "right": 0, "bottom": 24, "left": 0},
                    "workarea": {"x": 0, "y": 42, "width": 320, "height": 414},
                    "display_consistent": True,
                }
            elif target == "msys.core" and method == "stop":
                self.assertEqual(payload["component"], smoke.COMPONENT)
                result = {"state": "stopped"}
            elif target == "msys.core" and method == "start":
                self.assertEqual(payload["component"], smoke.COMPONENT)
                result = {"state": "ready"}
            elif target == "msys.core" and method == "activate":
                self.assertEqual(payload["name"], "wifi")
                stage += 1
                result = {"component": smoke.COMPONENT, "state": "ready"}
            elif target == "msys.core" and method == "navigation_back":
                stage += 1
                result = {"handled": True, "component": smoke.COMPONENT}
            else:
                result = {
                    "windows": [
                        {
                            "component": smoke.COMPONENT,
                            "identity": smoke.IDENTITY,
                            "role": "application",
                            "state": "visible",
                            "native_id": "0x123",
                            "geometry": {"x": 0, "y": 42, "width": 320, "height": 414},
                        }
                    ]
                }
            return {"response": {"type": "return", "payload": result}}

        def present(_xid: str) -> tuple[int, ...]:
            return (0, 0, 320, 414, 1, 132480, 0, stage + 1)

        with (
            mock.patch.object(smoke.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(smoke, "resolve_display", return_value=":24"),
            mock.patch.object(smoke, "read_dirty_stats", return_value={"available": False}),
            mock.patch.object(smoke, "collect_process_memory", return_value={"available": True}),
        ):
            status, document = smoke.run_settings_smoke(
                "/tmp/msys-main",
                rpc_call=rpc,
                present_reader=present,
                sleep=clock.sleep,
            )

        self.assertEqual(status, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["frames"]["idle_unchanged"])
        self.assertEqual(document["frames"]["open_present_delta"], 1)
        self.assertEqual(document["window"]["geometry"]["height"], 414)
        self.assertEqual(
            [row["step"] for row in document["operations"][:4]],
            ["layout", "stop", "start", "window"],
        )


class SettingsHostSmokeTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
        )

    def test_host_uses_one_remote_helper_over_one_ssh(self) -> None:
        reply = {"schema": smoke.SCHEMA, "ok": True, "frames": {"idle_unchanged": True}}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(reply))
        with mock.patch.object(dev, "ssh_capture", return_value=completed) as capture:
            status = dev.command_settings_smoke(
                self.context(),
                "/tmp/msys-main",
                timeout=12,
                display=None,
                display_log="/tmp/live.log",
                screenshot=None,
                force=False,
            )
        self.assertEqual(status, 0)
        capture.assert_called_once()
        command = capture.call_args.args[1]
        self.assertEqual(command.count("remote_settings_smoke"), 1)
        self.assertNotIn("remote_screenshot", command)

    def test_optional_png_is_returned_without_an_extra_remote_call(self) -> None:
        png = b"\x89PNG\r\n\x1a\nsmall"
        reply = {
            "schema": smoke.SCHEMA,
            "ok": True,
            "screenshot": {
                "bytes": len(png),
                "png_base64": base64.b64encode(png).decode("ascii"),
            },
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(reply))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "settings.png"
            with mock.patch.object(dev, "ssh_capture", return_value=completed) as capture:
                status = dev.command_settings_smoke(
                    self.context(),
                    "/tmp/msys-main",
                    timeout=12,
                    display=None,
                    display_log="/tmp/live.log",
                    screenshot=output,
                    force=False,
                )
            self.assertEqual(status, 0)
            self.assertEqual(output.read_bytes(), png)
            capture.assert_called_once()


if __name__ == "__main__":
    unittest.main()
