from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .config import ConfigError, RegistryConfig


@dataclass(frozen=True)
class ToolWrapperProbe:
    command: str = ""
    expected_pattern: str = ""
    strategy: str = "skill-local"


@dataclass(frozen=True)
class ToolWrapper:
    name: str
    path: str
    wraps: tuple[str, ...] = ()
    probe: ToolWrapperProbe = field(default_factory=ToolWrapperProbe)


@dataclass(frozen=True)
class SkillPackage:
    id: str
    path: Path
    title: str = ""
    targets: tuple[str, ...] = ()
    skill_dependencies: tuple[str, ...] = ()
    runtime_bins: tuple[str, ...] = ()
    tool_wrappers: tuple[ToolWrapper, ...] = ()
    origin: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    origin_present: bool = False


def list_registry_skills(config: RegistryConfig) -> tuple[SkillPackage, ...]:
    if not config.skills_dir.exists():
        return ()

    skills: list[SkillPackage] = []
    for path in sorted(config.skills_dir.iterdir()):
        if not path.is_dir():
            continue
        if not (path / "SKILL.md").exists() and not (path / "skill.yaml").exists():
            continue
        skills.append(load_skill(path))
    return tuple(skills)


def load_skill(path: Path) -> SkillPackage:
    data = _load_skill_yaml(path / "skill.yaml")
    skill_id = str(data.get("id") or path.name)
    origin_present = "origin" in data
    return SkillPackage(
        id=skill_id,
        path=path.resolve(strict=False),
        title=str(data.get("title", "")),
        targets=_string_tuple(data.get("targets", [])),
        skill_dependencies=_string_tuple(data.get("skill_dependencies", [])),
        runtime_bins=_string_tuple((data.get("runtime_dependencies") or {}).get("bins", [])),
        tool_wrappers=_parse_tool_wrappers(data.get("tool_wrappers", [])),
        origin=_load_origin(data.get("origin"), path / "skill.yaml", present=origin_present),
        origin_present=origin_present,
    )


def selected_skills(config: RegistryConfig, skill_ids: tuple[str, ...]) -> tuple[SkillPackage, ...]:
    skills = list_registry_skills(config)
    if not skill_ids:
        return skills
    wanted = set(skill_ids)
    return tuple(skill for skill in skills if skill.id in wanted)


def _load_skill_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Skill metadata must contain a YAML mapping: {path}")
    return data


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise ConfigError(f"Expected a string or list of strings, got {type(value).__name__}")


def _parse_tool_wrappers(value: Any) -> tuple[ToolWrapper, ...]:
    if not isinstance(value, list):
        raise ConfigError("tool_wrappers must be a list")

    wrappers: list[ToolWrapper] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("tool_wrappers entries must be mappings")

        probe_data = item["probe"] if "probe" in item and item["probe"] is not None else {}
        if not isinstance(probe_data, dict):
            name = str(item.get("name", "<unknown>") or "<unknown>")
            raise ConfigError(f"Tool wrapper {name} probe must be a mapping")

        wrappers.append(
            ToolWrapper(
                name=str(item.get("name", "")),
                path=str(item.get("path", "")),
                wraps=_string_tuple(item.get("wraps", [])),
                probe=ToolWrapperProbe(
                    command=str(probe_data.get("command", "")),
                    expected_pattern=str(probe_data.get("expected_pattern", "")),
                    strategy=str(probe_data.get("strategy", "skill-local")),
                ),
            )
        )
    return tuple(wrappers)


def _load_origin(value: Any, metadata_path: Path, *, present: bool) -> Mapping[str, Any]:
    if not present:
        return MappingProxyType({})
    if not isinstance(value, dict):
        raise ConfigError(f"Skill origin metadata must contain a YAML mapping: {metadata_path}")
    return _immutable_mapping(value)


def _immutable_mapping(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _immutable_value(item) for key, item in value.items()})


def _immutable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _immutable_mapping(value)
    if isinstance(value, list):
        return tuple(_immutable_value(item) for item in value)
    return value
