from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import TargetConfig
from .hashing import (
    FileInventoryItem,
    PayloadHashError,
    is_sha256,
    package_inventory_hash,
    validate_deployable_relative_path,
)


DEPLOYMENT_RECORD_SCHEMA_VERSION = 1


class DeploymentRecordError(ValueError):
    """Raised when a deployment record cannot prove registry ownership."""


@dataclass(frozen=True)
class ClassificationRecord:
    target: str
    skill_id: str
    classification: str
    reason: str
    target_path: str
    created_at: str


@dataclass(frozen=True)
class DeploymentRecord:
    schema_version: int
    target: str
    skill_id: str
    deployment_mode: str
    source_path: str
    target_path: str
    payload_hash: str
    file_count: int
    files: tuple[FileInventoryItem, ...]
    deployed_at: str
    deployed_by: str
    operation: str
    registry_commit: str = ""
    reason: str = ""


def load_classification(target: TargetConfig, skill_id: str) -> ClassificationRecord | None:
    path = classification_record_path(target, skill_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return ClassificationRecord(
        target=str(data.get("target", target.name)),
        skill_id=str(data.get("skill_id", skill_id)),
        classification=str(data.get("classification", "")),
        reason=str(data.get("reason", "")),
        target_path=str(data.get("target_path", "")),
        created_at=str(data.get("created_at", "")),
    )


def save_classification(
    target: TargetConfig,
    skill_id: str,
    *,
    classification: str,
    reason: str,
    target_path: str,
) -> ClassificationRecord:
    record = ClassificationRecord(
        target=target.name,
        skill_id=skill_id,
        classification=classification,
        reason=reason,
        target_path=target_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    path = classification_record_path(target, skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def classification_record_path(target: TargetConfig, skill_id: str) -> Path:
    return target.state_path / "classifications" / f"{skill_id}.json"


def load_deployment_record(target: TargetConfig, skill_id: str) -> DeploymentRecord | None:
    path = deployment_record_path(target, skill_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise DeploymentRecordError(f"deployment record path is not a file: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeploymentRecordError(f"deployment record is malformed JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise DeploymentRecordError(f"cannot read deployment record {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise DeploymentRecordError(f"deployment record must contain a JSON object: {path}")
    return _deployment_record_from_data(data, path=path)


def save_deployment_record(target: TargetConfig, record: DeploymentRecord) -> DeploymentRecord:
    _validate_deployment_record_shape(record, path=deployment_record_path(target, record.skill_id))
    if record.target != target.name:
        raise DeploymentRecordError(f"deployment record target mismatch: expected {target.name}, found {record.target}")
    path = deployment_record_path(target, record.skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _deployment_record_to_data(record)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return record


def deployment_record_path(target: TargetConfig, skill_id: str) -> Path:
    return target.state_path / "deployments" / f"{skill_id}.json"


def deployment_record_validation_error(
    record: DeploymentRecord,
    *,
    target: TargetConfig,
    skill_id: str,
    source_path: Path,
    target_path: Path,
) -> str:
    try:
        _validate_deployment_record_shape(record, path=deployment_record_path(target, skill_id))
    except DeploymentRecordError as exc:
        return str(exc)

    if record.target != target.name:
        return f"deployment record target mismatch: expected {target.name}, found {record.target}"
    if record.skill_id != skill_id:
        return f"deployment record skill_id mismatch: expected {skill_id}, found {record.skill_id}"

    recorded_source = Path(record.source_path).expanduser().resolve(strict=False)
    expected_source = source_path.expanduser().resolve(strict=False)
    if recorded_source != expected_source:
        return f"deployment record source_path mismatch: expected {expected_source}, found {recorded_source}"

    recorded_target = Path(record.target_path).expanduser().resolve(strict=False)
    expected_target = target_path.expanduser().resolve(strict=False)
    if recorded_target != expected_target:
        return f"deployment record target_path mismatch: expected {expected_target}, found {recorded_target}"

    if package_inventory_hash(record.files) != record.payload_hash:
        return "deployment record files do not match payload_hash"

    return ""


def _deployment_record_from_data(data: dict[str, Any], *, path: Path) -> DeploymentRecord:
    files = _deployment_record_files(data.get("files"), path=path)
    record = DeploymentRecord(
        schema_version=_required_int(data, "schema_version", path=path),
        target=_required_string(data, "target", path=path),
        skill_id=_required_string(data, "skill_id", path=path),
        deployment_mode=_required_string(data, "deployment_mode", path=path),
        source_path=_required_string(data, "source_path", path=path),
        target_path=_required_string(data, "target_path", path=path),
        payload_hash=_required_string(data, "payload_hash", path=path),
        file_count=_required_int(data, "file_count", path=path),
        files=files,
        deployed_at=_required_string(data, "deployed_at", path=path),
        deployed_by=_required_string(data, "deployed_by", path=path),
        operation=_required_string(data, "operation", path=path),
        registry_commit=str(data.get("registry_commit", "")),
        reason=str(data.get("reason", "")),
    )
    _validate_deployment_record_shape(record, path=path)
    return record


def _deployment_record_to_data(record: DeploymentRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "target": record.target,
        "skill_id": record.skill_id,
        "deployment_mode": record.deployment_mode,
        "source_path": record.source_path,
        "target_path": record.target_path,
        "payload_hash": record.payload_hash,
        "file_count": record.file_count,
        "files": [asdict(item) for item in record.files],
        "deployed_at": record.deployed_at,
        "deployed_by": record.deployed_by,
        "operation": record.operation,
        "registry_commit": record.registry_commit,
        "reason": record.reason,
    }


def _validate_deployment_record_shape(record: DeploymentRecord, *, path: Path) -> None:
    if record.schema_version != DEPLOYMENT_RECORD_SCHEMA_VERSION:
        raise DeploymentRecordError(
            f"deployment record schema_version must be {DEPLOYMENT_RECORD_SCHEMA_VERSION}: {path}"
        )
    for key in (
        "target",
        "skill_id",
        "deployment_mode",
        "source_path",
        "target_path",
        "deployed_at",
        "deployed_by",
        "operation",
    ):
        value = getattr(record, key)
        if not isinstance(value, str) or not value.strip():
            raise DeploymentRecordError(f"deployment record field must be a non-empty string: {key}")
    if not isinstance(record.registry_commit, str):
        raise DeploymentRecordError("deployment record field must be a string: registry_commit")
    if not isinstance(record.reason, str):
        raise DeploymentRecordError("deployment record field must be a string: reason")
    if record.deployment_mode not in {"symlink", "copy"}:
        raise DeploymentRecordError("deployment record deployment_mode must be symlink or copy")
    if not isinstance(record.file_count, int) or record.file_count < 0:
        raise DeploymentRecordError("deployment record file_count must be a non-negative integer")
    if record.file_count != len(record.files):
        raise DeploymentRecordError("deployment record file_count does not match files length")
    if not isinstance(record.payload_hash, str) or not is_sha256(record.payload_hash):
        raise DeploymentRecordError("deployment record payload_hash must be a 64-character sha256 hex digest")
    if not record.files:
        raise DeploymentRecordError("deployment record files must not be empty")
    if package_inventory_hash(record.files) != record.payload_hash:
        raise DeploymentRecordError("deployment record files do not match payload_hash")


def _deployment_record_files(raw_files: Any, *, path: Path) -> tuple[FileInventoryItem, ...]:
    if not isinstance(raw_files, list):
        raise DeploymentRecordError(f"deployment record files must be a list: {path}")

    files: list[FileInventoryItem] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise DeploymentRecordError(f"deployment record files[{index}] must be an object")
        relative_path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(relative_path, str) or not relative_path:
            raise DeploymentRecordError(f"deployment record files[{index}].path must be a non-empty string")
        try:
            validate_deployable_relative_path(relative_path, label=f"deployment record files[{index}].path")
        except PayloadHashError as exc:
            raise DeploymentRecordError(str(exc)) from exc
        if relative_path in seen:
            raise DeploymentRecordError(f"deployment record files contains duplicate path: {relative_path}")
        seen.add(relative_path)
        if not isinstance(size, int) or size < 0:
            raise DeploymentRecordError(f"deployment record files[{index}].size must be a non-negative integer")
        if not isinstance(sha256, str) or not is_sha256(sha256):
            raise DeploymentRecordError(
                f"deployment record files[{index}].sha256 must be a 64-character sha256 hex digest"
            )
        files.append(FileInventoryItem(path=relative_path, size=size, sha256=sha256))
    return tuple(files)


def _required_string(data: dict[str, Any], key: str, *, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeploymentRecordError(f"deployment record missing non-empty string field {key}: {path}")
    return value


def _required_int(data: dict[str, Any], key: str, *, path: Path) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise DeploymentRecordError(f"deployment record missing integer field {key}: {path}")
    return value
