from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.dev import Context
from msys_tools.remote_screenshot import (
    PNG_SIGNATURE,
    ScreenshotError,
    capture_screenshot,
)


class RemoteScreenshotBackendTests(unittest.TestCase):
    def test_scrot_is_preferred_without_invoking_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture.png"
            commands: list[list[str]] = []

            def which(name: str) -> str | None:
                return f"/usr/bin/{name}" if name in {"scrot", "ffmpeg"} else None

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(argv)
                output.write_bytes(PNG_SIGNATURE + b"scrot")
                output.with_name(f"{output.stem}_000.png").write_bytes(b"alternate")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                mock.patch("msys_tools.remote_screenshot.shutil.which", side_effect=which),
                mock.patch("msys_tools.remote_screenshot.subprocess.run", side_effect=run),
            ):
                result = capture_screenshot(output, ":24")

        self.assertEqual(result["backend"], "scrot")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], "/usr/bin/scrot")
        self.assertIn("--overwrite", commands[0])
        self.assertIn("--silent", commands[0])
        self.assertFalse(output.with_name(f"{output.stem}_000.png").exists())

    def test_failed_scrot_falls_back_to_sized_ffmpeg_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture.png"
            commands: list[list[str]] = []

            def which(name: str) -> str | None:
                return f"/usr/bin/{name}" if name in {"scrot", "ffmpeg", "xdpyinfo"} else None

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(argv)
                if argv[0].endswith("scrot"):
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no root")
                if argv[0].endswith("xdpyinfo"):
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout="  dimensions:    320x480 pixels (1x1 millimeters)\n",
                        stderr="",
                    )
                output.write_bytes(PNG_SIGNATURE + b"ffmpeg")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                mock.patch("msys_tools.remote_screenshot.shutil.which", side_effect=which),
                mock.patch("msys_tools.remote_screenshot.subprocess.run", side_effect=run),
            ):
                result = capture_screenshot(output, ":24")

        self.assertEqual(result["backend"], "ffmpeg")
        ffmpeg = next(command for command in commands if command[0].endswith("ffmpeg"))
        self.assertIn("-video_size", ffmpeg)
        self.assertIn("320x480", ffmpeg)
        self.assertIn("-draw_mouse", ffmpeg)
        self.assertEqual(ffmpeg[ffmpeg.index("-draw_mouse") + 1], "0")
        self.assertLess(ffmpeg.index("-draw_mouse"), ffmpeg.index("-i"))
        self.assertEqual(ffmpeg[ffmpeg.index("-i") + 1], ":24")
        self.assertIn("image2pipe", ffmpeg)
        self.assertEqual(ffmpeg[-1], "pipe:1")

    def test_legacy_ffmpeg_retries_without_draw_mouse_only_when_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture.png"
            ffmpeg_commands: list[list[str]] = []

            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                ffmpeg_commands.append(argv)
                if "-draw_mouse" in argv:
                    return subprocess.CompletedProcess(
                        argv,
                        1,
                        stdout=b"",
                        stderr=b"Unrecognized option 'draw_mouse'. Option not found.\n",
                    )
                output.write_bytes(PNG_SIGNATURE + b"legacy-ffmpeg")
                return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "msys_tools.remote_screenshot.shutil.which",
                    side_effect=lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
                ),
                mock.patch("msys_tools.remote_screenshot.subprocess.run", side_effect=run),
            ):
                result = capture_screenshot(output, ":24", backend="ffmpeg")

        self.assertEqual(result["backend"], "ffmpeg")
        self.assertEqual(len(ffmpeg_commands), 2)
        self.assertIn("-draw_mouse", ffmpeg_commands[0])
        self.assertNotIn("-draw_mouse", ffmpeg_commands[1])

    def test_missing_backends_has_an_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("msys_tools.remote_screenshot.shutil.which", return_value=None):
                with self.assertRaisesRegex(ScreenshotError, "static target binary"):
                    capture_screenshot(Path(temporary) / "capture.png", ":24")


