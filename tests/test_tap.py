from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.dev import Context, command_swipe, command_tap


class TapCommandTests(unittest.TestCase):
    def test_tap_targets_stable_identity_without_external_xdotool(self) -> None:
        context = Context(
            root=Path("/workspace"),
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        with mock.patch("msys_tools.dev.ssh") as ssh:
            self.assertEqual(
                command_tap(
                    context,
                    "org.msys.shell.navigation",
                    None,
                    270,
                    20,
                    runtime_dir="/tmp/msys-main",
                ),
                0,
            )
        command = ssh.call_args.args[1]
        self.assertIn("'-m' 'msys_tools.remote_x11_debug'", command)
        self.assertIn("--runtime-dir' '/tmp/msys-main'", command)
        self.assertIn("'tap' '270' '20' '--identity' 'org.msys.shell.navigation'", command)
        self.assertNotIn("'--display' ':24'", command)
        self.assertNotIn("xdotool", command)

    def test_tap_rejects_invalid_coordinates_before_ssh(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.ssh") as ssh:
            with self.assertRaises(ValueError):
                command_tap(context, "org.msys.shell.navigation", None, -1, 0, ":24")
            ssh.assert_not_called()

    def test_tap_default_routes_to_the_active_navigation_role(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.ssh") as ssh:
            result = command_tap(
                context,
                None,
                None,
                267,
                459,
                runtime_dir="/tmp/msys-main",
                role="navigation-bar",
            )

        self.assertEqual(result, 0)
        command = ssh.call_args.args[1]
        self.assertIn("'tap' '267' '459' '--role' 'navigation-bar'", command)
        self.assertNotIn("'--identity'", command)

    def test_remote_pointer_error_is_returned_without_a_local_traceback(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        completed = __import__("subprocess").CompletedProcess(
            ["ssh"], 64
        )
        with mock.patch("msys_tools.dev.ssh", return_value=completed) as ssh:
            result = command_swipe(
                context,
                "org.msys.shell.navigation-pill",
                None,
                160,
                34,
                160,
                5,
                220,
            )

        self.assertEqual(result, 64)
        self.assertFalse(ssh.call_args.kwargs["check"])

    def test_tap_can_fall_back_to_presentation_title_for_legacy_toplevel(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.ssh") as ssh:
            command_tap(
                context,
                "org.msys.shell.intent-chooser",
                "MSYS Intent Chooser",
                190,
                345,
                ":24",
            )
        self.assertIn(
            "'tap' '190' '345' '--identity' 'org.msys.shell.intent-chooser' "
            "'--title' 'MSYS Intent Chooser'",
            ssh.call_args.args[1],
        )

    def test_swipe_uses_native_xtest_helper_and_explicit_recovery_display(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.ssh") as ssh:
            result = command_swipe(
                context,
                "org.msys.shell.navigation-pill",
                None,
                160,
                34,
                160,
                5,
                220,
                ":91",
                "/tmp/session",
            )
        self.assertEqual(result, 0)
        command = ssh.call_args.args[1]
        self.assertIn("'--display' ':91'", command)
        self.assertIn(
            "'swipe' '160' '34' '160' '5' '--duration-ms' '220' "
            "'--identity' 'org.msys.shell.navigation-pill'",
            command,
        )
        self.assertNotIn("xdotool", command)

    def test_swipe_rejects_coordinates_duration_and_display_before_ssh(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        cases = [
            (-1, 0, 1, 1, 220, None),
            (0, 0, 32768, 1, 220, None),
            (0, 0, 1, 1, 39, None),
            (0, 0, 1, 1, 5001, None),
            (0, 0, 1, 1, 220, "localhost:0"),
        ]
        with mock.patch("msys_tools.dev.ssh") as ssh:
            for x1, y1, x2, y2, duration, display in cases:
                with self.subTest(values=(x1, y1, x2, y2, duration, display)):
                    with self.assertRaises(ValueError):
                        command_swipe(
                            context,
                            "org.msys.shell.navigation-pill",
                            None,
                            x1,
                            y1,
                            x2,
                            y2,
                            duration,
                            display,
                        )
            ssh.assert_not_called()

    def test_swipe_can_target_a_legacy_title_without_an_identity(self) -> None:
        context = Context(Path("."), "target", "/opt/msys-dev", "python")
        with mock.patch("msys_tools.dev.ssh") as ssh:
            result = command_swipe(
                context,
                None,
                "Legacy App",
                10,
                20,
                30,
                40,
                200,
                runtime_dir="/tmp/msys-main",
            )

        self.assertEqual(result, 0)
        self.assertIn("'--title' 'Legacy App'", ssh.call_args.args[1])
        self.assertNotIn("'--identity'", ssh.call_args.args[1])

    def test_swipe_cli_uses_persisted_runtime_and_ssh_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            key = root / "key"
            key.write_text("test", encoding="utf-8")
            control = root / "ssh" / "control-%C"
            config.write_text(
                json.dumps(
                    {
                        "root": str(root),
                        "target": "root@example",
                        "remote": "/opt/custom-msys",
                        "runtime_dir": "/tmp/custom-session",
                        "ssh_key": str(key),
                        "ssh_control_path": str(control),
                        "ssh_control_persist": "17m",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "CONFIG_PATH", config),
                mock.patch.object(dev, "command_swipe", return_value=0) as swipe,
            ):
                result = dev.main(
                    [
                        "swipe",
                        "1",
                        "2",
                        "3",
                        "4",
                        "--window",
                        "org.example.app",
                        "Example App",
                    ]
                )

        self.assertEqual(result, 0)
        context = swipe.call_args.args[0]
        self.assertEqual(context.target, "root@example")
        self.assertEqual(context.remote, "/opt/custom-msys")
        self.assertEqual(context.ssh_control_path, control)
        self.assertEqual(context.ssh_control_persist, "17m")
        self.assertEqual(swipe.call_args.args[1:3], ("org.example.app", "Example App"))
        self.assertEqual(swipe.call_args.args[-1], "/tmp/custom-session")

    def test_default_tap_and_swipe_cli_use_the_replaceable_navigation_role(self) -> None:
        with (
            mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@example"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/config.json")),
            mock.patch.object(dev, "command_tap", return_value=0) as tap,
            mock.patch.object(dev, "command_swipe", return_value=0) as swipe,
        ):
            self.assertEqual(dev.main(["tap", "267", "459"]), 0)
            self.assertEqual(dev.main(["swipe", "160", "34", "160", "5"]), 0)

        self.assertEqual(tap.call_args.args[1:3], (None, None))
        self.assertEqual(tap.call_args.kwargs["role"], "navigation-bar")
        self.assertEqual(swipe.call_args.args[1:3], (None, None))
        self.assertEqual(swipe.call_args.kwargs["role"], "navigation-bar")

    def test_explicit_tap_identity_takes_priority_over_role_resolution(self) -> None:
        with (
            mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@example"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/config.json")),
            mock.patch.object(dev, "command_tap", return_value=0) as tap,
        ):
            result = dev.main(
                ["tap", "50", "20", "--identity", "org.example.navigation"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(tap.call_args.args[1:3], ("org.example.navigation", None))
        self.assertIsNone(tap.call_args.kwargs["role"])

    def test_swipe_cli_rejects_ambiguous_exact_window_selector(self) -> None:
        stderr = __import__("io").StringIO()
        with (
            mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@example"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/config.json")),
            mock.patch.object(dev, "command_swipe") as swipe,
            mock.patch("sys.stderr", stderr),
        ):
            result = dev.main(
                [
                    "swipe",
                    "1",
                    "2",
                    "3",
                    "4",
                    "--identity",
                    "org.example.app",
                    "--window",
                    "org.example.app",
                    "Example App",
                ]
            )

        self.assertEqual(result, 2)
        swipe.assert_not_called()
        self.assertIn("cannot be combined", stderr.getvalue())

    def test_notify_cli_forwards_a_bounded_timeout(self) -> None:
        with (
            mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@example"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/config.json")),
            mock.patch.object(dev, "command_broadcast", return_value=0) as broadcast,
        ):
            result = dev.main(
                ["notify", "short message", "--timeout-ms", "6000"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            broadcast.call_args.args[-1],
            {"message": "short message", "timeout_ms": 6000},
        )

    def test_notify_cli_rejects_an_unbounded_timeout(self) -> None:
        with self.assertRaises(SystemExit):
            dev.build_parser().parse_args(
                ["notify", "message", "--timeout-ms", "6001"]
            )


if __name__ == "__main__":
    unittest.main()
