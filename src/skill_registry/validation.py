from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, RegistryConfig, TargetConfig
from .skills import SkillPackage, list_registry_skills


SUPPORTED_ORIGIN_KINDS = frozenset({"external_skill", "local_zip", "package_manager", "manual"})
RETRIEVED_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    diagnostics: tuple[Diagnostic, ...]

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_registry(config: RegistryConfig) -> ValidationResult:
    diagnostics: list[Diagnostic] = []

    _validate_root_dirs(config, diagnostics)
    skills = _load_registry_skills(config, diagnostics)
    if skills is not None:
        for skill in skills:
            if skill.lifecycle not in {"canonical", "placeholder", "retired"}:
                diagnostics.append(_error("invalid_skill_lifecycle", f"Skill {skill.id} has unsupported lifecycle: {skill.lifecycle}"))
        _validate_skill_origins(skills, diagnostics)
        _validate_skill_dependencies(skills, diagnostics)
        _validate_tool_wrappers(skills, diagnostics)
        _validate_skill_sets(config, skills, diagnostics)
    _validate_runtime_profiles(config, diagnostics)
    _validate_targets(config, diagnostics)
    _validate_runtime_dependencies(config, diagnostics)

    return ValidationResult(tuple(diagnostics))


def _validate_root_dirs(config: RegistryConfig, diagnostics: list[Diagnostic]) -> None:
    if not config.loaded_files:
        diagnostics.append(_error("missing_config", "No registry config files found."))

    if not config.skills_dir.exists():
        diagnostics.append(_warning("missing_skills_dir", f"Skills directory does not exist: {config.skills_dir}"))
    elif not config.skills_dir.is_dir():
        diagnostics.append(_error("invalid_skills_dir", f"Skills path is not a directory: {config.skills_dir}"))

    if not config.targets_dir.exists():
        diagnostics.append(_warning("missing_targets_dir", f"Targets directory does not exist: {config.targets_dir}"))
    elif not config.targets_dir.is_dir():
        diagnostics.append(_error("invalid_targets_dir", f"Targets path is not a directory: {config.targets_dir}"))


def _validate_runtime_profiles(config: RegistryConfig, diagnostics: list[Diagnostic]) -> None:
    if not config.runtime_profiles:
        if config.targets:
            diagnostics.append(_error("missing_runtime_profiles", "No runtime profiles declared in runtimes.yaml."))
        return

    seen: set[str] = set()
    for profile in config.runtime_profiles:
        if not profile.name:
            diagnostics.append(_error("runtime_missing_name", "Runtime profile is missing a name."))
            continue
        if profile.name in seen:
            diagnostics.append(_error("duplicate_runtime_profile", f"Duplicate runtime profile: {profile.name}"))
        seen.add(profile.name)

        if profile.probe_strategy != "path-list":
            diagnostics.append(
                _error(
                    "unsupported_probe_strategy",
                    f"Runtime profile {profile.name} uses unsupported probe strategy: {profile.probe_strategy}",
                )
            )


def _validate_targets(config: RegistryConfig, diagnostics: list[Diagnostic]) -> None:
    seen: set[str] = set()
    runtime_names = set(config.runtime_by_name)

    for target in config.targets:
        if not target.name:
            diagnostics.append(_error("target_missing_name", "Target config is missing a name."))
            continue
        if target.name in seen:
            diagnostics.append(_error("duplicate_target", f"Duplicate target config: {target.name}"))
        seen.add(target.name)

        _validate_target(target, runtime_names, diagnostics)


def _validate_target(target: TargetConfig, runtime_names: set[str], diagnostics: list[Diagnostic]) -> None:
    if not target.system:
        diagnostics.append(_error("target_missing_system", f"Target {target.name} is missing system."))
    if not target.root:
        diagnostics.append(_error("target_missing_root", f"Target {target.name} is missing root."))
    if target.deploy_mode not in {"symlink", "copy"}:
        diagnostics.append(
            _error(
                "target_invalid_deploy_mode",
                f"Target {target.name} has invalid deploy_mode: {target.deploy_mode}",
            )
        )
    for runtime in target.runtime_profiles:
        if runtime not in runtime_names:
            diagnostics.append(
                _error(
                    "unknown_runtime_profile",
                    f"Target {target.name} references unknown runtime profile: {runtime}",
                )
            )


