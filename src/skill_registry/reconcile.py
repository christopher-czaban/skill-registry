from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import RegistryConfig, TargetConfig
from .hashing import FileInventoryItem, PayloadHashError, deployable_payload_hash
from .proposals import build_import_proposal
from .skills import SkillPackage, selected_skills
from .state import (
    DEPLOYMENT_RECORD_SCHEMA_VERSION,
    ClassificationRecord,
    DeploymentRecord,
    DeploymentRecordError,
    deployment_record_path,
    load_classification,
    load_deployment_record,
    save_classification,
    save_deployment_record,
)
from .deploy import (
    _create_managed_backup,
    _create_temp_entry,
    _remove_temp_entry,
    _replace_target_from_temp,
    _restore_backup,
    _temporary_sibling,
)

PLAN_ONLY_ACTIONS: set[str] = set()
APPLY_ACTIONS = {"adopt", "local-only", "keep-blocked", "propose-import", "replace"}
ALL_ACTIONS = tuple(sorted(PLAN_ONLY_ACTIONS | APPLY_ACTIONS))


@dataclass(frozen=True)
class ReconcilePlan:
    target: str
    skill_id: str
    current_classification: str
    target_path: str
    selected_action: str
    recommended_action: str
    can_apply: bool
    mutates_target_files: bool
    reason: str
    available_actions: tuple[str, ...]
    payload_hash: str = ""
    file_count: int = 0


@dataclass(frozen=True)
class ReconcileApplyResult:
    target: str
    skill_id: str
    action: str
    classification: str
    target_path: str
    reason: str
    mutates_target_files: bool = False
    record_path: str = ""
    proposal_path: str = ""
    source_path: str = ""
    payload_hash: str = ""
    file_count: int = 0
    backup_path: str = ""


@dataclass(frozen=True)
class AdoptEligibility:
    can_apply: bool
    reason: str
    payload_hash: str = ""
    file_count: int = 0
    files: tuple[FileInventoryItem, ...] = ()


@dataclass(frozen=True)
class ReplaceEligibility:
    can_apply: bool
    reason: str
    payload_hash: str = ""
    file_count: int = 0
    files: tuple[FileInventoryItem, ...] = ()


def plan_reconciliation(
    config: RegistryConfig,
    *,
    target_name: str,
    skill_id: str,
    action: str = "",
    source_path: str = "",
) -> ReconcilePlan:
    target = config.target_by_name[target_name]
    skill = selected_skills(config, (skill_id,))[0]
    target_path = _resolve_target_skill_path(target, skill, source_path)
    existing_record = load_classification(target, skill.id)
    current = _current_classification(target, skill, existing_record, target_path)
    selected_action = action or _recommended_action(current)
    payload_hash = ""
    file_count = 0
    mutates_target_files = False

    if selected_action == "adopt":
        eligibility = _adopt_eligibility(target, skill, current, target_path)
        can_apply = eligibility.can_apply
        reason = eligibility.reason
        payload_hash = eligibility.payload_hash
        file_count = eligibility.file_count
    elif selected_action == "replace":
        eligibility = _replace_eligibility(target, skill, current, target_path)
        can_apply = eligibility.can_apply
        reason = eligibility.reason
        payload_hash = eligibility.payload_hash
        file_count = eligibility.file_count
        mutates_target_files = True
    else:
        can_apply = _can_apply(selected_action, current)
        reason = _plan_reason(current, selected_action, existing_record)

    return ReconcilePlan(
        target=target.name,
        skill_id=skill.id,
        current_classification=current,
        target_path=str(target_path),
        selected_action=selected_action,
        recommended_action=_recommended_action(current),
        can_apply=can_apply,
        mutates_target_files=mutates_target_files,
        reason=reason,
        available_actions=_available_actions(current),
        payload_hash=payload_hash,
        file_count=file_count,
    )


