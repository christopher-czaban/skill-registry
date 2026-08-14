from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import RegistryConfig, TargetConfig
from .skills import SkillPackage, list_registry_skills
from .state import DeploymentRecord, DeploymentRecordError, load_deployment_record


SUPPORTED_OPERATIONS = ("create", "edit", "patch", "delete", "rename", "move", "chmod", "copy")
GUARDRAIL_MODES = ("strict", "warn", "off")


class GuardrailConfigError(ValueError):
    """Raised when guardrail state cannot be trusted for a decision."""


@dataclass(frozen=True)
class GuardrailDecision:
    ok: bool
    decision: str
    mode: str
    operation: str
    path: str
    resolved_path: str
    target: str | None
    skill_id: str | None
    target_path: str | None
    canonical_path: str | None
    deployment_mode: str | None
    reason: str
    recommended_action: str


@dataclass(frozen=True)
class _TargetMatch:
    target: TargetConfig
    root_path: Path
    skill_id: str | None
    target_path: Path | None


def check_guardrail(
    config: RegistryConfig,
    *,
    operation: str,
    path: str,
    mode: str = "strict",
) -> GuardrailDecision:
    if operation not in SUPPORTED_OPERATIONS:
        raise ValueError(f"unknown guard operation: {operation}")
    if mode not in GUARDRAIL_MODES:
        raise ValueError(f"invalid guard mode: {mode}")

    _validate_targets(config)

    attempted_path = _absolute_path(path)
    resolved_path = _resolved_path(attempted_path)
    skills = list_registry_skills(config)
    skill_by_id = {skill.id: skill for skill in skills}
    canonical_by_path = {skill.path.resolve(strict=False): skill for skill in skills}

    target_match = _best_target_match(config, attempted_path)
    if target_match and target_match.skill_id and target_match.target_path:
        return _target_entry_decision(
            operation=operation,
            mode=mode,
            original_path=path,
            attempted_path=attempted_path,
            resolved_path=resolved_path,
            match=target_match,
            skill_by_id=skill_by_id,
            canonical_by_path=canonical_by_path,
        )

    registry_skill = _canonical_skill_for_path(attempted_path, skills) or _canonical_skill_for_path(resolved_path, skills)
    if registry_skill or _is_relative_to(attempted_path, config.root) or _is_relative_to(resolved_path, config.root):
        return GuardrailDecision(
            ok=True,
            decision="allow",
            mode=mode,
            operation=operation,
            path=path,
            resolved_path=str(resolved_path),
            target=None,
            skill_id=registry_skill.id if registry_skill else None,
            target_path=None,
            canonical_path=str(registry_skill.path) if registry_skill else None,
            deployment_mode=None,
            reason="original path is under the registry root",
            recommended_action="Edit the canonical registry package and run validation before deployment.",
        )

    return GuardrailDecision(
        ok=True,
        decision="allow",
        mode=mode,
        operation=operation,
        path=path,
        resolved_path=str(resolved_path),
        target=None,
        skill_id=None,
        target_path=None,
        canonical_path=None,
        deployment_mode=None,
        reason="path is outside configured target roots",
        recommended_action="No registry action is required.",
    )


def _target_entry_decision(
    *,
    operation: str,
    mode: str,
    original_path: str,
    attempted_path: Path,
    resolved_path: Path,
    match: _TargetMatch,
    skill_by_id: dict[str, SkillPackage],
    canonical_by_path: dict[Path, SkillPackage],
) -> GuardrailDecision:
    target = match.target
    skill_id = match.skill_id
    target_path = match.target_path
    assert skill_id is not None
    assert target_path is not None

    record = _load_relevant_record(target, skill_id, target_path)
    if record:
        canonical_path = _record_canonical_path(record, skill_by_id)
        return _managed_decision(
            operation=operation,
            mode=mode,
            original_path=original_path,
            resolved_path=resolved_path,
            target=target,
            skill_id=skill_id,
            target_path=target_path,
            canonical_path=canonical_path,
            deployment_mode=record.deployment_mode,
            reason=f"path is under registry-managed {record.deployment_mode} deployment",
        )

    if target_path.is_symlink():
        try:
            target_realpath = target_path.resolve(strict=True)
        except FileNotFoundError:
            return _unmanaged_warning(
                operation=operation,
                mode=mode,
                original_path=original_path,
                resolved_path=resolved_path,
                target=target,
                skill_id=skill_id,
                target_path=target_path,
                reason="target entry is a broken symlink and is not registry-managed",
            )

        canonical_skill = canonical_by_path.get(target_realpath)
        if canonical_skill:
            return _managed_decision(
                operation=operation,
                mode=mode,
                original_path=original_path,
                resolved_path=resolved_path,
                target=target,
                skill_id=canonical_skill.id,
                target_path=target_path,
                canonical_path=canonical_skill.path,
                deployment_mode="symlink",
                reason="target symlink points at a canonical registry package",
            )

        return _unmanaged_warning(
            operation=operation,
            mode=mode,
            original_path=original_path,
            resolved_path=resolved_path,
            target=target,
            skill_id=skill_id,
            target_path=target_path,
            reason=f"target symlink points outside the registry: {target_realpath}",
        )

    if _looks_like_unmanaged_skill(attempted_path, target_path, skill_id, skill_by_id):
        return _unmanaged_warning(
            operation=operation,
            mode=mode,
            original_path=original_path,
            resolved_path=resolved_path,
            target=target,
            skill_id=skill_id,
            target_path=target_path,
            reason="path is under an unmanaged skill-like target entry",
        )

    return GuardrailDecision(
        ok=True,
        decision="allow",
        mode=mode,
        operation=operation,
        path=original_path,
        resolved_path=str(resolved_path),
        target=target.name,
        skill_id=skill_id,
        target_path=str(target_path),
        canonical_path=None,
        deployment_mode=None,
        reason="path is under a configured target root, but no managed or skill-like target entry matched",
        recommended_action="No registry action is required.",
    )


