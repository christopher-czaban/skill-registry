from __future__ import annotations

from .config import RegistryConfig, TargetConfig


def selected_targets(
    config: RegistryConfig,
    target_names: tuple[str, ...] = (),
    *,
    all_targets: bool = False,
) -> tuple[TargetConfig, ...]:
    if target_names:
        by_name = config.target_by_name
        return tuple(by_name[name] for name in target_names if name in by_name)
    if all_targets:
        return tuple(target for target in config.targets if target.include_in_all)
    return config.targets


def target_filter_errors(config: RegistryConfig, target_names: tuple[str, ...]) -> list[str]:
    known = config.target_by_name
    return [f"Unknown target: {name}" for name in target_names if name not in known]
