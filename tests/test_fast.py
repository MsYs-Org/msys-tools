from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev


class FastWorkflowTests(unittest.TestCase):
    def context(self, root: Path = Path("/workspace")) -> dev.Context:
        return dev.Context(
            root=root,
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
            ssh_control_path=Path("/tmp/control-%C"),
            ssh_control_persist="2h",
        )

    @staticmethod
    def report_archive(*, screenshot: bytes | None = None) -> bytes:
        members = {
            "meta.json": json.dumps(
                {
                    "schema": "msys.fast-report.v1",
                    "health_status": 0,
                    "screenshot_status": 0,
                }
            ).encode(),
            "status.txt": json.dumps(
                {
                    "schema": "msys.runtime-status.v1",
                    "healthy": True,
                    "critical_components": ["org.example:shell"],
                    "issues": [],
                }
            ).encode(),
            "components.json": json.dumps(
                {
                    "type": "return",
                    "payload": {
                        "components": [
                            {
                                "id": "org.example:shell",
                                "state": "ready",
                                "package_version": "1.2.3",
                                "path": "/opt/msys/releases/r1/shell",
                            }
                        ]
                    },
                }
            ).encode(),
            "system.txt": (
                b"current_release=r1\n"
                b"disk_available_kib=500000\n"
                b"disk_used_percent=82%\n"
                b"memory_total_kib=256000\n"
                b"memory_available_kib=170000\n"
                b"swap_used_kib=24000\n"
            ),
            "log.txt": b"warning: display cable loose\n",
        }
        if screenshot is not None:
            members["screenshot.json"] = json.dumps(
                {
                    "schema": "msys.debug-screenshot.v1",
                    "ok": True,
                    "path": "/tmp/msys-screenshot-" + "a" * 32 + ".png",
                    "size": len(screenshot),
                    "display": ":24",
                    "backend": "ffmpeg",
                }
            ).encode()
            members["screenshot.png"] = screenshot
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return stream.getvalue()

    def test_default_fast_never_starts_or_installs_and_reports_existing_runtime(self) -> None:
        with (
            mock.patch.object(dev, "command_sync", return_value=0) as sync,
            mock.patch.object(dev, "command_run") as run,
            mock.patch.object(dev, "command_package_deliver") as deliver,
            mock.patch.object(dev, "command_fast_report", return_value=0) as report,
        ):
            result = dev.command_fast(
                self.context(),
                ["msys-settings"],
                safe=False,
                profile="mobile-spi",
                runtime_dir="/tmp/msys-main",
                state_dir="/opt/msys-state",
                log_file="/tmp/msysd.log",
                run=False,
                deliver=False,
                lines=80,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=45,
                force=False,
                full_sync=False,
            )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(self.context(), ["msys-settings"], force=False)
        run.assert_not_called()
        deliver.assert_not_called()
        report.assert_called_once()

    def test_bare_fast_is_diagnostic_only_without_full_workspace_sync(self) -> None:
        with (
            mock.patch.object(dev, "command_sync") as sync,
            mock.patch.object(dev, "command_fast_report", return_value=0) as report,
        ):
            result = dev.command_fast(
                self.context(),
                [],
                safe=False,
                profile="mobile-spi",
                runtime_dir="/tmp/msys-main",
                state_dir="/opt/msys-state",
                log_file="/tmp/msysd.log",
                run=False,
                deliver=False,
                lines=20,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=15,
                force=False,
                full_sync=False,
            )

        self.assertEqual(result, 0)
        sync.assert_not_called()
        report.assert_called_once()

    def test_report_fetches_compact_health_resources_and_errors_in_one_ssh(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 0, stdout=self.report_archive(), stderr=b""
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "ssh_capture_bytes", return_value=completed) as ssh,
            redirect_stdout(output),
        ):
            result = dev.command_fast_report(
                self.context(),
                "/tmp/msys-main",
                "/tmp/msysd.log",
                lines=80,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=45,
                force=False,
            )

        self.assertEqual(result, 0)
        ssh.assert_called_once()
        command = ssh.call_args.args[1]
        self.assertIn("remote_lifecycle", command)
        self.assertIn("list_components", command)
        self.assertIn("disk_available_kib", command)
        self.assertIn("warning|failed", command)
        text = output.getvalue()
        self.assertIn("healthy=true current_release=r1", text)
        self.assertIn("org.example:shell state=ready version=1.2.3", text)
        self.assertIn("display cable loose", text)
        self.assertNotIn('"components":', text)

    def test_report_can_return_png_without_scp_or_cleanup_calls(self) -> None:
        png = dev.PNG_SIGNATURE + b"test-png"
        archive = self.report_archive(screenshot=png)
        # The random path must match the command's generated token.
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "screen.png"
            completed = subprocess.CompletedProcess(["ssh"], 0, archive, b"")
            with (
                mock.patch.object(dev.secrets, "token_hex", return_value="a" * 32),
                mock.patch.object(dev, "ssh_capture_bytes", return_value=completed) as ssh,
                mock.patch.object(dev, "ssh") as ordinary_ssh,
                mock.patch.object(dev, "run_local") as local_transport,
            ):
                result = dev.command_fast_report(
                    self.context(),
                    "/tmp/msys-main",
                    "/tmp/msysd.log",
                    lines=80,
                    screenshot=output,
                    display=":24",
                    backend="ffmpeg",
                    timeout=10,
                    force=False,
                )

            self.assertEqual(result, 0)
            self.assertEqual(output.read_bytes(), png)
            ssh.assert_called_once()
            ordinary_ssh.assert_not_called()
            local_transport.assert_not_called()

    def test_fast_cli_defaults_to_status_bundle_and_requires_explicit_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "msys-settings").mkdir()
            with (
                mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_fast", return_value=0) as fast,
            ):
                status = dev.main(
                    ["fast", "--root", str(root), "--repo", "msys-settings"]
                )
                run_status = dev.main(
                    ["q", "--root", str(root), "--repo", "msys-settings", "--run"]
                )

        self.assertEqual((status, run_status), (0, 0))
        self.assertFalse(fast.call_args_list[0].kwargs["run"])
        self.assertFalse(fast.call_args_list[0].kwargs["deliver"])
        self.assertTrue(fast.call_args_list[1].kwargs["run"])

    def test_canonical_settings_and_apps_delivery_get_required_sdk_overlay(self) -> None:
        for repository, package_id in (
            ("msys-settings", "org.msys.settings"),
            ("msys-apps", "org.msys.apps"),
        ):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package = root / repository
                package.mkdir()
                (package / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema": "msys.manifest.v1",
                            "package": {"id": package_id},
                        }
                    ),
                    encoding="utf-8",
                )
                sdk = root / "msys-sdk" / "msys_sdk"
                sdk.mkdir(parents=True)
                (sdk / "__init__.py").write_text("", encoding="utf-8")
                output = io.StringIO()
                with (
                    mock.patch.object(dev, "command_sync", return_value=0),
                    mock.patch.object(
                        dev, "command_package_deliver", return_value=0
                    ) as deliver,
                    mock.patch.object(dev, "command_fast_report", return_value=0),
                    redirect_stdout(output),
                ):
                    result = dev.command_fast(
                        self.context(root),
                        [repository],
                        safe=False,
                        profile="mobile-spi",
                        runtime_dir="/tmp/msys-main",
                        state_dir="/opt/msys-state",
                        log_file="/tmp/msysd.log",
                        run=False,
                        deliver=True,
                        lines=0,
                        screenshot=None,
                        display=None,
                        backend="auto",
                        timeout=45,
                        force=False,
                        full_sync=False,
                    )

                self.assertEqual(result, 0)
                overlays = deliver.call_args.kwargs["overlays"]
                self.assertEqual(len(overlays), 1)
                self.assertEqual(overlays[0].source, sdk.resolve())
                self.assertEqual(
                    overlays[0].destination.as_posix(), "files/app/msys_sdk"
                )
                self.assertIn("automatically overlaying", output.getvalue())
                self.assertIn(repository, output.getvalue())

    def test_fast_cli_accepts_repeated_explicit_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "msys-settings").mkdir()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            with (
                mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_fast", return_value=0) as fast,
            ):
                result = dev.main(
                    [
                        "fast",
                        "--root",
                        str(root),
                        "--repo",
                        "msys-settings",
                        "--deliver",
                        "--overlay",
                        "first=files/app/first",
                        "--overlay",
                        "second=files/app/second",
                    ]
                )

        self.assertEqual(result, 0)
        overlays = fast.call_args.kwargs["overlays"]
        self.assertEqual(len(overlays), 2)
        self.assertEqual(
            [item.destination.as_posix() for item in overlays],
            ["files/app/first", "files/app/second"],
        )


if __name__ == "__main__":
    unittest.main()
