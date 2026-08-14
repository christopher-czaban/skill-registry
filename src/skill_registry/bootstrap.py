from __future__ import annotations

import ctypes
import errno
import importlib.metadata
import importlib.resources
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import yaml

from .config import ConfigError, load_registry
from .root import (
    RegistryRootError,
    default_pointer_path,
    normalize_registry_path,
    resolve_registry_root,
)
from .validation import validate_registry


ENGINE_DISTRIBUTION = "skill-registry"
SUPPORTED_TARGET_PRESETS = (
    "codex",
    "claude-code",
    "agents",
    "hermes-global",
    "openclaw-global",
)


class BootstrapError(Exception):
    """Raised when an instance cannot be initialized safely."""


@dataclass(frozen=True)
class EngineDistribution:
    name: str
    version: str


@dataclass(frozen=True)
class InstanceInitializationResult:
    root: Path
    name: str
    engine_distribution: str
    engine_version: str
    targets: tuple[str, ...]
    default_set: bool
    warnings: tuple[str, ...]
    next_steps: tuple[str, ...]


@dataclass(frozen=True)
class DefaultPointerMutationResult:
    pointer: Path
    root: Path | None
    changed: bool


@dataclass(frozen=True)
class _DestinationState:
    existed: bool
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True)
class _PublishedEntry:
    path: Path
    device: int
    inode: int
    is_directory: bool
    descriptor: int | None = None


@dataclass(frozen=True)
class _Publication:
    root: Path
    root_created: bool
    root_device: int
    root_inode: int
    entries: tuple[_PublishedEntry, ...]
    root_descriptor: int | None = None


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004


_TARGET_PRESETS: dict[str, dict[str, Any]] = {
    "codex": {
        "name": "codex",
        "system": "codex",
        "root": "~/.codex/skills",
        "state_dir": "~/.codex/skill-registry-state",
        "deploy_mode": "symlink",
        "include_in_all": False,
        "supports_symlink": True,
        "runtime_profiles": ["codex-desktop"],
    },
    "claude-code": {
        "name": "claude-code",
        "system": "claude-code",
        "root": "~/.claude/skills",
        "state_dir": "~/.claude/skill-registry-state",
        "deploy_mode": "symlink",
        "include_in_all": False,
        "supports_symlink": True,
        "runtime_profiles": ["claude-code"],
    },
    "agents": {
        "name": "agents",
        "system": "agents",
        "root": "~/.agents/skills",
        "state_dir": "~/.agents/skill-registry-state",
        "deploy_mode": "symlink",
        "include_in_all": False,
        "supports_symlink": True,
        "runtime_profiles": ["agents-shared"],
    },
    "hermes-global": {
        "name": "hermes-global",
        "system": "hermes",
        "root": "~/.hermes/skills",
        "state_dir": "~/.hermes/skill-registry-state",
        "deploy_mode": "symlink",
        "include_in_all": False,
        "supports_symlink": True,
        "runtime_profiles": ["hermes"],
    },
    "openclaw-global": {
        "name": "openclaw-global",
        "system": "openclaw",
        "root": "~/.openclaw/skills",
        "state_dir": "~/.openclaw/skill-registry-state",
        "deploy_mode": "symlink",
        "include_in_all": False,
        "supports_symlink": True,
        "runtime_profiles": ["openclaw-codex-harness"],
    },
}

