from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    def report_archive(
        *,
        screenshot: bytes | None = None,
        audio: bool = False,
        audio_status: int = 0,
    ) -> bytes:
        metadata = {
            "schema": "msys.fast-report.v1",
            "health_status": 0,
            "screenshot_status": 0,
        }
        if audio:
            metadata["audio_status"] = audio_status
        members = {
            "meta.json": json.dumps(metadata).encode(),
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
        if audio:
            components = json.loads(members["components.json"])
            components["payload"]["components"].append(
                {
                    "id": "org.msys.audio.bluez:audio-manager",
                    "state": "ready",
                    "package_version": "0.1.0",
                    "provides": [
                        {
                            "kind": "role",
                            "name": "audio-manager",
                            "exclusive": True,
                            "priority": 100,
                        }
                    ],
                }
            )
            members["components.json"] = json.dumps(components).encode()
            if audio_status:
                audio_document = {
                    "type": "error",
                    "id": 1,
                    "code": "ROLE_UNAVAILABLE",
                    "message": "no provider selected for role audio-manager",
                }
            else:
                audio_document = {
                    "type": "return",
                    "id": 1,
                    "payload": {
                        "schema": "msys.audio-state.v1",
                        "backend": "bluealsa",
                        "available": False,
                        "reason": "controller-not-registered",
                        "stack": [
                            {
                                "name": "bluetoothd",
                                "pid": 41,
                                "running": True,
                                "returncode": None,
                            }
                        ],
                        "outputs": [],
                    },
                }
            members["audio.json"] = json.dumps(audio_document).encode()
            members["audio-processes.txt"] = (
                b"268 1 6500 18000 S dbus-daemon /usr/bin/dbus-daemon --system\n"
                b"41 20 3720 12600 S bluetoothd "
                b"/opt/msys-state/packages/org.msys.audio.bluez/versions/0.1.5/"
                b"files/runtime/aarch64/bin/bluetoothd -n\n"
                b"42 20 2480 9800 S bluealsa "
                b"/opt/msys-state/packages/org.msys.audio.bluez/versions/0.1.5/"
                b"files/runtime/aarch64/bin/bluealsa\n"
                b"43 20 1000 8000 S squeezelite "
                b"/opt/msys-state/packages/org.msys.audio.bluez/versions/0.1.5/"
                b"files/runtime/aarch64/bin/squeezelite -n MSYS\n"
            )
            members["audio-memory.txt"] = b"41 1800\n42 1300\n43 700\n"
            members["audio-host-conflicts.txt"] = (
                b"13762 1 S bluetoothd /usr/libexec/bluetooth/bluetoothd\n"
            )
            members["audio-log.txt"] = b"msys-audio: private stack started\n"
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
        self.assertIn("health: ok release=r1", text)
        self.assertNotIn("org.example:shell", text)
        self.assertNotIn("display cable loose", text)
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

    def test_audio_report_fetches_role_state_process_rss_and_log_in_one_ssh(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 0, stdout=self.report_archive(audio=True), stderr=b""
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
                audio=True,
            )

        self.assertEqual(result, 0)
        ssh.assert_called_once()
        command = ssh.call_args.args[1]
        self.assertIn("role:audio-manager", command)
        self.assertIn("get_state", command)
        self.assertIn("audio-processes.txt", command)
        self.assertIn("audio-memory.txt", command)
        self.assertIn("audio-host-conflicts.txt", command)
        self.assertIn('index($7, "/org.msys.audio.bluez/")', command)
        self.assertIn("msys-audio|audio-manager|bluez|bluealsa", command)
        self.assertIn("public control socket", command)
        self.assertIn("{n=0; next}", command)
        text = output.getvalue()
        self.assertIn("health: ok release=r1", text)
        self.assertIn("component=org.msys.audio.bluez:audio-manager", text)
        self.assertIn("backend=bluealsa", text)
        self.assertIn("reason=controller-not-registered", text)
        self.assertIn("rss_total=7200KiB", text)
        self.assertIn("pss_total=3800KiB", text)
        self.assertIn("audio host conflicts: count=1", text)
        self.assertIn("/usr/libexec/bluetooth/bluetoothd", text)
        self.assertNotIn("pid=268", text)
        self.assertIn("command=squeezelite", text)
        self.assertIn("private stack started", text)

    def test_audio_debug_cli_is_read_only_and_routes_to_one_pass_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_fast_report", return_value=0) as report,
            ):
                status = dev.main(
                    [
                        "audio-debug",
                        "--root",
                        str(root),
                        "--runtime-dir",
                        "/tmp/msys-main",
                        "--no-logs",
                        "--json",
                    ]
                )

        self.assertEqual(status, 0)
        report.assert_called_once()
        self.assertTrue(report.call_args.kwargs["audio"])
        self.assertEqual(report.call_args.kwargs["lines"], 0)
        self.assertTrue(report.call_args.kwargs["json_output"])

    def test_audio_report_fails_clearly_when_role_is_not_registered(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"],
            1,
            stdout=self.report_archive(audio=True, audio_status=1),
            stderr=b"",
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "ssh_capture_bytes", return_value=completed),
            redirect_stdout(output),
        ):
            status = dev.command_fast_report(
                self.context(),
                "/tmp/msys-main",
                "/tmp/msysd.log",
                lines=80,
                screenshot=None,
                display=None,
                backend="auto",
                timeout=45,
                force=False,
                audio=True,
            )

        self.assertEqual(status, 1)
        self.assertIn("registered=false", output.getvalue())
        self.assertIn("ROLE_UNAVAILABLE", output.getvalue())

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

    def test_fast_cli_can_include_audio_acceptance_in_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "msys-audio").mkdir()
            with (
                mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_fast", return_value=0) as fast,
            ):
                status = dev.main(
                    [
                        "fast",
                        "--root",
                        str(root),
                        "--repo",
                        "msys-audio",
                        "--audio",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertTrue(fast.call_args.kwargs["audio"])

    def test_canonical_packages_get_required_sdk_overlay(self) -> None:
        for repository, package_id in (
            ("msys-settings", "org.msys.settings"),
            ("msys-notes", "org.msys.notes"),
            ("msys-calculator", "org.msys.calculator"),
            ("msys-device-info", "org.msys.device-info"),
            ("msys-file-manager", "org.msys.file-manager"),
            ("msys-touch-calibration", "org.msys.touch-calibration"),
            ("msys-input-touch", "org.msys.input.touch"),
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
                    mock.patch.object(dev, "command_sync", return_value=0) as sync,
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
                sync.assert_not_called()
                overlays = deliver.call_args.kwargs["overlays"]
                self.assertEqual(len(overlays), 1)
                self.assertEqual(overlays[0].source, sdk.resolve())
                self.assertEqual(
                    overlays[0].destination.as_posix(), "files/app/msys_sdk"
                )
                self.assertNotIn("automatically overlaying", output.getvalue())

    def test_batch_package_delivery_skips_source_sync_installs_in_order_and_reports_once(self) -> None:
        repositories = ["msys-settings", "msys-calculator", "msys-input-touch"]
        package_ids = [
            "org.msys.settings",
            "org.msys.calculator",
            "org.msys.input.touch",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "msys-sdk" / "msys_sdk"
            sdk.mkdir(parents=True)
            (sdk / "__init__.py").write_text("", encoding="utf-8")
            for repository, package_id in zip(repositories, package_ids):
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
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(
                    dev, "command_package_deliver", return_value=0
                ) as deliver,
                mock.patch.object(
                    dev, "command_fast_report", return_value=0
                ) as report,
            ):
                result = dev.command_fast(
                    self.context(root),
                    repositories,
                    safe=False,
                    profile="mobile-spi",
                    runtime_dir="/tmp/msys-main",
                    state_dir="/opt/msys-state",
                    log_file="/tmp/msysd.log",
                    run=False,
                    deliver=True,
                    lines=40,
                    screenshot=None,
                    display=None,
                    backend="auto",
                    timeout=45,
                    force=False,
                    full_sync=False,
                )

        self.assertEqual(result, 0)
        sync.assert_not_called()
        self.assertEqual(
            [call.args[2].name for call in deliver.call_args_list], repositories
        )
        self.assertEqual(len(deliver.call_args_list), 3)
        for call in deliver.call_args_list:
            overlays = call.kwargs["overlays"]
            self.assertEqual(len(overlays), 1)
            self.assertEqual(overlays[0].source, sdk.resolve())
            self.assertEqual(
                overlays[0].destination.as_posix(), "files/app/msys_sdk"
            )
        report.assert_called_once()

    def test_target_native_delivery_still_syncs_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "msys-hal"
            package.mkdir()
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "msys.manifest.v1",
                        "package": {"id": "org.msys.hal.linux"},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(dev, "command_package_deliver", return_value=0),
                mock.patch.object(dev, "command_fast_report", return_value=0),
            ):
                result = dev.command_fast(
                    self.context(root),
                    ["msys-hal"],
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
        sync.assert_called_once_with(self.context(root), ["msys-hal"], force=False)

    def test_audio_delivery_syncs_target_helper_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "msys-audio"
            package.mkdir()
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "msys.manifest.v1",
                        "package": {"id": "org.msys.audio.bluez"},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(
                    dev, "command_package_deliver", return_value=0
                ) as deliver,
                mock.patch.object(dev, "command_fast_report", return_value=0),
            ):
                result = dev.command_fast(
                    self.context(root),
                    ["msys-audio"],
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
                    audio=True,
                )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(
            self.context(root), ["msys-audio"], force=False
        )
        deliver.assert_called_once()

    def test_audio_native_manager_opt_in_reaches_target_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "msys-audio"
            package.mkdir()
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "msys.manifest.v1",
                        "package": {"id": "org.msys.audio.bluez"},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(dev, "command_package_deliver", return_value=0),
                mock.patch.object(dev, "command_fast_report", return_value=0),
            ):
                result = dev.command_fast(
                    self.context(root),
                    ["msys-audio"],
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
                    native_audio_manager=True,
                )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(
            self.context(root),
            ["msys-audio"],
            force=False,
            native_audio_manager=True,
        )

    def test_full_sync_preserves_explicit_source_upload_for_package_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package-a"
            package.mkdir()
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "msys.manifest.v1",
                        "package": {"id": "org.example.package-a"},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(dev, "command_package_deliver", return_value=0),
                mock.patch.object(dev, "command_fast_report", return_value=0),
            ):
                result = dev.command_fast(
                    self.context(root),
                    ["package-a"],
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
                    full_sync=True,
                )

        self.assertEqual(result, 0)
        sync.assert_called_once_with(self.context(root), ["package-a"], force=True)

    def test_batch_explicit_overlay_is_rejected_before_sync(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(dev, "command_sync") as sync,
            mock.patch.object(dev, "command_package_deliver") as deliver,
            mock.patch.object(dev, "command_fast_report") as report,
            redirect_stderr(stderr),
        ):
            result = dev.command_fast(
                self.context(),
                ["msys-settings", "msys-calculator"],
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
                overlays=[mock.sentinel.overlay],
            )

        self.assertEqual(result, 2)
        self.assertIn("only with exactly one --repo", stderr.getvalue())
        sync.assert_not_called()
        deliver.assert_not_called()
        report.assert_not_called()

    def test_batch_stops_on_first_delivery_failure_without_final_report(self) -> None:
        repositories = ["package-a", "package-b", "package-c"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for repository in repositories:
                package = root / repository
                package.mkdir()
                (package / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema": "msys.manifest.v1",
                            "package": {"id": f"org.example.{repository}"},
                        }
                    ),
                    encoding="utf-8",
                )
            with (
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
                mock.patch.object(
                    dev, "command_package_deliver", side_effect=[0, 9]
                ) as deliver,
                mock.patch.object(dev, "command_fast_report") as report,
            ):
                result = dev.command_fast(
                    self.context(root),
                    repositories,
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

        self.assertEqual(result, 9)
        sync.assert_not_called()
        self.assertEqual(
            [call.args[2].name for call in deliver.call_args_list],
            repositories[:2],
        )
        report.assert_not_called()

    def test_batch_with_core_or_tools_is_rejected_before_sync(self) -> None:
        for blocked in ("msys-core", "msys-tools"):
            with self.subTest(blocked=blocked):
                with (
                    mock.patch.object(dev, "command_sync") as sync,
                    mock.patch.object(dev, "command_package_deliver") as deliver,
                    mock.patch.object(dev, "command_fast_report") as report,
                ):
                    result = dev.command_fast(
                        self.context(),
                        ["msys-settings", blocked],
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
                    self.assertEqual(result, 2)
                    sync.assert_not_called()
                    deliver.assert_not_called()
                    report.assert_not_called()

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

    def test_fast_cli_preserves_repeated_repository_order_for_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repositories = ["msys-settings", "msys-calculator", "msys-input-touch"]
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
                        repositories[0],
                        "--repo",
                        repositories[1],
                        "--repo",
                        repositories[2],
                        "--deliver",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(fast.call_args.args[1], repositories)
        self.assertTrue(fast.call_args.kwargs["deliver"])


if __name__ == "__main__":
    unittest.main()
