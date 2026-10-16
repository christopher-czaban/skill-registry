from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import RegistryConfig, TargetConfig
from .hashing import PayloadHash, deployable_payload_hash
from .skills import SkillPackage, selected_skills
from .state import (
    DEPLOYMENT_RECORD_SCHEMA_VERSION,
    DeploymentRecordError,
    DeploymentRecord,
    deployment_record_path,
    deployment_record_validation_error,
    load_classification,
    load_deployment_record,
    save_deployment_record,
)
from .status import check_recorded_directory_health
from .targets import selected_targets
from .undeploy import pending_path, read_receipt


@dataclass(frozen=True)
class DeployPlanOperation:
    target: str
    skill_id: str
    mode: str
    operation: str
    source_path: str
    target_path: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.operation != "block"


@dataclass(frozen=True)
class DeployApplyOperation:
    target: str
    skill_id: str
    mode: str
    operation: str
    source_path: str
    target_path: str
    reason: str
    applied: bool
    record_path: str = ""
    payload_hash: str = ""
    file_count: int = 0
    backup_path: str = ""


@dataclass(frozen=True)
class ManagedEntryBackup:
    path: Path
    entry_path: Path
    target_path: Path
    previous_entry_kind: str
    previous_link_target: str = ""


APPLY_SAFE_OPERATIONS = {
    "skip",
    "create_symlink",
    "create_copy",
    "refresh_copy",
    "replace_with_symlink",
    "replace_with_copy",
    "repair_symlink",
}


def plan_deploy(
    config: RegistryConfig,
    *,
    mode: str,
    all_targets: bool,
    target_names: tuple[str, ...],
    skill_ids: tuple[str, ...],
    replace_managed: bool = False,
    replace_drifted_copy: bool = False,
) -> tuple[DeployPlanOperation, ...]:
    targets = selected_targets(config, target_names, all_targets=all_targets)
    skills = selected_skills(config, skill_ids)
    operations: list[DeployPlanOperation] = []

    for target in targets:
        for skill in skills:
            if skill.lifecycle == "retired" and not skill_ids:
                continue
            if not _skill_applies_to_target(skill, target):
                continue
            effective_mode = _effective_mode(mode, target)
            if pending_path(target, skill.id).exists() or pending_path(target, skill.id).is_symlink():
                operations.append(_operation(target, skill, effective_mode, "block", str(target.root_path / skill.id),
                                             "pending undeployment needs recovery before deployment"))
                continue
            try:
                intentionally_removed = (not skill_ids and not load_deployment_record(target, skill.id)
                                         and read_receipt(config, target, skill.id))
            except (OSError, ValueError, RuntimeError) as exc:
                operations.append(_operation(target, skill, effective_mode, "block", str(target.root_path / skill.id), str(exc)))
                continue
            if intentionally_removed:
                entry = target.root_path / skill.id
                recreated = entry.exists() or entry.is_symlink()
                operations.append(_operation(target, skill, effective_mode, "block" if recreated else "skip", str(entry),
                                             "undeployed entry was recreated without ownership" if recreated else
                                             "intentionally undeployed; select this skill explicitly to reinstall"))
                continue
            dependencies = {s.id: s for s in selected_skills(config, skill.skill_dependencies)} if skill.skill_dependencies else {}
            if any(s.lifecycle == "retired" for s in dependencies.values()):
                operations.append(_operation(target, skill, effective_mode, "block", str(target.root_path / skill.id),
                                             "active skill depends on a retired skill"))
                continue
            operations.append(
                _plan_target_skill(
                    target,
                    skill,
                    effective_mode,
                    replace_managed=replace_managed,
                    replace_drifted_copy=replace_drifted_copy,
                )
            )
    return tuple(operations)


def plan_ok(operations: tuple[DeployPlanOperation, ...]) -> bool:
    return all(operation.ok for operation in operations)


