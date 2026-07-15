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

from msys_tools import acceptance, dev


def report_document(*, ok: bool = True) -> dict[str, object]:
    return {
        "schema": acceptance.SCHEMA,
        "ok": ok,
        "release": "2026.07.14-test",
        "runtime": {"healthy": ok, "pids": [123], "issues": []},
        "components": {
            "settings": [
                {
                    "id": "org.msys.settings:main",
                    "state": "declared",
                    "version": "0.2.10",
                    "path": "/opt/msys/releases/test/settings",
                }
            ],
            "apps": [
                {
                    "id": "org.msys.notes:notes",
                    "state": "declared",
                    "version": "0.1.8",
                }
            ],
            "input": [
                {
                    "id": "org.msys.input.touch:keyboard",
                    "state": "declared",
                    "version": "0.1.8",
                }
            ],
            "shell": [
                {
                    "id": "org.msys.shell.native:desktop-shell",
                    "state": "ready",
                    "version": "0.3.4",
                }
            ],
            "display": [
                {
                    "id": "org.msys.openstick.ch347:x11-spi-touch-output",
                    "state": "ready",
                    "version": "0.1.11",
                }
            ],
        },
        "display": {
            "source": "window-manager",
            "session": {
                "display": ":24",
                "provider": "org.msys.openstick.ch347:x11-spi-touch-output",
                "geometry": {"width": 320, "height": 480},
            },
        },
        "windows": {
            "available": True,
            "count": 3,
            "key_window_count": 2,
            "checks": [],
            "items": [],
        },
        "resources": {
            "disk_available_kib": 400000,
            "disk_used_percent": "84%",
            "memory_available_kib": 180000,
            "swap_used_kib": 20000,
        },
        "recent_warnings_errors": ["warning: cable was reconnected"],
        "issues": [] if ok else [{"code": "TEST_FAILURE", "message": "not ready"}],
    }


def bundle(
    report: dict[str, object],
    *,
    acceptance_status: int = 0,
    screenshot_status: int = 0,
    screenshot: bytes | None = None,
    token: str = "a" * 32,
) -> bytes:
    members: dict[str, bytes] = {
        "meta.json": json.dumps(
            {
                "schema": acceptance.ENVELOPE_SCHEMA,
                "acceptance_status": acceptance_status,
                "screenshot_status": screenshot_status,
            }
        ).encode(),
        "acceptance.json": json.dumps(report).encode(),
    }
    if screenshot is not None:
        members["screenshot.json"] = json.dumps(
            {
                "schema": acceptance.SCREENSHOT_SCHEMA,
                "ok": True,
                "path": f"/tmp/msys-screenshot-{token}.png",
                "size": len(screenshot),
                "display": ":24",
                "backend": "ffmpeg",
            }
        ).encode()
        members["screenshot.png"] = screenshot
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in members.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