_RUNTIME_PRESETS: dict[str, dict[str, Any]] = {
    "codex-desktop": {
        "name": "codex-desktop",
        "kind": "local-shell",
        "description": "Codex desktop local shell runtime.",
        "visible_bin_dirs": ["~/.local/bin", "~/bin", "/opt/homebrew/bin"],
        "probe": {"strategy": "path-list"},
        "capabilities": {
            "inherits_interactive_shell_path": True,
            "honors_openclaw_exec_path_prepend": False,
        },
    },
    "claude-code": {
        "name": "claude-code",
        "kind": "local-shell",
        "description": "Claude Code local shell runtime.",
        "visible_bin_dirs": [
            "~/.local/bin",
            "~/bin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ],
        "probe": {"strategy": "path-list"},
        "capabilities": {
            "inherits_interactive_shell_path": True,
            "honors_openclaw_exec_path_prepend": False,
        },
    },
    "agents-shared": {
        "name": "agents-shared",
        "kind": "path-list",
        "description": "Generic .agents/skills consumer runtime profile.",
        "visible_bin_dirs": [],
        "probe": {"strategy": "path-list"},
        "capabilities": {
            "inherits_interactive_shell_path": False,
            "honors_openclaw_exec_path_prepend": False,
        },
    },
    "hermes": {
        "name": "hermes",
        "kind": "path-list",
        "description": "Hermes runtime profile.",
        "visible_bin_dirs": [],
        "probe": {"strategy": "path-list"},
        "capabilities": {
            "inherits_interactive_shell_path": False,
            "honors_openclaw_exec_path_prepend": False,
        },
    },
    "openclaw-codex-harness": {
        "name": "openclaw-codex-harness",
        "kind": "path-list",
        "description": "OpenClaw native Codex app-server harness for agentRuntime.id=codex.",
        "visible_bin_dirs": [
            "~/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ],
        "probe": {"strategy": "path-list"},
        "capabilities": {
            "inherits_interactive_shell_path": False,
            "honors_openclaw_exec_path_prepend": False,
        },
    },
}


def installed_engine_distribution() -> EngineDistribution:
    """Return identity from the installed package metadata only."""

    try:
        distribution = importlib.metadata.distribution(ENGINE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as exc:
        raise BootstrapError(
            f"Installed engine distribution metadata was not found for {ENGINE_DISTRIBUTION}."
        ) from exc
    except OSError as exc:
        raise BootstrapError(
            f"Could not inspect installed engine distribution metadata for {ENGINE_DISTRIBUTION}: {exc}"
        ) from exc

    name = str(distribution.metadata.get("Name", "")).strip()
    version = str(distribution.version).strip()
    if not name or not version:
        raise BootstrapError(
            f"Installed engine distribution metadata for {ENGINE_DISTRIBUTION} is missing Name or Version."
        )
    return EngineDistribution(name=name, version=version)


def initialize_registry_instance(
    path: Path | str,
    name: str | None = None,
    targets: Sequence[str] = (),
    set_default: bool = False,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    engine_identity: EngineDistribution | None = None,
) -> InstanceInitializationResult:
    """Create and validate a new user-owned registry instance without network access."""

    environment = os.environ if environ is None else environ
    destination = _normalize_destination(path, cwd=cwd, environ=environment)
    project_name = normalize_project_name(destination.name if name is None else name)
    selected_targets = _normalize_targets(targets)
    identity = installed_engine_distribution() if engine_identity is None else _validate_engine_identity(engine_identity)
    if _canonical_project_name(project_name) == _canonical_project_name(identity.name):
        raise BootstrapError(
            f"Project name {project_name!r} conflicts with the engine distribution {identity.name!r}; "
            "pass --name with a distinct instance project name."
        )
    destination_state = _inspect_destination(destination)

    parent = destination.parent
    try:
        parent_metadata = parent.stat()
    except OSError as exc:
        raise BootstrapError(f"Could not inspect destination parent {parent}: {exc}") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise BootstrapError(f"Destination parent is not a directory: {parent}")

    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.skill-registry-init-", dir=str(parent)))
    except OSError as exc:
        raise BootstrapError(f"Could not create staging directory beside {destination}: {exc}") from exc

    publication: _Publication | None = None
    warnings: tuple[str, ...] = ()
    published_valid = False
    primary_error: BootstrapError | None = None
    ownership_cleanup_error = ""
    try:
        _render_instance(staging, project_name, selected_targets, identity)
        warnings = _validated_warnings(staging, label="Staged registry instance")
        publication = _publish_staging(staging, destination, destination_state)
        final_warnings = _validated_warnings(destination, label="Published registry instance")
        _verify_publication(publication, staging)
        warnings = _dedupe((*warnings, *final_warnings))
        published_valid = True
    except BootstrapError as exc:
        primary_error = exc
        if publication is not None and not published_valid:
            rollback_error = _rollback_publication(publication)
            publication = None
            if rollback_error:
                primary_error = BootstrapError(f"{exc}; publication rollback also failed: {rollback_error}")

    if publication is not None and published_valid:
        ownership_cleanup_error = _close_publication_ownership_descriptors(publication)
        publication = None

    cleanup_error = _remove_staging(staging)
    if primary_error is not None:
        if cleanup_error:
            primary_error = BootstrapError(f"{primary_error}; staging cleanup also failed: {cleanup_error}")
        raise primary_error

    if ownership_cleanup_error:
        message = (
            f"Registry instance was created and validated at {destination}, but publication ownership handle "
            f"cleanup failed: {ownership_cleanup_error}"
        )
        if cleanup_error:
            message = f"{message}; staging cleanup also failed: {cleanup_error}"
        raise BootstrapError(message)

    if cleanup_error:
        raise BootstrapError(
            f"Registry instance was created and validated at {destination}, but staging cleanup failed for "
            f"{staging}: {cleanup_error}"
        )

    if set_default:
        try:
            set_default_registry_root(destination, environ=environment, cwd=cwd)
        except RegistryRootError as exc:
            raise BootstrapError(
                f"Registry instance was created and validated at {destination}, but the default-root pointer "
                f"could not be set: {exc}. Retry with: skill-registry root set-default {destination}"
            ) from exc

    resolved_destination = destination.resolve(strict=False)
    return InstanceInitializationResult(
        root=resolved_destination,
        name=project_name,
        engine_distribution=identity.name,
        engine_version=identity.version,
        targets=selected_targets,
        default_set=set_default,
        warnings=warnings,
        next_steps=(
            f"cd {shlex.quote(str(resolved_destination))}",
            "uv sync",
            "uv run skill-registry validate",
            "git init",
            "git add .",
            'git commit -m "Initialize skill registry instance"',
        ),
    )


def normalize_project_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not normalized:
        raise BootstrapError(f"Project name {value!r} is empty after normalization.")
    return normalized


def _canonical_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def set_default_registry_root(
    path: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> DefaultPointerMutationResult:
    environment = os.environ if environ is None else environ
    resolved = resolve_registry_root(path, cwd=cwd, environ=environment)
    pointer = default_pointer_path(cwd=cwd, environ=environment)
    if pointer is None:
        raise RegistryRootError("Could not determine default registry root pointer because HOME is not set.")

    content = f"{resolved.path}\n"
    existing = _read_existing_pointer(pointer)
    if existing == content:
        return DefaultPointerMutationResult(pointer=pointer, root=resolved.path, changed=False)

    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryRootError(f"Could not create default registry root pointer directory {pointer.parent}: {exc}") from exc

    temporary_path: Path | None = None
    write_error: OSError | None = None
    cleanup_error: OSError | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{pointer.name}.", dir=str(pointer.parent))
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, pointer)
        temporary_path = None
    except OSError as exc:
        write_error = exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = exc

    if write_error is not None:
        message = f"Could not write default registry root pointer {pointer}: {write_error}"
        if cleanup_error is not None:
            message = f"{message}; temporary pointer cleanup also failed: {cleanup_error}"
        raise RegistryRootError(message) from write_error
    if cleanup_error is not None:
        raise RegistryRootError(
            f"Could not clean temporary default registry root pointer beside {pointer}: {cleanup_error}"
        ) from cleanup_error

    return DefaultPointerMutationResult(pointer=pointer, root=resolved.path, changed=True)


def clear_default_registry_root(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> DefaultPointerMutationResult:
    environment = os.environ if environ is None else environ
    pointer = default_pointer_path(cwd=cwd, environ=environment)
    if pointer is None:
        raise RegistryRootError("Could not determine default registry root pointer because HOME is not set.")

    try:
        metadata = pointer.lstat()
    except FileNotFoundError:
        return DefaultPointerMutationResult(pointer=pointer, root=None, changed=False)
    except OSError as exc:
        raise RegistryRootError(f"Could not inspect default registry root pointer {pointer}: {exc}") from exc

    if stat.S_ISDIR(metadata.st_mode):
        raise RegistryRootError(f"Default registry root pointer is a directory and cannot be cleared: {pointer}")
    try:
        pointer.unlink()
    except OSError as exc:
        raise RegistryRootError(f"Could not clear default registry root pointer {pointer}: {exc}") from exc
    return DefaultPointerMutationResult(pointer=pointer, root=None, changed=True)


def _validate_engine_identity(identity: EngineDistribution) -> EngineDistribution:
    name = identity.name.strip()
    version = identity.version.strip()
    if not name or not version:
        raise BootstrapError("Engine distribution identity requires non-empty name and version values.")
    return EngineDistribution(name=name, version=version)


def _normalize_destination(
    value: Path | str,
    *,
    cwd: Path | str | None,
    environ: Mapping[str, str],
) -> Path:
    raw = str(value)
    if not raw.strip():
        raise BootstrapError("Registry instance path is empty.")
    if raw == "~" or raw.startswith("~/"):
        home = environ.get("HOME")
        if not home:
            raise BootstrapError("Could not expand registry instance path because HOME is not set.")
        suffix = raw[2:] if raw.startswith("~/") else ""
        candidate = Path(home) / suffix
    elif raw.startswith("~"):
        try:
            candidate = Path(raw).expanduser()
        except RuntimeError as exc:
            raise BootstrapError(f"Could not expand registry instance path {raw}: {exc}") from exc
    else:
        candidate = Path(raw)

    try:
        base = normalize_registry_path(".", cwd=cwd, environ=environ)
    except RegistryRootError as exc:
        raise BootstrapError(str(exc)) from exc
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(os.path.normpath(str(candidate))))


def _normalize_targets(targets: Sequence[str]) -> tuple[str, ...]:
    unknown = sorted({target for target in targets if target not in _TARGET_PRESETS})
    if unknown:
        supported = ", ".join(SUPPORTED_TARGET_PRESETS)
        raise BootstrapError(f"Unknown target preset(s): {', '.join(unknown)}. Supported presets: {supported}.")
    return _dedupe(tuple(targets))


def _inspect_destination(destination: Path) -> _DestinationState:
    try:
        path_metadata = destination.lstat()
    except FileNotFoundError:
        return _DestinationState(existed=False)
    except OSError as exc:
        raise BootstrapError(f"Could not inspect registry instance destination {destination}: {exc}") from exc

    if stat.S_ISLNK(path_metadata.st_mode):
        raise BootstrapError(f"Registry instance destination must not be a symbolic link: {destination}")
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise BootstrapError(f"Registry instance destination is not a directory: {destination}")

    descriptor: int | None = None
    try:
        descriptor = os.open(destination, _directory_open_flags())
        metadata = os.fstat(descriptor)
        current_path_metadata = destination.lstat()
        if (
            metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
            or current_path_metadata.st_dev != metadata.st_dev
            or current_path_metadata.st_ino != metadata.st_ino
        ):
            raise BootstrapError(f"Registry instance destination changed during inspection: {destination}")
        entries = os.listdir(descriptor)
    except OSError as exc:
        raise BootstrapError(f"Could not inspect registry instance destination {destination}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if entries:
        raise BootstrapError(f"Registry instance destination is not empty: {destination}")
    return _DestinationState(existed=True, device=metadata.st_dev, inode=metadata.st_ino)


def _render_instance(
    staging: Path,
    project_name: str,
    targets: tuple[str, ...],
    identity: EngineDistribution,
) -> None:
    try:
        for directory in ("skills", "targets", "proposals"):
            (staging / directory).mkdir()

        for template_name in (
            "registry.yaml",
            "skill-sets.yaml",
            "runtime-dependencies.yaml",
            ".gitignore",
        ):
            (staging / template_name).write_text(_read_template(template_name), encoding="utf-8")

        pyproject = _read_template("pyproject.toml").format(
            project_name=project_name,
            engine_distribution=identity.name,
            engine_version=identity.version,
        )
        (staging / "pyproject.toml").write_text(pyproject, encoding="utf-8")

        runtime_template = yaml.safe_load(_read_template("runtimes.yaml")) or {}
        runtime_names = _runtime_names_for_targets(targets)
        runtime_template["runtime_profiles"] = [_RUNTIME_PRESETS[name] for name in runtime_names]
        (staging / "runtimes.yaml").write_text(
            yaml.safe_dump(runtime_template, sort_keys=False),
            encoding="utf-8",
        )

        for target_name in targets:
            target_document = {"targets": [_TARGET_PRESETS[target_name]]}
            (staging / "targets" / f"{target_name}.yaml").write_text(
                yaml.safe_dump(target_document, sort_keys=False),
                encoding="utf-8",
            )

        (staging / "skills" / ".gitkeep").write_text("", encoding="utf-8")
        (staging / "proposals" / ".gitkeep").write_text("", encoding="utf-8")
        if not targets:
            (staging / "targets" / ".gitkeep").write_text("", encoding="utf-8")
    except (OSError, UnicodeError, yaml.YAMLError, KeyError) as exc:
        raise BootstrapError(f"Could not render staged registry instance at {staging}: {exc}") from exc


def _read_template(name: str) -> str:
    resource = importlib.resources.files("skill_registry").joinpath("templates", "registry-instance", name)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"Could not read packaged registry instance template {name}: {exc}") from exc


def _runtime_names_for_targets(targets: tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for target_name in targets:
        for runtime_name in _TARGET_PRESETS[target_name]["runtime_profiles"]:
            if runtime_name not in names:
                names.append(runtime_name)
    return tuple(names)


def _validated_warnings(root: Path, *, label: str) -> tuple[str, ...]:
    try:
        config = load_registry(root)
        result = validate_registry(config)
    except (ConfigError, OSError, UnicodeError) as exc:
        raise BootstrapError(f"{label} could not be loaded or validated at {root}: {exc}") from exc
    if result.errors:
        detail = "; ".join(f"{item.code}: {item.message}" for item in result.errors)
        raise BootstrapError(f"{label} failed validation at {root}: {detail}")
    return tuple(f"{item.code}: {item.message}" for item in result.warnings)


def _publish_staging(staging: Path, destination: Path, state: _DestinationState) -> _Publication:
    entries: list[_PublishedEntry] = []
    root_entries: list[_PublishedEntry] = []
    existing_root_descriptor: int | None = None
    publication_directories = staging / ".publication-directories"
    try:
        publication_directories.mkdir(mode=0o700)
        if state.existed:
            current, current_entries, existing_root_descriptor = _opened_directory_state(destination)
            if not stat.S_ISDIR(current.st_mode):
                raise BootstrapError(f"Registry instance destination changed before publication: {destination}")
            if current.st_dev != state.device or current.st_ino != state.inode:
                raise BootstrapError(f"Registry instance destination changed before publication: {destination}")
            if current_entries:
                raise BootstrapError(f"Registry instance destination is no longer empty: {destination}")
        else:
            try:
                root_source = Path(tempfile.mkdtemp(prefix="root-", dir=str(publication_directories)))
                _publish_directory(root_source, destination, root_entries)
            except FileExistsError as exc:
                raise BootstrapError(f"Registry instance destination appeared during publication: {destination}") from exc

        for source_directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()),
            key=lambda item: (len(item.relative_to(staging).parts), str(item.relative_to(staging))),
        ):
            if source_directory == publication_directories or publication_directories in source_directory.parents:
                continue
            published_directory = destination / source_directory.relative_to(staging)
            directory_source = Path(tempfile.mkdtemp(prefix="entry-", dir=str(publication_directories)))
            _publish_directory(directory_source, published_directory, entries)

        for source_file in sorted(item for item in staging.rglob("*") if item.is_file()):
            published_file = destination / source_file.relative_to(staging)
            _publish_file(source_file, published_file, destination, entries)

        publication_directories.rmdir()
    except BootstrapError as exc:
        publication = _publication_from_partial(
            destination,
            state,
            root_entries,
            entries,
            existing_root_descriptor,
        )
        rollback_error = _rollback_publication(publication)
        if rollback_error:
            raise BootstrapError(f"{exc}; publication rollback also failed: {rollback_error}") from exc
        raise
    except OSError as exc:
        publication = _publication_from_partial(
            destination,
            state,
            root_entries,
            entries,
            existing_root_descriptor,
        )
        rollback_error = _rollback_publication(publication)
        message = f"Could not publish registry instance to {destination}: {exc}"
        if rollback_error:
            message = f"{message}; rollback also failed: {rollback_error}"
        raise BootstrapError(message) from exc

    return _publication_from_partial(
        destination,
        state,
        root_entries,
        entries,
        existing_root_descriptor,
    )


def _copy_file(source: BinaryIO, target: BinaryIO) -> None:
    shutil.copyfileobj(source, target)
    target.flush()
    os.fsync(target.fileno())


def _publish_file(
    source_path: Path,
    published_path: Path,
    destination_root: Path,
    ownership_ledger: list[_PublishedEntry],
) -> None:
    try:
        quarantine = Path(
            tempfile.mkdtemp(
                prefix=f".{destination_root.name}.skill-registry-file-acquisition-",
                dir=str(destination_root.parent),
            )
        )
    except OSError as exc:
        raise BootstrapError(
            f"Could not create same-filesystem file-acquisition quarantine before publishing "
            f"{published_path}: {exc}"
        ) from exc

    target: BinaryIO | None = None
    primary_error: BootstrapError | OSError | None = None
    close_error: OSError | None = None
    try:
        with source_path.open("rb") as source:
            try:
                target = published_path.open("xb")
                entry = _capture_open_file_entry(published_path, target, quarantine / "entry")
                ownership_ledger.append(entry)
                _copy_file(source, target)
            except (BootstrapError, OSError) as exc:
                primary_error = exc
            finally:
                if target is not None:
                    try:
                        _close_open_publication_file(target, published_path)
                    except OSError as exc:
                        close_error = exc
    except OSError as exc:
        if primary_error is None:
            primary_error = exc

    quarantine_error = _remove_empty_file_acquisition_quarantine(quarantine)
    if primary_error is not None and close_error is None and not quarantine_error:
        raise primary_error

    errors: list[str] = []
    if primary_error is not None:
        errors.append(str(primary_error))
    if close_error is not None:
        errors.append(f"Could not close exclusive publication handle for {published_path}: {close_error}")
    if quarantine_error:
        errors.append(quarantine_error)
    if errors:
        raise BootstrapError("; ".join(errors)) from (primary_error or close_error)


def _capture_open_file_entry(
    path: Path,
    target: BinaryIO,
    quarantined_path: Path,
) -> _PublishedEntry:
    try:
        metadata = os.fstat(target.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapError(f"Exclusive publication handle is not a regular file: {path}")
        descriptor = _retain_ownership_descriptor(target.fileno())
    except (BootstrapError, OSError) as exc:
        recovery_error = _rollback_unledgered_open_file(path, target, quarantined_path)
        message = f"Could not acquire publication ownership handle for {path}: {exc}"
        if recovery_error:
            message = f"{message}; acquisition rollback also failed: {recovery_error}"
        raise BootstrapError(message) from exc

    return _PublishedEntry(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        is_directory=False,
        descriptor=descriptor,
    )


def _rollback_unledgered_open_file(
    path: Path,
    target: BinaryIO,
    quarantined_path: Path,
) -> str:
    try:
        _quarantine_public_path(path, quarantined_path)
    except FileNotFoundError:
        return ""
    except (BootstrapError, OSError) as exc:
        return f"could not atomically quarantine unledgered publication file {path}: {exc}"

    try:
        quarantined_metadata = quarantined_path.lstat()
    except OSError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, path, None)
        message = f"could not inspect quarantined unledgered publication file for {path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        return message

    try:
        owned_metadata = os.fstat(target.fileno())
    except OSError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, path, quarantined_metadata)
        message = f"could not prove ownership from the open publication handle for {path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; entry restored without overwrite"
        return message

    if (
        not stat.S_ISREG(owned_metadata.st_mode)
        or quarantined_metadata.st_dev != owned_metadata.st_dev
        or quarantined_metadata.st_ino != owned_metadata.st_ino
    ):
        restoration_error = _restore_quarantined_path(quarantined_path, path, quarantined_metadata)
        message = f"refused to remove changed unledgered publication file {path}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; foreign object restored without overwrite"
        return message

    try:
        quarantined_path.unlink()
    except OSError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, path, quarantined_metadata)
        message = f"could not remove quarantined owned publication file for {path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; entry restored without overwrite"
        return message
    return ""


