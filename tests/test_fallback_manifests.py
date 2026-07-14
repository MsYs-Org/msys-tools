from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev, fallback_manifests
from msys_tools.fallback_manifests import (
    FallbackManifestError,
    check_fallback_manifests,
    generate_fallback_manifests,
    generated_fallbacks,
)


PACKAGE_IDS = {
    "msys-shell-native": "org.msys.shell.native",
    "msys-shell-pyside": "org.msys.shell.pyside",
    "msys-x11-session": "org.msys.x11.session",
    "msys-hal": "org.msys.hal.linux",
    "msys-input-touch": "org.msys.input.touch",
    "msys-install": "org.msys.core.install",
}


def canonical_manifest(repository: str) -> dict:
    command = ["python", "-m", repository.replace("-", "_")]
    component: dict = {
        "id": "main",
        "runtime": "python",
        "exec": command,
        "lifecycle": "background",
        "restart": "on-failure",
        "readiness": {"mode": "mipc-ready", "timeout_ms": 5000},
        "provides": [
            {"interface": f"{PACKAGE_IDS[repository]}.v1", "exclusive": False}
        ],
        "permissions": ["mipc.call:msys.core"],
    }
    if repository == "msys-shell-pyside":
        component["exec"] = ["python", "@package/shell-entry.py"]
    if repository == "msys-shell-native":
        component["runtime"] = "native"
        component["exec"] = ["@package/bin/msys-shell-native"]
    if repository == "msys-x11-session":
        component["exec"] = [
            "python",
            "@package/scripts/entry.py",
            "--native",
            "@package/bin/policy",
            "@package-name-is-literal",
        ]
        component["cwd"] = "@package"
        component["env"] = {"UNCHANGED_TOKEN": "@package/data"}
    if repository == "msys-input-touch":
        component["exec"] = ["python", "@package/files/app/main.py"]
    return {
        "schema": "msys.manifest.v1",
        "package": {
            "id": PACKAGE_IDS[repository],
            "name": f"Canonical {repository}",
            "version": "9.8.7",
            "kind": "system",
            "vendor": "Fixture Vendor",
            "summary": "Canonical display metadata is synchronized too",
        },
        "components": [component],
    }


