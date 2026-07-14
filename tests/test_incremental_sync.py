from __future__ import annotations

import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from msys_tools import dev


class IncrementalSyncTests(unittest.TestCase):
    def context(self, root: Path) -> dev.Context:
        return dev.Context(
            root=root,
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_fingerprint_tracks_transferred_content_but_ignores_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "msys-example"
            repository.mkdir()
            source = repository / "main.py"
            source.write_text("first\n", encoding="utf-8")
            cache = repository / "__pycache__"
            cache.mkdir()
            cached = cache / "main.pyc"
            cached.write_bytes(b"ignored")
            build = repository / "build"
            build.mkdir()
            workstation_object = build / "mipc.o"
            workstation_object.write_bytes(b"x86")

            first = dev.repository_fingerprint(repository)
            cached.write_bytes(b"still ignored")
            workstation_object.write_bytes(b"different workstation build")
            self.assertEqual(dev.repository_fingerprint(repository), first)
            source.write_text("second\n", encoding="utf-8")
            self.assertNotEqual(dev.repository_fingerprint(repository), first)

    def test_remote_probe_parses_only_bounded_prefixed_records(self) -> None:
        digest = "a" * 64
        completed = subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout=(
                "OpenSSH warning\n"
                f"{dev.SYNC_FINGERPRINT_PREFIX}\trsync\t1\n"
                f"{dev.SYNC_FINGERPRINT_PREFIX}\tmsys-sdk\t{digest}\n"
                f"{dev.SYNC_FINGERPRINT_PREFIX}\tunknown\t{'b' * 64}\n"
            ),
        )
        context = self.context(Path("/workspace"))
        with mock.patch.object(dev, "ssh_capture", return_value=completed) as capture:
            rsync, fingerprints = dev.remote_sync_probe(context, ["msys-sdk"])

        self.assertTrue(rsync)
        self.assertEqual(fingerprints, {"msys-sdk": digest})
        capture.assert_called_once()
        self.assertIn(".msys-dev-source.sha256", capture.call_args.args[1])

    def test_matching_remote_fingerprint_skips_upload_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "msys-sdk"
            repository.mkdir()
            (repository / "README.md").write_text("sdk\n", encoding="utf-8")
            fingerprint = dev.repository_fingerprint(repository)
            context = self.context(root)
            with (
                mock.patch.object(
                    dev,
                    "remote_sync_probe",
                    return_value=(False, {"msys-sdk": fingerprint}),
                ),
                mock.patch.object(dev, "ssh") as ssh,
                mock.patch.object(dev, "run_local") as run_local,
            ):
                result = dev.command_sync(context, ["msys-sdk"])

        self.assertEqual(result, 0)
        self.assertEqual(ssh.call_count, 1)  # one mkdir/probe setup, no staging swap
        run_local.assert_not_called()

    def test_sdk_sync_rebuilds_target_archive_before_atomic_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "msys-sdk"
            repository.mkdir()
            (repository / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            context = self.context(root)
            captures = [
                subprocess.CompletedProcess([], 1, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="built\n"),
            ]
            with (
                mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                mock.patch.object(dev.shutil, "which", return_value=None),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "ssh") as ssh,
            ):
                result = dev.command_sync(context, ["msys-sdk"])

        self.assertEqual(result, 0)
        build_command = capture.call_args_list[1].args[1]
        self.assertIn("make -j1 clean", build_command)
        self.assertIn("make -j1", build_command)
        self.assertIn("all check", build_command)
        self.assertIn("build/libmsys-mipc.a", build_command)
        finalise = ssh.call_args_list[-1].args[1]
        self.assertIn(
            "mv '/opt/msys-dev/.sync/msys-sdk.new' '/opt/msys-dev/msys-sdk'",
            finalise,
        )


if __name__ == "__main__":
    unittest.main()