def _close_open_publication_file(target: BinaryIO, _path: Path) -> None:
    target.close()


def _remove_empty_file_acquisition_quarantine(quarantine: Path) -> str:
    try:
        quarantine.rmdir()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"file-acquisition quarantine retained at {quarantine}: {exc}"
    return ""


def _retain_ownership_descriptor(descriptor: int) -> int:
    return os.dup(descriptor)


def _publish_directory(
    source: Path,
    destination: Path,
    ownership_ledger: list[_PublishedEntry],
) -> _PublishedEntry:
    descriptor: int | None = None
    retained_descriptor: int | None = None
    try:
        descriptor = os.open(source, _directory_open_flags())
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapError(f"Staged publication entry is not a directory: {source}")
        retained_descriptor = _retain_ownership_descriptor(descriptor)
        _rename_noreplace(source, destination)
        entry = _PublishedEntry(
            path=destination,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            is_directory=True,
            descriptor=retained_descriptor,
        )
        ownership_ledger.append(entry)
        retained_descriptor = None
        published_metadata = _stat_published_path(destination)
        if published_metadata.st_dev != metadata.st_dev or published_metadata.st_ino != metadata.st_ino:
            raise BootstrapError(f"Registry publication directory changed during publication: {destination}")
        return entry
    finally:
        try:
            if retained_descriptor is not None:
                os.close(retained_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _stat_published_path(path: Path) -> os.stat_result:
    return path.lstat()


def _opened_directory_state(path: Path) -> tuple[os.stat_result, tuple[str, ...], int]:
    descriptor: int | None = None
    retained_descriptor: int | None = None
    try:
        descriptor = os.open(path, _directory_open_flags())
        metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
        if path_metadata.st_dev != metadata.st_dev or path_metadata.st_ino != metadata.st_ino:
            raise BootstrapError(f"Registry instance destination changed before publication: {path}")
        entries = tuple(os.listdir(descriptor))
        retained_descriptor = _retain_ownership_descriptor(descriptor)
        result = (metadata, entries, retained_descriptor)
        retained_descriptor = None
        return result
    finally:
        try:
            if retained_descriptor is not None:
                os.close(retained_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _rename_noreplace(source: Path, destination: Path) -> None:
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)

    if sys.platform == "darwin":
        platform_name = "Darwin"
        symbol = "renamex_np"
        arguments = (source_bytes, destination_bytes, _RENAME_EXCL)
        argument_types = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith("linux"):
        platform_name = "Linux"
        symbol = "renameat2"
        arguments = (_AT_FDCWD, source_bytes, _AT_FDCWD, destination_bytes, _RENAME_NOREPLACE)
        argument_types = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    else:
        raise BootstrapError(
            f"Atomic no-replace rename is unsupported on platform {sys.platform}."
        )

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise BootstrapError(
            f"Could not load native atomic no-replace rename support on {platform_name}: {exc}"
        ) from exc
    try:
        rename = getattr(libc, symbol)
    except (AttributeError, OSError) as exc:
        raise BootstrapError(
            f"Native atomic no-replace rename symbol {symbol} is unavailable on {platform_name}."
        ) from exc
    try:
        rename.argtypes = argument_types
        rename.restype = ctypes.c_int
    except (AttributeError, TypeError) as exc:
        raise BootstrapError(
            f"Could not configure native atomic no-replace rename symbol {symbol} on {platform_name}: {exc}"
        ) from exc
    try:
        result = rename(*arguments)
    except (OSError, ctypes.ArgumentError) as exc:
        raise BootstrapError(
            f"Native atomic no-replace rename call {symbol} failed on {platform_name}: {exc}"
        ) from exc

    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), str(destination))
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _publication_from_partial(
    destination: Path,
    state: _DestinationState,
    root_entries: list[_PublishedEntry],
    entries: list[_PublishedEntry],
    existing_root_descriptor: int | None,
) -> _Publication:
    root_entry = root_entries[0] if root_entries else None
    return _Publication(
        root=destination,
        root_created=root_entry is not None,
        root_device=(
            root_entry.device
            if root_entry is not None
            else state.device if state.device is not None else -1
        ),
        root_inode=(
            root_entry.inode
            if root_entry is not None
            else state.inode if state.inode is not None else -1
        ),
        entries=tuple(entries),
        root_descriptor=(
            root_entry.descriptor
            if root_entry is not None
            else existing_root_descriptor
        ),
    )