def apply_deploy(
    config: RegistryConfig,
    *,
    mode: str,
    all_targets: bool,
    target_names: tuple[str, ...],
    skill_ids: tuple[str, ...],
    deployed_by: str,
    reason: str,
    replace_managed: bool = False,
    replace_drifted_copy: bool = False,
) -> tuple[DeployApplyOperation, ...]:
    reason = reason.strip()
    if not reason:
        raise ValueError("deploy apply requires a non-empty reason")
    deployed_by = deployed_by.strip() or "skill-registry"

    planned = plan_deploy(
        config,
        mode=mode,
        all_targets=all_targets,
        target_names=target_names,
        skill_ids=skill_ids,
        replace_managed=replace_managed,
        replace_drifted_copy=replace_drifted_copy,
    )
    for operation in planned:
        if operation.operation not in APPLY_SAFE_OPERATIONS:
            raise ValueError(
                "deploy apply blocked by unsafe planned operation: "
                f"{operation.operation} {operation.skill_id} -> {operation.target}: {operation.reason}"
            )

    targets = {target.name: target for target in selected_targets(config, target_names, all_targets=all_targets)}
    skills = {skill.id: skill for skill in selected_skills(config, skill_ids)}
    results: list[DeployApplyOperation] = []
    for operation in planned:
        if operation.operation == "skip":
            results.append(
                DeployApplyOperation(
                    target=operation.target,
                    skill_id=operation.skill_id,
                    mode=operation.mode,
                    operation=operation.operation,
                    source_path=operation.source_path,
                    target_path=operation.target_path,
                    reason=operation.reason,
                    applied=False,
                )
            )
            continue

        target = targets[operation.target]
        skill = skills[operation.skill_id]
        if operation.operation == "create_symlink":
            results.append(_create_symlink(target, skill, deployed_by=deployed_by, reason=reason))
        elif operation.operation == "create_copy":
            results.append(_create_copy(target, skill, deployed_by=deployed_by, reason=reason))
        elif operation.operation in {"refresh_copy", "replace_with_symlink", "replace_with_copy", "repair_symlink"}:
            results.append(
                _replace_managed_entry(
                    target,
                    skill,
                    operation=operation,
                    deployed_by=deployed_by,
                    reason=reason,
                    replace_managed=replace_managed,
                    replace_drifted_copy=replace_drifted_copy,
                )
            )
        else:
            raise ValueError(f"deploy apply cannot execute unknown operation: {operation.operation}")
    return tuple(results)


def _skill_applies_to_target(skill: SkillPackage, target: TargetConfig) -> bool:
    return not skill.targets or target.name in skill.targets


def _effective_mode(mode: str, target: TargetConfig) -> str:
    if mode == "target-default":
        return target.deploy_mode
    return mode


