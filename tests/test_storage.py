from __future__ import annotations

import datetime as dt
import hashlib
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

from msys_tools import dev, remote_storage


class RemoteStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dev = self.root / "dev"
        self.state = self.root / "state"
        self.release = self.root / "release"
        self.usb = self.root / "usb"
        self.log = self.root / "logs/msysd.log"
        for path in (self.dev, self.state, self.release, self.usb, self.log.parent):
            path.mkdir(parents=True, exist_ok=True)
        (self.state / "registry").mkdir()
        (self.state / "packages").mkdir()
        (self.release / "releases/r1").mkdir(parents=True)
        (self.release / "releases/r2").mkdir(parents=True)
        (self.release / "current").symlink_to("releases/r2")
        (self.release / "previous").symlink_to("releases/r1")
        self._package("org.example.app", "0.5.0", "old but inventory only")
        self._package("org.example.app", "1.0.0", "previous")
        self._package("org.example.app", "2.0.0", "current")
        package_root = self.state / "packages/org.example.app"
        self._pointer(package_root / "current.json", "2.0.0")
        self._pointer(package_root / "previous.json", "1.0.0")
        self._write_json(
            self.state / "registry/installed.json",
            {
                "schema": "msys.installed.v1",
                "packages": [self._pointer_document("2.0.0")],
            },
        )
        repository = self.dev / "msys-example"
        (repository / "__pycache__").mkdir(parents=True)
        (repository / "__pycache__/module.pyc").write_bytes(b"cache")
        (repository / "source.py").write_text("keep\n", encoding="utf-8")
        previous = self.dev / ".msys-example.previous"
        previous.mkdir()
        (previous / "old.py").write_text("old\n", encoding="utf-8")
        application_state = self.state / "apps/org.example.app"
        application_state.mkdir(parents=True)
        (application_state / "notes.db").write_bytes(b"must stay")
        incoming = self.state / "updates/incoming"
        incoming.mkdir(parents=True)
        (incoming / ("a" * 64 + ".maf")).write_bytes(b"rebuildable")
        self.log.write_text("current\n", encoding="utf-8")
        (self.log.parent / "msysd.log.1").write_text("rotated\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_json(self, path: Path, document: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def _pointer_document(self, version: str) -> dict[str, str]:
        return {
            "package": "org.example.app",
            "version": version,
            "path": str(
                (
                    self.state
                    / "packages/org.example.app/versions"
                    / version
                ).resolve()
            ),
        }

    def _pointer(self, path: Path, version: str) -> None:
        self._write_json(path, self._pointer_document(version))

    def _package(self, package: str, version: str, payload: str) -> None:
        target = self.state / "packages" / package / "versions" / version
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(
            json.dumps({"package": {"id": package, "version": version}}),
            encoding="utf-8",
        )
        (target / "payload.txt").write_text(payload, encoding="utf-8")

    def plan(self) -> dict[str, object]:
        return remote_storage.run(
            self.dev,
            self.state,
            self.release,
            self.usb,
            log_file=self.log,
        )

    def test_dry_run_deletes_no_package_or_release_and_reports_them(self) -> None:
        report = self.plan()
        candidates = {item["path"]: item for item in report["candidates"]}

        self.assertIn(str((self.dev / ".msys-example.previous").resolve()), candidates)
        self.assertIn(str((self.dev / "msys-example/__pycache__").resolve()), candidates)
        self.assertIn(
            str(
                (
                    self.state / "updates/incoming" / ("a" * 64 + ".maf")
                ).resolve()
            ),
            candidates,
        )
        self.assertIn(str((self.log.parent / "msysd.log.1").resolve()), candidates)
        for version in ("0.5.0", "1.0.0", "2.0.0"):
            package_path = (
                self.state / "packages/org.example.app/versions" / version
            ).resolve()
            self.assertNotIn(str(package_path), candidates)
        self.assertTrue(
            all(item["deletion_eligible"] is False for item in report["package_versions"])
        )
        self.assertTrue(
            all(item["deletion_eligible"] is False for item in report["releases"]["items"])
        )
        self.assertEqual(report["releases"]["current"], "r2")
        self.assertEqual(report["releases"]["previous"], "r1")
        self.assertNotIn(str((self.state / "apps/org.example.app").resolve()), candidates)
        self.assertNotIn(str(self.log.resolve()), candidates)

    def test_apply_archives_verifies_then_removes_only_whitelist(self) -> None:
        now = dt.datetime(2026, 7, 14, 12, 30, tzinfo=dt.timezone.utc)
        with mock.patch.object(remote_storage, "_is_distinct_mount", return_value=True):
            report = remote_storage.run(
                self.dev,
                self.state,
                self.release,
                self.usb,
                log_file=self.log,
                apply=True,
                now=now,
            )

        archive = Path(report["archive"]["path"])
        checksum = archive.with_suffix(".tar.sha256")
        expected = checksum.read_text(encoding="ascii").split()[0]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), expected)
        for item in report["removed"]:
            self.assertFalse(Path(item["path"]).exists())
        for version in ("0.5.0", "1.0.0", "2.0.0"):
            self.assertTrue(
                (self.state / "packages/org.example.app/versions" / version).is_dir()
            )
        self.assertTrue((self.state / "apps/org.example.app/notes.db").is_file())
        self.assertTrue((self.release / "releases/r1").is_dir())
        self.assertTrue((self.release / "releases/r2").is_dir())
        self.assertTrue(self.log.is_file())

    def test_archive_failure_deletes_nothing(self) -> None:
        cache = self.dev / "msys-example/__pycache__"
        with (
            mock.patch.object(remote_storage, "_is_distinct_mount", return_value=True),
            mock.patch.object(
                remote_storage,
                "_verify_archive",
                side_effect=remote_storage.StorageError("verify failed"),
            ),
            self.assertRaises(remote_storage.StorageError),
        ):
            remote_storage.run(
                self.dev,
                self.state,
                self.release,
                self.usb,
                log_file=self.log,
                apply=True,
            )
        self.assertTrue(cache.is_dir())
        self.assertTrue((self.dev / ".msys-example.previous").is_dir())

    def test_apply_without_usb_deletes_nothing(self) -> None:
        with self.assertRaises(remote_storage.StorageError):
            remote_storage.run(
                self.dev,
                self.state,
                self.release,
                self.usb,
                log_file=self.log,
                apply=True,
            )
        self.assertTrue((self.dev / "msys-example/__pycache__").is_dir())

    def test_readable_but_incomplete_tar_fails_tree_verification(self) -> None:
        raw = remote_storage.build_plan(
            self.dev, self.state, self.release, self.usb, self.log
        )
        candidate = raw["_candidate_objects"][0]
        archive = self.root / "incomplete.tar"
        with tarfile.open(archive, mode="w:") as handle:
            info = tarfile.TarInfo(remote_storage._archive_name(candidate.path))
            info.type = tarfile.DIRTYPE
            handle.addfile(info)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaises(remote_storage.StorageError):
            remote_storage._verify_archive(archive, digest, [candidate])

    def test_no_archive_is_deliberately_explicit(self) -> None:
        with self.assertRaises(remote_storage.StorageError):
            remote_storage.run(
                self.dev,
                self.state,
                self.release,
                self.usb,
                log_file=self.log,
                archive=False,
            )
        report = remote_storage.run(
            self.dev,
            self.state,
            self.release,
            self.usb,
            log_file=self.log,
            apply=True,
            archive=False,
        )
        self.assertTrue(report["archive_skipped"])
        self.assertIsNone(report["archive"])

    def test_invalid_registry_only_marks_inventory_issue(self) -> None:
        (self.state / "registry/installed.json").write_text("invalid", encoding="utf-8")
        report = self.plan()
        self.assertIn("REGISTRY_UNREADABLE", {item["code"] for item in report["issues"]})
        self.assertFalse(
            any(item["kind"] == "package-version" for item in report["candidates"])
        )

    def test_protected_host_parent_is_rejected(self) -> None:
        with self.assertRaises(remote_storage.StorageError):
            remote_storage._root(Path("/root"), "development root")


class StorageHostCommandTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            root=Path("/workspace"),
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys/current/.runtime/python/bin/python3",
            ssh_key=None,
            ssh_control_path=Path("/tmp/control-%C"),
            ssh_control_persist="2h",
        )

    @staticmethod
    def report() -> dict[str, object]:
        return {
            "schema": remote_storage.SCHEMA,
            "mode": "dry-run",
            "candidate_count": 2,
            "reclaimable_bytes": 4096,
            "candidates": [],
            "issues": [],
            "filesystems": {"root": {"free_bytes": 12345}},
            "usb": {"mounted": True, "free_bytes": 99999},
        }

    def test_host_uses_exactly_one_ssh_and_defaults_to_dry_run(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(self.report()) + "\n", stderr=""
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "ssh_capture", return_value=completed) as ssh,
            redirect_stdout(output),
        ):
            status = dev.command_storage(
                self.context(),
                "/opt/msys-dev",
                "/opt/msys-state",
                "/opt/msys",
                "/mnt/msys-usb",
                apply=False,
                no_archive=False,
                json_output=False,
            )
        self.assertEqual(status, 0)
        ssh.assert_called_once()
        command = ssh.call_args.args[1]
        self.assertIn("msys_tools.remote_storage", command)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertIn("python3' '-B' '-m'", command)
        self.assertIn(
            "PYTHONPATH='/opt/msys-dev/msys-tools:/opt/msys/current/msys-tools'",
            command,
        )
        self.assertNotIn("--apply", command)
        self.assertIn("storage: mode=dry-run", output.getvalue())

    def test_cli_routes_explicit_no_archive_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(dev, "command_storage", return_value=0) as storage,
            ):
                status = dev.main(
                    ["storage-clean", "--root", str(root), "--apply", "--no-archive"]
                )
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    dev.main(["storage", "--root", str(root), "--no-archive"])
        self.assertEqual(status, 0)
        self.assertTrue(storage.call_args.kwargs["apply"])
        self.assertTrue(storage.call_args.kwargs["no_archive"])


if __name__ == "__main__":
    unittest.main()