def _verify_publication(publication: _Publication, staging: Path) -> None:
    try:
        root_owned = _retained_ownership_metadata(
            _PublishedEntry(
                path=publication.root,
                device=publication.root_device,
                inode=publication.root_inode,
                is_directory=True,
                descriptor=publication.root_descriptor,
            )
        )
        root_metadata = publication.root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_dev != root_owned.st_dev
            or root_metadata.st_ino != root_owned.st_ino
        ):
            raise BootstrapError(f"Registry instance destination changed during publication: {publication.root}")

        expected_paths = {item.relative_to(staging) for item in staging.rglob("*")}
        actual_paths = {item.relative_to(publication.root) for item in publication.root.rglob("*")}
        if actual_paths != expected_paths:
            raise BootstrapError(
                f"Registry instance destination changed during publication: {publication.root}"
            )

        entries_by_path = {entry.path: entry for entry in publication.entries}
        for relative_path in sorted(expected_paths):
            source = staging / relative_path
            target = publication.root / relative_path
            entry = entries_by_path[target]
            owned = _retained_ownership_metadata(entry)
            metadata = target.lstat()
            if metadata.st_dev != owned.st_dev or metadata.st_ino != owned.st_ino:
                raise BootstrapError(f"Registry publication entry changed during publication: {target}")
            if source.is_file() and target.read_bytes() != source.read_bytes():
                raise BootstrapError(f"Registry publication entry changed during publication: {target}")
    except BootstrapError:
        raise
    except (OSError, KeyError) as exc:
        raise BootstrapError(f"Could not verify published registry instance at {publication.root}: {exc}") from exc


