from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.package_flow import PackageFlowError


WORKSPACE = Path(__file__).resolve().parents[2]


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TargetNativeDeliveryTests(unittest.TestCase):
    def context(self, root: Path) -> dev.Context:
        return dev.Context(
            root=root,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def make_hal_package(self, root: Path) -> Path:
        package = root / "msys-hal"
        binary = package / "files/bin/msys-hal-native"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"stale-workstation-copy")
        binary.chmod(0o755)
        manifest = {
            "schema": "msys.manifest.v1",
            "package": {
                "id": "org.msys.hal.linux",
                "version": "0.2.3",
                "kind": "system",
            },
            "components": [
                {
                    "id": "native-manager",
                    "runtime": "native",
                    "exec": ["@package/files/bin/msys-hal-native"],
                    "lifecycle": "background",
                    "restart": "on-failure",
                    "readiness": {"mode": "mipc-ready", "timeout_ms": 5000},
                }
            ],
        }
        (package / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return package

    def test_hal_self_check_version_must_equal_manifest(self) -> None:
        spec = dev.TARGET_NATIVE_ARTIFACTS["org.msys.hal.linux"]
        native_hash = "a" * 64
        with mock.patch.object(
            dev,
            "ssh_capture",
            side_effect=[
                completed(0, f"{native_hash}  binary\n"),
                completed(0, '{"ok":true,"version":"0.2.2"}\n'),
            ],
        ):
            with self.assertRaisesRegex(PackageFlowError, "version mismatch"):
                dev._probe_remote_native_binary(
                    self.context(Path("/workspace")),
                    "/opt/msys-dev/msys-hal/files/bin/msys-hal-native",
                    spec,
                    expected_version="0.2.3",
                )

    def test_sync_marker_binds_manifest_probe_and_exact_elf_hash(self) -> None:
        spec = dev.TARGET_NATIVE_ARTIFACTS["org.msys.hal.linux"]
        native_hash = "b" * 64
        context = self.context(Path("/workspace"))
        with (
            mock.patch.object(
                dev,
                "ssh_capture",
                side_effect=[
                    completed(0, f"{native_hash}  binary\n"),
                    completed(0, '{"ok":true,"version":"0.2.3"}\n'),
                    completed(0, f"{native_hash}  binary\n"),
                ],
            ),
            mock.patch.object(dev, "ssh") as ssh,
        ):
            marker = dev._record_target_native_artifact(
                context,
                "/opt/msys-dev/.sync/msys-hal.new",
                spec,
                package_id="org.msys.hal.linux",
                version="0.2.3",
            )

        self.assertEqual(marker["sha256"], native_hash)
        self.assertEqual(marker["probe"], {"kind": "self-check", "version": "0.2.3"})
        marker_command = ssh.call_args.args[1]
        self.assertIn(dev.TARGET_NATIVE_MARKER_NAME, marker_command)
        self.assertIn(native_hash, marker_command)
        self.assertIn('"version":"0.2.3"', marker_command)

    def test_shell_version_and_x11_loader_probes_are_enforced(self) -> None:
        cases = (
            (
                dev.TARGET_NATIVE_ARTIFACTS["org.msys.shell.native"],
                "0.3.1",
                completed(0, "0.3.1\n"),
                {"kind": "version", "version": "0.3.1"},
            ),
            (
                dev.TARGET_NATIVE_ARTIFACTS["org.msys.x11.session"],
                "0.2.4",
                completed(64, ""),
                {"kind": "build-probe", "status": 64},
            ),
        )
        for spec, version, probe_result, expected_probe in cases:
            with self.subTest(package=spec.package_id):
                native_hash = "c" * 64
                with mock.patch.object(
                    dev,
                    "ssh_capture",
                    side_effect=[
                        completed(0, f"{native_hash}  binary\n"),
                        probe_result,
                        completed(0, f"{native_hash}  binary\n"),
                    ],
                ):
                    checked = dev._probe_remote_native_binary(
                        self.context(Path("/workspace")),
                        f"/opt/msys-dev/{spec.repository}/{spec.relative_path}",
                        spec,
                        expected_version=version,
                    )
                self.assertEqual(checked["sha256"], native_hash)
                self.assertEqual(checked["probe"], expected_probe)

    def test_deliver_packages_recovered_target_elf_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_hal_package(root)
            local_binary = package / "files/bin/msys-hal-native"
            target_bytes = b"verified-aarch64-target-elf"
            target_hash = digest(target_bytes)
            marker = {
                "schema": dev.TARGET_NATIVE_MARKER_SCHEMA,
                "package": "org.msys.hal.linux",
                "version": "0.2.3",
                "path": "files/bin/msys-hal-native",
                "sha256": target_hash,
                "probe": {"kind": "self-check", "version": "0.2.3"},
            }
            output = root / "dist"

            def download(argv: list[str], check: bool = True):
                del check
                destination = Path(argv[-1])
                destination.write_bytes(target_bytes)
                destination.chmod(0o755)
                return completed()

            with (
                mock.patch.object(
                    dev, "_load_target_native_marker", return_value=marker
                ),
                mock.patch.object(dev, "run_local", side_effect=download) as scp,
                mock.patch.object(
                    dev, "command_install_archive", return_value=0
                ) as install,
                redirect_stdout(io.StringIO()),
            ):
                result = dev.command_package_deliver(
                    self.context(WORKSPACE),
                    WORKSPACE,
                    package,
                    output,
                    runtime_dir="/tmp/msys-main",
                    state_dir="/opt/msys-state",
                    force=False,
                    source_date_epoch=0,
                    manifest_path=None,
                    artifact_format="maf",
                    overlays=[],
                    legacy_events=False,
                )

            self.assertEqual(result, 0)
            self.assertEqual(local_binary.read_bytes(), b"stale-workstation-copy")
            self.assertIn(
                "root@example:/opt/msys-dev/msys-hal/files/bin/msys-hal-native",
                scp.call_args.args[0],
            )
            artifact = output / "org.msys.hal.linux-0.2.3.maf"
            with tarfile.open(artifact, "r:gz") as archive:
                member = archive.extractfile("./files/bin/msys-hal-native")
                self.assertIsNotNone(member)
                packaged = member.read()
            self.assertEqual(packaged, target_bytes)
            self.assertEqual(digest(packaged), target_hash)
            install.assert_called_once()
            self.assertEqual(install.call_args.args[2], artifact)

    def test_missing_marker_blocks_build_download_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_hal_package(root)
            with (
                mock.patch.object(
                    dev,
                    "_load_target_native_marker",
                    side_effect=PackageFlowError("marker missing"),
                ),
                mock.patch.object(dev, "run_local") as download,
                mock.patch.object(dev, "build_package") as build,
                mock.patch.object(dev, "command_install_archive") as install,
            ):
                with self.assertRaisesRegex(PackageFlowError, "marker missing"):
                    dev.command_package_deliver(
                        self.context(WORKSPACE),
                        WORKSPACE,
                        package,
                        root / "dist",
                        runtime_dir="/tmp/msys-main",
                        state_dir="/opt/msys-state",
                        force=False,
                        source_date_epoch=0,
                        manifest_path=None,
                        artifact_format="maf",
                        overlays=[],
                        legacy_events=False,
                    )
            download.assert_not_called()
            build.assert_not_called()
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