def _plan_target_skill(
    target: TargetConfig,
    skill: SkillPackage,
    mode: str,
    *,
    replace_managed: bool,
    replace_drifted_copy: bool,
) -> DeployPlanOperation:
    if pending_path(target, skill.id).exists() or pending_path(target, skill.id).is_symlink():
        return _operation(target, skill, mode, "block", str(target.root_path / skill.id),
                          "pending undeployment needs recovery before deployment")
    if skill.lifecycle not in {"canonical", "placeholder"}:
        return _operation(target, skill, mode, "block", str(target.root_path / skill.id),
                          "skill is retired or has an unsupported lifecycle; reactivate through reviewed metadata")
    root = target.root_path
    target_path = root / skill.id
    record = load_classification(target, skill.id)

    if record and record.classification == "local_only":
        return _operation(target, skill, mode, "skip", str(target_path), f"local-only classification: {record.reason}")
    if record and record.classification == "blocked":
        return _operation(target, skill, mode, "block", str(target_path), f"blocked classification: {record.reason}")

    if mode == "symlink" and not target.supports_symlink:
        return _operation(target, skill, mode, "block", str(target_path), "target does not support symlink mode")

    if target_path.is_symlink():
        record_proof, record_error = _validated_deployment_record_proof(target, skill, target_path)
        if not record_proof:
            return _operation(
                target,
                skill,
                mode,
                "block",
                str(target_path),
                f"unmanaged symlink remains blocked; {record_error}",
            )
        if not target_path.exists():
            if mode == "copy":
                return _managed_mode_switch_operation(
                    target,
                    skill,
                    mode,
                    target_path,
                    "replace_with_copy",
                    required_flag="--replace-managed",
                    flag_present=replace_managed,
                    record_proof=record_proof,
                    replacement="replace broken managed symlink with canonical copy",
                )
            return _managed_mode_switch_operation(
                target,
                skill,
                mode,
                target_path,
                "repair_symlink",
                required_flag="--replace-managed",
                flag_present=replace_managed,
                record_proof=record_proof,
                replacement="repair broken managed symlink to canonical package",
            )
        resolved = target_path.resolve(strict=True)
        if resolved == skill.path and mode == "symlink":
            return _operation(target, skill, mode, "skip", str(target_path), f"symlink already in sync; {record_proof}")
        if mode == "copy":
            return _managed_mode_switch_operation(
                target,
                skill,
                mode,
                target_path,
                "replace_with_copy",
                required_flag="--replace-managed",
                flag_present=replace_managed,
                record_proof=record_proof,
                replacement=f"replace managed symlink pointing at {resolved} with canonical copy",
            )
        return _managed_mode_switch_operation(
            target,
            skill,
            mode,
            target_path,
            "repair_symlink",
            required_flag="--replace-managed",
            flag_present=replace_managed,
            record_proof=record_proof,
            replacement=f"repair managed symlink pointing at {resolved} to canonical package",
        )

    if target_path.exists():
        recorded_health = check_recorded_directory_health(target, skill, target_path)
        if recorded_health:
            return _plan_recorded_directory(
                target,
                skill,
                mode,
                target_path,
                recorded_health,
                replace_managed=replace_managed,
                replace_drifted_copy=replace_drifted_copy,
            )
        return _operation(target, skill, mode, "block", str(target_path), "target path exists and is not registry-managed")

    recorded_health = check_recorded_directory_health(target, skill, target_path)
    if recorded_health:
        return _operation(
            target,
            skill,
            mode,
            "block",
            str(target_path),
            f"recorded target is {recorded_health.status}: {recorded_health.reason}",
        )

    if mode == "copy":
        reason = "would create copy"
        operation = "create_copy"
    else:
        reason = "would create symlink"
        operation = "create_symlink"

    if not root.exists():
        reason += "; target root would need to be created"
    return _operation(target, skill, mode, operation, str(target_path), reason)


def _operation(
    target: TargetConfig,
    skill: SkillPackage,
    mode: str,
    operation: str,
    target_path: str,
    reason: str,
) -> DeployPlanOperation:
    return DeployPlanOperation(
        target=target.name,
        skill_id=skill.id,
        mode=mode,
        operation=operation,
        source_path=str(skill.path),
        target_path=target_path,
        reason=reason,
    )


def _validated_deployment_record_proof(target: TargetConfig, skill: SkillPackage, target_path: Path) -> tuple[str, str]:
    try:
        record = load_deployment_record(target, skill.id)
    except DeploymentRecordError as exc:
        return "", f"deployment record is malformed: {exc}"
    if record is None:
        return "", "no valid deployment record proves ownership"
    validation_error = deployment_record_validation_error(
        record,
        target=target,
        skill_id=skill.id,
        source_path=skill.path,
        target_path=target_path,
    )
    if validation_error:
        return "", f"deployment record does not prove ownership: {validation_error}"
    return f"deployment record proves ownership: {deployment_record_path(target, skill.id)}", ""


