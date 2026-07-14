from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".maf")
PACKAGE_FORMAT_SUFFIXES = {"tar.gz": ".tar.gz", "maf": ".maf"}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
MANIFEST_DISCOVERY_SCHEMA = "msys.manifest-discovery.v1"
PACKAGE_IGNORE_FILE = ".msys-packageignore"
MAX_PACKAGE_IGNORE_BYTES = 64 * 1024
PROTECTED_OVERLAY_ROOTS = frozenset(
    {"manifest.json", "hashes.json", "signature.optional"}
)


class PackageFlowError(RuntimeError):
    """A package developer command could not be completed."""


@dataclass(frozen=True, slots=True)
class PackageOverlay:
    source: Path
    destination: PurePosixPath


def _overlay_destination(value: str) -> PurePosixPath:
    raw_parts = value.split("/")
    if "\\" in value or "\0" in value:
        raise PackageFlowError(f"unsafe package overlay destination: {value!r}")
    destination = PurePosixPath(value)
    if (
        not value
        or destination.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(ord(character) < 32 for character in value)
        or ":" in raw_parts[0]
    ):
        raise PackageFlowError(f"unsafe package overlay destination: {value!r}")
    if destination.parts[0].casefold() in PROTECTED_OVERLAY_ROOTS:
        raise PackageFlowError(
            f"package overlay cannot replace protected root {destination.parts[0]!r}"
        )
    return destination


def parse_overlay_spec(workspace: Path, value: str) -> PackageOverlay:
    """Parse ``SOURCE=RELATIVE_DEST`` without allowing package-root writes."""

    raw_source, separator, raw_destination = value.partition("=")
    if not separator or not raw_source or not raw_destination:
        raise PackageFlowError(
            "package overlay must use SOURCE=RELATIVE_DEST syntax"
        )
    destination = _overlay_destination(raw_destination)
    source_candidate = Path(raw_source).expanduser()
    if not source_candidate.is_absolute():
        source_candidate = Path(workspace).expanduser().resolve() / source_candidate
    if source_candidate.is_symlink():
        raise PackageFlowError(
            f"package overlay source must not be a symbolic link: {source_candidate}"
        )
    source = source_candidate.resolve()
    if not (source.is_file() or source.is_dir()):
        raise PackageFlowError(f"package overlay source is not a real file or directory: {source}")
    return PackageOverlay(source=source, destination=destination)


def _apply_package_overlays(staging: Path, overlays: Iterable[PackageOverlay]) -> list[dict[str, str]]:
    staging_root = staging.resolve(strict=True)
    applied: list[dict[str, str]] = []
    for overlay in overlays:
        destination_relative = _overlay_destination(
            overlay.destination.as_posix()
        )
        source_candidate = overlay.source.expanduser()
        if source_candidate.is_symlink():
            raise PackageFlowError(
                f"package overlay source must not be a symbolic link: {source_candidate}"
            )
        source = source_candidate.resolve()
        if not (source.is_file() or source.is_dir()):
            raise PackageFlowError(
                f"package overlay source is not a real file or directory: {source}"
            )
        destination = staging.joinpath(*destination_relative.parts)
        current = staging
        for part in destination_relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise PackageFlowError(
                    f"package overlay parent is a symbolic link: {destination_relative}"
                )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.parent.resolve(strict=True).relative_to(staging_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PackageFlowError(
                f"package overlay destination escapes staging: {destination_relative}"
            ) from exc
        if destination.exists() or destination.is_symlink():
            raise PackageFlowError(
                f"package overlay refuses to overwrite staged content: {destination_relative}"
            )
        try:
            if source.is_dir():
                shutil.copytree(
                    source,
                    destination,
                    symlinks=True,
                    ignore=_copy_ignore(source, frozenset()),
                )
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
        except Exception as exc:
            raise PackageFlowError(
                f"cannot apply package overlay {source}={destination_relative}: {exc}"
            ) from exc
        applied.append(
            {"source": str(source), "destination": destination_relative.as_posix()}
        )
    return applied


@dataclass(frozen=True, slots=True)
class InstallerApi:
    validate_manifest: Callable[[object], dict[str, Any]]
    make_hashes: Callable[[Path], Path]
    inspect_archive: Callable[..., dict[str, str]]
    make_update_index: Callable[..., dict[str, Any]]
    generate_keypair: Callable[..., dict[str, Any]]
    sign_update_index: Callable[..., dict[str, Any]]
    load_public_key_document: Callable[..., tuple[dict[str, Any], bytes]]


def _installer_source(workspace: Path) -> Path:
    configured = os.environ.get("MSYS_INSTALL_SOURCE")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            workspace / "msys-install",
            Path(__file__).resolve().parents[2] / "msys-install",
        ]
    )
    for candidate in candidates:
        source = candidate.resolve()
        if (source / "msys_install" / "cli.py").is_file():
            return source
    searched = ", ".join(str(path) for path in candidates)
    raise PackageFlowError(
        "msys-install source was not found; keep it beside msys-tools or set "
        f"MSYS_INSTALL_SOURCE (searched: {searched})"
    )


