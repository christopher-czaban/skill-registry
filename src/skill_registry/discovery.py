from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import RegistryConfig, TargetConfig
from .skills import SkillPackage, list_registry_skills
from .state import load_classification
from .status import check_recorded_directory_health
from .targets import selected_targets


@dataclass(frozen=True)
class DiscoveryEntry:
    target: str
    path: str
    skill_id: str
    classification: str
    reason: str
    resolved_path: str = ""


def discover(config: RegistryConfig, target_names: tuple[str, ...] = ()) -> tuple[DiscoveryEntry, ...]:
    registry_skills = list_registry_skills(config)
    registry_ids = {skill.id for skill in registry_skills}
    registry_by_id = {skill.id: skill for skill in registry_skills}
    canonical_paths = {skill.path.resolve(strict=False): skill for skill in registry_skills}

    entries: list[DiscoveryEntry] = []
    for target in selected_targets(config, target_names):
        entries.extend(_discover_target(target, registry_ids, registry_by_id, canonical_paths))
    return tuple(entries)


def _discover_target(
    target: TargetConfig,
    registry_ids: set[str],
    registry_by_id: dict[str, SkillPackage],
    canonical_paths,
) -> tuple[DiscoveryEntry, ...]:
    root = target.root_path
    if not root.exists():
        return (
            DiscoveryEntry(
                target=target.name,
                path=str(root),
                skill_id="",
                classification="target_missing",
                reason="target root does not exist",
            ),
        )

    entries: list[DiscoveryEntry] = []
    for child in _iter_skill_entries(root):
        entries.append(_classify_entry(target, child, registry_ids, registry_by_id, canonical_paths))
    return tuple(entries)


def _iter_skill_entries(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.relative_to(root).as_posix()):
            if _should_skip_discovery_path(child):
                continue
            if _looks_like_skill_entry(child):
                entries.append(child)
                continue
            if child.is_dir() and not child.is_symlink():
                walk(child)

    walk(root)
    return tuple(entries)


def _should_skip_discovery_path(path: Path) -> bool:
    return path.name.startswith(".") or path.name in {"__pycache__", "node_modules"}


def _looks_like_skill_entry(path: Path) -> bool:
    if path.is_symlink():
        return True
    return path.is_dir() and ((path / "SKILL.md").exists() or (path / "skill.yaml").exists())


def _classify_entry(
    target: TargetConfig,
    path: Path,
    registry_ids: set[str],
    registry_by_id: dict[str, SkillPackage],
    canonical_paths,
) -> DiscoveryEntry:
    skill_id = path.name
    resolved_path = ""
    record = load_classification(target, skill_id)
    if record and record.classification == "local_only":
        return DiscoveryEntry(
            target=target.name,
            path=str(path),
            skill_id=skill_id,
            classification="local_only",
            reason=record.reason,
        )
    if record and record.classification == "blocked":
        return DiscoveryEntry(
            target=target.name,
            path=str(path),
            skill_id=skill_id,
            classification="blocked",
            reason=record.reason,
        )

    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return DiscoveryEntry(
                target=target.name,
                path=str(path),
                skill_id=skill_id,
                classification="blocked",
                reason="broken symlink",
            )
        resolved_path = str(resolved)
        if resolved in canonical_paths:
            return DiscoveryEntry(
                target=target.name,
                path=str(path),
                skill_id=canonical_paths[resolved].id,
                classification="managed",
                reason="symlink points at canonical registry skill",
                resolved_path=resolved_path,
            )
        return DiscoveryEntry(
            target=target.name,
            path=str(path),
            skill_id=skill_id,
            classification="external_vendor",
            reason="symlink points outside registry",
            resolved_path=resolved_path,
        )

    if skill_id in registry_ids:
        recorded_health = check_recorded_directory_health(target, registry_by_id[skill_id], path)
        if recorded_health:
            classification = "managed" if recorded_health.status == "in_sync" else recorded_health.status
            return DiscoveryEntry(
                target=target.name,
                path=str(path),
                skill_id=skill_id,
                classification=classification,
                reason=recorded_health.reason,
                resolved_path=resolved_path,
            )
        return DiscoveryEntry(
            target=target.name,
            path=str(path),
            skill_id=skill_id,
            classification="candidate_duplicate",
            reason="target entry has same ID as a registry skill but is not registry-managed",
            resolved_path=resolved_path,
        )

    return DiscoveryEntry(
        target=target.name,
        path=str(path),
        skill_id=skill_id,
        classification="unmanaged",
        reason="target entry is not registry-managed",
        resolved_path=resolved_path,
    )
