from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


ENV_ROOT = "SKILL_REGISTRY_ROOT"
DEFAULT_POINTER_NAME = Path("skill-registry") / "default-root"


class RegistryRootError(Exception):
    """Raised when a registry root cannot be resolved safely."""


class _PathResolutionError(Exception):
    """Raised for path-resolution failures that use RuntimeError on older Python."""


@dataclass(frozen=True)
class ResolvedRegistryRoot:
    path: Path
    source: str


def resolve_registry_root(
    explicit_root: Path | str | None = None,
    *,
    cwd: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedRegistryRoot:
    """Resolve a valid registry root using the documented precedence."""

    environment = os.environ if environ is None else environ
    cwd_path = _resolve_cwd(cwd, environment)

    if explicit_root is not None:
        return _resolve_configured_root(
            explicit_root,
            source="explicit",
            label="Explicit registry root",
            cwd=cwd_path,
            environ=environment,
        )

    if ENV_ROOT in environment:
        return _resolve_configured_root(
            environment[ENV_ROOT],
            source="environment",
            label=ENV_ROOT,
            cwd=cwd_path,
            environ=environment,
        )

    for candidate in (cwd_path, *cwd_path.parents):
        if _has_registry_file(candidate, source_label="CWD registry candidate"):
            return ResolvedRegistryRoot(path=candidate, source="cwd")

    pointer_path = _default_pointer_path(environment, cwd_path)
    if pointer_path is not None and _default_pointer_is_present(pointer_path):
        return _resolve_pointer_root(pointer_path, cwd_path, environment)

    raise RegistryRootError(
        "Could not resolve a skill registry root. Pass --root PATH, set "
        f"{ENV_ROOT}, run the command from inside a registry checkout, or "
        "configure the default-root pointer."
    )


def default_pointer_path(
    *,
    cwd: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the machine-local default-root pointer path, when HOME is available."""

    environment = os.environ if environ is None else environ
    cwd_path = _resolve_cwd(cwd, environment)
    return _default_pointer_path(environment, cwd_path)


def normalize_registry_path(
    value: Path | str,
    *,
    cwd: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Normalize a registry-related path using the resolver's HOME and CWD rules."""

    environment = os.environ if environ is None else environ
    cwd_path = _resolve_cwd(cwd, environment)
    try:
        return _normalize_path(value, base=cwd_path, environ=environment)
    except (OSError, ValueError, _PathResolutionError) as exc:
        raise RegistryRootError(f"Could not resolve path {value!s}: {exc}") from exc


def _resolve_cwd(cwd: Path | str | None, environ: Mapping[str, str]) -> Path:
    try:
        process_cwd = Path.cwd()
    except OSError as exc:
        raise RegistryRootError(f"Could not inspect current working directory: {exc}") from exc

    candidate = process_cwd if cwd is None else cwd
    try:
        return _normalize_path(candidate, base=process_cwd, environ=environ)
    except (OSError, ValueError, _PathResolutionError) as exc:
        raise RegistryRootError(f"Could not resolve current working directory {candidate!s}: {exc}") from exc


def _resolve_configured_root(
    value: Path | str,
    *,
    source: str,
    label: str,
    cwd: Path,
    environ: Mapping[str, str],
) -> ResolvedRegistryRoot:
    if not str(value).strip():
        raise RegistryRootError(f"{label} is empty.")

    try:
        root = _normalize_path(value, base=cwd, environ=environ)
    except (OSError, ValueError, _PathResolutionError) as exc:
        raise RegistryRootError(f"Could not resolve {label.lower()} {value!s}: {exc}") from exc

    if not _has_registry_file(root, source_label=label):
        raise RegistryRootError(f"{label} does not contain a regular registry.yaml: {root}")
    return ResolvedRegistryRoot(path=root, source=source)


def _resolve_pointer_root(
    pointer_path: Path,
    cwd: Path,
    environ: Mapping[str, str],
) -> ResolvedRegistryRoot:
    try:
        content = pointer_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistryRootError(f"Could not read default registry root pointer {pointer_path}: {exc}") from exc

    if not content.strip():
        raise RegistryRootError(f"Default registry root pointer is empty: {pointer_path}")

    lines = content.splitlines()
    if len(lines) != 1:
        raise RegistryRootError(f"Default registry root pointer must contain exactly one path: {pointer_path}")

    pointer_value = lines[0].strip()
    if not pointer_value:
        raise RegistryRootError(f"Default registry root pointer is empty: {pointer_path}")

    try:
        root = _normalize_path(pointer_value, base=pointer_path.parent, environ=environ)
    except (OSError, ValueError, _PathResolutionError) as exc:
        raise RegistryRootError(f"Default registry root pointer is invalid {pointer_path}: {exc}") from exc

    if not _has_registry_file(root, source_label="Default registry root"):
        raise RegistryRootError(
            f"Default registry root does not contain a regular registry.yaml: {root} (from {pointer_path})"
        )
    return ResolvedRegistryRoot(path=root, source="default")


def _default_pointer_path(environ: Mapping[str, str], cwd: Path) -> Path | None:
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home:
        try:
            base = _normalize_path(config_home, base=cwd, environ=environ)
        except (OSError, ValueError, _PathResolutionError) as exc:
            raise RegistryRootError(f"Could not resolve XDG_CONFIG_HOME {config_home}: {exc}") from exc
        return base / DEFAULT_POINTER_NAME

    home = environ.get("HOME")
    if not home:
        return None
    try:
        home_path = _normalize_path(home, base=cwd, environ=environ)
    except (OSError, ValueError, _PathResolutionError) as exc:
        raise RegistryRootError(f"Could not resolve HOME {home}: {exc}") from exc
    return home_path / ".config" / DEFAULT_POINTER_NAME


def _normalize_path(value: Path | str, *, base: Path, environ: Mapping[str, str]) -> Path:
    path = _expand_home(str(value), environ)
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=False)
    except RuntimeError as exc:
        raise _PathResolutionError(str(exc)) from exc


def _expand_home(value: str, environ: Mapping[str, str]) -> Path:
    if value == "~" or value.startswith("~/"):
        home = environ.get("HOME")
        if not home:
            raise ValueError("cannot expand '~' because HOME is not set")
        suffix = value[2:] if value.startswith("~/") else ""
        return Path(home) / suffix
    if value.startswith("~"):
        try:
            return Path(value).expanduser()
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    return Path(value)


def _default_pointer_is_present(pointer_path: Path) -> bool:
    try:
        pointer_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RegistryRootError(f"Could not inspect default registry root pointer {pointer_path}: {exc}") from exc
    return True


def _has_registry_file(root: Path, *, source_label: str) -> bool:
    registry_path = root / "registry.yaml"
    try:
        metadata = registry_path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RegistryRootError(f"Could not inspect registry.yaml for {source_label} at {registry_path}: {exc}") from exc
    return stat.S_ISREG(metadata.st_mode)