def load_installer_api(workspace: Path) -> InstallerApi:
    """Load the sibling installer implementation without installing it globally."""

    workspace = workspace.resolve()
    source = _installer_source(workspace)
    sdk_candidates = (
        workspace / "msys-sdk",
        Path(__file__).resolve().parents[2] / "msys-sdk",
    )
    for sdk_source in sdk_candidates:
        resolved_sdk = sdk_source.resolve()
        if (resolved_sdk / "msys_sdk" / "__init__.py").is_file():
            sdk_text = str(resolved_sdk)
            if sdk_text not in sys.path:
                sys.path.insert(0, sdk_text)
            break
    source_text = str(source)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from msys_install.cli import make_hashes
        from msys_install.contract import validate_manifest
        from msys_install.store import inspect_archive
        from msys_install.trust import (
            generate_keypair,
            load_public_key_document,
            sign_update_index,
        )
        from msys_install.update import make_update_index
    except ImportError as exc:
        raise PackageFlowError(f"cannot load msys-install from {source}: {exc}") from exc
    return InstallerApi(
        validate_manifest=validate_manifest,
        make_hashes=make_hashes,
        inspect_archive=inspect_archive,
        make_update_index=make_update_index,
        generate_keypair=generate_keypair,
        sign_update_index=sign_update_index,
        load_public_key_document=load_public_key_document,
    )