def _rollback_publication(publication: _Publication) -> str:
    owned_entries = list(reversed(publication.entries))
    if publication.root_created:
        owned_entries.append(
            _PublishedEntry(
                path=publication.root,
                device=publication.root_device,
                inode=publication.root_inode,
                is_directory=True,
                descriptor=publication.root_descriptor,
            )
        )
    errors: list[str] = []
    try:
        if owned_entries:
            try:
                quarantine = Path(
                    tempfile.mkdtemp(
                        prefix=f".{publication.root.name}.skill-registry-rollback-",
                        dir=str(publication.root.parent),
                    )
                )
            except OSError as exc:
                errors.append(
                    f"could not create same-filesystem rollback quarantine beside {publication.root}: {exc}"
                )
            else:
                for index, entry in enumerate(owned_entries):
                    quarantined_path = quarantine / f"entry-{index:04d}"
                    _rollback_entry(entry, quarantined_path, errors)

                try:
                    quarantine.rmdir()
                except OSError as exc:
                    errors.append(f"rollback quarantine retained at {quarantine}: {exc}")
    finally:
        descriptor_cleanup_error = _close_publication_ownership_descriptors(publication)
        if descriptor_cleanup_error:
            errors.append(descriptor_cleanup_error)
    return "; ".join(errors)


