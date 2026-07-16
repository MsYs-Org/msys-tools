from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


class SystemReleaseDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )

    def test_remote_release_helper_uses_only_the_isolated_development_runtime(self) -> None:
        with mock.patch.object(dev, "ssh_capture", return_value=completed()) as capture:
            result = dev._remote_release_command(
                self.context,
                "/opt/msys",
                "verify",
                ["release-1"],
            )
        self.assertEqual(result.returncode, 0)
        command = capture.call_args.args[1]
        self.assertIn("PYTHONPATH='/opt/msys-dev/msys-install'", command)
        self.assertIn("'/opt/msys-dev/.runtime/python/bin/python3'", command)
        self.assertIn("'/opt/msys-dev/.runtime/python/bin/python3' '-B' '-m'", command)
        self.assertIn("'--release-root' '/opt/msys' 'verify' 'release-1'", command)
        lowered = command.lower()
        for forbidden in ("systemctl", "dbus", "apt ", "apt-get", "pip install"):
            self.assertNotIn(forbidden, lowered)

    def test_stage_passes_an_explicit_reproducible_entry_set(self) -> None:
        with mock.patch.object(
            dev,
            "_remote_release_command",
            return_value=completed(0, '{"release_id":"r1"}\n'),
        ) as remote:
            result = dev.command_release_stage(
                self.context,
                "/opt/msys",
                "r1",
                "/opt/msys-dev",
                [".runtime", "msys-core", "msys-core"],
                keep=4,
                activate=False,
                restart_service=False,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
            )
        self.assertEqual(result, 0)
        _context, root, action, arguments = remote.call_args.args
        self.assertEqual(root, "/opt/msys")
        self.assertEqual(action, "stage")
        self.assertEqual(arguments.count("msys-core"), 1)
        self.assertEqual(arguments[-4:], ["--entry", ".runtime", "--entry", "msys-core"])

    def test_default_formal_entry_set_excludes_development_repositories(self) -> None:
        self.assertEqual(dev.COMPOSED_ENTRIES[0], ".runtime")
        self.assertIn("msys-core", dev.COMPOSED_ENTRIES)
        self.assertIn("msys-sdk", dev.COMPOSED_ENTRIES)
        self.assertNotIn("msys-contracts", dev.COMPOSED_ENTRIES)
        self.assertNotIn("msys-tools", dev.COMPOSED_ENTRIES)

    def test_compose_uses_isolated_python_and_never_switches_release(self) -> None:
        with mock.patch.object(
            dev,
            "ssh_capture",
            return_value=completed(0, '{"release_id":"candidate-1"}\n'),
        ) as capture:
            result = dev.command_release_compose(
                self.context,
                "/opt/msys",
                "candidate-1",
                "base-1",
                "/opt/msys-dev",
                "/opt/msys-dev/release-sources",
                ["msys-core=/opt/final/core"],
                [
                    "msys-shell-native=/opt/input/shell.maf",
                    "msys-audio=/opt/input/audio.maf",
                ],
                "/opt/msys-dev/tk-xft-runtime/candidates/verified-xft",
            )
        self.assertEqual(result, 0)
        command = capture.call_args.args[1]
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertIn("'/opt/msys-dev/.runtime/python/bin/python3' '-B'", command)
        self.assertIn("'msys_tools.release_compose'", command)
        self.assertIn("'--baseline-release' 'base-1'", command)
        self.assertIn(
            "'--python-runtime' "
            "'/opt/msys-dev/tk-xft-runtime/candidates/verified-xft'",
            command,
        )
        self.assertIn("'msys-core=/opt/final/core'", command)
        self.assertIn("'msys-shell-native=/opt/input/shell.maf'", command)
        self.assertIn("'msys-audio=/opt/input/audio.maf'", command)
        for forbidden in ("activate", "rollback", "restart", "systemctl"):
            self.assertNotIn(forbidden, command.lower())

    def test_failed_health_gate_restores_exact_previous_current_release(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                dev,
                "_release_status_document",
                return_value={"current": "known-good"},
            ),
            mock.patch.object(
                dev,
                "ssh_capture",
                side_effect=[
                    completed(),  # formal launcher prerequisite
                    completed(0, "stopped\n"),
                    completed(0, "started candidate\n"),
                    completed(0, "stopped candidate\n"),
                    completed(0, "started restored\n"),
                ],
            ),
            mock.patch.object(
                dev,
                "_remote_release_command",
                side_effect=[
                    completed(0, '{"verified":true}\n'),
                    completed(0, '{"verified":true}\n'),
                    completed(0, '{"current":"candidate"}\n'),
                    completed(0, '{"current":"known-good"}\n'),
                ],
            ) as release_command,
            mock.patch.object(
                dev,
                "_remote_lifecycle_command",
                side_effect=[
                    completed(1, '{"healthy":false}\n'),
                    completed(0, '{"healthy":true}\n'),
                ],
            ) as lifecycle_command,
            redirect_stdout(output),
        ):
            result = dev.command_release_switch(
                self.context,
                "/opt/msys",
                "activate",
                "candidate",
                restart_service=True,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
                health_timeout=137,
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            release_command.call_args_list[0].args[2:],
            ("verify", ["known-good"]),
        )
        self.assertEqual(
            release_command.call_args_list[3].args[2:],
            ("activate", ["known-good"]),
        )
        self.assertIn('"restored_healthy": true', output.getvalue())
        self.assertEqual(
            [call.kwargs["timeout"] for call in lifecycle_command.call_args_list],
            [137, 137],
        )

    def test_unverified_current_release_fails_before_service_or_pointer_change(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dev,
                "_release_status_document",
                return_value={"current": "known-good"},
            ),
            mock.patch.object(
                dev,
                "_remote_release_command",
                return_value=completed(
                    2,
                    "release known-good content digest changed\n",
                ),
            ) as release_command,
            mock.patch.object(dev, "ssh_capture") as ssh_capture,
            mock.patch.object(dev, "_remote_lifecycle_command") as lifecycle_command,
            redirect_stderr(stderr),
        ):
            result = dev.command_release_switch(
                self.context,
                "/opt/msys",
                "activate",
                "candidate",
                restart_service=True,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
            )

        self.assertEqual(result, 2)
        release_command.assert_called_once_with(
            self.context,
            "/opt/msys",
            "verify",
            ["known-good"],
        )
        ssh_capture.assert_not_called()
        lifecycle_command.assert_not_called()
        self.assertIn("content digest changed", stderr.getvalue())
        self.assertIn("refusing to stop the service or switch pointers", stderr.getvalue())

    def test_unverified_rollback_target_fails_before_service_or_pointer_change(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dev,
                "_release_status_document",
                return_value={"current": "known-good", "previous": "damaged"},
            ),
            mock.patch.object(
                dev,
                "_remote_release_command",
                side_effect=[
                    completed(0, '{"verified":true}\n'),
                    completed(2, "release damaged content digest changed\n"),
                ],
            ) as release_command,
            mock.patch.object(dev, "ssh_capture") as ssh_capture,
            mock.patch.object(dev, "_remote_lifecycle_command") as lifecycle_command,
            redirect_stderr(stderr),
        ):
            result = dev.command_release_switch(
                self.context,
                "/opt/msys",
                "rollback",
                None,
                restart_service=True,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            [call.args[2:] for call in release_command.call_args_list],
            [("verify", ["known-good"]), ("verify", ["damaged"])],
        )
        ssh_capture.assert_not_called()
        lifecycle_command.assert_not_called()
        self.assertIn("target release 'damaged' failed verification", stderr.getvalue())
        self.assertIn("refusing to stop the service or switch pointers", stderr.getvalue())

    def test_successful_health_checked_rollback_reports_actual_release_id(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                dev,
                "_release_status_document",
                return_value={"current": "release-2", "previous": "release-1"},
            ),
            mock.patch.object(
                dev,
                "_remote_release_command",
                side_effect=[
                    completed(0, '{"verified":true}\n'),
                    completed(0, '{"verified":true}\n'),
                    completed(0, '{"current":"release-1","previous":"release-2"}\n'),
                ],
            ),
            mock.patch.object(
                dev,
                "ssh_capture",
                side_effect=[
                    completed(),
                    completed(0, "stopped\n"),
                    completed(0, "started\n"),
                ],
            ),
            mock.patch.object(
                dev,
                "_remote_lifecycle_command",
                return_value=completed(0, '{"healthy":true}\n'),
            ),
            redirect_stdout(output),
        ):
            result = dev.command_release_switch(
                self.context,
                "/opt/msys",
                "rollback",
                None,
                restart_service=True,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
            )

        self.assertEqual(result, 0)
        self.assertIn('"current": "release-1"', output.getvalue())
        self.assertNotIn('"current": "previous"', output.getvalue())

    def test_stage_forwards_health_timeout_to_activated_release(self) -> None:
        with (
            mock.patch.object(
                dev,
                "_remote_release_command",
                return_value=completed(0, '{"release_id":"r1"}\n'),
            ),
            mock.patch.object(dev, "command_release_switch", return_value=0) as switch,
        ):
            result = dev.command_release_stage(
                self.context,
                "/opt/msys",
                "r1",
                "/opt/msys-dev",
                [".runtime", "msys-core"],
                keep=3,
                activate=True,
                restart_service=True,
                runtime_dir="/tmp/msys-main",
                log_file="/tmp/msysd.log",
                health_timeout=123,
            )
        self.assertEqual(result, 0)
        self.assertEqual(switch.call_args.kwargs["health_timeout"], 123)

    def test_release_health_timeout_cli_is_bounded_and_shared_by_switches(self) -> None:
        parser = dev.build_parser()
        cases = (
            (["release", "compose", "r1", "--baseline-release", "r0"], None),
            (["release", "stage", "r1"], dev.DEFAULT_RELEASE_HEALTH_TIMEOUT),
            (["release", "activate", "r1", "--health-timeout", "10"], 10.0),
            (["release", "rollback", "--health-timeout", "180"], 180.0),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                parsed = parser.parse_args(argv)
                if expected is None:
                    self.assertFalse(hasattr(parsed, "health_timeout"))
                else:
                    self.assertEqual(parsed.health_timeout, expected)

        for value in ("9.99", "180.01", "not-a-number"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(
                    ["release", "activate", "r1", "--health-timeout", value]
                )

        self.assertEqual(parser.parse_args(["release", "stage", "r1"]).keep, 2)

    def test_release_and_formal_service_cli_are_explicit_opt_in(self) -> None:
        parser = dev.build_parser()
        release = parser.parse_args(
            [
                "release",
                "stage",
                "r1",
                "--release-root",
                "/opt/msys",
                "--activate",
                "--restart-service",
            ]
        )
        self.assertEqual(release.release_command, "stage")
        self.assertEqual(release.release_root, "/opt/msys")
        service = parser.parse_args(
            ["host-service", "install", "--release-root", "/opt/msys"]
        )
        self.assertEqual(service.release_root, "/opt/msys")

    def test_cache_repair_is_preview_by_default_and_apply_is_explicit(self) -> None:
        parser = dev.build_parser()
        preview = parser.parse_args(
            ["release", "repair-python-cache", "dirty-1"]
        )
        self.assertFalse(preview.apply)
        self.assertIsNone(preview.backup)

        with mock.patch.object(dev, "command_release_simple", return_value=0) as remote:
            result = dev.main(
                [
                    "release",
                    "repair-python-cache",
                    "dirty-1",
                    "--apply",
                    "--backup",
                    "/opt/msys/repair-backups/dirty-1.tar.gz",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            remote.call_args.args[1:],
            (
                "/opt/msys",
                "repair-python-cache",
                [
                    "dirty-1",
                    "--apply",
                    "--backup",
                    "/opt/msys/repair-backups/dirty-1.tar.gz",
                ],
            ),
        )


if __name__ == "__main__":
    unittest.main()
