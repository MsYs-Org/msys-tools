"""Generate Core's development fallback manifests from canonical packages."""

from __future__ import annotations

import copy
import difflib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


CORE_FALLBACK_DIRECTORY = Path("msys-core/examples/config/manifests")
REMOTE_WORKSPACE_ROOT = PurePosixPath("/opt/msys-dev")


class FallbackManifestError(ValueError):
    """Raised when the canonical workspace inputs cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class FallbackSpec:
    repository: str
    package_id: str
    fallback_name: str
    rewrite_package_paths: bool = False

    @property
    def canonical_path(self) -> Path:
        return Path(self.repository) / "manifest.json"

    @property
    def fallback_path(self) -> Path:
        return CORE_FALLBACK_DIRECTORY / self.fallback_name


@dataclass(frozen=True, slots=True)
class GeneratedFallback:
    spec: FallbackSpec
    path: Path
    document: dict[str, Any]
    text: str


@dataclass(frozen=True, slots=True)
class FallbackDifference:
    path: Path
    reason: str
    diff: str


FALLBACK_SPECS = (
    FallbackSpec(
        repository="msys-shell-native",
        package_id="org.msys.shell.native",
        fallback_name="shell-native.json",
        rewrite_package_paths=True,
    ),
    FallbackSpec(
        repository="msys-shell-pyside",
        package_id="org.msys.shell.pyside",
        fallback_name="shell-pyside.json",
    ),
    FallbackSpec(
        repository="msys-x11-session",
        package_id="org.msys.x11.session",
        fallback_name="x11-session.json",
        rewrite_package_paths=True,
    ),
    FallbackSpec(
        repository="msys-hal",
        package_id="org.msys.hal.linux",
        fallback_name="msys-hal.json",
        rewrite_package_paths=True,
    ),
    FallbackSpec(
        repository="msys-input-touch",
        package_id="org.msys.input.touch",
        fallback_name="input-touch.json",
        rewrite_package_paths=True,
    ),
    FallbackSpec(
        repository="msys-install",
        package_id="org.msys.core.install",
        fallback_name="core-install.json",
    ),
)


def _load_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FallbackManifestError(f"cannot read {description} {path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FallbackManifestError(
            f"invalid JSON in {description} {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise FallbackManifestError(f"{description} {path} must contain a JSON object")
    return document


def _rewrite_package_value(value: str, repository: str) -> str:
    if value == "@package":
        relative = ""
    elif value.startswith("@package/"):
        relative = value.removeprefix("@package/")
    else:
        return value

    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise FallbackManifestError(
            f"unsafe @package path in {repository}/manifest.json: {value!r}"
        )
    rewritten = REMOTE_WORKSPACE_ROOT / repository
    if relative:
        rewritten /= relative_path
    return str(rewritten)


def _rewrite_x11_package_paths(
    document: dict[str, Any], spec: FallbackSpec
) -> dict[str, Any]:
    generated = copy.deepcopy(document)
    components = generated.get("components")
    if not isinstance(components, list):
        raise FallbackManifestError(
            f"canonical {spec.canonical_path} must contain a components array"
        )
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise FallbackManifestError(
                f"canonical {spec.canonical_path} component {index} must be an object"
            )
        command = component.get("exec")
        if not isinstance(command, list) or not all(
            isinstance(argument, str) for argument in command
        ):
            raise FallbackManifestError(
                f"canonical {spec.canonical_path} component {index} exec must be a string array"
            )
        component["exec"] = [
            _rewrite_package_value(argument, spec.repository) for argument in command
        ]
        if "cwd" in component:
            cwd = component["cwd"]
            if not isinstance(cwd, str):
                raise FallbackManifestError(
                    f"canonical {spec.canonical_path} component {index} cwd must be a string"
                )
            component["cwd"] = _rewrite_package_value(cwd, spec.repository)
    return generated


def _validate_canonical(document: dict[str, Any], spec: FallbackSpec) -> None:
    package = document.get("package")
    if not isinstance(package, dict) or package.get("id") != spec.package_id:
        actual = package.get("id") if isinstance(package, dict) else None
        raise FallbackManifestError(
            f"canonical {spec.canonical_path} package id must be {spec.package_id!r}, "
            f"got {actual!r}"
        )


def _render(document: dict[str, Any], *, sort_keys: bool = False) -> str:
    return json.dumps(
        document,
        indent=2,
        ensure_ascii=False,
        sort_keys=sort_keys,
    ) + "\n"


def generated_fallbacks(workspace_root: Path) -> tuple[GeneratedFallback, ...]:
    root = workspace_root.expanduser().resolve()
    generated: list[GeneratedFallback] = []
    for spec in FALLBACK_SPECS:
        canonical = _load_object(
            root / spec.canonical_path,
            description="canonical manifest",
        )
        _validate_canonical(canonical, spec)
        document = (
            _rewrite_x11_package_paths(canonical, spec)
            if spec.rewrite_package_paths
            else copy.deepcopy(canonical)
        )
        generated.append(
            GeneratedFallback(
                spec=spec,
                path=root / spec.fallback_path,
                document=document,
                text=_render(document),
            )
        )
    return tuple(generated)


def _semantic_diff(
    relative_path: Path,
    actual: dict[str, Any] | None,
    expected: dict[str, Any],
    *,
    actual_text: str | None = None,
) -> str:
    if actual is None:
        before = actual_text or ""
    else:
        before = _render(actual, sort_keys=True)
    after = _render(expected, sort_keys=True)
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{relative_path} (actual)",
            tofile=f"{relative_path} (generated)",
            lineterm="",
        )
    )


def check_fallback_manifests(workspace_root: Path) -> tuple[FallbackDifference, ...]:
    root = workspace_root.expanduser().resolve()
    differences: list[FallbackDifference] = []
    for generated in generated_fallbacks(root):
        relative_path = generated.spec.fallback_path
        try:
            actual_text = generated.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            differences.append(
                FallbackDifference(
                    path=generated.path,
                    reason="missing fallback manifest",
                    diff=_semantic_diff(relative_path, None, generated.document),
                )
            )
            continue
        except OSError as exc:
            raise FallbackManifestError(
                f"cannot read fallback manifest {generated.path}: {exc}"
            ) from exc
        try:
            actual = json.loads(actual_text)
        except json.JSONDecodeError as exc:
            differences.append(
                FallbackDifference(
                    path=generated.path,
                    reason=f"invalid JSON at {exc.lineno}:{exc.colno}: {exc.msg}",
                    diff=_semantic_diff(
                        relative_path,
                        None,
                        generated.document,
                        actual_text=actual_text,
                    ),
                )
            )
            continue
        if not isinstance(actual, dict):
            differences.append(
                FallbackDifference(
                    path=generated.path,
                    reason="fallback manifest must contain a JSON object",
                    diff=_semantic_diff(relative_path, None, generated.document),
                )
            )
            continue
        if actual != generated.document:
            differences.append(
                FallbackDifference(
                    path=generated.path,
                    reason="semantic content differs from canonical manifest",
                    diff=_semantic_diff(relative_path, actual, generated.document),
                )
            )
    return tuple(differences)


def _stage_atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        os.chmod(temporary, mode)
        return temporary
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def generate_fallback_manifests(workspace_root: Path) -> tuple[Path, ...]:
    generated = generated_fallbacks(workspace_root)
    staged: list[tuple[Path, Path]] = []
    try:
        for fallback in generated:
            staged.append((fallback.path, _stage_atomic_write(fallback.path, fallback.text)))
        for destination, temporary in staged:
            os.replace(temporary, destination)
        return tuple(fallback.path for fallback in generated)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