def apply_reconciliation(
    config: RegistryConfig,
    *,
    target_name: str,
    skill_id: str,
    action: str,
    reason: str,
    adopted_by: str = "",
    source_path: str = "",
) -> ReconcileApplyResult:
    reason = reason.strip()
    if not reason:
        raise ValueError("reason is required")
    plan = plan_reconciliation(
        config,
        target_name=target_name,
        skill_id=skill_id,
        action=action,
        source_path=source_path,
    )
    if action not in APPLY_ACTIONS:
        raise ValueError(f"Action is plan-only in this slice: {action}")
    if not plan.can_apply:
        raise ValueError(f"Action {action} cannot be applied: {plan.reason}")

    target = config.target_by_name[target_name]
    skill = selected_skills(config, (skill_id,))[0]
    resolved_source_path = Path(plan.target_path)
    if action == "adopt":
        return _apply_adopt(target, skill, plan, reason=reason, adopted_by=adopted_by, target_path=resolved_source_path)
    if action == "replace":
        return _apply_replace(
            target,
            skill,
            plan,
            reason=reason,
            replaced_by=adopted_by,
            target_path=resolved_source_path,
        )

    if action == "propose-import":
        proposal = build_import_proposal(config, target=target, skill=skill, reason=reason, source_path=resolved_source_path)
        return ReconcileApplyResult(
            target=target.name,
            skill_id=skill.id,
            action=action,
            classification=plan.current_classification,
            target_path=plan.target_path,
            reason=reason,
            proposal_path=str(proposal.path),
            source_path=str(proposal.source_path),
        )

    classification = "local_only" if action == "local-only" else "blocked"
    record = save_classification(
        target,
        skill_id,
        classification=classification,
        reason=reason,
        target_path=plan.target_path,
    )

    return ReconcileApplyResult(
        target=target.name,
        skill_id=skill_id,
        action=action,
        classification=record.classification,
        record_path=str(_record_path(target, skill_id)),
        target_path=record.target_path,
        reason=record.reason,
    )


def _current_classification(
    target: TargetConfig,
    skill: SkillPackage,
    existing_record: ClassificationRecord | None,
    target_path: Path,
) -> str:
    if existing_record and existing_record.classification == "local_only":
        return "local_only"
    if existing_record and existing_record.classification == "blocked":
        return "blocked"

    if not target_path.exists() and not target_path.is_symlink():
        return "absent"
    if target_path.is_symlink():
        try:
            resolved = target_path.resolve(strict=True)
        except FileNotFoundError:
            return "blocked"
        if resolved == skill.path:
            return "registry_managed"
        return "unmanaged"
    if target_path.is_dir():
        return "candidate_duplicate"
    return "blocked"


def _recommended_action(current: str) -> str:
    if current == "candidate_duplicate":
        return "propose-import"
    if current == "unmanaged":
        return "propose-import"
    if current == "absent":
        return "replace"
    if current == "local_only":
        return "local-only"
    return "keep-blocked"


def _available_actions(current: str) -> tuple[str, ...]:
    if current in {"candidate_duplicate", "unmanaged"}:
        return ("propose-import", "local-only", "keep-blocked", "replace", "adopt")
    if current == "absent":
        return ("replace",)
    if current == "local_only":
        return ("local-only", "keep-blocked")
    return ("keep-blocked",)


def _can_apply(action: str, current: str) -> bool:
    if action == "adopt":
        return False
    if action == "replace":
        return False
    if action == "propose-import":
        return current in {"candidate_duplicate", "unmanaged"}
    return action in APPLY_ACTIONS and current in {"candidate_duplicate", "unmanaged", "blocked", "local_only"}


def _plan_reason(current: str, action: str, existing_record: ClassificationRecord | None) -> str:
    if existing_record:
        return f"existing registry classification: {existing_record.classification}; requested action: {action}"
    if action in PLAN_ONLY_ACTIONS:
        return f"{action} is plan-only in this slice and will not mutate target files"
    if action == "propose-import":
        return f"would create a target-read-only import proposal for current {current} target entry"
    if action == "local-only":
        return f"would record current {current} target entry as intentionally local-only"
    if action == "keep-blocked":
        return f"would record current {current} target entry as intentionally blocked"
    return f"current classification is {current}"


def _record_path(target: TargetConfig, skill_id: str):
    from .state import classification_record_path

    return classification_record_path(target, skill_id)


