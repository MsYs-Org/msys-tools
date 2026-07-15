from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

from msys_tools import release_compose


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def make_maf(
    root: Path,
    name: str,
    package_id: str,
    *,
    include_cache: bool = False,
    escaping_link: bool = False,
) -> Path:
    package = root / f"source-{name}"
    package.mkdir(parents=True)
    manifest = {
        "schema": "msys.manifest.v1",
        "package": {
            "id": package_id,
            "version": "1.0.0",
            "kind": "system",
        },
        "components": [
            {
                "id": "probe",
                "runtime": "python",
                "exec": ["python", "-c", "pass"],
                "lifecycle": "manual",
                "restart": "never",
                "readiness": {"mode": "exec", "timeout_ms": 1000},
            }
        ],
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = {"manifest.json": sha256_file(manifest_path)}
    if include_cache:
        cache = package / "lib" / "__pycache__"
        cache.mkdir(parents=True)
        pyc = cache / "generated.cpython-310.pyc"
        pyc.write_bytes(b"generated-cache")
        files[pyc.relative_to(package).as_posix()] = sha256_file(pyc)
    if escaping_link:
        os.symlink("../../outside", package / "escape")
    (package / "hashes.json").write_text(
        json.dumps(
            {"schema": "msys.hashes.v1", "files": files},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    archive = root / f"{name}.maf"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as handle:
        handle.dereference = False
        handle.add(package, arcname=f"{package_id}-1.0.0", recursive=True)
    return archive


class ReleaseComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.formal = self.root / "formal"
        self.baseline = self.formal / "releases" / "base-1"
        runtime_python = self.baseline / ".runtime" / "python" / "bin" / "python3"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime_python.chmod(runtime_python.stat().st_mode | stat.S_IXUSR)
        os.symlink("releases/base-1", self.formal / "current")
        os.symlink("releases/old-1", self.formal / "previous")

        real_api = release_compose.load_installer_api()

        def verify(release_id: str, release_root: Path):
            self.assertEqual(release_id, "base-1")
            self.assertEqual(Path(release_root), self.formal.resolve())
            return {
                "release_id": release_id,
                "content_sha256": "a" * 64,
                "path": str(self.baseline),
                "verified": True,
            }

        self.api = release_compose.InstallerApi(
            verify_release=verify,
            validate_release_id=real_api.validate_release_id,
            validate_release_entry=real_api.validate_release_entry,
            validate_release_tree=real_api.validate_release_tree,
            inspect_archive=real_api.inspect_archive,
            extract_archive=real_api.extract_archive,
            locate_archive_package=real_api.locate_archive_package,
            validate_staged_package=real_api.validate_staged_package,
        )
        self.sources: dict[str, Path] = {}
        for entry in release_compose.SOURCE_ENTRY_NAMES:
            source = self.root / "workspace" / entry
            source.mkdir(parents=True)
            (source / "source.txt").write_text(entry + "\n", encoding="utf-8")
            self.sources[entry] = source
        cache = self.sources["msys-tools"] / "msys_tools" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "ignored.pyc").write_bytes(b"ignored")

        archive_root = self.root / "archives"
        archive_root.mkdir()
        self.mafs = {
            entry: make_maf(archive_root, str(index), package_id)
            for index, (entry, package_id) in enumerate(
                release_compose.MAF_ENTRY_PACKAGE_IDS.items()
            )
        }
        self.output = self.root / "composed"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def compose(self) -> dict[str, object]:
        return release_compose.compose_release_source(
            release_id="candidate-1",
            release_root=self.formal,
            baseline_release="base-1",
            output_root=self.output,
            source_entries=self.sources,
            maf_entries=self.mafs,
            api=self.api,
        )

    def test_compose_is_cache_free_idempotent_and_pointer_neutral(self) -> None:
        current_before = os.readlink(self.formal / "current")
        previous_before = os.readlink(self.formal / "previous")

        first = self.compose()
        second = self.compose()

        self.assertFalse(first["already_present"])
        self.assertTrue(second["already_present"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        candidate = Path(str(first["path"]))
        self.assertTrue((candidate / "compose.json").is_file())
        rejected = [
            path
            for path in candidate.rglob("*")
            if path.name == "__pycache__" or path.name.endswith(".pyc")
        ]
        self.assertEqual(rejected, [])
        self.assertEqual(os.readlink(self.formal / "current"), current_before)
        self.assertEqual(os.readlink(self.formal / "previous"), previous_before)

    def test_explicit_xft_runtime_replaces_stock_baseline_runtime(self) -> None:
        runtime = self.root / "tk-xft-runtime"
        python = runtime / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        tk = runtime / "lib" / "libtcl9tk9.0.so"
        tk.parent.mkdir()
        tk.write_bytes(b"ELF fixture\0libXft.so.2\0")
        (runtime / ".msys-tk-xft-runtime.json").write_text(
            json.dumps(
                {
                    "schema": "msys.tk-xft-runtime.v1",
                    "xft_backend": "libXft.so.2",
                    "font_doctor": "passed",
                }
            ),
            encoding="utf-8",
        )
        (runtime / "runtime-marker.txt").write_text("xft\n", encoding="utf-8")

        result = release_compose.compose_release_source(
            release_id="candidate-1",
            release_root=self.formal,
            baseline_release="base-1",
            output_root=self.output,
            source_entries=self.sources,
            maf_entries=self.mafs,
            python_runtime=runtime,
            api=self.api,
        )

        candidate = Path(str(result["path"]))
        self.assertEqual(
            (candidate / ".runtime/python/runtime-marker.txt").read_text(
                encoding="utf-8"
            ),
            "xft\n",
        )
        self.assertEqual(result["python_runtime"]["origin"], "xft-override")
        self.assertTrue(result["python_runtime"]["xft"])

    def test_runtime_override_without_xft_is_rejected(self) -> None:
        runtime = self.root / "stock-runtime"
        python = runtime / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        tk = runtime / "lib" / "libtcl9tk9.0.so"
        tk.parent.mkdir()
        tk.write_bytes(b"ELF fixture without outline backend")

        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "not linked to libXft"
        ):
            release_compose.compose_release_source(
                release_id="candidate-1",
                release_root=self.formal,
                baseline_release="base-1",
                output_root=self.output,
                source_entries=self.sources,
                maf_entries=self.mafs,
                python_runtime=runtime,
                api=self.api,
            )

    def test_unverified_xft_runtime_override_is_rejected(self) -> None:
        runtime = self.root / "unverified-xft-runtime"
        python = runtime / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        tk = runtime / "lib" / "libtcl9tk9.0.so"
        tk.parent.mkdir()
        tk.write_bytes(b"ELF fixture\0libXft.so.2\0")

        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "verification attestation"
        ):
            release_compose.compose_release_source(
                release_id="candidate-1",
                release_root=self.formal,
                baseline_release="base-1",
                output_root=self.output,
                source_entries=self.sources,
                maf_entries=self.mafs,
                python_runtime=runtime,
                api=self.api,
            )

    def test_formal_maf_map_covers_audio_and_split_application_packages(self) -> None:
        self.assertNotIn("msys-apps", release_compose.MAF_ENTRY_PACKAGE_IDS)
        self.assertEqual(
            release_compose.MAF_ENTRY_PACKAGE_IDS["msys-audio"],
            "org.msys.audio.bluez",
        )
        self.assertEqual(
            {
                name: release_compose.MAF_ENTRY_PACKAGE_IDS[name]
                for name in ("msys-notes", "msys-calculator", "msys-device-info")
            },
            {
                "msys-notes": "org.msys.notes",
                "msys-calculator": "org.msys.calculator",
                "msys-device-info": "org.msys.device-info",
            },
        )
        self.compose()
        audio_manifest = json.loads(
            (self.output / "candidate-1" / "msys-audio" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(audio_manifest["package"]["id"], "org.msys.audio.bluez")

    def test_audio_maf_is_required_for_a_formal_compose(self) -> None:
        mafs = dict(self.mafs)
        del mafs["msys-audio"]
        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError,
            r"MAF entry set mismatch .*msys-audio",
        ):
            release_compose.compose_release_source(
                release_id="candidate-1",
                release_root=self.formal,
                baseline_release="base-1",
                output_root=self.output,
                source_entries=self.sources,
                maf_entries=mafs,
                api=self.api,
            )

    def test_reusing_id_after_source_change_is_rejected(self) -> None:
        self.compose()
        (self.sources["msys-core"] / "source.txt").write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaises(release_compose.ImmutableComposeError):
            self.compose()

    def test_maf_identity_is_bound_to_release_entry(self) -> None:
        wrong = make_maf(
            self.root / "archives",
            "wrong-id",
            "org.example.wrong",
        )
        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "MAF identity mismatch"
        ):
            release_compose._extract_verified_maf(
                self.api,
                wrong,
                self.root / "wrong-output",
                entry="msys-shell-native",
                expected_package_id="org.msys.shell.native",
                scratch_root=self.root,
            )

    def test_symlink_maf_and_escaping_archive_link_are_rejected(self) -> None:
        link = self.root / "linked.maf"
        os.symlink(self.mafs["msys-shell-native"], link)
        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "must not be a symbolic link"
        ):
            release_compose._extract_verified_maf(
                self.api,
                link,
                self.root / "linked-output",
                entry="msys-shell-native",
                expected_package_id="org.msys.shell.native",
                scratch_root=self.root,
            )

        escaping = make_maf(
            self.root / "archives",
            "escaping-link",
            "org.msys.shell.native",
            escaping_link=True,
        )
        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "cannot verify MAF input"
        ):
            release_compose._extract_verified_maf(
                self.api,
                escaping,
                self.root / "escaping-output",
                entry="msys-shell-native",
                expected_package_id="org.msys.shell.native",
                scratch_root=self.root,
            )

    def test_hashed_cache_inside_maf_is_still_rejected_by_compose_policy(self) -> None:
        cached = make_maf(
            self.root / "archives",
            "cached",
            "org.msys.shell.native",
            include_cache=True,
        )
        destination = self.root / "cached-output"
        release_compose._extract_verified_maf(
            self.api,
            cached,
            destination,
            entry="msys-shell-native",
            expected_package_id="org.msys.shell.native",
            scratch_root=self.root,
        )
        with self.assertRaisesRegex(
            release_compose.ReleaseComposeError, "generated Python cache"
        ):
            release_compose._reject_python_caches(destination)

    def test_mapping_requires_unique_allowed_absolute_paths(self) -> None:
        parsed = release_compose.parse_mapping(
            ["msys-core=/opt/source/core"],
            allowed=release_compose.SOURCE_ENTRY_NAMES,
            label="source entry",
        )
        self.assertEqual(parsed["msys-core"], Path("/opt/source/core"))
        with self.assertRaisesRegex(release_compose.ReleaseComposeError, "duplicate"):
            release_compose.parse_mapping(
                ["msys-core=/a", "msys-core=/b"],
                allowed=release_compose.SOURCE_ENTRY_NAMES,
                label="source entry",
            )
        with self.assertRaises(release_compose.ReleaseComposeError):
            release_compose.parse_mapping(
                ["msys-core=relative"],
                allowed=release_compose.SOURCE_ENTRY_NAMES,
                label="source entry",
            )


if __name__ == "__main__":
    unittest.main()
