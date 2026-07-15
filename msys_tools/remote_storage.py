"""Small, conservative target storage report and cleanup helper.

Package versions and whole-system Releases are inventory-only.  Mutation is
limited to data that is unambiguously rebuildable or diagnostic: development
caches, atomic-sync rollback copies, installer transient staging, and rotated
daemon logs.  The default operation is read-only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator


SCHEMA = "msys.storage-report.v1"
ARCHIVE_SCHEMA = "msys.storage-offload.v1"
CACHE_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"}
)
REPOSITORY = re.compile(r"^msys-[A-Za-z0-9._-]+$")
DEV_PREVIOUS = re.compile(r"^\.(msys-[A-Za-z0-9._-]+)\.previous$")
PACKAGE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+~-]{0,126}[A-Za-z0-9])?$")
TRANSIENT_ARCHIVE = re.compile(
    r"^[0-9a-f]{64}(?:\.maf|\.tar|\.tar\.gz|\.tgz|\.tar\.xz|\.tar\.bz2|\.zip|\.pkg)$"
)
ROTATED_LOG = re.compile(r"^(?:[0-9]+|[0-9]{8}(?:T[0-9]{6}Z)?|old)$")
FORBIDDEN_ROOTS = (Path("/root/.codex"), Path("/app"))
MAX_JSON_BYTES = 8 * 1024 * 1024
ARCHIVE_RESERVE_BYTES = 16 * 1024 * 1024


class StorageError(RuntimeError):
    """A storage operation could not stay inside its conservative boundary."""


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    managed_root: Path
    kind: str
    reason: str
    allocated_bytes: int
    logical_bytes: int
    tree_sha256: str
    device: int
    inode: int
    mode: int

    def document(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "reason": self.reason,
            "allocated_bytes": self.allocated_bytes,
            "logical_bytes": self.logical_bytes,
            "tree_sha256": self.tree_sha256,
        }


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _root(path: Path, label: str, *, required: bool = False) -> Path:
    raw = Path(path)
    if not raw.is_absolute() or raw == Path("/"):
        raise StorageError(f"{label} must be an absolute non-root path")
    if raw.is_symlink():
        raise StorageError(f"{label} must not be a symbolic link: {raw}")
    try:
        resolved = raw.resolve(strict=required)
    except OSError as exc:
        raise StorageError(f"cannot resolve {label} {raw}: {exc}") from exc
    for forbidden in FORBIDDEN_ROOTS:
        if (
            resolved == forbidden
            or _within(resolved, forbidden)
            or _within(forbidden, resolved)
        ):
            raise StorageError(f"{label} overlaps protected host data: {resolved}")
    if required and (resolved.is_symlink() or not resolved.is_dir()):
        raise StorageError(f"{label} is not a real directory: {resolved}")
    return resolved


def _validate_roots(dev: Path, state: Path, release: Path, usb: Path) -> None:
    managed = (dev, state, release)
    for index, left in enumerate(managed):
        for right in managed[index + 1 :]:
            if _within(left, right) or _within(right, left):
                raise StorageError(f"managed roots overlap: {left}, {right}")
    if any(_within(usb, item) or _within(item, usb) for item in managed):
        raise StorageError("USB root overlaps managed MSYS data")


def _canonical_digest(records: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(records):
        digest.update(name.encode("utf-8", "surrogateescape") + b"\0" + payload + b"\0")
    return digest.hexdigest()


def _tree_snapshot(path: Path) -> tuple[int, int, str]:
    root = path.resolve(strict=True)
    root_info = path.lstat()
    if stat.S_ISLNK(root_info.st_mode) or os.path.ismount(path):
        raise StorageError(f"candidate root is a link or mount: {path}")
    records: list[tuple[str, bytes]] = []
    allocated = 0
    logical = 0

    def visit(current: Path, relative: PurePosixPath) -> None:
        nonlocal allocated, logical
        info = current.lstat()
        if info.st_dev != root_info.st_dev or (current != path and os.path.ismount(current)):
            raise StorageError(f"candidate crosses a mounted filesystem: {current}")
        blocks = getattr(info, "st_blocks", None)
        allocated += int(blocks) * 512 if blocks is not None else int(info.st_size)
        name = relative.as_posix()
        if stat.S_ISREG(info.st_mode):
            logical += int(info.st_size)
            file_digest = hashlib.sha256()
            with current.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            payload = b"f\0" + str(info.st_size).encode("ascii") + b"\0" + file_digest.digest()
        elif stat.S_ISDIR(info.st_mode):
            payload = b"d"
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                visit(child, relative / child.name)
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(current)
            if not target or os.path.isabs(target):
                raise StorageError(f"candidate has an unsafe symlink: {current}")
            try:
                current.resolve(strict=True).relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StorageError(f"candidate symlink escapes its root: {current}") from exc
            payload = b"l\0" + os.fsencode(target)
        else:
            raise StorageError(f"candidate contains an unsupported file type: {current}")
        records.append((name, payload))

    visit(path, PurePosixPath("."))
    return allocated, logical, _canonical_digest(records)


def _tree_size(path: Path) -> tuple[int, int]:
    """Measure an inventory tree without hashing every read-only file."""

    allocated = 0
    logical = 0
    root_info = path.lstat()
    root_device = root_info.st_dev
    root_blocks = getattr(root_info, "st_blocks", None)
    allocated += (
        int(root_blocks) * 512 if root_blocks is not None else int(root_info.st_size)
    )
    for directory, directory_names, file_names in os.walk(
        path, topdown=True, followlinks=False
    ):
        base = Path(directory)
        retained: list[str] = []
        for name in sorted(directory_names):
            child = base / name
            info = child.lstat()
            blocks = getattr(info, "st_blocks", None)
            allocated += int(blocks) * 512 if blocks is not None else int(info.st_size)
            if not child.is_symlink() and info.st_dev == root_device:
                retained.append(name)
        directory_names[:] = retained
        for name in sorted(file_names):
            child = base / name
            info = child.lstat()
            blocks = getattr(info, "st_blocks", None)
            allocated += int(blocks) * 512 if blocks is not None else int(info.st_size)
            if stat.S_ISREG(info.st_mode):
                logical += int(info.st_size)
    return allocated, logical


def _candidate(path: Path, root: Path, kind: str, reason: str) -> Candidate | None:
    try:
        info = path.lstat()
        root_info = root.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_dev != root_info.st_dev
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        ):
            return None
        allocated, logical, digest = _tree_snapshot(path)
    except (OSError, StorageError):
        return None
    resolved = path.resolve()
    if not _within(resolved, root):
        return None
    return Candidate(
        resolved,
        root,
        kind,
        reason,
        allocated,
        logical,
        digest,
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
    )


def _dev_candidates(dev: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    if dev.is_symlink() or not dev.is_dir():
        return candidates
    for entry in sorted(dev.iterdir(), key=lambda item: item.name):
        match = DEV_PREVIOUS.fullmatch(entry.name)
        if match and (dev / match.group(1)).is_dir() and not (dev / match.group(1)).is_symlink():
            item = _candidate(entry, dev, "dev-previous", "atomic sync rollback copy")
            if item is not None:
                candidates.append(item)
    for repository in sorted(dev.iterdir(), key=lambda item: item.name):
        if (
            not REPOSITORY.fullmatch(repository.name)
            or repository.is_symlink()
            or not repository.is_dir()
        ):
            continue
        for directory, directory_names, _files in os.walk(
            repository, topdown=True, followlinks=False
        ):
            base = Path(directory)
            retained: list[str] = []
            for name in sorted(directory_names):
                child = base / name
                if child.is_symlink():
                    continue
                if name in CACHE_NAMES:
                    item = _candidate(
                        child, dev, "dev-cache", f"generated {name} directory"
                    )
                    if item is not None:
                        candidates.append(item)
                elif name != ".git":
                    retained.append(name)
            directory_names[:] = retained
    return candidates


def _update_candidates(state: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    selections = (
        (state / "updates/staged", lambda name: name.startswith("install-")),
        (
            state / "updates/incoming",
            lambda name: name.startswith(".incoming-") or bool(TRANSIENT_ARCHIVE.fullmatch(name)),
        ),
        (state / "updates/downloads", lambda name: name.startswith(".download-")),
    )
    for directory, predicate in selections:
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if predicate(path.name):
                item = _candidate(
                    path, state, "update-transient", "installer-owned rebuildable residue"
                )
                if item is not None:
                    candidates.append(item)
    return candidates


def _log_candidates(log_file: Path) -> list[Candidate]:
    result: list[Candidate] = []
    parent = log_file.parent.resolve()
    if parent.is_symlink() or not parent.is_dir():
        return result
    prefix = log_file.name + "."
    for path in sorted(parent.glob(log_file.name + ".*"), key=lambda item: item.name):
        suffix = path.name[len(prefix) :]
        if not ROTATED_LOG.fullmatch(suffix):
            continue
        item = _candidate(path, parent, "old-log", "rotated daemon log")
        if item is not None:
            result.append(item)
    return result


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise StorageError(f"unsafe JSON file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"cannot read {path}: {exc}") from exc


def _package_inventory(state: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    referenced: set[tuple[str, str]] = set()
    registry = state / "registry/installed.json"
    try:
        document = _read_json(registry)
        for item in document.get("packages", []) if isinstance(document, dict) else []:
            if isinstance(item, dict):
                package, version = item.get("package"), item.get("version")
                if isinstance(package, str) and isinstance(version, str):
                    referenced.add((package, version))
    except StorageError as exc:
        issues.append({"code": "REGISTRY_UNREADABLE", "message": str(exc)})
    packages = state / "packages"
    inventory: list[dict[str, Any]] = []
    if packages.is_symlink() or not packages.is_dir():
        return inventory, issues
    for package_root in sorted(packages.iterdir(), key=lambda item: item.name):
        if not PACKAGE_ID.fullmatch(package_root.name) or package_root.is_symlink():
            continue
        for pointer_name in ("current.json", "previous.json"):
            pointer = package_root / pointer_name
            if not pointer.exists() and not pointer.is_symlink():
                continue
            try:
                document = _read_json(pointer)
                version = document.get("version") if isinstance(document, dict) else None
                if isinstance(version, str):
                    referenced.add((package_root.name, version))
            except StorageError as exc:
                issues.append({"code": "POINTER_UNREADABLE", "message": str(exc)})
        versions = package_root / "versions"
        if versions.is_symlink() or not versions.is_dir():
            continue
        for version_root in sorted(versions.iterdir(), key=lambda item: item.name):
            if not VERSION.fullmatch(version_root.name) or version_root.is_symlink():
                continue
            try:
                allocated, logical = _tree_size(version_root)
            except (OSError, StorageError) as exc:
                issues.append(
                    {
                        "code": "PACKAGE_INVENTORY_UNREADABLE",
                        "message": f"{version_root}: {exc}",
                    }
                )
                continue
            inventory.append(
                {
                    "package": package_root.name,
                    "version": version_root.name,
                    "path": str(version_root.resolve()),
                    "allocated_bytes": allocated,
                    "logical_bytes": logical,
                    "referenced": (package_root.name, version_root.name) in referenced,
                    "deletion_eligible": False,
                }
            )
    return inventory, issues


def _release_inventory(release: Path) -> dict[str, Any]:
    pointers: dict[str, str | None] = {}
    releases = release / "releases"
    resolved_releases = releases.resolve() if releases.is_dir() else releases
    for name in ("current", "previous"):
        pointer = release / name
        value = None
        if pointer.is_symlink():
            try:
                target = pointer.resolve(strict=True)
                if _within(target, resolved_releases):
                    value = target.name
            except OSError:
                pass
        pointers[name] = value
    items: list[dict[str, Any]] = []
    if releases.is_dir() and not releases.is_symlink():
        for path in sorted(releases.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                allocated, logical = _tree_size(path)
            except (OSError, StorageError):
                continue
            items.append(
                {
                    "release": path.name,
                    "path": str(path.resolve()),
                    "allocated_bytes": allocated,
                    "logical_bytes": logical,
                    "current": path.name == pointers["current"],
                    "previous": path.name == pointers["previous"],
                    "deletion_eligible": False,
                }
            )
    return {"current": pointers["current"], "previous": pointers["previous"], "items": items}


def _filesystem(path: Path) -> dict[str, Any]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
        return {
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}


def _usb(path: Path) -> dict[str, Any]:
    report = _filesystem(path)
    report["exists"] = path.exists()
    report["mounted"] = path.exists() and not path.is_symlink() and os.path.ismount(path)
    report["archive_eligible"] = _is_distinct_mount(path)
    return report


def _is_distinct_mount(path: Path) -> bool:
    try:
        return (
            path.is_dir()
            and not path.is_symlink()
            and os.path.ismount(path)
            and path.stat().st_dev != Path("/").stat().st_dev
        )
    except OSError:
        return False


def build_plan(
    dev_root: Path,
    state_root: Path,
    release_root: Path,
    usb_root: Path,
    log_file: Path = Path("/tmp/msysd.log"),
) -> dict[str, Any]:
    dev = _root(dev_root, "development root")
    state = _root(state_root, "state root")
    release = _root(release_root, "release root")
    usb = _root(usb_root, "USB root")
    log = _root(log_file, "log path")
    _validate_roots(dev, state, release, usb)
    candidates = _dev_candidates(dev) + _update_candidates(state) + _log_candidates(log)
    ordered: list[Candidate] = []
    for item in sorted(candidates, key=lambda value: (len(value.path.parts), str(value.path))):
        if not any(_within(item.path, outer.path) for outer in ordered):
            ordered.append(item)
    package_versions, issues = _package_inventory(state)
    return {
        "schema": SCHEMA,
        "mode": "dry-run",
        "roots": {
            "development": str(dev),
            "state": str(state),
            "release": str(release),
            "usb": str(usb),
            "log": str(log),
        },
        "filesystems": {
            "root": _filesystem(Path("/")),
            "development": _filesystem(dev),
            "state": _filesystem(state),
            "release": _filesystem(release),
        },
        "usb": _usb(usb),
        "package_versions": package_versions,
        "package_version_bytes": sum(item["allocated_bytes"] for item in package_versions),
        "releases": _release_inventory(release),
        "issues": issues,
        "candidates": [item.document() for item in ordered],
        "candidate_count": len(ordered),
        "reclaimable_bytes": sum(item.allocated_bytes for item in ordered),
        "_candidate_objects": ordered,
    }


@contextlib.contextmanager
def _install_lock(state: Path) -> Iterator[None]:
    registry = state / "registry"
    if registry.is_symlink():
        raise StorageError(f"registry path is a symlink: {registry}")
    registry.mkdir(parents=True, exist_ok=True)
    lock = registry / ".install.lock"
    if lock.is_symlink():
        raise StorageError(f"install lock is a symlink: {lock}")
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_name(path: Path) -> str:
    return "rootfs/" + path.as_posix().lstrip("/")


def _verify_archive(path: Path, expected: str, candidates: list[Candidate]) -> None:
    if _sha256(path) != expected:
        raise StorageError("archive SHA-256 changed during complete reread")
    expected_items = {_archive_name(item.path): item for item in candidates}
    records = {root: [] for root in expected_items}
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if name in seen or ".." in PurePosixPath(name).parts:
                    raise StorageError(f"unsafe or duplicate archive member: {name}")
                seen.add(name)
                root = next(
                    (item for item in expected_items if name == item or name.startswith(item + "/")),
                    None,
                )
                if root is None:
                    raise StorageError(f"unexpected archive member: {name}")
                relative = "." if name == root else name[len(root) + 1 :]
                if member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise StorageError(f"cannot read archive member: {name}")
                    digest = hashlib.sha256()
                    size = 0
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                    if size != member.size:
                        raise StorageError(f"truncated archive member: {name}")
                    payload = b"f\0" + str(size).encode("ascii") + b"\0" + digest.digest()
                elif member.isdir():
                    payload = b"d"
                elif member.issym():
                    target = PurePosixPath(member.linkname)
                    if not member.linkname or target.is_absolute() or ".." in target.parts:
                        raise StorageError(f"unsafe archive link: {name}")
                    payload = b"l\0" + os.fsencode(member.linkname)
                else:
                    raise StorageError(f"unsupported archive member: {name}")
                records[root].append((relative, payload))
    except (OSError, tarfile.TarError) as exc:
        raise StorageError(f"cannot completely read archive: {exc}") from exc
    for root, item in expected_items.items():
        if _canonical_digest(records[root]) != item.tree_sha256:
            raise StorageError(f"archive tree digest mismatch: {item.path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_archive(
    usb: Path,
    candidates: list[Candidate],
    now: dt.datetime,
) -> dict[str, Any]:
    if not _is_distinct_mount(usb):
        raise StorageError(f"USB root is not a distinct mounted filesystem: {usb}")
    required = sum(item.logical_bytes for item in candidates) + ARCHIVE_RESERVE_BYTES
    if shutil.disk_usage(usb).free < required:
        raise StorageError("USB does not have enough free space for the verified archive")
    parent = usb / "msys-offload"
    if parent.is_symlink():
        raise StorageError(f"offload directory is a symlink: {parent}")
    parent.mkdir(mode=0o700, exist_ok=True)
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transaction = parent / stamp
    suffix = 0
    while transaction.exists() or transaction.is_symlink():
        suffix += 1
        transaction = parent / f"{stamp}-{suffix:02d}"
    transaction.mkdir(mode=0o700)
    archive_path = transaction / "payload.tar"
    with tarfile.open(archive_path, mode="x:", dereference=False) as archive:
        for item in candidates:
            archive.add(item.path, arcname=_archive_name(item.path), recursive=True)
    with archive_path.open("rb") as handle:
        os.fsync(handle.fileno())
    digest = _sha256(archive_path)
    checksum = transaction / "payload.tar.sha256"
    checksum.write_text(f"{digest}  payload.tar\n", encoding="ascii")
    with checksum.open("rb") as handle:
        os.fsync(handle.fileno())
    recorded = checksum.read_text(encoding="ascii").split()[0]
    _verify_archive(archive_path, recorded, candidates)
    manifest = {
        "schema": ARCHIVE_SCHEMA,
        "created_at": now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "path": str(archive_path),
        "archive_sha256": digest,
        "archive_bytes": archive_path.stat().st_size,
        "candidates": [item.document() for item in candidates],
    }
    manifest_path = transaction / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with manifest_path.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(transaction)
    _fsync_directory(parent)
    _fsync_directory(usb)
    return manifest


def _revalidate(item: Candidate) -> None:
    info = item.path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or info.st_dev != item.device
        or info.st_ino != item.inode
        or stat.S_IFMT(info.st_mode) != stat.S_IFMT(item.mode)
    ):
        raise StorageError(f"candidate identity changed before cleanup: {item.path}")
    _allocated, _logical, digest = _tree_snapshot(item.path)
    if digest != item.tree_sha256:
        raise StorageError(f"candidate content changed before cleanup: {item.path}")


def _remove(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise StorageError(f"candidate became a symlink: {path}")
    if stat.S_ISREG(info.st_mode):
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode):
        raise StorageError(f"candidate has unsupported type: {path}")
    for directory, directory_names, _files in os.walk(path, topdown=False):
        base = Path(directory)
        for name in directory_names:
            child = base / name
            if not child.is_symlink():
                with contextlib.suppress(OSError):
                    child.chmod(0o700)
        with contextlib.suppress(OSError):
            base.chmod(0o700)
    shutil.rmtree(path)


def run(
    dev_root: Path,
    state_root: Path,
    release_root: Path,
    usb_root: Path,
    *,
    log_file: Path = Path("/tmp/msysd.log"),
    apply: bool = False,
    archive: bool = True,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if not apply and not archive:
        raise StorageError("--no-archive requires explicit --apply")
    state = _root(state_root, "state root")
    if not apply:
        report = build_plan(dev_root, state, release_root, usb_root, log_file)
        report.pop("_candidate_objects", None)
        return report
    with _install_lock(state):
        report = build_plan(dev_root, state, release_root, usb_root, log_file)
        candidates = list(report.pop("_candidate_objects"))
        created = None
        if candidates and archive:
            created = _create_archive(
                Path(report["roots"]["usb"]),
                candidates,
                now or dt.datetime.now(dt.timezone.utc),
            )
        for item in candidates:
            _revalidate(item)
        for item in sorted(candidates, key=lambda value: len(value.path.parts), reverse=True):
            _remove(item.path)
        report.update(
            {
                "mode": "apply",
                "archive": created,
                "archive_skipped": not archive,
                "removed": [item.document() for item in candidates],
                "removed_count": len(candidates),
                "reclaimed_bytes": sum(item.allocated_bytes for item in candidates),
            }
        )
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="safe MSYS storage inventory/offload")
    parser.add_argument("--dev-root", default="/opt/msys-dev")
    parser.add_argument("--state-dir", default="/opt/msys-state")
    parser.add_argument("--release-root", default="/opt/msys")
    parser.add_argument("--usb-root", default="/mnt/msys-usb")
    parser.add_argument("--log-file", default="/tmp/msysd.log")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(
            Path(args.dev_root),
            Path(args.state_dir),
            Path(args.release_root),
            Path(args.usb_root),
            log_file=Path(args.log_file),
            apply=args.apply,
            archive=not args.no_archive,
        )
    except (OSError, StorageError) as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