def _rollback_entry(entry: _PublishedEntry, quarantined_path: Path, errors: list[str]) -> None:
    try:
        _quarantine_public_path(entry.path, quarantined_path)
    except FileNotFoundError:
        return
    except (BootstrapError, OSError) as exc:
        errors.append(f"could not atomically quarantine {entry.path}: {exc}")
        return

    try:
        metadata = quarantined_path.lstat()
    except OSError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, entry.path, None)
        message = f"could not inspect quarantined publication entry for {entry.path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        errors.append(message)
        return

    try:
        owned = _retained_ownership_metadata(entry)
    except BootstrapError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, entry.path, metadata)
        message = f"could not prove ownership of quarantined publication entry for {entry.path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; entry restored without overwrite"
        errors.append(message)
        return

    if metadata.st_dev != owned.st_dev or metadata.st_ino != owned.st_ino:
        restoration_error = _restore_quarantined_path(quarantined_path, entry.path, metadata)
        message = f"refused to remove changed publication entry {entry.path}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; foreign object restored without overwrite"
        errors.append(message)
        return

    try:
        if entry.is_directory:
            quarantined_path.rmdir()
        else:
            quarantined_path.unlink()
    except OSError as exc:
        restoration_error = _restore_quarantined_path(quarantined_path, entry.path, metadata)
        message = f"could not remove quarantined owned publication entry for {entry.path}: {exc}"
        if restoration_error:
            message = f"{message}; {restoration_error}"
        else:
            message = f"{message}; entry restored without overwrite"
        errors.append(message)


