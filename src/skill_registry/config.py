from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when registry configuration cannot be loaded."""


@dataclass(frozen=True)
class Probe:
    command: str = ""
    expected_pattern: str = ""
    strategy: str = "path-list"


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    kind: str = "path-list"
    description: str = ""
    visible_bin_dirs: tuple[str, ...] = ()
    probe_strategy: str = "path-list"
    capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def inherits_interactive_shell_path(self) -> bool:
        return bool(self.capabilities.get("inherits_interactive_shell_path", False))


@dataclass(frozen=True)
class BinDependency:
    name: str
    required: bool = True
    required_by_skills: tuple[str, ...] = ()
    target_runtimes: tuple[str, ...] = ()
    probe: Probe = field(default_factory=Probe)
    visibility_policy: str = ""


@dataclass(frozen=True)
class EnvDependency:
    name: str
    required: bool = True
    target_runtimes: tuple[str, ...] = ()
    secret: bool = False


@dataclass(frozen=True)
class RuntimeDependencies:
    bins: tuple[BinDependency, ...] = ()
    env: tuple[EnvDependency, ...] = ()


@dataclass(frozen=True)
class TargetConfig:
    name: str
    system: str
    root: str
    state_dir: str = ""
    deploy_mode: str = "symlink"
    include_in_all: bool = False
    supports_symlink: bool = True
    runtime_profiles: tuple[str, ...] = ()

    @property
    def root_path(self) -> Path:
        return Path(self.root).expanduser().resolve(strict=False)

    @property
    def state_path(self) -> Path:
        if self.state_dir:
            return Path(self.state_dir).expanduser().resolve(strict=False)
        return self.root_path / ".skill-registry"


@dataclass(frozen=True)
class SkillSet:
    id: str
    title: str = ""
    members: tuple[str, ...] = ()
    default_targets: tuple[str, ...] = ()
    native_exports: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistryConfig:
    root: Path
    skills_dir: Path
    targets_dir: Path
    local_targets_dir: Path
    state_dir: Path
    runtime_profiles: tuple[RuntimeProfile, ...]
    runtime_dependencies: RuntimeDependencies
    targets: tuple[TargetConfig, ...]
    skill_sets: tuple[SkillSet, ...]
    loaded_files: tuple[Path, ...]

    @property
    def runtime_by_name(self) -> dict[str, RuntimeProfile]:
        return {profile.name: profile for profile in self.runtime_profiles}

    @property
    def target_by_name(self) -> dict[str, TargetConfig]:
        return {target.name: target for target in self.targets}

    @property
    def skill_set_by_id(self) -> dict[str, SkillSet]:
        return {skill_set.id: skill_set for skill_set in self.skill_sets}


def load_registry(root: Path | str) -> RegistryConfig:
    root_path = Path(root).expanduser().resolve()
    registry_path = root_path / "registry.yaml"
    runtimes_path = root_path / "runtimes.yaml"
    dependencies_path = root_path / "runtime-dependencies.yaml"
    skill_sets_path = root_path / "skill-sets.yaml"

    registry_data = _load_yaml_mapping(registry_path, required=False)
    runtimes_data = _load_yaml_mapping(runtimes_path, required=False)
    dependencies_data = _load_yaml_mapping(dependencies_path, required=False)
    skill_sets_data = _load_yaml_mapping(skill_sets_path, required=False)

    skills_dir = root_path / str(registry_data.get("skills_dir", "skills"))
    targets_dir = root_path / str(registry_data.get("targets_dir", "targets"))
    local_targets_dir = root_path / str(registry_data.get("local_targets_dir", "targets.local"))
    state_dir = root_path / str(registry_data.get("state_dir", ".skill-registry"))

    base_targets = _load_targets(targets_dir)
    local_targets = _load_targets(local_targets_dir)
    targets = _merge_targets(base_targets, local_targets)
    target_files = tuple(sorted(targets_dir.glob("*.yaml"))) if targets_dir.exists() else ()
    local_target_files = tuple(sorted(local_targets_dir.glob("*.yaml"))) if local_targets_dir.exists() else ()
    loaded_files = (
        tuple(path for path in (registry_path, runtimes_path, dependencies_path, skill_sets_path) if path.exists())
        + target_files
        + local_target_files
    )

    return RegistryConfig(
        root=root_path,
        skills_dir=skills_dir,
        targets_dir=targets_dir,
        local_targets_dir=local_targets_dir,
        state_dir=state_dir,
        runtime_profiles=tuple(_parse_runtime_profile(item) for item in runtimes_data.get("runtime_profiles", [])),
        runtime_dependencies=_parse_runtime_dependencies(dependencies_data.get("runtime_dependencies", {})),
        targets=targets,
        skill_sets=_parse_skill_sets(skill_sets_data, skill_sets_path),
        loaded_files=loaded_files,
    )


def _load_yaml_mapping(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Missing required config file: {path}")
        return {}

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file must contain a YAML mapping: {path}")
    return loaded


def _parse_runtime_profile(item: Any) -> RuntimeProfile:
    if not isinstance(item, dict):
        raise ConfigError("runtime_profiles entries must be mappings")

    probe = item.get("probe") or {}
    if not isinstance(probe, dict):
        raise ConfigError(f"Runtime profile {item.get('name', '<unknown>')} probe must be a mapping")

    profile_name = str(item.get("name", "<unknown>") or "<unknown>")
    if "managed_shims" in item:
        raise ConfigError(
            f"Runtime profile {profile_name} managed_shims is deprecated; "
            "v1 runtime health is wrapper-first. Remove managed_shims and use direct bin dependencies "
            "or skill-local scripts/ wrappers."
        )
    if "managed_shim_dirs" in item:
        raise ConfigError(
            f"Runtime profile {profile_name} managed_shim_dirs is deprecated; "
            "v1 runtime health is wrapper-first. Remove managed_shim_dirs and use direct bin dependencies "
            "or skill-local scripts/ wrappers."
        )

    capabilities = dict(item.get("capabilities") or {})
    if "supports_managed_shims" in capabilities:
        raise ConfigError(
            f"Runtime profile {profile_name} capability supports_managed_shims is deprecated; "
            "remove it. v1 runtime health is wrapper-first."
        )

    return RuntimeProfile(
        name=str(item.get("name", "")),
        kind=str(item.get("kind", "path-list")),
        description=str(item.get("description", "")),
        visible_bin_dirs=_string_tuple(item.get("visible_bin_dirs", [])),
        probe_strategy=str(probe.get("strategy", "path-list")),
        capabilities=capabilities,
    )


def _parse_runtime_dependencies(data: Any) -> RuntimeDependencies:
    if not data:
        return RuntimeDependencies()
    if not isinstance(data, dict):
        raise ConfigError("runtime_dependencies must be a mapping")
    if "managed_shims" in data:
        raise ConfigError(
            "runtime_dependencies.managed_shims is deprecated; v1 runtime health is wrapper-first. "
            "Remove managed_shims and use direct bin dependencies or planned skill-local wrappers."
        )

    return RuntimeDependencies(
        bins=tuple(_parse_bin_dependency(item) for item in data.get("bins", [])),
        env=tuple(_parse_env_dependency(item) for item in data.get("env", [])),
    )


def _load_targets(targets_dir: Path) -> tuple[TargetConfig, ...]:
    if not targets_dir.exists():
        return ()

    targets: list[TargetConfig] = []
    for path in sorted(targets_dir.glob("*.yaml")):
        data = _load_yaml_mapping(path, required=True)
        if "targets" in data:
            entries = data.get("targets") or []
            if not isinstance(entries, list):
                raise ConfigError(f"targets must be a list in {path}")
            targets.extend(_parse_target(item, path) for item in entries)
        elif data:
            targets.append(_parse_target(data, path))
    return tuple(targets)


def _merge_targets(
    base_targets: tuple[TargetConfig, ...],
    local_targets: tuple[TargetConfig, ...],
) -> tuple[TargetConfig, ...]:
    if not local_targets:
        return base_targets
    if _target_name_duplicates(base_targets) or _target_name_duplicates(local_targets):
        return base_targets + local_targets

    merged = list(base_targets)
    positions = {target.name: index for index, target in enumerate(base_targets) if target.name}
    for target in local_targets:
        if target.name in positions:
            merged[positions[target.name]] = target
            continue
        if target.name:
            positions[target.name] = len(merged)
        merged.append(target)
    return tuple(merged)


def _target_name_duplicates(targets: tuple[TargetConfig, ...]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for target in targets:
        if not target.name:
            continue
        if target.name in seen:
            duplicates.add(target.name)
        seen.add(target.name)
    return duplicates


def _parse_target(item: Any, path: Path) -> TargetConfig:
    if not isinstance(item, dict):
        raise ConfigError(f"Target entries must be mappings in {path}")

    return TargetConfig(
        name=str(item.get("name", "")),
        system=str(item.get("system", "")),
        root=str(item.get("root", "")),
        state_dir=str(item.get("state_dir", "")),
        deploy_mode=str(item.get("deploy_mode", "symlink")),
        include_in_all=bool(item.get("include_in_all", False)),
        supports_symlink=bool(item.get("supports_symlink", True)),
        runtime_profiles=_string_tuple(item.get("runtime_profiles", [])),
    )


def _parse_skill_sets(data: dict[str, Any], path: Path) -> tuple[SkillSet, ...]:
    if not data:
        return ()
    entries = data.get("skill_sets", [])
    if not isinstance(entries, list):
        raise ConfigError(f"skill_sets must be a list in {path}")
    return tuple(_parse_skill_set(item, path) for item in entries)


def _parse_skill_set(item: Any, path: Path) -> SkillSet:
    if not isinstance(item, dict):
        raise ConfigError(f"Skill-set entries must be mappings in {path}")
    native_exports = item.get("native_exports", {})
    if native_exports is None:
        native_exports = {}
    if not isinstance(native_exports, dict):
        skill_set_id = str(item.get("id", "<unknown>") or "<unknown>")
        raise ConfigError(f"Skill set {skill_set_id} native_exports must be a mapping")
    return SkillSet(
        id=str(item.get("id", "")),
        title=str(item.get("title", "")),
        members=_string_tuple(item.get("members", [])),
        default_targets=_string_tuple(item.get("default_targets", [])),
        native_exports=dict(native_exports),
    )


def _parse_bin_dependency(item: Any) -> BinDependency:
    if not isinstance(item, dict):
        raise ConfigError("runtime_dependencies.bins entries must be mappings")

    probe_data = item.get("probe") or {}
    if not isinstance(probe_data, dict):
        raise ConfigError(f"Bin dependency {item.get('name', '<unknown>')} probe must be a mapping")

    return BinDependency(
        name=str(item.get("name", "")),
        required=bool(item.get("required", True)),
        required_by_skills=_string_tuple(item.get("required_by_skills", item.get("required_for", []))),
        target_runtimes=_string_tuple(item.get("target_runtimes", [])),
        probe=Probe(
            command=str(probe_data.get("command", "")),
            expected_pattern=str(probe_data.get("expected_pattern", "")),
            strategy=str(probe_data.get("strategy", "path-list")),
        ),
        visibility_policy=str(item.get("visibility_policy", "")),
    )


def _parse_env_dependency(item: Any) -> EnvDependency:
    if not isinstance(item, dict):
        raise ConfigError("runtime_dependencies.env entries must be mappings")
    return EnvDependency(
        name=str(item.get("name", "")),
        required=bool(item.get("required", True)),
        target_runtimes=_string_tuple(item.get("target_runtimes", [])),
        secret=bool(item.get("secret", False)),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise ConfigError(f"Expected a string or list of strings, got {type(value).__name__}")