def _plan_recorded_directory(
    target: TargetConfig,
    skill: SkillPackage,
    mode: str,
    target_path: Path,
    recorded_health,
    *,
    replace_managed: bool,
    replace_drifted_copy: bool,
) -> DeployPlanOperation:
    record_proof = f"deployment record proves ownership: {deployment_record_path(target, skill.id)}"
    health_reason = f"recorded target is {recorded_health.status}: {recorded_health.reason}"

    if recorded_health.status == "blocked":
        return _operation(target, skill, mode, "block", str(target_path), health_reason)

    if recorded_health.status == "in_sync":
        if mode == "copy":
            return _operation(target, skill, mode, "skip", str(target_path), f"{recorded_health.reason}; {record_proof}")
        return _managed_mode_switch_operation(
            target,
            skill,
            mode,
            target_path,
            "replace_with_symlink",
            required_flag="--replace-managed",
            flag_present=replace_managed,
            record_proof=record_proof,
            replacement="replace managed copy with canonical symlink",
            health_reason=health_reason,
        )

    if recorded_health.status == "stale":
        if mode == "copy":
            return _managed_mode_switch_operation(
                target,
                skill,
                mode,
                target_path,
                "refresh_copy",
                required_flag="--replace-managed",
                flag_present=replace_managed,
                record_proof=record_proof,
                replacement="refresh managed copy from canonical payload",
                health_reason=health_reason,
            )
        return _managed_mode_switch_operation(
            target,
            skill,
            mode,
            target_path,
            "replace_with_symlink",
            required_flag="--replace-managed",
            flag_present=replace_managed,
            record_proof=record_proof,
            replacement="replace stale managed copy with canonical symlink",
            health_reason=health_reason,
        )

    if recorded_health.status == "drifted":
        if mode == "copy":
            return _managed_mode_switch_operation(
                target,
                skill,
                mode,
                target_path,
                "refresh_copy",
                required_flag="--replace-drifted-copy",
                flag_present=replace_drifted_copy,
                record_proof=record_proof,
                replacement="overwrite drifted managed copy from canonical payload",
                health_reason=health_reason,
            )
        return _managed_mode_switch_operation(
            target,
            skill,
            mode,
            target_path,
            "replace_with_symlink",
            required_flag="--replace-drifted-copy",
            flag_present=replace_drifted_copy,
            record_proof=record_proof,
            replacement="replace drifted managed copy with canonical symlink",
            health_reason=health_reason,
        )

    return _operation(target, skill, mode, "block", str(target_path), health_reason)


def _managed_mode_switch_operation(
    target: TargetConfig,
    skill: SkillPackage,
    mode: str,
    target_path: Path,
    operation: str,
    *,
    required_flag: str,
    flag_present: bool,
    record_proof: str,
    replacement: str,
    health_reason: str = "",
) -> DeployPlanOperation:
    details = [record_proof]
    if health_reason:
        details.append(health_reason)
    details.append(f"would {replacement}")
    if not flag_present:
        details.insert(0, f"missing {required_flag}")
        return _operation(target, skill, mode, "block", str(target_path), "; ".join(details))
    details.append(f"{required_flag} set")
    return _operation(target, skill, mode, operation, str(target_path), "; ".join(details))


def _create_symlink(
    target: TargetConfig,
    skill: SkillPackage,
    *,
    deployed_by: str,
    reason: str,
) -> DeployApplyOperation:
    target_path = target.root_path / skill.id
    source_path = skill.path.resolve(strict=False)
    payload = deployable_payload_hash(source_path)
    _prepare_absent_target(target.root_path, target_path)

    temp_path = _temporary_sibling(target_path)
    created_target = False
    try:
        temp_path.symlink_to(source_path, target_is_directory=True)
        _fail_if_target_exists(target_path)
        temp_path.rename(target_path)
        created_target = True
        return _record_success(
            target,
            skill,
            mode="symlink",
            operation="create_symlink",
            source_path=source_path,
            target_path=target_path,
            payload=payload,
            deployed_by=deployed_by,
            reason=reason,
        )
    except Exception as exc:
        _remove_temp_entry(temp_path)
        if created_target:
            _remove_created_entry(target_path)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"deploy apply failed for {skill.id} -> {target.name}: {exc}") from exc


def _create_copy(
    target: TargetConfig,
    skill: SkillPackage,
    *,
    deployed_by: str,
    reason: str,
) -> DeployApplyOperation:
    target_path = target.root_path / skill.id
    source_path = skill.path.resolve(strict=False)
    payload = deployable_payload_hash(source_path)
    _prepare_absent_target(target.root_path, target_path)

    temp_path = _temporary_sibling(target_path)
    created_target = False
    try:
        temp_path.mkdir()
        for item in payload.files:
            source_file = source_path / item.path
            target_file = temp_path / item.path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
        _fail_if_target_exists(target_path)
        temp_path.rename(target_path)
        created_target = True
        return _record_success(
            target,
            skill,
            mode="copy",
            operation="create_copy",
            source_path=source_path,
            target_path=target_path,
            payload=payload,
            deployed_by=deployed_by,
            reason=reason,
        )
    except Exception as exc:
        _remove_temp_entry(temp_path)
        if created_target:
            _remove_created_entry(target_path)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"deploy apply failed for {skill.id} -> {target.name}: {exc}") from exc


