from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.package_flow import (
    PackageFlowError,
    build_index,
    build_package,
    discover_manifests,
    parse_overlay_spec,
    validate_package,
)


WORKSPACE = Path(__file__).resolve().parents[2]


def manifest(version: str = "1.0.0") -> dict:
    return {
        "schema": "msys.manifest.v1",
        "package": {
            "id": "org.example.flow",
            "name": "Package Flow",
            "version": version,
            "kind": "application",
        },
        "components": [
            {
                "id": "main",
                "runtime": "shell",
                "exec": ["sh", "files/main.sh"],
                "lifecycle": "manual",
                "restart": "never",
            }
        ],
    }


class PackageFlowTests(unittest.TestCase):
    def make_package(self, root: Path) -> Path:
        package = root / "package"
        (package / "files").mkdir(parents=True)
        (package / "manifest.json").write_text(
            json.dumps(manifest(), indent=2) + "\n", encoding="utf-8"
        )
        script = package / "files" / "main.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        (package / ".git").mkdir()
        (package / ".git" / "config").write_text("developer-only\n", encoding="utf-8")
        return package

    def test_build_is_hashed_verified_and_does_not_mutate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            first = build_package(WORKSPACE, package, root / "dist-a")
            second = build_package(WORKSPACE, package, root / "dist-b")

            first_archive = Path(first["artifact"])
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertFalse((package / "hashes.json").exists())
            with tarfile.open(first_archive, "r:gz") as archive:
                names = {name.removeprefix("./") for name in archive.getnames()}
            self.assertIn("hashes.json", names)
            self.assertNotIn(".git/config", names)

            checked = validate_package(
                WORKSPACE, first_archive, require_content_hashes=True
            )
            self.assertEqual(checked["package"], "org.example.flow")
            self.assertEqual(checked["version"], "1.0.0")

    def test_maf_build_is_deterministic_tar_gzip_alias_and_validates_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            tar_result = build_package(WORKSPACE, package, root / "tar-dist")
            maf_result = build_package(
                WORKSPACE,
                package,
                root / "maf-dist",
                artifact_format="maf",
            )
            second_maf = build_package(
                WORKSPACE,
                package,
                root / "second-maf-dist",
                artifact_format="maf",
            )

            maf = Path(maf_result["artifact"])
            self.assertEqual(maf.name, "org.example.flow-1.0.0.maf")
            self.assertEqual(maf_result["format"], "maf")
            self.assertEqual(Path(tar_result["artifact"]).suffixes[-2:], [".tar", ".gz"])
            self.assertEqual(maf_result["sha256"], second_maf["sha256"])
            self.assertEqual(maf_result["sha256"], tar_result["sha256"])
            self.assertEqual(maf.read_bytes()[:2], b"\x1f\x8b")
            with tarfile.open(maf, "r:gz") as archive:
                self.assertIn("./hashes.json", archive.getnames())
            checked = validate_package(
                WORKSPACE, maf, require_content_hashes=True
            )
            self.assertEqual(checked["package"], "org.example.flow")

            with self.assertRaisesRegex(PackageFlowError, "conflicts"):
                build_package(
                    WORKSPACE,
                    package,
                    root / "wrong.tar.gz",
                    artifact_format="maf",
                )

    def test_maf_delivery_revalidates_hashes_and_preserves_staged_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            built = build_package(
                WORKSPACE,
                package,
                root / "dist",
                artifact_format="maf",
            )
            artifact = Path(built["artifact"])
            context = dev.Context(
                root=WORKSPACE,
                target="root@example",
                remote="/opt/msys-dev",
                remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            )
            with (
                mock.patch.object(dev, "ssh"),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "_typed_agent_request", return_value=0) as request,
                redirect_stdout(io.StringIO()),
            ):
                status = dev.command_install_archive(
                    context,
                    "/tmp/msys-main",
                    artifact,
                    state_dir="/srv/msys-state",
                )

        self.assertEqual(status, 0)
        payload = request.call_args.kwargs["payload"]
        self.assertTrue(payload["path"].endswith(".maf"))
        self.assertEqual(payload["sha256"], built["sha256"])
        self.assertTrue(payload["require_sha256"])
        self.assertTrue(payload["require_content_hashes"])

    def test_package_ignore_excludes_development_trees_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            nested = package / "examples" / "other" / "manifest.json"
            nested.parent.mkdir(parents=True)
            nested.write_text(json.dumps(manifest("2.0.0")), encoding="utf-8")
            tests = package / "tests"
            tests.mkdir()
            (tests / "test_example.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            (package / ".msys-packageignore").write_text(
                "# source-only trees\nexamples/\ntests/\n",
                encoding="utf-8",
            )

            checked = validate_package(WORKSPACE, package)
            built = build_package(WORKSPACE, package, root / "dist")

            self.assertEqual(checked["package"], "org.example.flow")
            with tarfile.open(built["artifact"], "r:gz") as archive:
                names = {name.removeprefix("./") for name in archive.getnames()}
            self.assertNotIn("examples/other/manifest.json", names)
            self.assertNotIn("tests/test_example.py", names)
            self.assertNotIn(".msys-packageignore", names)

    def test_package_ignore_rejects_escape_and_manifest_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            ignore = package / ".msys-packageignore"
            ignore.write_text("../outside\n", encoding="utf-8")
            with self.assertRaisesRegex(PackageFlowError, "unsafe package ignore"):
                validate_package(WORKSPACE, package)

            ignore.write_text("manifest.json\n", encoding="utf-8")
            with self.assertRaisesRegex(PackageFlowError, "cannot exclude manifest"):
                build_package(WORKSPACE, package, root / "dist")

    def test_build_overlay_is_vendored_before_one_complete_hash_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            sdk = root / "sdk/msys_sdk"
            sdk.mkdir(parents=True)
            (sdk / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (sdk / ".git").mkdir()
            (sdk / ".git/config").write_text("ignored\n", encoding="utf-8")
            overlay = parse_overlay_spec(
                root, "sdk/msys_sdk=vendor/msys_sdk"
            )

            first = build_package(
                WORKSPACE,
                package,
                root / "dist-a",
                overlays=[overlay],
            )
            second = build_package(
                WORKSPACE,
                package,
                root / "dist-b",
                overlays=[overlay],
            )
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(
                first["overlays"][0]["destination"], "vendor/msys_sdk"
            )
            with tarfile.open(first["artifact"], "r:gz") as archive:
                names = {name.removeprefix("./") for name in archive.getnames()}
                hashes = json.loads(archive.extractfile("./hashes.json").read())
            self.assertIn("vendor/msys_sdk/__init__.py", names)
            self.assertNotIn("vendor/msys_sdk/.git/config", names)
            self.assertIn("vendor/msys_sdk/__init__.py", hashes["files"])
            validate_package(
                WORKSPACE, Path(first["artifact"]), require_content_hashes=True
            )

    def test_overlay_rejects_escape_protected_roots_symlinks_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            source = root / "vendor.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            for destination in (
                "../escape",
                "/absolute",
                "manifest.json",
                "hashes.json/child",
                "signature.optional",
            ):
                with self.subTest(destination=destination):
                    with self.assertRaises(PackageFlowError):
                        parse_overlay_spec(root, f"vendor.py={destination}")

            overwrite = parse_overlay_spec(root, "vendor.py=files/main.sh")
            with self.assertRaisesRegex(PackageFlowError, "refuses to overwrite"):
                build_package(
                    WORKSPACE,
                    package,
                    root / "dist-overwrite",
                    overlays=[overwrite],
                )

            if hasattr(Path, "symlink_to"):
                link = root / "vendor-link.py"
                try:
                    link.symlink_to(source)
                except OSError:
                    pass
                else:
                    with self.assertRaisesRegex(PackageFlowError, "symbolic link"):
                        parse_overlay_spec(root, "vendor-link.py=vendor/link.py")

    def test_index_uses_verified_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.make_package(root)
            repository = root / "repository"
            built = build_package(WORKSPACE, package, repository)

            result = build_index(
                WORKSPACE,
                repository,
                base_url="https://updates.example/msys/",
            )
            self.assertEqual(result["schema"], "msys.update-index.v1")
            entry = result["packages"][0]
            self.assertEqual(entry["sha256"], built["sha256"])
            self.assertEqual(
                entry["artifact"],
                "https://updates.example/msys/org.example.flow-1.0.0.tar.gz",
            )

    def test_invalid_manifest_returns_a_clean_cli_error_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            invalid = manifest()
            invalid["package"]["unexpected"] = True
            path.write_text(json.dumps(invalid), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = dev.main(
                    [
                        "package",
                        "validate",
                        str(path),
                        "--root",
                        str(WORKSPACE),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("invalid", stderr.getvalue())
            self.assertNotIn("--target", stderr.getvalue())

    def test_build_rejects_output_inside_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self.make_package(Path(temporary))
            with self.assertRaisesRegex(PackageFlowError, "outside"):
                build_package(WORKSPACE, package, package / "dist")

    def test_nested_canonical_manifest_is_staged_as_installable_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "hal-source"
            (package / "manifests").mkdir(parents=True)
            (package / "msys_hal").mkdir()
            (package / "msys_hal" / "__init__.py").write_text("\n", encoding="utf-8")
            nested_manifest = package / "manifests" / "hal.json"
            nested_manifest.write_text(
                json.dumps(manifest(), indent=2) + "\n", encoding="utf-8"
            )
            (package / "files").mkdir()
            script = package / "files" / "main.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

            checked = validate_package(WORKSPACE, package)
            built = build_package(WORKSPACE, package, root / "dist")

            self.assertEqual(checked["package"], "org.example.flow")
            with tarfile.open(built["artifact"], "r:gz") as archive:
                names = {name.removeprefix("./") for name in archive.getnames()}
                root_manifest = archive.extractfile("./manifest.json")
                self.assertIsNotNone(root_manifest)
                root_data = json.loads(root_manifest.read().decode("utf-8"))
            self.assertEqual(root_data["package"]["id"], "org.example.flow")
            self.assertIn("msys_hal/__init__.py", names)

    def test_discover_strictly_validates_manifest_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid" / "manifest.json"
            valid.parent.mkdir()
            valid.write_text(json.dumps(manifest()), encoding="utf-8")
            invalid = root / "driver" / "manifests" / "broken.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text('{"schema":"not-msys"}', encoding="utf-8")

            result = discover_manifests(WORKSPACE, root)

            self.assertEqual(result["schema"], "msys.manifest-discovery.v1")
            self.assertEqual(result["count"], 2)
            self.assertFalse(result["valid"])
            invalid_rows = [row for row in result["manifests"] if not row["valid"]]
            self.assertEqual(len(invalid_rows), 1)
            self.assertIn("invalid", invalid_rows[0]["error"])

    def test_workspace_discovery_finds_msys_apps_root_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps_manifest = root / "msys-apps" / "manifest.json"
            apps_manifest.parent.mkdir()
            data = manifest()
            data["package"].update(
                {"id": "org.msys.apps", "name": "MSYS Applications", "version": "0.1.0"}
            )
            apps_manifest.write_text(json.dumps(data), encoding="utf-8")

            result = discover_manifests(WORKSPACE, root)

        self.assertTrue(result["valid"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["manifests"][0]["package"], "org.msys.apps")
        self.assertEqual(Path(result["manifests"][0]["path"]), apps_manifest)

    def test_remote_rollback_uses_typed_install_agent_rpc(self) -> None:
        context = dev.Context(
            root=WORKSPACE,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        with mock.patch.object(dev, "_typed_agent_request", return_value=0) as request:
            result = dev.command_rollback(
                context, "/tmp/msys-main", "org.example.flow"
            )
        self.assertEqual(result, 0)
        request.assert_called_once_with(
            context,
            "/tmp/msys-main",
            target="role:install-agent",
            method="rollback",
            payload={"package": "org.example.flow"},
            operation="rollback",
        )

    def test_legacy_rollback_requires_explicit_flag(self) -> None:
        context = dev.Context(
            root=WORKSPACE,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        with (
            mock.patch.object(dev, "command_broadcast", return_value=0) as broadcast,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = dev.command_rollback(
                context,
                "/tmp/msys-main",
                "org.example.flow",
                legacy_events=True,
            )
        self.assertEqual(result, 0)
        self.assertIn("best-effort", stderr.getvalue())
        broadcast.assert_called_once_with(
            context,
            "/tmp/msys-main",
            "msys.install.rollback",
            {"package": "org.example.flow"},
        )

    def test_remote_uninstall_uses_typed_install_agent_rpc(self) -> None:
        context = dev.Context(
            root=WORKSPACE,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        with mock.patch.object(dev, "_typed_agent_request", return_value=0) as request:
            result = dev.command_uninstall(
                context, "/tmp/msys-main", "org.example.flow"
            )
        self.assertEqual(result, 0)
        request.assert_called_once_with(
            context,
            "/tmp/msys-main",
            target="role:install-agent",
            method="uninstall",
            payload={"package": "org.example.flow"},
            operation="uninstall",
        )

    def test_package_uninstall_cli_routes_exact_typed_operation(self) -> None:
        with (
            mock.patch.object(dev, "CONFIG_PATH", WORKSPACE / ".missing-config.json"),
            mock.patch.object(dev, "command_uninstall", return_value=0) as uninstall,
        ):
            result = dev.main([
                "package",
                "uninstall",
                "org.example.flow",
                "--root",
                str(WORKSPACE),
                "--target",
                "root@example",
                "--runtime-dir",
                "/tmp/msys-main",
            ])

        self.assertEqual(result, 0)
        self.assertEqual(uninstall.call_args.args[1:], (
            "/tmp/msys-main",
            "org.example.flow",
        ))
        self.assertFalse(uninstall.call_args.kwargs["legacy_events"])

    def test_legacy_uninstall_requires_explicit_flag(self) -> None:
        context = dev.Context(
            root=WORKSPACE,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        with (
            mock.patch.object(dev, "command_broadcast", return_value=0) as broadcast,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = dev.command_uninstall(
                context,
                "/tmp/msys-main",
                "org.example.flow",
                legacy_events=True,
            )
        self.assertEqual(result, 0)
        self.assertIn("best-effort", stderr.getvalue())
        broadcast.assert_called_once_with(
            context,
            "/tmp/msys-main",
            "msys.install.uninstall",
            {"package": "org.example.flow"},
        )


if __name__ == "__main__":
    unittest.main()