def _managed_decision(
    *,
    operation: str,
    mode: str,
    original_path: str,
    resolved_path: Path,
    target: TargetConfig,
    skill_id: str,
    target_path: Path,
    canonical_path: Path,
    deployment_mode: str,
    reason: str,
) -> GuardrailDecision:
    if mode == "strict":
        decision = "block"
        ok = False
    elif mode == "warn":
        decision = "warn"
        ok = True
    else:
        decision = "allow"
        ok = True

    if mode == "off":
        reason = f"{reason}; guardrail enforcement is off"

    return GuardrailDecision(
        ok=ok,
        decision=decision,
        mode=mode,
        operation=operation,
        path=original_path,
        resolved_path=str(resolved_path),
        target=target.name,
        skill_id=skill_id,
        target_path=str(target_path),
        canonical_path=str(canonical_path),
        deployment_mode=deployment_mode,
        reason=reason,
        recommended_action=(
            f"Edit the canonical package at {canonical_path}, or create a reviewed proposal before redeploying."
        ),
    )


def _unmanaged_warning(
    *,
    operation: str,
    mode: str,
    original_path: str,
    resolved_path: Path,
    target: TargetConfig,
    skill_id: str,
    target_path: Path,
    reason: str,
) -> GuardrailDecision:
    if mode == "off":
        decision = "allow"
        ok = True
        reason = f"{reason}; guardrail enforcement is off"
    else:
        decision = "warn"
        ok = True

    return GuardrailDecision(
        ok=ok,
        decision=decision,
        mode=mode,
        operation=operation,
        path=original_path,
        resolved_path=str(resolved_path),
        target=target.name,
        skill_id=skill_id,
        target_path=str(target_path),
        canonical_path=None,
        deployment_mode=None,
        reason=reason,
        recommended_action=(
            f"Review with skill-registry discover --target {target.name} before treating this target entry as managed."
        ),
    )


def _load_relevant_record(target: TargetConfig, skill_id: str, target_path: Path) -> DeploymentRecord | None:
    try:
        record = load_deployment_record(target, skill_id)
    except DeploymentRecordError as exc:
        raise GuardrailConfigError(f"deployment record is malformed: {exc}") from exc
    if record is None:
        return None
    if record.target != target.name:
        raise GuardrailConfigError(f"deployment record target mismatch: expected {target.name}, found {record.target}")
    if record.skill_id != skill_id:
        raise GuardrailConfigError(f"deployment record skill_id mismatch: expected {skill_id}, found {record.skill_id}")
    recorded_target = _absolute_path(record.target_path)
    if recorded_target != target_path:
        raise GuardrailConfigError(
            f"deployment record target_path mismatch: expected {target_path}, found {recorded_target}"
        )
    return record


def _record_canonical_path(record: DeploymentRecord, skill_by_id: dict[str, SkillPackage]) -> Path:
    skill = skill_by_id.get(record.skill_id)
    if skill:
        return skill.path
    return Path(record.source_path).expanduser().resolve(strict=False)


def _looks_like_unmanaged_skill(
    attempted_path: Path,
    target_path: Path,
    skill_id: str,
    skill_by_id: dict[str, SkillPackage],
) -> bool:
    if skill_id in skill_by_id:
        return True
    if target_path.is_dir() and ((target_path / "SKILL.md").exists() or (target_path / "skill.yaml").exists()):
        return True
    return attempted_path.name in {"SKILL.md", "skill.yaml"}


def _canonical_skill_for_path(path: Path, skills: tuple[SkillPackage, ...]) -> SkillPackage | None:
    matches = [skill for skill in skills if _is_relative_to(path, skill.path)]
    if not matches:
        return None
    return max(matches, key=lambda skill: len(skill.path.parts))


def _best_target_match(config: RegistryConfig, path: Path) -> _TargetMatch | None:
    matches: list[_TargetMatch] = []
    for target in config.targets:
        match_root = _matching_target_root(path, target)
        if match_root is None:
            continue
        root = target.root_path
        relative = path.relative_to(match_root)
        skill_id = relative.parts[0] if relative.parts else None
        target_path = root / skill_id if skill_id else None
        matches.append(_TargetMatch(target=target, root_path=match_root, skill_id=skill_id, target_path=target_path))
    if not matches:
        return None
    return max(matches, key=lambda match: len(match.root_path.parts))


def _matching_target_root(path: Path, target: TargetConfig) -> Path | None:
    for root in (_absolute_path(target.root), target.root_path):
        if _is_relative_to(path, root):
            return root
    return None


def _absolute_path(path: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(path)))


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_targets(config: RegistryConfig) -> None:
    for target in config.targets:
        if not target.name.strip():
            raise GuardrailConfigError("target config contains an empty target name")
        if not target.root.strip():
            raise GuardrailConfigError(f"target config has empty root: {target.name}")