def _validate_runtime_dependencies(config: RegistryConfig, diagnostics: list[Diagnostic]) -> None:
    runtime_names = set(config.runtime_by_name)

    for dependency in config.runtime_dependencies.bins:
        if not dependency.name:
            diagnostics.append(_error("bin_missing_name", "Runtime bin dependency is missing a name."))
        for runtime in dependency.target_runtimes:
            if runtime not in runtime_names:
                diagnostics.append(
                    _error(
                        "unknown_runtime_profile",
                        f"Bin dependency {dependency.name} references unknown runtime profile: {runtime}",
                    )
                )
        for skill_id in dependency.required_by_skills:
            if not (config.skills_dir / skill_id).exists():
                diagnostics.append(
                    _warning(
                        "missing_required_skill",
                        f"Bin dependency {dependency.name} references skill not present in registry yet: {skill_id}",
                    )
                )

    for dependency in config.runtime_dependencies.env:
        if not dependency.name:
            diagnostics.append(_error("env_missing_name", "Runtime env dependency is missing a name."))
        for runtime in dependency.target_runtimes:
            if runtime not in runtime_names:
                diagnostics.append(
                    _error(
                        "unknown_runtime_profile",
                        f"Env dependency {dependency.name} references unknown runtime profile: {runtime}",
                    )
                )


def _load_registry_skills(config: RegistryConfig, diagnostics: list[Diagnostic]) -> tuple[SkillPackage, ...] | None:
    try:
        return list_registry_skills(config)
    except ConfigError as exc:
        diagnostics.append(_error("invalid_skill_metadata", str(exc)))
        return None


def _validate_skill_origins(skills: tuple[SkillPackage, ...], diagnostics: list[Diagnostic]) -> None:
    for skill in skills:
        _validate_skill_origin(skill, diagnostics)


def _validate_skill_dependencies(skills: tuple[SkillPackage, ...], diagnostics: list[Diagnostic]) -> None:
    known_ids = {skill.id for skill in skills}
    retired_ids = {skill.id for skill in skills if skill.lifecycle == "retired"}
    for skill in skills:
        seen: set[str] = set()
        for dependency in skill.skill_dependencies:
            if skill.lifecycle != "retired" and dependency in retired_ids:
                diagnostics.append(_error("retired_skill_dependency", f"Active skill {skill.id} depends on retired skill: {dependency}"))
            if not dependency:
                diagnostics.append(_error("skill_dependency_missing_id", f"Skill {skill.id} has an empty skill dependency."))
                continue
            if dependency == skill.id:
                diagnostics.append(_error("skill_dependency_self_reference", f"Skill {skill.id} depends on itself."))
            if dependency in seen:
                diagnostics.append(
                    _error("duplicate_skill_dependency", f"Skill {skill.id} declares duplicate dependency: {dependency}")
                )
            seen.add(dependency)
            if dependency not in known_ids:
                diagnostics.append(
                    _error("unknown_skill_dependency", f"Skill {skill.id} depends on unknown skill: {dependency}")
                )


def _validate_skill_sets(
    config: RegistryConfig,
    skills: tuple[SkillPackage, ...],
    diagnostics: list[Diagnostic],
) -> None:
    known_skill_ids = {skill.id for skill in skills}
    retired_ids = {skill.id for skill in skills if skill.lifecycle == "retired"}
    known_target_names = set(config.target_by_name)
    seen_sets: set[str] = set()

    for skill_set in config.skill_sets:
        label = skill_set.id or "<unknown>"
        if not skill_set.id:
            diagnostics.append(_error("skill_set_missing_id", "Skill set is missing an id."))
            continue
        if skill_set.id in seen_sets:
            diagnostics.append(_error("duplicate_skill_set", f"Duplicate skill set: {skill_set.id}"))
        seen_sets.add(skill_set.id)

        if not skill_set.members:
            diagnostics.append(_error("skill_set_missing_members", f"Skill set {label} has no members."))

        seen_members: set[str] = set()
        for member in skill_set.members:
            if member in retired_ids:
                diagnostics.append(_error("retired_skill_set_member", f"Skill set {label} references retired skill: {member}"))
            if not member:
                diagnostics.append(_error("skill_set_member_missing_id", f"Skill set {label} has an empty member."))
                continue
            if member in seen_members:
                diagnostics.append(
                    _error("duplicate_skill_set_member", f"Skill set {label} declares duplicate member: {member}")
                )
            seen_members.add(member)
            if member not in known_skill_ids:
                diagnostics.append(_error("unknown_skill_set_member", f"Skill set {label} references unknown skill: {member}"))

        seen_targets: set[str] = set()
        for target in skill_set.default_targets:
            if not target:
                diagnostics.append(_error("skill_set_target_missing_id", f"Skill set {label} has an empty default target."))
                continue
            if target in seen_targets:
                diagnostics.append(
                    _error("duplicate_skill_set_default_target", f"Skill set {label} declares duplicate target: {target}")
                )
            seen_targets.add(target)
            if target not in known_target_names:
                diagnostics.append(
                    _error("unknown_skill_set_default_target", f"Skill set {label} references unknown target: {target}")
                )