def _replace_managed_entry(
    target: TargetConfig,
    skill: SkillPackage,
    *,
    operation: DeployPlanOperation,
    deployed_by: str,
    reason: str,
    replace_managed: bool,
    replace_drifted_copy: bool,
) -> DeployApplyOperation:
    target_path = target.root_path / skill.id
    source_path = skill.path.resolve(strict=False)
    payload = deployable_payload_hash(source_path)
    final_mode = "copy" if operation.operation in {"refresh_copy", "replace_with_copy"} else "symlink"

    current_plan = _plan_target_skill(
        target,
        skill,
        operation.mode,
        replace_managed=replace_managed,
        replace_drifted_copy=replace_drifted_copy,
    )
    if current_plan.operation != operation.operation:
        raise ValueError(
            "deploy apply target state changed before replacement: "
            f"planned {operation.operation}, now {current_plan.operation}: {current_plan.reason}"
        )

    temp_path = _temporary_sibling(target_path)
    backup: ManagedEntryBackup | None = None
    try:
        _create_temp_entry(temp_path, source_path, payload, final_mode)
        backup = _create_managed_backup(
            target,
            skill,
            operation=operation.operation,
            source_path=source_path,
            target_path=target_path,
            deployment_mode=final_mode,
            actor=deployed_by,
            reason=reason,
        )
        _replace_target_from_temp(target_path, temp_path, backup)
        try:
            return _record_success(
                target,
                skill,
                mode=final_mode,
                operation=operation.operation,
                source_path=source_path,
                target_path=target_path,
                payload=payload,
                deployed_by=deployed_by,
                reason=reason,
                record_operation=operation.operation,
                backup_path=str(backup.path),
            )
        except Exception as exc:
            try:
                _restore_backup(backup)
            except Exception as rollback_exc:
                raise ValueError(
                    "deployment record write failed after target replacement and rollback failed; "
                    f"manual recovery required for target path {target_path} from backup {backup.path}: "
                    f"{rollback_exc}; original record error: {exc}"
                ) from exc
            raise ValueError(
                "deployment record write failed after target replacement; "
                f"rolled target back from backup {backup.path}: {exc}"
            ) from exc
    except Exception as exc:
        _remove_temp_entry(temp_path)
        if isinstance(exc, ValueError):
            raise
        backup_hint = f" backup={backup.path}" if backup else ""
        raise ValueError(f"deploy apply failed for {skill.id} -> {target.name}:{backup_hint} {exc}") from exc


def _prepare_absent_target(root: Path, target_path: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _fail_if_target_exists(target_path)


def _fail_if_target_exists(target_path: Path) -> None:
    if target_path.exists() or target_path.is_symlink():
        raise ValueError(f"target path exists at apply time: {target_path}")


def _temporary_sibling(target_path: Path) -> Path:
    return target_path.with_name(f".{target_path.name}.deploy-{uuid4().hex}")


def _create_temp_entry(temp_path: Path, source_path: Path, payload: PayloadHash, mode: str) -> None:
    if mode == "symlink":
        temp_path.symlink_to(source_path, target_is_directory=True)
        return

    temp_path.mkdir()
    for item in payload.files:
        source_file = source_path / item.path
        target_file = temp_path / item.path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)


