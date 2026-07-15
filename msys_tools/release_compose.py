"""Compose a whole-system release source from verified, immutable inputs.

This is developer tooling, not an installer.  It never changes the formal
``current``/``previous`` pointers and it never talks to the running service.
The resulting directory is consumed by ``msys_install.release stage``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


COMPOSE_SCHEMA = "msys.release-compose.v1"
SOURCE_ENTRY_NAMES = (
    "msys-contracts",
    "msys-core",
    "msys-sdk",
    "msys-tools",
)
MAF_ENTRY_PACKAGE_IDS = {
    "msys-shell-native": "org.msys.shell.native",
    "msys-shell-pyside": "org.msys.shell.pyside",
    "msys-x11-session": "org.msys.x11.session",
    "msys-hal": "org.msys.hal.linux",
    "msys-audio": "org.msys.audio.bluez",
    "msys-input-touch": "org.msys.input.touch",
    "msys-settings": "org.msys.settings",
    "msys-notes": "org.msys.notes",
    "msys-calculator": "org.msys.calculator",
    "msys-device-info": "org.msys.device-info",
    "msys-openstick-ch347": "org.msys.openstick.ch347",
    "msys-install": "org.msys.core.install",
}
COMPOSED_ENTRIES = (
    ".runtime",
    *SOURCE_ENTRY_NAMES,
    *MAF_ENTRY_PACKAGE_IDS,
)
_IGNORED_SOURCE_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
    }
)


class ReleaseComposeError(RuntimeError):
    """A reproducible release source could not be composed."""


class ImmutableComposeError(ReleaseComposeError):
    """A compose id already exists with different content."""


@dataclass(frozen=True, slots=True)
class InstallerApi:
    verify_release: Callable[..., dict[str, Any]]
    validate_release_id: Callable[[object], str]
    validate_release_entry: Callable[[object], str]
    validate_release_tree: Callable[[Path], None]
    inspect_archive: Callable[..., dict[str, str]]
    extract_archive: Callable[[Path, Path], None]
    locate_archive_package: Callable[[Path], Path]
    validate_staged_package: Callable[..., tuple[Any, str]]


def load_installer_api() -> InstallerApi:
    """Load the sibling zero-dependency installer without a global install."""

    try:
        from msys_install.release import (
            _validate_tree,
            validate_release_entry,
            validate_release_id,
            verify_release,
        )
        from msys_install.store import (
            _extract_archive,
            _locate_archive_package,
            _validate_staged_package,
            inspect_archive,
        )
    except ImportError:
        sibling = Path(__file__).resolve().parents[2] / "msys-install"
        if not (sibling / "msys_install" / "release.py").is_file():
            raise ReleaseComposeError(
                "msys-install source is unavailable; synchronize msys-install beside "
                "msys-tools or include it in PYTHONPATH"
            ) from None
        sibling_text = str(sibling)
        if sibling_text not in sys.path:
            sys.path.insert(0, sibling_text)
        try:
            from msys_install.release import (
                _validate_tree,
                validate_release_entry,
                validate_release_id,
                verify_release,
            )
            from msys_install.store import (
                _extract_archive,
                _locate_archive_package,
                _validate_staged_package,
                inspect_archive,
            )
        except ImportError as exc:
            raise ReleaseComposeError(f"cannot load msys-install: {exc}") from exc
    return InstallerApi(
        verify_release=verify_release,
        validate_release_id=validate_release_id,
        validate_release_entry=validate_release_entry,
        validate_release_tree=_validate_tree,
        inspect_archive=inspect_archive,
        extract_archive=_extract_archive,
        locate_archive_package=_locate_archive_package,
        validate_staged_package=_validate_staged_package,
    )


def _absolute_non_root(path: Path, *, label: str, must_exist: bool = True) -> Path:
    unresolved = Path(path).expanduser()
    if not unresolved.is_absolute() or unresolved == Path(unresolved.anchor):
        raise ReleaseComposeError(f"{label} must be a non-root absolute path: {unresolved}")
    if unresolved.is_symlink():
        raise ReleaseComposeError(f"{label} must not be a symbolic link: {unresolved}")
    try:
        return unresolved.resolve(strict=must_exist)
    except OSError as exc:
        raise ReleaseComposeError(f"cannot resolve {label} {unresolved}: {exc}") from exc


def _real_directory(path: Path, *, label: str) -> Path:
    resolved = _absolute_non_root(path, label=label)
    if resolved.is_symlink() or not resolved.is_dir():
        raise ReleaseComposeError(f"{label} is not a real directory: {path}")
    return resolved


def _regular_maf(path: Path, *, label: str) -> Path:
    unresolved = Path(path).expanduser()
    if unresolved.suffix.lower() != ".maf":
        raise ReleaseComposeError(f"{label} must use the .maf package format: {unresolved}")
    resolved = _absolute_non_root(unresolved, label=label)
    try:
        info = unresolved.lstat()
    except OSError as exc:
        raise ReleaseComposeError(f"cannot inspect {label} {unresolved}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseComposeError(f"{label} is not a regular file: {unresolved}")
    return resolved


def parse_mapping(
    values: Iterable[str],
    *,
    allowed: Iterable[str],
    label: str,
) -> dict[str, Path]:
    allowed_names = set(allowed)
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name not in allowed_names or not raw_path:
            choices = ", ".join(sorted(allowed_names))
            raise ReleaseComposeError(
                f"{label} must use NAME=/absolute/path with NAME in: {choices}"
            )
        if name in result:
            raise ReleaseComposeError(f"duplicate {label} mapping for {name}")
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or path == Path(path.anchor) or "\0" in raw_path:
            raise ReleaseComposeError(
                f"{label} path must be non-root and absolute for {name}: {raw_path!r}"
            )
        result[name] = path
    return result


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in _IGNORED_SOURCE_NAMES or name.endswith(".pyc")
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_tree(root: Path) -> Iterator[tuple[Path, Path, os.stat_result]]:
    def fail(error: OSError) -> None:
        raise ReleaseComposeError(f"cannot traverse composed tree: {error}") from error

    for directory, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=fail
    ):
        directory_names.sort()
        file_names.sort()
        base = Path(directory)
        for name in list(directory_names) + file_names:
            path = base / name
            yield path.relative_to(root), path, path.lstat()


def _tree_digest(root: Path, *, omit_compose_metadata: bool = False) -> str:
    digest = hashlib.sha256()
    for relative, path, info in _walk_tree(root):
        if omit_compose_metadata and relative.as_posix() == "compose.json":
            continue
        encoded = relative.as_posix().encode("utf-8", errors="surrogateescape")
        if stat.S_ISDIR(info.st_mode):
            kind = b"d"
            payload = b""
        elif stat.S_ISLNK(info.st_mode):
            kind = b"l"
            payload = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            kind = b"x" if info.st_mode & 0o111 else b"f"
            payload = bytes.fromhex(_sha256_file(path))
        else:
            raise ReleaseComposeError(f"unsupported file type in compose: {relative}")
        digest.update(kind + b"\0" + encoded + b"\0" + payload + b"\0")
    return digest.hexdigest()


def _reject_python_caches(root: Path) -> None:
    rejected = [
        relative.as_posix()
        for relative, _path, _info in _walk_tree(root)
        if relative.name == "__pycache__" or relative.name.endswith(".pyc")
    ]
    if rejected:
        preview = ", ".join(rejected[:8])
        suffix = "" if len(rejected) <= 8 else f" (+{len(rejected) - 8} more)"
        raise ReleaseComposeError(
            f"composed release contains generated Python cache files: {preview}{suffix}"
        )


def _copy_source_tree(source: Path, destination: Path, *, label: str) -> None:
    resolved = _real_directory(source, label=label)
    if destination.exists() or destination.is_symlink():
        raise ReleaseComposeError(f"duplicate compose destination: {destination.name}")
    shutil.copytree(
        resolved,
        destination,
        symlinks=True,
        ignore=_copy_ignore,
        copy_function=shutil.copy2,
    )


def _python_runtime(path: Path, *, label: str) -> Path:
    """Accept only a complete, cache-free Python runtime tree."""

    runtime = _real_directory(path, label=label)
    python = runtime / "bin" / "python3"
    try:
        info = python.stat()
    except OSError as exc:
        raise ReleaseComposeError(f"{label} has no bin/python3: {python}") from exc
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise ReleaseComposeError(f"{label} bin/python3 is not executable: {python}")
    _reject_python_caches(runtime)
    return runtime


def _xft_python_runtime(path: Path) -> Path:
    """Validate an explicitly selected runtime before it replaces the baseline.

    The normal compose path keeps the already-verified baseline runtime.  An
    override is reserved for the target-built Tk/Xft runtime used by Settings
    and other Tk applications.  Requiring Tk's ELF to name libXft prevents an
    accidental copy of the stock core-font runtime that renders CJK as empty
    glyphs on the SPI X server.
    """

    runtime = _python_runtime(path, label="Python runtime override")
    tk = runtime / "lib" / "libtcl9tk9.0.so"
    try:
        payload = tk.read_bytes()
    except OSError as exc:
        raise ReleaseComposeError(
            f"Python runtime override has no Tk 9 shared library: {tk}"
        ) from exc
    if b"libXft.so.2" not in payload:
        raise ReleaseComposeError(
            "Python runtime override Tk library is not linked to libXft.so.2"
        )
    attestation = runtime / ".msys-tk-xft-runtime.json"
    try:
        document = json.loads(attestation.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseComposeError(
            "Python runtime override has no valid Tk/Xft verification attestation"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != "msys.tk-xft-runtime.v1"
        or document.get("xft_backend") != "libXft.so.2"
        or document.get("font_doctor") != "passed"
    ):
        raise ReleaseComposeError(
            "Python runtime override Tk/Xft verification attestation is unhealthy"
        )
    return runtime


def _metadata_digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_metadata(path: Path, document: dict[str, Any]) -> None:
    unsigned = dict(document)
    unsigned.pop("metadata_sha256", None)
    document = {**unsigned, "metadata_sha256": _metadata_digest(unsigned)}
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_metadata(path: Path, *, release_id: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseComposeError(f"compose metadata is missing or unsafe: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseComposeError(f"cannot read compose metadata {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != COMPOSE_SCHEMA:
        raise ReleaseComposeError(f"unsupported compose metadata: {path}")
    if document.get("release_id") != release_id:
        raise ReleaseComposeError(
            f"compose metadata id does not match directory {release_id!r}"
        )
    expected = document.get("metadata_sha256")
    unsigned = dict(document)
    unsigned.pop("metadata_sha256", None)
    if not isinstance(expected, str) or expected != _metadata_digest(unsigned):
        raise ReleaseComposeError(f"compose metadata digest changed: {path}")
    content = document.get("content_sha256")
    if not isinstance(content, str) or len(content) != 64:
        raise ReleaseComposeError(f"compose metadata has invalid content digest: {path}")
    return document


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return

    def make_writable_then_retry(
        function: Callable[..., Any], failed_path: str, _error: Any
    ) -> None:
        os.chmod(failed_path, 0o700)
        function(failed_path)

    shutil.rmtree(path, onerror=make_writable_then_retry)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _extract_verified_maf(
    api: InstallerApi,
    archive_path: Path,
    destination: Path,
    *,
    entry: str,
    expected_package_id: str,
    scratch_root: Path,
) -> dict[str, str]:
    archive = _regular_maf(archive_path, label=f"MAF input {entry}")
    before = _sha256_file(archive)
    try:
        inspected = api.inspect_archive(archive, require_content_hashes=True)
    except Exception as exc:
        raise ReleaseComposeError(f"cannot verify MAF input {entry}: {exc}") from exc
    if _sha256_file(archive) != before:
        raise ReleaseComposeError(f"MAF input changed while inspecting {entry}")
    if inspected.get("id") != expected_package_id:
        raise ReleaseComposeError(
            f"MAF identity mismatch for {entry}: expected {expected_package_id}, "
            f"got {inspected.get('id')!r}"
        )

    extraction = Path(tempfile.mkdtemp(prefix=f".extract-{entry}-", dir=scratch_root))
    extracted = extraction / "archive"
    extracted.mkdir()
    try:
        api.extract_archive(archive, extracted)
        package_dir = api.locate_archive_package(extracted)
        identity, content_digest = api.validate_staged_package(
            package_dir, require_content_hashes=True
        )
        if _sha256_file(archive) != before:
            raise ReleaseComposeError(f"MAF input changed while extracting {entry}")
        if identity.package_id != expected_package_id:
            raise ReleaseComposeError(
                f"MAF identity mismatch for {entry}: expected {expected_package_id}, "
                f"got {identity.package_id}"
            )
        if (
            inspected.get("version") != identity.version
            or inspected.get("content_sha256") != content_digest
        ):
            raise ReleaseComposeError(
                f"MAF inspection changed while composing {entry}"
            )
        if destination.exists() or destination.is_symlink():
            raise ReleaseComposeError(f"duplicate compose destination: {entry}")
        shutil.copytree(
            package_dir,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
        )
    finally:
        _remove_tree(extraction)
    return {
        "package_id": expected_package_id,
        "version": str(inspected["version"]),
        "artifact_sha256": before,
        "content_sha256": str(inspected["content_sha256"]),
        "manifest_sha256": str(inspected["manifest_sha256"]),
    }


def _verify_existing_compose(
    api: InstallerApi,
    final: Path,
    *,
    release_id: str,
) -> dict[str, Any]:
    if final.is_symlink() or not final.is_dir():
        raise ReleaseComposeError(f"existing compose is not a real directory: {final}")
    api.validate_release_tree(final)
    _reject_python_caches(final)
    metadata = _read_metadata(final / "compose.json", release_id=release_id)
    actual = _tree_digest(final, omit_compose_metadata=True)
    if actual != metadata["content_sha256"]:
        raise ReleaseComposeError(
            f"existing compose {release_id} content digest changed: expected "
            f"{metadata['content_sha256']}, got {actual}"
        )
    return metadata


def compose_release_source(
    *,
    release_id: str,
    release_root: Path,
    baseline_release: str,
    output_root: Path,
    source_entries: dict[str, Path],
    maf_entries: dict[str, Path],
    python_runtime: Path | None = None,
    api: InstallerApi | None = None,
) -> dict[str, Any]:
    """Build one immutable, stage-ready source without changing live state."""

    api = api or load_installer_api()
    release_id = api.validate_release_id(release_id)
    baseline_release = api.validate_release_id(baseline_release)
    if set(source_entries) != set(SOURCE_ENTRY_NAMES):
        missing = sorted(set(SOURCE_ENTRY_NAMES) - set(source_entries))
        extra = sorted(set(source_entries) - set(SOURCE_ENTRY_NAMES))
        raise ReleaseComposeError(
            f"source entry set mismatch (missing={missing}, extra={extra})"
        )
    if set(maf_entries) != set(MAF_ENTRY_PACKAGE_IDS):
        missing = sorted(set(MAF_ENTRY_PACKAGE_IDS) - set(maf_entries))
        extra = sorted(set(maf_entries) - set(MAF_ENTRY_PACKAGE_IDS))
        raise ReleaseComposeError(
            f"MAF entry set mismatch (missing={missing}, extra={extra})"
        )
    for entry in COMPOSED_ENTRIES:
        api.validate_release_entry(entry)

    formal_root = _real_directory(release_root, label="formal release root")
    try:
        baseline = api.verify_release(baseline_release, formal_root)
    except Exception as exc:
        raise ReleaseComposeError(
            f"baseline release {baseline_release!r} failed verification: {exc}"
        ) from exc
    baseline_path = _real_directory(Path(str(baseline["path"])), label="baseline release")
    expected_baseline_path = formal_root / "releases" / baseline_release
    try:
        expected_baseline = expected_baseline_path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseComposeError(
            f"verified baseline path is unavailable: {expected_baseline_path}"
        ) from exc
    if baseline_path != expected_baseline:
        raise ReleaseComposeError(
            "verified baseline path does not match the formal release directory: "
            f"{baseline_path}"
        )
    baseline_runtime = _python_runtime(
        baseline_path / ".runtime" / "python", label="baseline Python runtime"
    )
    runtime_python = (
        _xft_python_runtime(python_runtime)
        if python_runtime is not None
        else baseline_runtime
    )
    runtime_origin = "xft-override" if python_runtime is not None else "baseline"

    unresolved_output = Path(output_root).expanduser()
    if not unresolved_output.is_absolute() or unresolved_output == Path(
        unresolved_output.anchor
    ):
        raise ReleaseComposeError(
            f"compose output root must be a non-root absolute path: {unresolved_output}"
        )
    if unresolved_output.is_symlink():
        raise ReleaseComposeError(
            f"compose output root must not be a symbolic link: {unresolved_output}"
        )
    unresolved_output.mkdir(parents=True, exist_ok=True)
    output = _real_directory(unresolved_output, label="compose output root")
    final = output / release_id
    staging = output / f".compose-{release_id}-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        (staging / ".runtime").mkdir()
        shutil.copytree(
            runtime_python,
            staging / ".runtime" / "python",
            symlinks=True,
            ignore=_copy_ignore,
            copy_function=shutil.copy2,
        )

        source_metadata: dict[str, dict[str, str]] = {}
        for entry in SOURCE_ENTRY_NAMES:
            _copy_source_tree(
                source_entries[entry], staging / entry, label=f"source entry {entry}"
            )
            source_metadata[entry] = {
                "content_sha256": _tree_digest(staging / entry)
            }

        maf_metadata: dict[str, dict[str, str]] = {}
        for entry, package_id in MAF_ENTRY_PACKAGE_IDS.items():
            maf_metadata[entry] = _extract_verified_maf(
                api,
                maf_entries[entry],
                staging / entry,
                entry=entry,
                expected_package_id=package_id,
                scratch_root=output,
            )

        api.validate_release_tree(staging)
        _reject_python_caches(staging)
        content_digest = _tree_digest(staging, omit_compose_metadata=True)
        metadata: dict[str, Any] = {
            "schema": COMPOSE_SCHEMA,
            "release_id": release_id,
            "baseline": {
                "release_id": baseline_release,
                "content_sha256": str(baseline["content_sha256"]),
            },
            "python_runtime": {
                "origin": runtime_origin,
                "content_sha256": _tree_digest(staging / ".runtime" / "python"),
                "xft": python_runtime is not None,
            },
            "entries": list(COMPOSED_ENTRIES),
            "source_entries": source_metadata,
            "maf_entries": maf_metadata,
            "content_sha256": content_digest,
        }
        _write_metadata(staging / "compose.json", metadata)
        metadata = _read_metadata(staging / "compose.json", release_id=release_id)

        if final.exists() or final.is_symlink():
            existing = _verify_existing_compose(api, final, release_id=release_id)
            if existing["content_sha256"] != content_digest:
                raise ImmutableComposeError(
                    f"compose {release_id} already exists with different content"
                )
            return {**existing, "path": str(final), "already_present": True}
        try:
            os.rename(staging, final)
        except FileExistsError:
            existing = _verify_existing_compose(api, final, release_id=release_id)
            if existing["content_sha256"] != content_digest:
                raise ImmutableComposeError(
                    f"compose {release_id} already exists with different content"
                )
            return {**existing, "path": str(final), "already_present": True}
        _fsync_directory(output)
        return {**metadata, "path": str(final), "already_present": False}
    finally:
        if staging.exists() and not staging.is_symlink():
            _remove_tree(staging)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m msys_tools.release_compose")
    parser.add_argument("release_id")
    parser.add_argument("--release-root", default="/opt/msys")
    parser.add_argument("--baseline-release", required=True)
    parser.add_argument("--workspace-root", default="/opt/msys-dev")
    parser.add_argument("--output-root", default="/opt/msys-dev/release-sources")
    parser.add_argument(
        "--python-runtime",
        help=(
            "copy this complete target-built Tk/Xft Python runtime instead of "
            "inheriting the baseline runtime"
        ),
    )
    parser.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="override a synchronized source entry path",
    )
    parser.add_argument(
        "--maf",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="map a built-in release entry to one fully hashed MAF",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = _real_directory(Path(args.workspace_root), label="workspace root")
        overrides = parse_mapping(
            args.entry, allowed=SOURCE_ENTRY_NAMES, label="source entry"
        )
        sources = {
            name: overrides.get(name, workspace / name) for name in SOURCE_ENTRY_NAMES
        }
        mafs = parse_mapping(args.maf, allowed=MAF_ENTRY_PACKAGE_IDS, label="MAF")
        result = compose_release_source(
            release_id=args.release_id,
            release_root=Path(args.release_root),
            baseline_release=args.baseline_release,
            output_root=Path(args.output_root),
            source_entries=sources,
            maf_entries=mafs,
            python_runtime=(Path(args.python_runtime) if args.python_runtime else None),
        )
    except (ReleaseComposeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