class FallbackManifestTests(unittest.TestCase):
    def make_workspace(self, root: Path, *, fallbacks: bool = False) -> None:
        for repository in PACKAGE_IDS:
            directory = root / repository
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(
                json.dumps(canonical_manifest(repository), indent=2) + "\n",
                encoding="utf-8",
            )
        fallback_directory = (
            root / fallback_manifests.CORE_FALLBACK_DIRECTORY
        )
        fallback_directory.mkdir(parents=True)
        if fallbacks:
            for spec in fallback_manifests.FALLBACK_SPECS:
                (root / spec.fallback_path).write_text(
                    '{"stale": true}\n', encoding="utf-8"
                )

    def test_generation_syncs_semantics_and_only_rewrites_x11_exec_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_workspace(root)

            paths = generate_fallback_manifests(root)

            self.assertEqual(len(paths), 6)
            native_shell = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[0].fallback_path).read_text()
            )
            shell = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[1].fallback_path).read_text()
            )
            x11 = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[2].fallback_path).read_text()
            )
            hal = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[3].fallback_path).read_text()
            )
            input_touch = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[4].fallback_path).read_text()
            )
            install = json.loads(
                (root / fallback_manifests.FALLBACK_SPECS[5].fallback_path).read_text()
            )

            self.assertEqual(
                native_shell["components"][0]["exec"][0],
                "/opt/msys-dev/msys-shell-native/bin/msys-shell-native",
            )
            self.assertEqual(shell, canonical_manifest("msys-shell-pyside"))
            self.assertEqual(hal, canonical_manifest("msys-hal"))
            self.assertEqual(install, canonical_manifest("msys-install"))
            self.assertEqual(
                input_touch["components"][0]["exec"][1],
                "/opt/msys-dev/msys-input-touch/files/app/main.py",
            )
            self.assertEqual(shell["components"][0]["exec"][1], "@package/shell-entry.py")
            component = x11["components"][0]
            self.assertEqual(
                component["exec"],
                [
                    "python",
                    "/opt/msys-dev/msys-x11-session/scripts/entry.py",
                    "--native",
                    "/opt/msys-dev/msys-x11-session/bin/policy",
                    "@package-name-is-literal",
                ],
            )
            self.assertEqual(component["cwd"], "/opt/msys-dev/msys-x11-session")
            self.assertEqual(component["env"]["UNCHANGED_TOKEN"], "@package/data")
            self.assertEqual(x11["package"]["vendor"], "Fixture Vendor")
            self.assertFalse(check_fallback_manifests(root))

    def test_check_is_semantic_and_reports_a_precise_unified_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_workspace(root)
            generate_fallback_manifests(root)
            shell_path = root / fallback_manifests.FALLBACK_SPECS[1].fallback_path
            shell = json.loads(shell_path.read_text(encoding="utf-8"))
            shell["components"][0]["permissions"].append("forbidden:drift")
            shell_path.write_text(
                json.dumps(shell, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            differences = check_fallback_manifests(root)

            self.assertEqual(len(differences), 1)
            difference = differences[0]
            self.assertEqual(difference.path, shell_path)
            self.assertIn("semantic content differs", difference.reason)
            self.assertIn("--- msys-core/examples/config/manifests/shell-pyside.json", difference.diff)
            self.assertIn("-        \"forbidden:drift\"", difference.diff)

            shell["components"][0]["permissions"].pop()
            shell_path.write_text(
                json.dumps(shell, sort_keys=True, indent=7) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(check_fallback_manifests(root))

    def test_cli_is_local_only_and_check_never_changes_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_workspace(root, fallbacks=True)
            shell_path = root / fallback_manifests.FALLBACK_SPECS[1].fallback_path
            original = shell_path.read_bytes()
            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "no-config.json"),
                redirect_stderr(stderr),
            ):
                result = dev.main(
                    ["fallback-manifests", "--root", str(root), "--check"]
                )

            self.assertEqual(result, 1)
            self.assertEqual(shell_path.read_bytes(), original)
            self.assertIn("fallback manifest(s) are stale", stderr.getvalue())
            self.assertIn("(actual)", stderr.getvalue())
            self.assertNotIn("--target", stderr.getvalue())

            stdout = io.StringIO()
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "no-config.json"),
                redirect_stdout(stdout),
            ):
                self.assertEqual(
                    dev.main(["fallback-manifests", "--root", str(root)]), 0
                )
            self.assertIn("generated", stdout.getvalue())
            self.assertFalse(check_fallback_manifests(root))

    def test_all_outputs_are_staged_before_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_workspace(root, fallbacks=True)
            destinations = [root / spec.fallback_path for spec in fallback_manifests.FALLBACK_SPECS]
            before = {path: path.read_bytes() for path in destinations}

            with mock.patch.object(
                fallback_manifests.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ) as replace:
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    generate_fallback_manifests(root)

            replace.assert_called_once()
            self.assertEqual({path: path.read_bytes() for path in destinations}, before)
            fallback_directory = root / fallback_manifests.CORE_FALLBACK_DIRECTORY
            self.assertFalse(list(fallback_directory.glob(".*.tmp")))

    def test_invalid_canonical_identity_and_unsafe_x11_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_workspace(root)
            hal_path = root / "msys-hal" / "manifest.json"
            hal = json.loads(hal_path.read_text(encoding="utf-8"))
            hal["package"]["id"] = "org.example.wrong"
            hal_path.write_text(json.dumps(hal), encoding="utf-8")
            with self.assertRaisesRegex(FallbackManifestError, "package id"):
                generated_fallbacks(root)

            hal["package"]["id"] = PACKAGE_IDS["msys-hal"]
            hal_path.write_text(json.dumps(hal), encoding="utf-8")
            x11_path = root / "msys-x11-session" / "manifest.json"
            x11 = json.loads(x11_path.read_text(encoding="utf-8"))
            x11["components"][0]["cwd"] = "@package/../escape"
            x11_path.write_text(json.dumps(x11), encoding="utf-8")
            with self.assertRaisesRegex(FallbackManifestError, "unsafe @package"):
                generated_fallbacks(root)


if __name__ == "__main__":
    unittest.main()
