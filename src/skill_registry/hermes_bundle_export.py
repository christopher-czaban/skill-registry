from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import RegistryConfig, SkillSet, TargetConfig
from .skills import SkillPackage, list_registry_skills
from .status import check_status


HERMES_GLOBAL_TARGET = "hermes-global"


class _IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


@dataclass(frozen=True)
class HermesBundleExportResult:
    ok: bool
    dry_run: bool
    operation: str
    skill_set_id: str
    target: str
    bundle_path: str
    slug: str
    skills: tuple[str, ...]
    yaml_sha256: str
    reason: str
    exported_by: str


def export_hermes_bundle(
    config: RegistryConfig,
    *,
    skill_set_id: str,
    target_name: str,
    dry_run: bool,
    instruction: str = "",
    reason: str = "",
    exported_by: str = "skill-registry",
) -> HermesBundleExportResult:
    blocked = _validate_export(config, skill_set_id=skill_set_id, target_name=target_name)
    if blocked:
        return _blocked_result(
            dry_run=dry_run,
            skill_set_id=skill_set_id,
            target_name=target_name,
            reason=blocked,
            exported_by=exported_by,
        )

    skill_set = config.skill_set_by_id[skill_set_id]
    target = config.target_by_name[target_name]
    bundle_path = hermes_bundle_path(target, skill_set.id)
    yaml_text = render_hermes_bundle_yaml(skill_set, instruction=instruction)
    yaml_sha256 = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()

    if bundle_path.exists() or bundle_path.is_symlink():
        return _blocked_result(
            dry_run=dry_run,
            skill_set_id=skill_set.id,
            target_name=target.name,
            reason=f"Bundle file already exists: {bundle_path}",
            exported_by=exported_by,
            bundle_path=str(bundle_path),
            slug=skill_set.id,
            skills=skill_set.members,
            yaml_sha256=yaml_sha256,
        )

    if dry_run:
        return HermesBundleExportResult(
            ok=True,
            dry_run=True,
            operation="would_create",
            skill_set_id=skill_set.id,
            target=target.name,
            bundle_path=str(bundle_path),
            slug=skill_set.id,
            skills=skill_set.members,
            yaml_sha256=yaml_sha256,
            reason=reason,
            exported_by=exported_by,
        )

    try:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with bundle_path.open("x", encoding="utf-8") as handle:
            handle.write(yaml_text)
    except FileExistsError:
        return _blocked_result(
            dry_run=False,
            skill_set_id=skill_set.id,
            target_name=target.name,
            reason=f"Bundle file already exists: {bundle_path}",
            exported_by=exported_by,
            bundle_path=str(bundle_path),
            slug=skill_set.id,
            skills=skill_set.members,
            yaml_sha256=yaml_sha256,
        )
    except OSError as exc:
        return _blocked_result(
            dry_run=False,
            skill_set_id=skill_set.id,
            target_name=target.name,
            reason=f"Could not write bundle file {bundle_path}: {exc}",
            exported_by=exported_by,
            bundle_path=str(bundle_path),
            slug=skill_set.id,
            skills=skill_set.members,
            yaml_sha256=yaml_sha256,
        )

    return HermesBundleExportResult(
        ok=True,
        dry_run=False,
        operation="created",
        skill_set_id=skill_set.id,
        target=target.name,
        bundle_path=str(bundle_path),
        slug=skill_set.id,
        skills=skill_set.members,
        yaml_sha256=yaml_sha256,
        reason=reason,
        exported_by=exported_by,
    )


def hermes_bundle_path(target: TargetConfig, slug: str) -> Path:
    parts = target.root_path.parts
    if "skills" in parts:
        skills_index = parts.index("skills")
        hermes_home = Path(*parts[:skills_index])
    else:
        hermes_home = target.root_path.parent
    return hermes_home / "skill-bundles" / f"{slug}.yaml"


def render_hermes_bundle_yaml(skill_set: SkillSet, *, instruction: str = "") -> str:
    name = skill_set.title or skill_set.id
    data: dict[str, object] = {
        "name": name,
        "description": name,
        "skills": list(skill_set.members),
    }
    if instruction.strip():
        data["instruction"] = instruction.strip()
    return yaml.dump(data, Dumper=_IndentedSafeDumper, sort_keys=False, default_flow_style=False).rstrip("\n") + "\n"


def _validate_export(config: RegistryConfig, *, skill_set_id: str, target_name: str) -> str:
    skill_set = config.skill_set_by_id.get(skill_set_id)
    if skill_set is None:
        return f"Unknown skill set: {skill_set_id}"

    target = config.target_by_name.get(target_name)
    if target is None:
        return f"Unknown target: {target_name}"
    if target.system != "hermes":
        return f"Target {target.name} must use system hermes; got {target.system}"
    hermes_global = config.target_by_name.get(HERMES_GLOBAL_TARGET)
    if hermes_global is None:
        return f"Hermes bundle export requires registered target {HERMES_GLOBAL_TARGET}"
    if target.name != HERMES_GLOBAL_TARGET:
        global_root = hermes_global.root_path.resolve(strict=False)
        target_root = target.root_path.resolve(strict=False)
        try:
            target_root.relative_to(global_root)
        except ValueError:
            return (
                f"Hermes bundle export target must be {HERMES_GLOBAL_TARGET} "
                f"or a subtarget under {global_root}; got {target.name}"
            )

    skill_by_id = {skill.id: skill for skill in list_registry_skills(config)}
    missing = tuple(member for member in skill_set.members if member not in skill_by_id)
    if missing:
        return f"Unknown skill-set member(s): {', '.join(missing)}"
    retired = tuple(member for member in skill_set.members if skill_by_id[member].lifecycle == "retired")
    if retired:
        return f"Retired skill-set member(s) cannot be exported: {', '.join(retired)}"

    not_applicable = tuple(
        member for member in skill_set.members if not _skill_applies_to_target(skill_by_id[member], target)
    )
    if not_applicable:
        return f"Skill-set member(s) not applicable to {target.name}: {', '.join(not_applicable)}"

    for member in skill_set.members:
        statuses = check_status(config, target_names=(target.name,), skill_ids=(member,))
        if not statuses:
            return f"Skill-set member {member} has no deployment status for {target.name}"
        status = statuses[0]
        if status.status != "in_sync":
            return (
                f"Skill-set member {member} is not in_sync on {target.name}: "
                f"{status.status} ({status.reason})"
            )

    return ""


def _skill_applies_to_target(skill: SkillPackage, target: TargetConfig) -> bool:
    return not skill.targets or target.name in skill.targets


def _blocked_result(
    *,
    dry_run: bool,
    skill_set_id: str,
    target_name: str,
    reason: str,
    exported_by: str,
    bundle_path: str = "",
    slug: str = "",
    skills: tuple[str, ...] = (),
    yaml_sha256: str = "",
) -> HermesBundleExportResult:
    return HermesBundleExportResult(
        ok=False,
        dry_run=dry_run,
        operation="blocked",
        skill_set_id=skill_set_id,
        target=target_name,
        bundle_path=bundle_path,
        slug=slug,
        skills=skills,
        yaml_sha256=yaml_sha256,
        reason=reason,
        exported_by=exported_by,
    )
