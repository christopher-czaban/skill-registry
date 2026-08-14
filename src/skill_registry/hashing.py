from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path


class PayloadHashError(ValueError):
    """Raised when deployable payload content cannot be inventoried."""


@dataclass(frozen=True)
class FileInventoryItem:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PayloadHash:
    payload_hash: str
    files: tuple[FileInventoryItem, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)


_EXCLUDED_NAMES = {".DS_Store"}
_EXCLUDED_PARTS = {".git", ".skill-registry", "__pycache__"}
_EXCLUDED_PATTERNS = ("*.pyc",)
_CONTROL_FILE_PATHS = {"skill.yaml"}


def inventory_deployable_files(source_path: Path) -> tuple[FileInventoryItem, ...]:
    if not source_path.exists():
        raise PayloadHashError(f"Cannot inventory deployable payload; source path is missing: {source_path}")
    if not source_path.is_dir():
        raise PayloadHashError(f"Cannot inventory deployable payload; source path is not a directory: {source_path}")

    try:
        paths = sorted(source_path.rglob("*"), key=lambda item: item.relative_to(source_path).as_posix())
    except OSError as exc:
        raise PayloadHashError(f"Cannot scan deployable payload directory {source_path}: {exc}") from exc

    files: list[FileInventoryItem] = []
    for path in paths:
        relative_path = path.relative_to(source_path).as_posix()
        if is_excluded_path(relative_path) or is_control_file(relative_path):
            continue
        if path.is_dir():
            continue
        if path.is_symlink() and not path.exists():
            raise PayloadHashError(f"Cannot read broken symlink in deployable payload: {path}")
        if not path.is_file():
            raise PayloadHashError(f"Cannot inventory non-file deployable payload entry: {path}")

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PayloadHashError(f"Cannot read deployable payload file {path}: {exc}") from exc

        files.append(
            FileInventoryItem(
                path=relative_path,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )

    if not files:
        raise PayloadHashError(f"Cannot inventory empty deployable payload directory: {source_path}")
    return tuple(files)


def deployable_payload_hash(source_path: Path) -> PayloadHash:
    files = inventory_deployable_files(source_path)
    return PayloadHash(payload_hash=package_inventory_hash(files), files=files)


def package_inventory_hash(files: tuple[FileInventoryItem, ...]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda file_item: file_item.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def legacy_package_inventory_hash(files: tuple[FileInventoryItem, ...]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda file_item: file_item.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def inventory_control_files(source_path: Path) -> tuple[str, ...]:
    found: list[str] = []
    for relative_path in sorted(_CONTROL_FILE_PATHS):
        path = source_path / relative_path
        if path.exists() and path.is_file():
            found.append(relative_path)
    return tuple(found)


def is_control_file(relative_path: str) -> bool:
    return Path(relative_path).as_posix() in _CONTROL_FILE_PATHS


def is_excluded_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.name in _EXCLUDED_NAMES:
        return True
    if any(fnmatch.fnmatch(path.name, pattern) for pattern in _EXCLUDED_PATTERNS):
        return True
    return any(part in _EXCLUDED_PARTS for part in path.parts)


def validate_deployable_relative_path(relative_path: str, *, label: str) -> None:
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PayloadHashError(f"{label} must be a safe relative path")
    if is_excluded_path(relative_path):
        raise PayloadHashError(f"{label} is excluded by deployable payload inventory rules")
    if is_control_file(relative_path):
        raise PayloadHashError(f"{label} is registry control metadata, not deployable payload")


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)