class ScreenshotCommandTests(unittest.TestCase):
    def context(self, root: Path) -> Context:
        return Context(
            root,
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_download_is_verified_committed_and_remote_temp_is_cleaned(self) -> None:
        png = PNG_SIGNATURE + b"verified-image"
        token = "a" * 32
        remote_path = f"/tmp/msys-screenshot-{token}.png"
        response = json.dumps({
            "schema": "msys.debug-screenshot.v1",
            "ok": True,
            "display": ":24",
            "backend": "scrot",
            "path": remote_path,
            "size": len(png),
        })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "shots" / "screen.png"

            def download(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
                self.assertFalse(check)
                Path(argv[-1]).write_bytes(png)
                return subprocess.CompletedProcess(argv, 0)

            with (
                mock.patch.object(dev.secrets, "token_hex", return_value=token),
                mock.patch.object(
                    dev,
                    "ssh_capture",
                    return_value=subprocess.CompletedProcess([], 0, stdout=response),
                ) as capture,
                mock.patch.object(dev, "run_local", side_effect=download) as scp,
                mock.patch.object(
                    dev,
                    "ssh",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as cleanup,
            ):
                status = dev.command_screenshot(
                    self.context(root),
                    "/tmp/msys-main",
                    output,
                    display=None,
                    backend="auto",
                    timeout=15,
                    force=False,
                )

            self.assertEqual(status, 0)
            self.assertEqual(output.read_bytes(), png)
            remote_command = capture.call_args.args[1]
            self.assertIn("msys_tools.remote_screenshot", remote_command)
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", remote_command)
            self.assertIn("python3' '-B' '-m'", remote_command)
            self.assertNotIn("'--display'", remote_command)
            self.assertEqual(scp.call_args.args[0][-2], f"root@device:{remote_path}")
            cleanup_command = cleanup.call_args.args[1]
            self.assertIn(f"rm -f -- '{remote_path}'", cleanup_command)
            self.assertIn(f"test ! -e '{remote_path}'", cleanup_command)
            self.assertFalse(list(output.parent.glob("*.part")))

    def test_failed_download_does_not_publish_output_but_still_cleans_remote(self) -> None:
        token = "b" * 32
        remote_path = f"/tmp/msys-screenshot-{token}.png"
        response = json.dumps({
            "schema": "msys.debug-screenshot.v1",
            "ok": True,
            "display": ":24",
            "backend": "scrot",
            "path": remote_path,
            "size": 20,
        })
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "screen.png"
            with (
                mock.patch.object(dev.secrets, "token_hex", return_value=token),
                mock.patch.object(
                    dev,
                    "ssh_capture",
                    return_value=subprocess.CompletedProcess([], 0, stdout=response),
                ),
                mock.patch.object(
                    dev,
                    "run_local",
                    return_value=subprocess.CompletedProcess([], 7),
                ),
                mock.patch.object(
                    dev,
                    "ssh",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as cleanup,
                redirect_stderr(stderr),
            ):
                status = dev.command_screenshot(
                    self.context(Path(temporary)),
                    "/tmp/msys-main",
                    output,
                    display=":24",
                    backend="auto",
                    timeout=15,
                    force=False,
                )

            self.assertEqual(status, 7)
            self.assertFalse(output.exists())
            cleanup.assert_called_once()
            self.assertIn("scp download failed", stderr.getvalue())

    def test_invalid_display_is_rejected_before_ssh(self) -> None:
        context = self.context(Path("."))
        with (
            mock.patch.object(dev, "ssh_capture") as capture,
            mock.patch.object(dev, "ssh") as cleanup,
        ):
            status = dev.command_screenshot(
                context,
                "/tmp/msys-main",
                Path("screen.png"),
                display="localhost:0",
                backend="auto",
                timeout=15,
                force=False,
            )
        self.assertEqual(status, 2)
        capture.assert_not_called()
        cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
