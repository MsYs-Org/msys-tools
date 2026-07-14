from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from msys_tools import dev


WINDOW_ID = "msys.x11-window.v1:100:42:abcdef"


class WindowManagerCommandTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
        )

    def test_stable_window_actions_map_to_typed_methods_and_payloads(self) -> None:
        cases = [
            ("focus", {}, "focus_window", {"window_id": WINDOW_ID}),
            ("minimize", {}, "minimize_window", {"window_id": WINDOW_ID}),
            ("move", {"x": -20, "y": 30}, "move_window", {
                "window_id": WINDOW_ID, "x": -20, "y": 30
            }),
            ("resize", {"width": 240, "height": 320}, "resize_window", {
                "window_id": WINDOW_ID, "width": 240, "height": 320
            }),
            (
                "move-resize",
                {"x": 1, "y": 2, "width": 300, "height": 400},
                "move_resize_window",
                {"window_id": WINDOW_ID, "x": 1, "y": 2, "width": 300, "height": 400},
            ),
            ("close", {}, "close_window", {"window_id": WINDOW_ID}),
        ]
        for action, geometry, method, payload in cases:
            with self.subTest(action=action), mock.patch.object(
                dev, "remote_control_command", return_value=0
            ) as remote:
                status = dev.command_wm(
                    self.context(),
                    "/tmp/msys-main",
                    action,
                    window_id=WINDOW_ID,
                    **geometry,
                )
            self.assertEqual(status, 0)
            self.assertEqual(remote.call_args.args[2:4], (method, payload))
            self.assertEqual(
                remote.call_args.kwargs["target"], "role:window-manager"
            )

    def test_list_alias_and_legacy_navigation_remain_typed_and_compatible(self) -> None:
        for action, method, idempotent in [
            ("list", "list_windows", True),
            ("list_windows", "list_windows", True),
            ("recents", "recents", True),
            ("home", "home", False),
            ("back", "back", False),
            ("close_active", "close_active", False),
        ]:
            with self.subTest(action=action), mock.patch.object(
                dev, "remote_control_command", return_value=0
            ) as remote:
                status = dev.command_wm(
                    self.context(), "/tmp/msys-main", action
                )
            self.assertEqual(status, 0)
            self.assertEqual(remote.call_args.args[2:4], (method, {}))
            self.assertEqual(
                remote.call_args.kwargs["idempotent"], idempotent
            )

    def test_invalid_id_and_geometry_are_rejected_before_transport(self) -> None:
        cases = [
            ("focus", {"window_id": "0x42"}),
            ("move", {"window_id": WINDOW_ID, "x": 1}),
            ("move", {"window_id": WINDOW_ID, "x": -32769, "y": 0}),
            ("resize", {"window_id": WINDOW_ID, "width": 0, "height": 1}),
            ("close", {"window_id": WINDOW_ID, "width": 20}),
            ("home", {"window_id": WINDOW_ID}),
        ]
        with mock.patch.object(dev, "remote_control_command") as remote:
            for action, values in cases:
                with self.subTest(action=action, values=values):
                    with self.assertRaises(ValueError):
                        dev.command_wm(
                            self.context(), "/tmp/msys-main", action, **values
                        )
            remote.assert_not_called()

    def test_cli_removes_the_need_for_powershell_json_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(
                '{"target":"root@device","runtime_dir":"/tmp/msys-main"}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "CONFIG_PATH", config),
                mock.patch.object(dev, "command_wm", return_value=0) as command,
            ):
                status = dev.main([
                    "wm",
                    "move-resize",
                    "--window-id",
                    WINDOW_ID,
                    "--x",
                    "-10",
                    "--y",
                    "20",
                    "--width",
                    "300",
                    "--height",
                    "400",
                ])

        self.assertEqual(status, 0)
        self.assertEqual(command.call_args.args[2], "move-resize")
        self.assertEqual(command.call_args.kwargs, {
            "window_id": WINDOW_ID,
            "x": -10,
            "y": 20,
            "width": 300,
            "height": 400,
        })

    def test_cli_reports_validation_without_a_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@device"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/config.json")),
            redirect_stderr(stderr),
        ):
            status = dev.main(["wm", "move", "--window-id", WINDOW_ID, "--x", "1"])
        self.assertEqual(status, 2)
        self.assertIn("wm move requires --x --y", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
