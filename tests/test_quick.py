from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev


class QuickCommandTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            root=Path("/workspace"),
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_default_is_sync_then_run_without_repeated_full_gates(self) -> None:
        with (
            mock.patch.object(dev, "command_doctor") as doctor,
            mock.patch.object(dev, "command_sync", return_value=0) as sync,
            mock.patch.object(dev, "command_run", return_value=0) as run,
            mock.patch.object(dev, "command_status") as status,
            mock.patch.object(dev, "command_screenshot") as screenshot,
        ):
            result = dev.command_quick(
                self.context(),
                ["msys-settings"],
                safe=False,
                profile="desktop-spi",
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
                status_only=False,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=15.0,
                force=False,
            )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(self.context(), ["msys-settings"])
        run.assert_called_once_with(
            self.context(),
            "desktop-spi",
            "/tmp/msys-main",
            "/tmp/msysd.log",
            self.context().remote_python,
            15.0,
        )
        doctor.assert_not_called()
        status.assert_not_called()
        screenshot.assert_not_called()

    def test_safe_status_and_screenshot_are_one_ordered_thin_composition(self) -> None:
        calls: list[str] = []

        def completed(name: str):
            def run(*_args: object, **_kwargs: object) -> int:
                calls.append(name)
                return 0

            return run

        output = Path("/workspace/dist/quick.png")
        with (
            mock.patch.object(dev, "command_doctor", side_effect=completed("doctor")),
            mock.patch.object(dev, "command_sync", side_effect=completed("sync")),
            mock.patch.object(dev, "command_run") as run,
            mock.patch.object(dev, "command_status", side_effect=completed("status")),
            mock.patch.object(
                dev, "command_screenshot", side_effect=completed("screenshot")
            ) as screenshot,
        ):
            result = dev.command_quick(
                self.context(),
                ["msys-settings", "msys-apps"],
                safe=True,
                profile="desktop-spi",
                runtime_dir="/tmp/quick",
                log_file="/tmp/quick.log",
                status_only=True,
                screenshot=output,
                display=":77",
                backend="scrot",
                timeout=8.0,
                force=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(calls, ["doctor", "sync", "status", "screenshot"])
        run.assert_not_called()
        screenshot.assert_called_once_with(
            self.context(),
            "/tmp/quick",
            output,
            display=":77",
            backend="scrot",
            timeout=8.0,
            force=True,
        )

    def test_failed_gate_or_sync_stops_before_later_mutations(self) -> None:
        with (
            mock.patch.object(dev, "command_doctor", return_value=7),
            mock.patch.object(dev, "command_sync") as sync,
            mock.patch.object(dev, "command_run") as run,
            mock.patch.object(dev, "command_status") as status,
            mock.patch.object(dev, "command_screenshot") as screenshot,
        ):
            result = dev.command_quick(
                self.context(),
                ["msys-settings"],
                safe=True,
                profile="desktop-spi",
                runtime_dir="/tmp/main",
                log_file="/tmp/main.log",
                status_only=True,
                screenshot=Path("/tmp/fail.png"),
                display=None,
                backend="auto",
                timeout=15.0,
                force=False,
            )

        self.assertEqual(result, 7)
        sync.assert_not_called()
        run.assert_not_called()
        status.assert_not_called()
        screenshot.assert_not_called()

    def test_full_sync_is_an_explicit_passthrough_not_another_workflow(self) -> None:
        with (
            mock.patch.object(dev, "command_sync", return_value=0) as sync,
            mock.patch.object(dev, "command_run", return_value=0),
        ):
            result = dev.command_quick(
                self.context(),
                ["msys-sdk"],
                safe=False,
                profile="desktop-spi",
                runtime_dir="/tmp/main",
                log_file="/tmp/main.log",
                status_only=False,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=15.0,
                force=False,
                full_sync=True,
            )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(self.context(), ["msys-sdk"], force=True)

        with (
            mock.patch.object(dev, "command_doctor") as doctor,
            mock.patch.object(dev, "command_sync", return_value=9),
            mock.patch.object(dev, "command_run") as run,
            mock.patch.object(dev, "command_status") as status,
            mock.patch.object(dev, "command_screenshot") as screenshot,
        ):
            result = dev.command_quick(
                self.context(),
                ["msys-settings"],
                safe=False,
                profile="desktop-spi",
                runtime_dir="/tmp/main",
                log_file="/tmp/main.log",
                status_only=False,
                screenshot=Path("/tmp/fail.png"),
                display=None,
                backend="auto",
                timeout=15.0,
                force=False,
            )

        self.assertEqual(result, 9)
        doctor.assert_not_called()
        run.assert_not_called()
        status.assert_not_called()
        screenshot.assert_not_called()

    def test_cli_routes_quick_and_deploy_alias_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "quick.png"
            with (
                mock.patch.dict(
                    os.environ,
                    {"MSYS_DEV_TARGET": "root@device"},
                    clear=False,
                ),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_quick", return_value=0) as quick,
            ):
                result = dev.main(
                    [
                        "quick",
                        "--root",
                        str(root),
                        "--repo",
                        "msys-settings",
                        "--safe",
                        "--profile",
                        "desktop-spi",
                        "--runtime-dir",
                        "/tmp/quick",
                        "--log-file",
                        "/tmp/quick.log",
                        "--status",
                        "--screenshot",
                        str(output),
                        "--display",
                        ":77",
                        "--backend",
                        "scrot",
                        "--timeout",
                        "8",
                        "--force",
                    ]
                )
                alias_result = dev.main(
                    ["deploy", "--root", str(root), "--repo", "msys-apps"]
                )

        self.assertEqual(result, 0)
        self.assertEqual(alias_result, 0)
        first = quick.call_args_list[0]
        self.assertEqual(first.args[1], ["msys-settings"])
        self.assertEqual(
            first.kwargs,
            {
                "safe": True,
                "profile": "desktop-spi",
                "runtime_dir": "/tmp/quick",
                "log_file": "/tmp/quick.log",
                "status_only": True,
                "screenshot": output,
                "display": ":77",
                "backend": "scrot",
                "timeout": 8.0,
                "force": True,
                "full_sync": False,
            },
        )
        self.assertEqual(quick.call_args_list[1].args[1], ["msys-apps"])


if __name__ == "__main__":
    unittest.main()