def _apply_adopt(
    target: TargetConfig,
    skill: SkillPackage,
    plan: ReconcilePlan,
    *,
    reason: str,
    adopted_by: str,
    target_path: Path,
) -> ReconcileApplyResult:
    adopted_by = adopted_by.strip() or "skill-registry"
    eligibility = _adopt_eligibility(target, skill, plan.current_classification, target_path)
    if not eligibility.can_apply:
        raise ValueError(eligibility.reason)

    record = DeploymentRecord(
        schema_version=DEPLOYMENT_RECORD_SCHEMA_VERSION,
        target=target.name,
        skill_id=skill.id,
        deployment_mode=target.deploy_mode,
        source_path=str(skill.path),
        target_path=str(target_path),
        payload_hash=eligibility.payload_hash,
        file_count=eligibility.file_count,
        files=eligibility.files,
        deployed_at=datetime.now(timezone.utc).isoformat(),
        deployed_by=adopted_by,
        operation="adopt",
        reason=reason,
    )
    save_deployment_record(target, record)

    return ReconcileApplyResult(
        target=target.name,
        skill_id=skill.id,
        action="adopt",
        classification=plan.current_classification,
        target_path=str(target_path),
        reason=reason,
        mutates_target_files=False,
        record_path=str(deployment_record_path(target, skill.id)),
        payload_hash=eligibility.payload_hash,
        file_count=eligibility.file_count,
    )


def _apply_replace(
    target: TargetConfig,
    skill: SkillPackage,
    plan: ReconcilePlan,
    *,
    reason: str,
    replaced_by: str,
    target_path: Path,
) -> ReconcileApplyResult:
    replaced_by = replaced_by.strip() or "skill-registry"
    eligibility = _replace_eligibility(target, skill, plan.current_classification, target_path)
    if not eligibility.can_apply:
        raise ValueError(eligibility.reason)

    source_path = skill.path.resolve(strict=False)
    final_mode = target.deploy_mode
    temp_path = _temporary_sibling(target_path)
    backup = None
    try:
        _create_temp_entry(temp_path, source_path, eligibility, final_mode)
        backup = _create_managed_backup(
            target,
            skill,
            operation="replace_unmanaged",
            source_path=source_path,
            target_path=target_path,
            deployment_mode=final_mode,
            actor=replaced_by,
            reason=reason,
        )
        _replace_target_from_temp(target_path, temp_path, backup)
        record = DeploymentRecord(
            schema_version=DEPLOYMENT_RECORD_SCHEMA_VERSION,
            target=target.name,
            skill_id=skill.id,
            deployment_mode=final_mode,
            source_path=str(source_path),
            target_path=str(target_path),
            payload_hash=eligibility.payload_hash,
            file_count=eligibility.file_count,
            files=eligibility.files,
            deployed_at=datetime.now(timezone.utc).isoformat(),
            deployed_by=replaced_by,
            operation="replace_unmanaged",
            reason=reason,
        )
        try:
            save_deployment_record(target, record)
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
        raise ValueError(f"replace failed for {skill.id} -> {target.name}:{backup_hint} {exc}") from exc

    return ReconcileApplyResult(
        target=target.name,
        skill_id=skill.id,
        action="replace",
        classification=plan.current_classification,
        target_path=str(target_path),
        reason=reason,
        mutates_target_files=True,
        record_path=str(deployment_record_path(target, skill.id)),
        payload_hash=eligibility.payload_hash,
        file_count=eligibility.file_count,
        backup_path=str(backup.path),
    )