def _create_managed_backup(
    target: TargetConfig,
    skill: SkillPackage,
    *,
    operation: str,
    source_path: Path,
    target_path: Path,
    deployment_mode: str,
    actor: str,
    reason: str,
) -> ManagedEntryBackup:
    timestamp = datetime.now(timezone.utc)
    short_id = uuid4().hex[:8]
    backup_path = target.state_path / "backups" / f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}-{skill.id}-{operation}-{short_id}"
    entry_root = backup_path / "entry"
    entry_path = entry_root / skill.id

    previous_entry_kind = _entry_kind(target_path)
    previous_link_target = os.readlink(target_path) if target_path.is_symlink() else ""
    try:
        entry_root.mkdir(parents=True, exist_ok=False)
        if previous_entry_kind in {"symlink", "broken_symlink"}:
            entry_path.symlink_to(previous_link_target, target_is_directory=True)
        elif previous_entry_kind == "directory":
            shutil.copytree(target_path, entry_path, symlinks=True)
        elif previous_entry_kind == "file":
            shutil.copy2(target_path, entry_path, follow_symlinks=False)
        else:
            raise ValueError(f"cannot back up missing target entry: {target_path}")

        metadata = {
            "target": target.name,
            "skill_id": skill.id,
            "operation": operation,
            "source_path": str(source_path),
            "target_path": str(target_path),
            "deployment_mode": deployment_mode,
            "previous_entry_kind": previous_entry_kind,
            "previous_link_target": previous_link_target,
            "reason": reason,
            "actor": actor,
            "timestamp": timestamp.isoformat(),
        }
        (backup_path / "backup.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        if backup_path.exists():
            shutil.rmtree(backup_path, ignore_errors=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"failed to create backup before managed replacement at {backup_path}: {exc}") from exc

    return ManagedEntryBackup(
        path=backup_path,
        entry_path=entry_path,
        target_path=target_path,
        previous_entry_kind=previous_entry_kind,
        previous_link_target=previous_link_target,
    )


def _entry_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink" if path.exists() else "broken_symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "missing"


def _replace_target_from_temp(target_path: Path, temp_path: Path, backup: ManagedEntryBackup) -> None:
    try:
        _remove_created_entry(target_path)
        temp_path.rename(target_path)
    except Exception as exc:
        _remove_temp_entry(temp_path)
        try:
            _restore_backup(backup)
        except Exception as rollback_exc:
            raise ValueError(
                "target replacement failed after backup and rollback failed; "
                f"manual recovery required for target path {target_path} from backup {backup.path}: "
                f"{rollback_exc}; original replacement error: {exc}"
            ) from exc
        raise ValueError(f"target replacement failed; rolled back from backup {backup.path}: {exc}") from exc


def _restore_backup(backup: ManagedEntryBackup) -> None:
    _remove_temp_entry(backup.target_path)
    backup.target_path.parent.mkdir(parents=True, exist_ok=True)
    if backup.previous_entry_kind in {"symlink", "broken_symlink"}:
        backup.target_path.symlink_to(backup.previous_link_target, target_is_directory=True)
    elif backup.previous_entry_kind == "directory":
        shutil.copytree(backup.entry_path, backup.target_path, symlinks=True)
    elif backup.previous_entry_kind == "file":
        shutil.copy2(backup.entry_path, backup.target_path, follow_symlinks=False)
    else:
        raise ValueError(f"backup cannot restore unsupported entry kind: {backup.previous_entry_kind}")


def _record_success(
    target: TargetConfig,
    skill: SkillPackage,
    *,
    mode: str,
    operation: str,
    source_path: Path,
    target_path: Path,
    payload: PayloadHash,
    deployed_by: str,
    reason: str,
    record_operation: str = "deploy",
    backup_path: str = "",
) -> DeployApplyOperation:
    record = DeploymentRecord(
        schema_version=DEPLOYMENT_RECORD_SCHEMA_VERSION,
        target=target.name,
        skill_id=skill.id,
        deployment_mode=mode,
        source_path=str(source_path),
        target_path=str(target_path),
        payload_hash=payload.payload_hash,
        file_count=payload.file_count,
        files=payload.files,
        deployed_at=datetime.now(timezone.utc).isoformat(),
        deployed_by=deployed_by,
        operation=record_operation,
        reason=reason,
    )
    save_deployment_record(target, record)
    return DeployApplyOperation(
        target=target.name,
        skill_id=skill.id,
        mode=mode,
        operation=operation,
        source_path=str(source_path),
        target_path=str(target_path),
        reason=reason,
        applied=True,
        record_path=str(deployment_record_path(target, skill.id)),
        payload_hash=payload.payload_hash,
        file_count=payload.file_count,
        backup_path=backup_path,
    )


def _remove_temp_entry(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _remove_created_entry(path)


def _remove_created_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