def _validate_skill_origin(skill: SkillPackage, diagnostics: list[Diagnostic]) -> None:
    origin = skill.origin
    if not skill.origin_present:
        return

    kind = _text_value(origin.get("kind"))
    if not kind:
        diagnostics.append(_error("skill_origin_missing_kind", f"Skill {skill.id} origin is missing kind."))
        return

    if kind not in SUPPORTED_ORIGIN_KINDS:
        diagnostics.append(
            _error("skill_origin_unknown_kind", f"Skill {skill.id} origin has unsupported kind: {kind}")
        )
        return

    if kind == "external_skill" and not any(
        _has_non_empty_value(origin, key) for key in ("url", "retrieved_from", "source_path")
    ):
        diagnostics.append(
            _error(
                "skill_origin_missing_source_locator",
                f"Skill {skill.id} external_skill origin requires one of url, retrieved_from, or source_path.",
            )
        )

    if kind == "local_zip" and not _has_non_empty_value(origin, "source_path"):
        diagnostics.append(
            _error("skill_origin_missing_source_path", f"Skill {skill.id} local_zip origin requires source_path.")
        )

    if "retrieved_commit" in origin and not RETRIEVED_COMMIT_PATTERN.match(
        _text_value(origin.get("retrieved_commit"))
    ):
        diagnostics.append(
            _error(
                "skill_origin_invalid_retrieved_commit",
                f"Skill {skill.id} origin retrieved_commit must be a 40-character hex commit.",
            )
        )


def _validate_tool_wrappers(skills: tuple[SkillPackage, ...], diagnostics: list[Diagnostic]) -> None:
    for skill in skills:
        seen_names: set[str] = set()
        skill_path = skill.path.resolve(strict=False)
        for wrapper in skill.tool_wrappers:
            wrapper_label = wrapper.name or "<unknown>"
            if not wrapper.name:
                diagnostics.append(_error("wrapper_missing_name", f"Skill {skill.id} tool wrapper is missing a name."))
            elif wrapper.name in seen_names:
                diagnostics.append(
                    _error("duplicate_tool_wrapper", f"Skill {skill.id} declares duplicate tool wrapper: {wrapper.name}")
                )
            else:
                seen_names.add(wrapper.name)

            if not wrapper.path:
                diagnostics.append(
                    _error("wrapper_missing_path", f"Skill {skill.id} tool wrapper {wrapper_label} is missing path.")
                )
                continue

            declared_path = Path(wrapper.path)
            if declared_path.is_absolute():
                diagnostics.append(
                    _error(
                        "wrapper_absolute_path",
                        f"Skill {skill.id} tool wrapper {wrapper_label} path must be relative: {wrapper.path}",
                    )
                )
                continue

            wrapper_path = (skill_path / declared_path).resolve(strict=False)
            try:
                wrapper_path.relative_to(skill_path)
            except ValueError:
                diagnostics.append(
                    _error(
                        "wrapper_path_escapes_skill",
                        f"Skill {skill.id} tool wrapper {wrapper_label} path escapes the skill package: {wrapper.path}",
                    )
                )
                continue

            if not wrapper_path.exists():
                diagnostics.append(
                    _error(
                        "wrapper_missing_file",
                        f"Skill {skill.id} tool wrapper {wrapper_label} file does not exist: {wrapper.path}",
                    )
                )
                continue
            if not wrapper_path.is_file():
                diagnostics.append(
                    _error(
                        "wrapper_not_file",
                        f"Skill {skill.id} tool wrapper {wrapper_label} path is not a file: {wrapper.path}",
                    )
                )
                continue
            if not os.access(wrapper_path, os.X_OK):
                diagnostics.append(
                    _error(
                        "wrapper_not_executable",
                        f"Skill {skill.id} tool wrapper {wrapper_label} file is not executable: {wrapper.path}",
                    )
                )


def _has_non_empty_value(mapping: Mapping[str, object], key: str) -> bool:
    return bool(_text_value(mapping.get(key)))


def _text_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _error(code: str, message: str) -> Diagnostic:
    return Diagnostic("error", code, message)


def _warning(code: str, message: str) -> Diagnostic:
    return Diagnostic("warning", code, message)