def _adopt_eligibility(target: TargetConfig, skill: SkillPackage, current: str, target_path: Path) -> AdoptEligibility:
    default_target_path = target.root_path / skill.id
    if target_path.resolve(strict=False) != default_target_path.resolve(strict=False):
        return AdoptEligibility(
            False,
            "adopt is only supported for the target's default skill path; use propose-import for nested source paths",
        )
    if current not in {"candidate_duplicate", "unmanaged"}:
        return AdoptEligibility(False, f"adopt requires an unmanaged target directory; current classification is {current}")
    comparison_path = target_path
    if target_path.is_symlink():
        try:
            comparison_path = target_path.resolve(strict=True)
        except FileNotFoundError:
            return AdoptEligibility(False, "adopt requires an existing target directory; target path is a broken symlink")
        if not comparison_path.is_dir():
            return AdoptEligibility(False, "adopt requires an existing target directory; symlink target is not a directory")
    if not target_path.exists():
        return AdoptEligibility(False, "adopt requires an existing target directory; target path is absent")
    if not target_path.is_dir():
        return AdoptEligibility(False, "adopt requires an existing target directory; target path is not a directory")

    try:
        existing_record = load_deployment_record(target, skill.id)
    except DeploymentRecordError as exc:
        return AdoptEligibility(False, f"adopt blocked by malformed deployment record: {exc}")
    if existing_record is not None:
        return AdoptEligibility(
            False,
            f"adopt blocked by existing deployment record: {deployment_record_path(target, skill.id)}",
        )

    try:
        canonical_payload = deployable_payload_hash(skill.path)
    except PayloadHashError as exc:
        return AdoptEligibility(False, f"canonical payload failed comparison: {exc}")
    try:
        target_payload = deployable_payload_hash(comparison_path)
    except PayloadHashError as exc:
        return AdoptEligibility(False, f"target payload failed comparison: {exc}")

    if target_payload.payload_hash != canonical_payload.payload_hash:
        return AdoptEligibility(
            False,
            "adopt requires exact payload match; "
            f"canonical payload hash {canonical_payload.payload_hash} "
            f"differs from target payload hash {target_payload.payload_hash}",
        )

    return AdoptEligibility(
        True,
        "target deployable payload matches canonical payload; adoption would write deployment record only",
        payload_hash=canonical_payload.payload_hash,
        file_count=canonical_payload.file_count,
        files=canonical_payload.files,
    )


def _replace_eligibility(target: TargetConfig, skill: SkillPackage, current: str, target_path: Path) -> ReplaceEligibility:
    default_target_path = target.root_path / skill.id
    if target_path.resolve(strict=False) != default_target_path.resolve(strict=False):
        return ReplaceEligibility(
            False,
            "replace is only supported for the target's default skill path; use propose-import for nested source paths",
        )
    if current not in {"candidate_duplicate", "unmanaged"}:
        return ReplaceEligibility(False, f"replace requires an unmanaged target entry; current classification is {current}")
    if target.deploy_mode == "symlink" and not target.supports_symlink:
        return ReplaceEligibility(False, "replace cannot use symlink mode because target does not support symlinks")
    if not target_path.exists() and not target_path.is_symlink():
        return ReplaceEligibility(False, "replace requires an existing target entry; use deploy for absent targets")

    try:
        existing_record = load_deployment_record(target, skill.id)
    except DeploymentRecordError as exc:
        return ReplaceEligibility(False, f"replace blocked by malformed deployment record: {exc}")
    if existing_record is not None:
        return ReplaceEligibility(
            False,
            f"replace blocked by existing deployment record: {deployment_record_path(target, skill.id)}; use deploy repair/refresh",
        )

    try:
        canonical_payload = deployable_payload_hash(skill.path)
    except PayloadHashError as exc:
        return ReplaceEligibility(False, f"canonical payload failed replacement: {exc}")

    return ReplaceEligibility(
        True,
        f"would back up unmanaged target entry and replace it with canonical {target.deploy_mode} deployment",
        payload_hash=canonical_payload.payload_hash,
        file_count=canonical_payload.file_count,
        files=canonical_payload.files,
    )


def _resolve_target_skill_path(target: TargetConfig, skill: SkillPackage, source_path: str) -> Path:
    if not source_path:
        return target.root_path / skill.id

    raw_path = Path(source_path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else target.root_path / raw_path
    target_root = target.root_path.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(target_root)
    except ValueError as exc:
        raise ValueError(f"source path must stay under target root {target.root_path}: {candidate}") from exc
    if candidate.name != skill.id:
        raise ValueError(f"source path basename must match skill id {skill.id}: {candidate}")
    return candidate_resolved