class HostAcceptanceTests(unittest.TestCase):
    def config(self, **overrides: object) -> acceptance.AcceptanceConfig:
        values: dict[str, object] = {
            "remote": "/opt/msys-dev",
            "remote_python": "/opt/msys-dev/.runtime/python/bin/python3",
            "runtime_dir": "/tmp/msys-main",
            "log_file": "/tmp/msysd.log",
        }
        values.update(overrides)
        return acceptance.AcceptanceConfig(**values)  # type: ignore[arg-type]

    def test_read_only_report_uses_one_transport_and_summarizes_runtime(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 0, bundle(report_document()), b""
        )
        transport = mock.Mock(return_value=completed)
        output = io.StringIO()
        with redirect_stdout(output):
            status = acceptance.run(self.config(), transport)

        self.assertEqual(status, 0)
        transport.assert_called_once()
        command, label = transport.call_args.args
        self.assertIn("msys_tools.remote_acceptance", command)
        self.assertNotIn(" install ", command)
        self.assertNotIn("--method 'start'", command)
        self.assertNotIn("remote_lifecycle", command)
        self.assertIn("read-only", label)
        text = output.getvalue()
        self.assertIn("accept: ok=true release=2026.07.14-test", text)
        self.assertIn("org.msys.settings:main state=declared version=0.2.10", text)
        self.assertIn("windows: available=true count=3 key=2", text)
        self.assertIn("warning: cable was reconnected", text)

    def test_failed_acceptance_keeps_evidence_and_returns_probe_status(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 1, bundle(report_document(ok=False), acceptance_status=1), b""
        )
        output = io.StringIO()
        with redirect_stdout(output):
            status = acceptance.run(self.config(json_output=True), lambda *_: completed)

        self.assertEqual(status, 1)
        document = json.loads(output.getvalue())
        self.assertFalse(document["ok"])
        self.assertEqual(document["issues"][0]["code"], "TEST_FAILURE")

    def test_optional_screenshot_is_extracted_from_the_same_archive(self) -> None:
        png = acceptance.PNG_SIGNATURE + b"test"
        archive = bundle(report_document(), screenshot=png)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "accept.png"
            completed = subprocess.CompletedProcess(["ssh"], 0, archive, b"")
            transport = mock.Mock(return_value=completed)
            with (
                mock.patch.object(acceptance.secrets, "token_hex", return_value="a" * 32),
                redirect_stdout(io.StringIO()),
            ):
                status = acceptance.run(
                    self.config(
                        screenshot=output,
                        display=":24",
                        backend="ffmpeg",
                    ),
                    transport,
                )

            self.assertEqual(status, 0)
            self.assertEqual(output.read_bytes(), png)
            self.assertIn("msys_tools.remote_screenshot", transport.call_args.args[0])

    def test_invalid_window_expectation_fails_before_transport(self) -> None:
        transport = mock.Mock()
        error = io.StringIO()
        with redirect_stderr(error):
            status = acceptance.run(
                self.config(expect_windows=("unknown=value",)), transport
            )
        self.assertEqual(status, 2)
        transport.assert_not_called()
        self.assertIn("window expectation", error.getvalue())

    def test_archive_rejects_extra_members(self) -> None:
        data = bundle(report_document())
        stream = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source, tarfile.open(
            fileobj=stream, mode="w"
        ) as target:
            for entry in source.getmembers():
                target.addfile(entry, source.extractfile(entry))
            extra = tarfile.TarInfo("unexpected")
            extra.size = 0
            target.addfile(extra, io.BytesIO())
        completed = subprocess.CompletedProcess(["ssh"], 0, stream.getvalue(), b"")
        with redirect_stderr(io.StringIO()):
            status = acceptance.run(self.config(), lambda *_: completed)
        self.assertEqual(status, 2)


class AcceptanceCliTests(unittest.TestCase):
    def test_dev_cli_is_a_thin_config_and_transport_adapter(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
            mock.patch.object(dev, "CONFIG_PATH", Path(temporary) / "missing.json"),
            mock.patch.object(dev, "run_acceptance", return_value=7) as run,
        ):
            status = dev.main(
                [
                    "accept",
                    "--runtime-dir",
                    "/tmp/test-runtime",
                    "--log-file",
                    "/tmp/test.log",
                    "--logs",
                    "17",
                    "--strict-logs",
                    "--expect-window",
                    "role=desktop",
                    "--json",
                ]
            )

        self.assertEqual(status, 7)
        run.assert_called_once()
        config = run.call_args.args[0]
        self.assertIsInstance(config, acceptance.AcceptanceConfig)
        self.assertEqual(config.runtime_dir, "/tmp/test-runtime")
        self.assertEqual(config.log_file, "/tmp/test.log")
        self.assertEqual(config.lines, 17)
        self.assertTrue(config.strict_logs)
        self.assertEqual(config.expect_windows, ("role=desktop",))
        self.assertTrue(config.json_output)
        self.assertTrue(callable(run.call_args.args[1]))


if __name__ == "__main__":
    unittest.main()
