from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import RegistryConfig, TargetConfig
from .hashing import PayloadHashError, deployable_payload_hash
from .skills import SkillPackage, selected_skills
from .state import (
    DeploymentRecordError,
    deployment_record_validation_error,
    load_classification,
    load_deployment_record,
)
from .targets import selected_targets

HEALTHY_STATUSES = {"in_sync", "not_applicable", "local_only"}


@dataclass(frozen=True)
class DeploymentStatus:
    target: str
    skill_id: str
    deploy_mode: str
    status: str
    source_path: str
    target_path: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.status in HEALTHY_STATUSES


@dataclass(frozen=True)
class RecordedDirectoryHealth:
    status: str
    reason: str
    deployment_mode: str = ""


def check_status(
    config: RegistryConfig,
    target_names: tuple[str, ...] = (),
    skill_ids: tuple[str, ...] = (),
) -> tuple[DeploymentStatus, ...]:
    statuses: list[DeploymentStatus] = []
    targets = selected_targets(config, target_names)
    skills = selected_skills(config, skill_ids)

    for target in targets:
        for skill in skills:
            if not _skill_applies_to_target(skill, target):
                continue
            statuses.append(_check_target_skill(target, skill))
    return tuple(statuses)


def status_ok(statuses: tuple[DeploymentStatus, ...]) -> bool:
    return all(item.ok for item in statuses)


def _skill_applies_to_target(skill: SkillPackage, target: TargetConfig) -> bool:
    return not skill.targets or target.name in skill.targets


def _check_target_skill(target: TargetConfig, skill: SkillPackage) -> DeploymentStatus:
    root = target.root_path
    target_path = root / skill.id
    record = load_classification(target, skill.id)
    if record and record.classification == "local_only":
        return _status(target, skill, "local_only", str(target_path), record.reason)
    if record and record.classification == "blocked":
        return _status(target, skill, "blocked", str(target_path), record.reason)

    if not root.exists():
        recorded_health = check_recorded_directory_health(target, skill, target_path)
        if recorded_health:
            return _status(
                target,
                skill,
                recorded_health.status,
                str(target_path),
                recorded_health.reason,
                deploy_mode=recorded_health.deployment_mode,
            )
        return _status(target, skill, "target_missing", str(target_path), "target root does not exist")

    if target_path.is_symlink():
        symlink_record_error = _deployment_record_proof_error(target, skill, target_path)
        if symlink_record_error:
            return _status(target, skill, "blocked", str(target_path), symlink_record_error)
        if not target_path.exists():
            return _status(target, skill, "broken_link", str(target_path), "target entry is a broken symlink", deploy_mode="symlink")
        resolved = target_path.resolve(strict=True)
        if resolved == skill.path:
            return _status(
                target,
                skill,
                "in_sync",
                str(target_path),
                "symlink points at canonical registry skill",
                deploy_mode="symlink",
            )
        return _status(target, skill, "wrong_link", str(target_path), f"symlink points at {resolved}", deploy_mode="symlink")

    if not target_path.exists():
        recorded_health = check_recorded_directory_health(target, skill, target_path)
        if recorded_health:
            return _status(
                target,
                skill,
                recorded_health.status,
                str(target_path),
                recorded_health.reason,
                deploy_mode=recorded_health.deployment_mode,
            )
        return _status(target, skill, "missing", str(target_path), "target entry is not deployed")

    recorded_health = check_recorded_directory_health(target, skill, target_path)
    if recorded_health:
        return _status(
            target,
            skill,
            recorded_health.status,
            str(target_path),
            recorded_health.reason,
            deploy_mode=recorded_health.deployment_mode,
        )

    return _status(target, skill, "blocked", str(target_path), "target path exists and is not registry-managed")


def _deployment_record_proof_error(target: TargetConfig, skill: SkillPackage, target_path: Path) -> str:
    try:
        record = load_deployment_record(target, skill.id)
    except DeploymentRecordError as exc:
        return f"deployment record is malformed: {exc}"
    if record is None:
        return "target symlink is not registry-managed"

    validation_error = deployment_record_validation_error(
        record,
        target=target,
        skill_id=skill.id,
        source_path=skill.path,
        target_path=target_path,
    )
    return validation_error


def check_recorded_directory_health(
    target: TargetConfig,
    skill: SkillPackage,
    target_path: Path,
) -> RecordedDirectoryHealth | None:
    try:
        record = load_deployment_record(target, skill.id)
    except DeploymentRecordError as exc:
        return RecordedDirectoryHealth("blocked", f"deployment record is malformed: {exc}")
    if record is None:
        return None

    validation_error = deployment_record_validation_error(
        record,
        target=target,
        skill_id=skill.id,
        source_path=skill.path,
        target_path=target_path,
    )
    if validation_error:
        return RecordedDirectoryHealth("blocked", validation_error, deployment_mode=record.deployment_mode)

    try:
        target_payload = deployable_payload_hash(target_path)
    except PayloadHashError as exc:
        return RecordedDirectoryHealth("blocked", f"target payload failed comparison: {exc}", deployment_mode=record.deployment_mode)
    if target_payload.payload_hash != record.payload_hash:
        return RecordedDirectoryHealth("drifted", "target payload hash differs from deployment record", deployment_mode=record.deployment_mode)

    try:
        canonical_payload = deployable_payload_hash(skill.path)
    except PayloadHashError as exc:
        return RecordedDirectoryHealth("blocked", f"canonical payload failed comparison: {exc}", deployment_mode=record.deployment_mode)
    if canonical_payload.payload_hash != record.payload_hash:
        return RecordedDirectoryHealth("stale", "canonical payload hash differs from deployment record", deployment_mode=record.deployment_mode)

    return RecordedDirectoryHealth(
        "in_sync",
        "target payload matches deployment record and canonical payload",
        deployment_mode=record.deployment_mode,
    )


def _status(
    target: TargetConfig,
    skill: SkillPackage,
    status: str,
    target_path: str,
    reason: str,
    deploy_mode: str = "",
) -> DeploymentStatus:
    return DeploymentStatus(
        target=target.name,
        skill_id=skill.id,
        deploy_mode=deploy_mode or target.deploy_mode,
        status=status,
        source_path=str(skill.path),
        target_path=target_path,
        reason=reason,
    )