def _read_manifest(api: InstallerApi, manifest_path: Path) -> dict[str, Any]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PackageFlowError(f"manifest is not a regular file: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        return api.validate_manifest(data)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageFlowError(f"cannot read {manifest_path}: {exc}") from exc
    except Exception as exc:
        raise PackageFlowError(f"invalid {manifest_path}: {exc}") from exc


def _within_source(path: Path, source: Path) -> bool:
    try:
        path.relative_to(source)
        return True
    except ValueError:
        return False


def resolve_source_manifest(
    source: Path,
    manifest_path: Path | None = None,
) -> Path:
    """Resolve a project's package manifest without changing the project.

    Installable applications normally keep ``manifest.json`` at their root.
    System source repositories may instead keep one package manifest directly
    under ``manifests/`` (for example ``msys-hal``).  Supporting that layout in
    the workstation packager avoids adding target-side Python or package
    manager setup merely for development delivery.
    """

    source = source.expanduser().resolve()
    if manifest_path is not None:
        candidate = manifest_path.expanduser()
        if not candidate.is_absolute():
            candidate = source / candidate
        if candidate.is_symlink():
            raise PackageFlowError(f"manifest is not a regular file: {candidate}")
        candidate = candidate.resolve()
        if not _within_source(candidate, source):
            raise PackageFlowError(
                f"package manifest must stay within the package source: {candidate}"
            )
        if candidate.is_symlink() or not candidate.is_file():
            raise PackageFlowError(f"manifest is not a regular file: {candidate}")
        return candidate

    conventional = source / "manifest.json"
    if conventional.is_file() and not conventional.is_symlink():
        return conventional

    manifest_dir = source / "manifests"
    candidates = (
        sorted(
            path
            for path in manifest_dir.glob("*.json")
            if path.is_file() and not path.is_symlink()
        )
        if manifest_dir.is_dir()
        else []
    )
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise PackageFlowError(
            f"missing manifest.json in {source}; no package manifest was found in "
            f"{manifest_dir}"
        )
    names = ", ".join(path.name for path in candidates)
    raise PackageFlowError(
        f"multiple package manifests found in {manifest_dir}: {names}; select one "
        "with --manifest"
    )


def _stage_source(source: Path, manifest_path: Path, staging: Path) -> None:
    try:
        ignored_paths = _read_package_ignore(source)
        shutil.copytree(
            source,
            staging,
            symlinks=True,
            ignore=_copy_ignore(source, ignored_paths),
        )
        staged_manifest = staging / "manifest.json"
        source_relative = manifest_path.relative_to(source)
        if source_relative != Path("manifest.json"):
            shutil.copy2(manifest_path, staged_manifest, follow_symlinks=False)
    except Exception as exc:
        raise PackageFlowError(f"cannot stage package {source}: {exc}") from exc


def _manifest_candidate(path: Path) -> bool:
    return path.name == "manifest.json" or path.parent.name == "manifests"


def _walk_manifest_candidates(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    result: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in IGNORED_DIRECTORY_NAMES)
        parent = Path(directory)
        for name in sorted(files):
            path = parent / name
            if name.endswith(".json") and _manifest_candidate(path):
                result.append(path)
    return result


def discover_manifests(workspace: Path, search_root: Path) -> dict[str, Any]:
    """Discover and strictly validate MSYS manifests below *search_root*.

    Non-MSYS JSON files in a ``manifests`` directory are reported as invalid;
    unrelated JSON elsewhere is deliberately ignored.  The result is stable
    JSON suitable for CI as well as an interactive developer check.
    """

    api = load_installer_api(workspace)
    root = search_root.expanduser().resolve()
    if not root.exists():
        raise PackageFlowError(f"manifest search path does not exist: {root}")
    rows: list[dict[str, Any]] = []
    for path in _walk_manifest_candidates(root):
        try:
            manifest = _read_manifest(api, path)
            package = manifest["package"]
            rows.append(
                {
                    "path": str(path),
                    "valid": True,
                    "package": package["id"],
                    "version": package["version"],
                    "kind": package.get("kind", "application"),
                    "components": len(manifest["components"]),
                }
            )
        except PackageFlowError as exc:
            rows.append({"path": str(path), "valid": False, "error": str(exc)})
    return {
        "schema": MANIFEST_DISCOVERY_SCHEMA,
        "root": str(root),
        "valid": all(row["valid"] for row in rows),
        "count": len(rows),
        "manifests": rows,
    }


def _normalise_tar_info(info: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    elif info.issym():
        info.mode = 0o777
    return info


def _write_tar_gz(source: Path, output: Path, *, epoch: int = 0) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                archive.add(
                    source,
                    arcname=".",
                    recursive=True,
                    filter=lambda info: _normalise_tar_info(info, epoch),
                )


def _temporary_archive(source: Path) -> Path:
    handle, name = tempfile.mkstemp(prefix="msys-validate-", suffix=".tar.gz")
    os.close(handle)
    output = Path(name)
    output.unlink()
    _write_tar_gz(source, output)
    return output


def _summary(details: dict[str, str], path: Path) -> dict[str, Any]:
    return {
        "valid": True,
        "path": str(path),
        "package": details["id"],
        "version": details["version"],
        "manifest_sha256": details["manifest_sha256"],
        "content_sha256": details["content_sha256"],
    }


def validate_package(
    workspace: Path,
    package: Path,
    *,
    require_content_hashes: bool = False,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Validate a manifest, package directory, or package archive."""

    api = load_installer_api(workspace)
    package = package.expanduser().resolve()
    if package.is_dir():
        manifest = resolve_source_manifest(package, manifest_path)
        with tempfile.TemporaryDirectory(prefix="msys-validate-source-") as temporary:
            staging = Path(temporary) / "package"
            _stage_source(package, manifest, staging)
            archive = _temporary_archive(staging)
            try:
                details = api.inspect_archive(
                    archive, require_content_hashes=require_content_hashes
                )
            except Exception as exc:
                raise PackageFlowError(f"invalid package directory {package}: {exc}") from exc
            finally:
                archive.unlink(missing_ok=True)
        return _summary(details, package)
    if package.is_file() and package.name.lower().endswith(".json"):
        manifest = _read_manifest(api, package)
        package_data = manifest["package"]
        return {
            "valid": True,
            "path": str(package),
            "package": package_data["id"],
            "version": package_data["version"],
            "components": len(manifest["components"]),
        }
    if not package.is_file():
        raise PackageFlowError(f"package path does not exist: {package}")
    try:
        details = api.inspect_archive(
            package, require_content_hashes=require_content_hashes
        )
    except Exception as exc:
        raise PackageFlowError(f"invalid package archive {package}: {exc}") from exc
    return _summary(details, package)


def _read_package_ignore(source: Path) -> frozenset[PurePosixPath]:
    path = source / PACKAGE_IGNORE_FILE
    if not path.exists() and not path.is_symlink():
        return frozenset()
    if path.is_symlink() or not path.is_file():
        raise PackageFlowError(f"package ignore file is not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackageFlowError(f"cannot read package ignore file {path}: {exc}") from exc
    if len(raw) > MAX_PACKAGE_IGNORE_BYTES:
        raise PackageFlowError(
            f"package ignore file exceeds {MAX_PACKAGE_IGNORE_BYTES} bytes: {path}"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PackageFlowError(f"package ignore file is not UTF-8: {path}") from exc

    ignored: set[PurePosixPath] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        value = value.rstrip("/")
        candidate = PurePosixPath(value)
        if (
            not value
            or candidate.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or any(ord(character) < 32 for character in value)
        ):
            raise PackageFlowError(
                f"unsafe package ignore path at {path}:{line_number}: {raw_line!r}"
            )
        if candidate == PurePosixPath("manifest.json"):
            raise PackageFlowError(
                f"package ignore file cannot exclude manifest.json: {path}:{line_number}"
            )
        ignored.add(candidate)
    return frozenset(ignored)


def _copy_ignore(
    source: Path,
    ignored_paths: frozenset[PurePosixPath],
) -> Callable[[str, list[str]], set[str]]:
    source = source.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        relative = Path(directory).resolve().relative_to(source)
        parent = PurePosixPath(relative.as_posix())
        ignored = {
            name
            for name in names
            if name in IGNORED_DIRECTORY_NAMES
            or name.endswith((".pyc", ".pyo"))
            or (parent / name) in ignored_paths
            or (relative == Path(".") and name == PACKAGE_IGNORE_FILE)
        }
        return ignored

    return ignore


def _archive_output(
    output: Path,
    package_id: str,
    version: str,
    *,
    artifact_format: str,
) -> Path:
    suffix = PACKAGE_FORMAT_SUFFIXES.get(artifact_format)
    if suffix is None:
        supported = ", ".join(sorted(PACKAGE_FORMAT_SUFFIXES))
        raise PackageFlowError(
            f"unsupported package format {artifact_format!r}; choose {supported}"
        )
    output = output.expanduser()
    lower = output.name.lower()
    explicit_suffix = next(
        (candidate for candidate in ARCHIVE_SUFFIXES if lower.endswith(candidate)),
        None,
    )
    if explicit_suffix is not None:
        explicit_format = "maf" if explicit_suffix == ".maf" else "tar.gz"
        if explicit_format != artifact_format:
            raise PackageFlowError(
                f"output suffix {explicit_suffix} conflicts with "
                f"--format {artifact_format}"
            )
        return output.resolve()
    return (output / f"{package_id}-{version}{suffix}").resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_package(
    workspace: Path,
    package_dir: Path,
    output: Path,
    *,
    force: bool = False,
    source_date_epoch: int | None = None,
    manifest_path: Path | None = None,
    artifact_format: str = "tar.gz",
    overlays: Iterable[PackageOverlay] = (),
) -> dict[str, Any]:
    """Build a verified, content-hashed update artifact without changing source."""

    api = load_installer_api(workspace)
    source = package_dir.expanduser().resolve()
    if source.is_symlink() or not source.is_dir():
        raise PackageFlowError(f"package root is not a real directory: {source}")
    source_manifest = resolve_source_manifest(source, manifest_path)
    manifest = _read_manifest(api, source_manifest)
    package_data = manifest["package"]
    artifact = _archive_output(
        output,
        package_data["id"],
        package_data["version"],
        artifact_format=artifact_format,
    )
    if _is_within(artifact, source):
        raise PackageFlowError("package output must be outside the package source directory")
    if artifact.exists() and not force:
        raise PackageFlowError(f"output already exists (use --force): {artifact}")
    if artifact.exists() and (artifact.is_symlink() or not artifact.is_file()):
        raise PackageFlowError(f"output is not a regular file: {artifact}")

    epoch = source_date_epoch
    if epoch is None:
        raw_epoch = os.environ.get("SOURCE_DATE_EPOCH", "0")
        try:
            epoch = int(raw_epoch)
        except ValueError as exc:
            raise PackageFlowError("SOURCE_DATE_EPOCH must be an integer") from exc
    if epoch < 0:
        raise PackageFlowError("SOURCE_DATE_EPOCH must not be negative")

    selected_overlays = tuple(overlays)
    with tempfile.TemporaryDirectory(prefix="msys-package-") as temporary:
        staging = Path(temporary) / "package"
        try:
            _stage_source(source, source_manifest, staging)
            applied_overlays = _apply_package_overlays(staging, selected_overlays)
            api.make_hashes(staging)
        except Exception as exc:
            raise PackageFlowError(f"cannot stage package {source}: {exc}") from exc
        artifact.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact.name}.", suffix=".tmp", dir=artifact.parent
        )
        os.close(descriptor)
        temporary_artifact = Path(temporary_name)
        temporary_artifact.unlink()
        try:
            _write_tar_gz(staging, temporary_artifact, epoch=epoch)
            details = api.inspect_archive(
                temporary_artifact, require_content_hashes=True
            )
            if details["id"] != package_data["id"] or details["version"] != package_data["version"]:
                raise PackageFlowError("built archive identity does not match source manifest")
            temporary_artifact.replace(artifact)
        except PackageFlowError:
            temporary_artifact.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary_artifact.unlink(missing_ok=True)
            raise PackageFlowError(f"cannot build package archive: {exc}") from exc

    return {
        "artifact": str(artifact),
        "package": details["id"],
        "version": details["version"],
        "sha256": _sha256_file(artifact),
        "manifest_sha256": details["manifest_sha256"],
        "content_sha256": details["content_sha256"],
        "format": artifact_format,
        "overlays": applied_overlays,
    }


def build_index(
    workspace: Path,
    repository: Path,
    output: Path | None = None,
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Generate an update index using the installer's verification path."""

    api = load_installer_api(workspace)
    repository = repository.expanduser().resolve()
    target = (output.expanduser().resolve() if output else repository / "index.json")
    try:
        data = api.make_update_index(repository, target, base_url=base_url)
    except Exception as exc:
        raise PackageFlowError(f"cannot build update index: {exc}") from exc
    return {"index": str(target), **data}


def generate_update_signing_key(
    workspace: Path,
    private_key: Path,
    public_key: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Generate an Ed25519 publisher keypair without exposing private bytes."""

    api = load_installer_api(workspace)
    try:
        return api.generate_keypair(
            private_key,
            public_key,
            force=force,
        )
    except Exception as exc:
        raise PackageFlowError(f"cannot generate update signing key: {exc}") from exc


def sign_update_index_file(
    workspace: Path,
    index: Path,
    private_key: Path,
    *,
    sequence: int,
    expires: str,
    output: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Canonically sign one update index with a local Ed25519 private key."""

    api = load_installer_api(workspace)
    try:
        return api.sign_update_index(
            index,
            private_key,
            sequence=sequence,
            expires=expires,
            output=output,
            force=force,
        )
    except Exception as exc:
        raise PackageFlowError(f"cannot sign update index: {exc}") from exc


def inspect_update_public_key(
    workspace: Path,
    public_key: Path,
) -> dict[str, Any]:
    """Validate a public-only publisher document before remote provisioning."""

    api = load_installer_api(workspace)
    try:
        document, raw_key = api.load_public_key_document(public_key)
    except Exception as exc:
        raise PackageFlowError(f"invalid update public key: {exc}") from exc
    return {
        "schema": str(document["schema"]),
        "algorithm": str(document["algorithm"]),
        "key_id": str(document["key_id"]),
        "public_key": str(document["public_key"]),
        "size": len(raw_key),
        "path": str(public_key.expanduser().resolve()),
    }