def _retained_ownership_metadata(entry: _PublishedEntry) -> os.stat_result:
    if entry.descriptor is None:
        raise BootstrapError(f"No retained ownership handle is available for {entry.path}.")
    try:
        metadata = os.fstat(entry.descriptor)
    except OSError as exc:
        raise BootstrapError(f"Could not inspect retained ownership handle for {entry.path}: {exc}") from exc

    expected_type = stat.S_ISDIR if entry.is_directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        kind = "directory" if entry.is_directory else "regular file"
        raise BootstrapError(f"Retained ownership handle for {entry.path} is not a {kind}.")
    if metadata.st_dev != entry.device or metadata.st_ino != entry.inode:
        raise BootstrapError(f"Retained ownership handle identity changed for {entry.path}.")
    return metadata


def _close_publication_ownership_descriptors(publication: _Publication) -> str:
    descriptors = {
        descriptor
        for descriptor in (
            publication.root_descriptor,
            *(entry.descriptor for entry in publication.entries),
        )
        if descriptor is not None
    }
    errors: list[str] = []
    for descriptor in sorted(descriptors):
        try:
            os.close(descriptor)
        except OSError as exc:
            errors.append(f"could not close publication ownership handle {descriptor}: {exc}")
    return "; ".join(errors)


def _quarantine_public_path(source: Path, destination: Path) -> None:
    _rename_noreplace(source, destination)


def _restore_quarantined_path(
    quarantined_path: Path,
    public_path: Path,
    expected: os.stat_result | None,
) -> str:
    try:
        _rename_noreplace(quarantined_path, public_path)
    except (BootstrapError, OSError) as exc:
        return f"could not restore {public_path} without overwrite; preserved at {quarantined_path}: {exc}"

    if expected is None:
        return f"restored {public_path}, but its identity could not be proven after quarantine inspection failed"

    try:
        restored = public_path.lstat()
    except OSError as exc:
        return f"restored {public_path}, but could not verify its identity: {exc}"
    if restored.st_dev != expected.st_dev or restored.st_ino != expected.st_ino:
        return f"restored {public_path}, but its identity changed before verification"
    return ""


def _remove_staging(staging: Path) -> str:
    try:
        shutil.rmtree(staging)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return str(exc)
    return ""


def _read_existing_pointer(pointer: Path) -> str | None:
    try:
        metadata = pointer.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryRootError(f"Could not inspect default registry root pointer {pointer}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RegistryRootError(f"Default registry root pointer is not a regular file: {pointer}")
    try:
        return pointer.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistryRootError(f"Could not read default registry root pointer {pointer}: {exc}") from exc


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)
