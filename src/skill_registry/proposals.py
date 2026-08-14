from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from .config import RegistryConfig, TargetConfig
from .hashing import (
    FileInventoryItem,
    PayloadHashError,
    inventory_control_files,
    inventory_deployable_files,
    is_control_file as _is_control_file,
    is_excluded_path as _is_excluded,
    is_sha256 as _is_sha256,
    legacy_package_inventory_hash,
    validate_deployable_relative_path,
)
from .skills import SkillPackage


class ProposalError(ValueError):
    """Raised when a proposal cannot be built safely."""


@dataclass(frozen=True)
class ImportProposal:
    id: str
    path: Path
    source_path: Path
    canonical_path: Path
    package_hash: str
    files: tuple[FileInventoryItem, ...]


@dataclass(frozen=True)
class SourceVerification:
    checked: bool
    ok: bool
    source_path: str
    package_hash: str = ""
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalInspection:
    id: str
    type: str
    status: str
    skill_id: str
    source_target: str
    source_path: str
    canonical_path: str
    proposal_path: str
    target_mutation: bool
    mutates_target_files: bool
    package_hash: str
    file_count: int
    files: tuple[FileInventoryItem, ...]
    verification: Optional[SourceVerification] = None


@dataclass(frozen=True)
class ProposalApplyResult:
    proposal_id: str
    skill_id: str
    source_target: str
    source_path: str
    canonical_path: str
    approved_by: str
    reason: str
    applied_at: str
    package_hash: str
    target_mutation: bool
    mutates_target_files: bool
    backup_path: str
    apply_record_path: str
    copied_files: tuple[str, ...]
    skipped_control_files: tuple[str, ...]


@dataclass(frozen=True)
class _LoadedImportProposal:
    metadata: dict[str, Any]
    proposal_dir: Path
    proposal_file: Path
    skill: SkillPackage
    source_target: TargetConfig
    source_path: Path
    canonical_path: Path
    files: tuple[FileInventoryItem, ...]


_APPLY_RECORD_NAME = "applied.yaml"
_BACKUP_DIR_NAME = "canonical-before"


def build_import_proposal(
    config: RegistryConfig,
    *,
    target: TargetConfig,
    skill: SkillPackage,
    reason: str,
    source_path: Path | None = None,
) -> ImportProposal:
    source_path = source_path or target.root_path / skill.id
    canonical_path = skill.path
    files = inventory_files(source_path)
    package_hash = legacy_package_inventory_hash(files)
    created_at = datetime.now(timezone.utc)
    proposal_id = f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{skill.id}-import"
    proposal_dir = config.root / "proposals" / proposal_id

    if proposal_dir.exists():
        raise ProposalError(f"Proposal directory already exists: {proposal_dir}")

    metadata = _proposal_metadata(
        proposal_id=proposal_id,
        target=target,
        skill=skill,
        source_path=source_path,
        canonical_path=canonical_path,
        created_at=created_at,
        reason=reason,
        files=files,
        package_hash=package_hash,
    )
    readme = _proposal_readme(metadata)
    diff_text = _proposal_diff(source_path, canonical_path, target.name, files)

    created_dir = False
    try:
        proposal_dir.mkdir(parents=True, exist_ok=False)
        created_dir = True
        _write_yaml_file(proposal_dir / "proposal.yaml", metadata)
        (proposal_dir / "README.md").write_text(readme, encoding="utf-8")
        (proposal_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    except Exception:
        if created_dir and proposal_dir.exists():
            shutil.rmtree(proposal_dir)
        raise

    return ImportProposal(
        id=proposal_id,
        path=proposal_dir,
        source_path=source_path,
        canonical_path=canonical_path,
        package_hash=package_hash,
        files=files,
    )


def inspect_import_proposal(
    config: RegistryConfig,
    proposal_path: Union[Path, str],
    *,
    verify_source: bool = False,
) -> ProposalInspection:
    loaded = _load_import_proposal(config, proposal_path, require_proposed=False)
    verification = _verify_loaded_source(loaded) if verify_source else None
    return _inspection_from_loaded(loaded, verification)


def apply_import_proposal(
    config: RegistryConfig,
    proposal_path: Union[Path, str],
    *,
    approved_by: str,
    reason: str,
) -> ProposalApplyResult:
    approved_by = approved_by.strip()
    reason = reason.strip()
    if not approved_by:
        raise ProposalError("approved-by is required")
    if not reason:
        raise ProposalError("reason is required")

    loaded = _load_import_proposal(config, proposal_path, require_proposed=True)
    verification = _verify_loaded_source(loaded)
    if not verification.ok:
        raise ProposalError("source package has drifted since proposal creation: " + "; ".join(verification.errors))

    if not _canonical_is_pending_import(loaded.canonical_path):
        raise ProposalError(f"canonical package is not placeholder or pending import: {loaded.canonical_path}")

    backup_path = loaded.proposal_dir / _BACKUP_DIR_NAME
    apply_record_path = loaded.proposal_dir / _APPLY_RECORD_NAME
    if backup_path.exists():
        raise ProposalError(f"backup directory already exists: {backup_path}")
    if apply_record_path.exists():
        raise ProposalError(f"apply record already exists: {apply_record_path}")

    if not loaded.canonical_path.is_dir():
        raise ProposalError(f"canonical path is not a directory: {loaded.canonical_path}")

    applied_at = datetime.now(timezone.utc).isoformat()
    copied_files: tuple[str, ...] = ()
    skipped_control_files: tuple[str, ...] = ()
    apply_record: dict[str, Any] = {}
    record_written = False

    try:
        shutil.copytree(loaded.canonical_path, backup_path, symlinks=True)
        copied_files, skipped_control_files = _replace_canonical_package(
            loaded,
            approved_by=approved_by,
            applied_at=applied_at,
        )
        apply_record = _apply_record(
            loaded,
            approved_by=approved_by,
            reason=reason,
            applied_at=applied_at,
            copied_files=copied_files,
            skipped_control_files=skipped_control_files,
        )
        _write_yaml_file(apply_record_path, apply_record)
        record_written = True
        _mark_proposal_applied(
            loaded,
            approved_by=approved_by,
            reason=reason,
            applied_at=applied_at,
        )
    except Exception as exc:
        try:
            if record_written and apply_record_path.exists():
                apply_record_path.unlink()
            if backup_path.exists():
                _restore_canonical_backup(loaded.canonical_path, backup_path)
        except Exception as restore_exc:
            raise ProposalError(
                f"apply failed; manual recovery may be required from {backup_path}: {restore_exc}"
            ) from exc
        if isinstance(exc, ProposalError):
            raise
        raise ProposalError(f"apply failed and canonical package was restored from {backup_path}: {exc}") from exc

    return ProposalApplyResult(
        proposal_id=str(loaded.metadata["id"]),
        skill_id=str(loaded.metadata["skill_id"]),
        source_target=str(loaded.metadata["source_target"]),
        source_path=str(loaded.source_path),
        canonical_path=str(loaded.canonical_path),
        approved_by=approved_by,
        reason=reason,
        applied_at=applied_at,
        package_hash=str(loaded.metadata["package_hash"]),
        target_mutation=False,
        mutates_target_files=False,
        backup_path=str(backup_path),
        apply_record_path=str(apply_record_path),
        copied_files=copied_files,
        skipped_control_files=skipped_control_files,
    )


def inventory_files(source_path: Path) -> tuple[FileInventoryItem, ...]:
    try:
        return inventory_deployable_files(source_path)
    except PayloadHashError as exc:
        raise ProposalError(str(exc)) from exc


def _load_import_proposal(
    config: RegistryConfig,
    proposal_path: Union[Path, str],
    *,
    require_proposed: bool,
) -> _LoadedImportProposal:
    proposal_dir, proposal_file = _resolve_proposal_paths(config, proposal_path)
    if not proposal_file.exists():
        raise ProposalError(f"proposal file is missing: {proposal_file}")
    if not proposal_file.is_file():
        raise ProposalError(f"proposal path is not a file: {proposal_file}")

    try:
        metadata = yaml.safe_load(proposal_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProposalError(f"invalid YAML in proposal file {proposal_file}: {exc}") from exc
    except OSError as exc:
        raise ProposalError(f"cannot read proposal file {proposal_file}: {exc}") from exc

    if not isinstance(metadata, dict):
        raise ProposalError(f"proposal file must contain a YAML mapping: {proposal_file}")

    required = (
        "id",
        "type",
        "status",
        "skill_id",
        "source_target",
        "source_path",
        "canonical_path",
        "target_mutation",
        "files",
        "package_hash",
    )
    for key in required:
        if key not in metadata:
            raise ProposalError(f"missing required proposal key: {key}")

    _required_string(metadata, "id")
    proposal_type = _required_string(metadata, "type")
    proposal_status = _required_string(metadata, "status")
    if proposal_type != "import":
        raise ProposalError("proposal type must be import")
    if proposal_status not in {"proposed", "applied", "superseded"}:
        raise ProposalError("proposal status must be proposed, applied, or superseded")
    if require_proposed and proposal_status != "proposed":
        raise ProposalError("proposal status must be proposed")
    if metadata["target_mutation"] is not False:
        raise ProposalError("proposal target_mutation must be false")

    skill_id = _required_string(metadata, "skill_id")
    skill_by_id = {skill.id: skill for skill in _list_registry_skills(config)}
    if skill_id not in skill_by_id:
        raise ProposalError(f"unknown skill: {skill_id}")
    skill = skill_by_id[skill_id]

    canonical_path = Path(_required_string(metadata, "canonical_path")).expanduser().resolve(strict=False)
    skills_dir = config.skills_dir.resolve(strict=False)
    _require_under(canonical_path, skills_dir, "canonical_path")
    if canonical_path != skill.path.resolve(strict=False):
        raise ProposalError("canonical_path does not match registry skill path")

    source_target_name = _required_string(metadata, "source_target")
    target_by_name = config.target_by_name
    if source_target_name not in target_by_name:
        raise ProposalError(f"unknown source target: {source_target_name}")
    source_target = target_by_name[source_target_name]

    source_path = Path(_required_string(metadata, "source_path")).expanduser().resolve(strict=False)
    expected_source_path = (source_target.root_path / skill.id).resolve(strict=False)
    if source_path != expected_source_path:
        try:
            source_path.relative_to(source_target.root_path.resolve(strict=False))
        except ValueError as exc:
            raise ProposalError("source_path must be under source target root") from exc
        if source_path.name != skill.id:
            raise ProposalError("source_path basename does not match skill id")
        if not (source_path / "SKILL.md").exists() and not (source_path / "skill.yaml").exists():
            raise ProposalError("source_path does not look like a skill directory")

    files = _proposal_inventory(metadata["files"])
    package_hash = _required_string(metadata, "package_hash")
    if not _is_sha256(package_hash):
        raise ProposalError("proposal package_hash must be a 64-character sha256 hex digest")

    return _LoadedImportProposal(
        metadata=metadata,
        proposal_dir=proposal_dir,
        proposal_file=proposal_file,
        skill=skill,
        source_target=source_target,
        source_path=source_path,
        canonical_path=canonical_path,
        files=files,
    )


def _resolve_proposal_paths(config: RegistryConfig, proposal_path: Union[Path, str]) -> tuple[Path, Path]:
    raw_path = Path(proposal_path).expanduser()
    resolved_path = raw_path.resolve(strict=False)
    if resolved_path.name == "proposal.yaml":
        proposal_file = resolved_path
        proposal_dir = resolved_path.parent
    else:
        proposal_dir = resolved_path
        proposal_file = proposal_dir / "proposal.yaml"

    proposals_root = (config.root / "proposals").resolve(strict=False)
    _require_under(proposal_dir, proposals_root, "proposal path")
    return proposal_dir, proposal_file


def _require_under(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProposalError(f"{label} must be under {root}") from exc


def _required_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProposalError(f"proposal key must be a non-empty string: {key}")
    return value


def _proposal_inventory(raw_files: Any) -> tuple[FileInventoryItem, ...]:
    if not isinstance(raw_files, list):
        raise ProposalError("proposal files must be a list")

    files: list[FileInventoryItem] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise ProposalError(f"proposal files[{index}] must be a mapping")
        relative_path = item.get("path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if not isinstance(relative_path, str) or not relative_path:
            raise ProposalError(f"proposal files[{index}].path must be a non-empty string")
        _validate_relative_inventory_path(relative_path, index)
        if relative_path in seen:
            raise ProposalError(f"proposal files contains duplicate path: {relative_path}")
        seen.add(relative_path)
        if not isinstance(size, int) or size < 0:
            raise ProposalError(f"proposal files[{index}].size must be a non-negative integer")
        if not isinstance(sha256, str) or not _is_sha256(sha256):
            raise ProposalError(f"proposal files[{index}].sha256 must be a 64-character sha256 hex digest")
        files.append(FileInventoryItem(path=relative_path, size=size, sha256=sha256))

    if not files:
        raise ProposalError("proposal files must not be empty")
    return tuple(files)


def _validate_relative_inventory_path(relative_path: str, index: int) -> None:
    try:
        validate_deployable_relative_path(relative_path, label=f"proposal files[{index}].path")
    except PayloadHashError as exc:
        raise ProposalError(str(exc)) from exc


def _verify_loaded_source(loaded: _LoadedImportProposal) -> SourceVerification:
    try:
        actual_files = inventory_files(loaded.source_path)
    except ProposalError as exc:
        return SourceVerification(
            checked=True,
            ok=False,
            source_path=str(loaded.source_path),
            errors=(str(exc),),
        )

    errors: list[str] = []
    expected_by_path = {item.path: item for item in loaded.files}
    actual_by_path = {item.path: item for item in actual_files}

    expected_paths = set(expected_by_path)
    actual_paths = set(actual_by_path)
    missing_paths = sorted(expected_paths - actual_paths)
    added_paths = sorted(actual_paths - expected_paths)
    if missing_paths:
        errors.append("missing source files: " + ", ".join(missing_paths))
    if added_paths:
        errors.append("unexpected source files: " + ", ".join(added_paths))

    for relative_path in sorted(expected_paths & actual_paths):
        expected = expected_by_path[relative_path]
        actual = actual_by_path[relative_path]
        if expected.size != actual.size:
            errors.append(f"size mismatch for {relative_path}: expected {expected.size}, found {actual.size}")
        if expected.sha256 != actual.sha256:
            errors.append(f"sha256 mismatch for {relative_path}")

    actual_hash = legacy_package_inventory_hash(actual_files)
    if str(loaded.metadata["package_hash"]) != actual_hash:
        errors.append("package_hash mismatch")

    return SourceVerification(
        checked=True,
        ok=not errors,
        source_path=str(loaded.source_path),
        package_hash=actual_hash,
        errors=tuple(errors),
    )


def _inspection_from_loaded(
    loaded: _LoadedImportProposal,
    verification: Optional[SourceVerification],
) -> ProposalInspection:
    return ProposalInspection(
        id=str(loaded.metadata["id"]),
        type=str(loaded.metadata["type"]),
        status=str(loaded.metadata["status"]),
        skill_id=str(loaded.metadata["skill_id"]),
        source_target=str(loaded.metadata["source_target"]),
        source_path=str(loaded.source_path),
        canonical_path=str(loaded.canonical_path),
        proposal_path=str(loaded.proposal_dir),
        target_mutation=bool(loaded.metadata["target_mutation"]),
        mutates_target_files=False,
        package_hash=str(loaded.metadata["package_hash"]),
        file_count=len(loaded.files),
        files=loaded.files,
        verification=verification,
    )


def _canonical_is_pending_import(canonical_path: Path) -> bool:
    metadata_path = canonical_path / "skill.yaml"
    if not metadata_path.exists():
        raise ProposalError(f"canonical skill metadata is missing: {metadata_path}")
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProposalError(f"invalid YAML in canonical skill metadata {metadata_path}: {exc}") from exc
    except OSError as exc:
        raise ProposalError(f"cannot read canonical skill metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ProposalError(f"canonical skill metadata must contain a YAML mapping: {metadata_path}")

    lifecycle = str(metadata.get("lifecycle", ""))
    canonical_status = str(metadata.get("canonical_status", ""))
    return lifecycle == "placeholder" or canonical_status == "pending_import"


def _replace_canonical_package(
    loaded: _LoadedImportProposal,
    *,
    approved_by: str,
    applied_at: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _remove_stale_canonical_files(loaded.canonical_path)
    copied_files: list[str] = []
    skipped_control_files = set(inventory_control_files(loaded.source_path))

    for item in loaded.files:
        if _is_control_file(item.path):
            skipped_control_files.add(item.path)
            continue

        source_file = loaded.source_path / item.path
        target_file = loaded.canonical_path / item.path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        copied_files.append(item.path)

    _write_imported_skill_metadata(
        loaded,
        approved_by=approved_by,
        applied_at=applied_at,
    )
    _remove_empty_dirs(loaded.canonical_path)
    return tuple(copied_files), tuple(sorted(skipped_control_files))


def _remove_stale_canonical_files(canonical_path: Path) -> None:
    for path in sorted(canonical_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            continue
        relative_path = path.relative_to(canonical_path).as_posix()
        if _is_control_file(relative_path) or _is_excluded(relative_path):
            continue
        path.unlink()


def _remove_empty_dirs(canonical_path: Path) -> None:
    for path in sorted(canonical_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path == canonical_path or not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def _write_imported_skill_metadata(
    loaded: _LoadedImportProposal,
    *,
    approved_by: str,
    applied_at: str,
) -> None:
    metadata_path = loaded.canonical_path / "skill.yaml"
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProposalError(f"invalid YAML in canonical skill metadata {metadata_path}: {exc}") from exc
    except OSError as exc:
        raise ProposalError(f"cannot read canonical skill metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ProposalError(f"canonical skill metadata must contain a YAML mapping: {metadata_path}")

    metadata["lifecycle"] = "canonical"
    metadata["canonical_status"] = "imported"
    metadata["source_import"] = {
        "proposal_id": str(loaded.metadata["id"]),
        "source_target": str(loaded.metadata["source_target"]),
        "source_path": str(loaded.source_path),
        "package_hash": str(loaded.metadata["package_hash"]),
        "approved_by": approved_by,
        "applied_at": applied_at,
    }
    _write_yaml_file(metadata_path, metadata)


def _apply_record(
    loaded: _LoadedImportProposal,
    *,
    approved_by: str,
    reason: str,
    applied_at: str,
    copied_files: tuple[str, ...],
    skipped_control_files: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "proposal_id": str(loaded.metadata["id"]),
        "skill_id": str(loaded.metadata["skill_id"]),
        "source_target": str(loaded.metadata["source_target"]),
        "source_path": str(loaded.source_path),
        "canonical_path": str(loaded.canonical_path),
        "approved_by": approved_by,
        "reason": reason,
        "applied_at": applied_at,
        "package_hash": str(loaded.metadata["package_hash"]),
        "target_mutation": False,
        "copied_files": list(copied_files),
        "skipped_control_files": list(skipped_control_files),
    }


def _mark_proposal_applied(
    loaded: _LoadedImportProposal,
    *,
    approved_by: str,
    reason: str,
    applied_at: str,
) -> None:
    metadata = dict(loaded.metadata)
    metadata["status"] = "applied"
    metadata["applied_at"] = applied_at
    metadata["approved_by"] = approved_by
    metadata["approval_reason"] = reason
    _write_yaml_file(loaded.proposal_file, metadata)


def _restore_canonical_backup(canonical_path: Path, backup_path: Path) -> None:
    if canonical_path.exists() or canonical_path.is_symlink():
        if canonical_path.is_dir() and not canonical_path.is_symlink():
            shutil.rmtree(canonical_path)
        else:
            canonical_path.unlink()
    shutil.copytree(backup_path, canonical_path, symlinks=True)


def _write_yaml_file(path: Path, data: Any) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        temp_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _list_registry_skills(config: RegistryConfig) -> tuple[SkillPackage, ...]:
    from .skills import list_registry_skills

    return list_registry_skills(config)


def _proposal_metadata(
    *,
    proposal_id: str,
    target: TargetConfig,
    skill: SkillPackage,
    source_path: Path,
    canonical_path: Path,
    created_at: datetime,
    reason: str,
    files: tuple[FileInventoryItem, ...],
    package_hash: str,
) -> dict[str, Any]:
    return {
        "id": proposal_id,
        "type": "import",
        "status": "proposed",
        "skill_id": skill.id,
        "source_target": target.name,
        "source_path": str(source_path),
        "canonical_path": str(canonical_path),
        "created_at": created_at.isoformat(),
        "reason": reason,
        "risk": "review required before replacing the canonical placeholder",
        "target_mutation": False,
        "files": [_file_metadata(item) for item in files],
        "package_hash": package_hash,
    }


def _file_metadata(item: FileInventoryItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "size": item.size,
        "sha256": item.sha256,
    }


def _proposal_readme(metadata: dict[str, Any]) -> str:
    return (
        f"# Import Proposal: {metadata['skill_id']}\n\n"
        f"- Status: {metadata['status']}\n"
        f"- Source target: {metadata['source_target']}\n"
        f"- Source path: `{metadata['source_path']}`\n"
        f"- Canonical path: `{metadata['canonical_path']}`\n"
        f"- Package hash: `{metadata['package_hash']}`\n"
        f"- Target mutation: `{str(metadata['target_mutation']).lower()}`\n\n"
        "This proposal is metadata-only. It records file hashes and text diffs for review, "
        "but it does not copy files from the target skill directory and does not mutate target content.\n"
    )


def _proposal_diff(
    source_path: Path,
    canonical_path: Path,
    source_target: str,
    files: tuple[FileInventoryItem, ...],
) -> str:
    chunks: list[str] = []
    for item in files:
        source_file = source_path / item.path
        canonical_file = canonical_path / item.path
        if not canonical_file.is_file():
            continue

        source_text = _read_utf8_text(source_file)
        canonical_text = _read_utf8_text(canonical_file)
        if source_text is None or canonical_text is None:
            continue

        diff_lines = list(
            difflib.unified_diff(
                canonical_text.splitlines(),
                source_text.splitlines(),
                fromfile=f"canonical/{item.path}",
                tofile=f"{source_target}/{item.path}",
                lineterm="",
            )
        )
        if diff_lines:
            chunks.extend(line + "\n" for line in diff_lines)
            chunks.append("\n")

    if not chunks:
        return "No UTF-8 text diffs were generated for matching canonical/source files.\n"
    return "".join(chunks)


def _read_utf8_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    except OSError as exc:
        raise ProposalError(f"Cannot read text file {path}: {exc}") from exc
