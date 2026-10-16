from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import BinDependency, EnvDependency, RegistryConfig, RuntimeProfile
from .skills import SkillPackage, ToolWrapper, list_registry_skills, selected_skills

FAILURE_STATUSES = {
    "missing",
    "version_mismatch",
    "probe_failed",
    "runtime_unavailable",
}


@dataclass(frozen=True)
class ToolHealthFilters:
    runtimes: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    bins: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeResult:
    kind: str
    runtime_profile: str
    dependency_name: str
    skill_ids: tuple[str, ...]
    command_attempted: str
    resolved_path: str
    version_output: str
    expected_pattern: str
    status: str
    stderr_excerpt: str
    checked_at: str

    @property
    def ok(self) -> bool:
        return self.status not in FAILURE_STATUSES


def check_tool_health(config: RegistryConfig, filters: ToolHealthFilters) -> tuple[ProbeResult, ...]:
    results: list[ProbeResult] = []
    runtimes = _selected_runtimes(config, filters)
    skills = tuple(skill for skill in selected_skills(config, filters.skills) if skill.lifecycle != "retired")
    if filters.skills and not skills:
        return ()

    for runtime in runtimes:
        wrappers = _selected_tool_wrappers(skills, filters)
        for dependency in _selected_bin_dependencies(config, runtime, filters, skills, wrappers):
            results.append(_check_bin_dependency(runtime, dependency))
        for skill, wrapper in wrappers:
            results.append(_check_tool_wrapper(runtime, skill, wrapper))
        for dependency in _selected_env_dependencies(config, runtime, filters):
            results.append(_check_env_dependency(runtime, dependency))

    return tuple(results)


def health_ok(results: tuple[ProbeResult, ...]) -> bool:
    return all(result.ok for result in results)


def _selected_runtimes(config: RegistryConfig, filters: ToolHealthFilters) -> tuple[RuntimeProfile, ...]:
    runtime_by_name = config.runtime_by_name
    if filters.runtimes:
        return tuple(runtime_by_name[name] for name in filters.runtimes if name in runtime_by_name)
    return config.runtime_profiles


def _selected_bin_dependencies(
    config: RegistryConfig,
    runtime: RuntimeProfile,
    filters: ToolHealthFilters,
    skills: tuple[SkillPackage, ...],
    wrappers: tuple[tuple[SkillPackage, ToolWrapper], ...],
) -> tuple[BinDependency, ...]:
    selected = [
        dependency
        for dependency in config.runtime_dependencies.bins
        if _dependency_applies_to_runtime(dependency.target_runtimes, runtime.name)
        and not _retired_only_dependency(config, dependency.required_by_skills)
        and _dependency_applies_to_skill(dependency.required_by_skills, filters.skills)
        and _dependency_applies_to_bin(dependency.name, filters.bins)
        and not _is_wrapper_managed_dependency(dependency, skills)
    ]

    known_names = {dependency.name for dependency in selected}
    known_names.update(_selected_wrapper_bin_names(wrappers))
    for bin_name in filters.bins:
        if bin_name not in known_names:
            selected.append(
                BinDependency(
                    name=bin_name,
                    required=True,
                    target_runtimes=(runtime.name,),
                    probe=_default_probe(bin_name),
                    visibility_policy="ad_hoc_probe",
                )
            )

    return tuple(selected)


def _selected_tool_wrappers(
    skills: tuple[SkillPackage, ...],
    filters: ToolHealthFilters,
) -> tuple[tuple[SkillPackage, ToolWrapper], ...]:
    selected: list[tuple[SkillPackage, ToolWrapper]] = []
    for skill in skills:
        for wrapper in skill.tool_wrappers:
            if _wrapper_applies_to_bin(wrapper, filters.bins):
                selected.append((skill, wrapper))
    return tuple(selected)


def _selected_wrapper_bin_names(wrappers: tuple[tuple[SkillPackage, ToolWrapper], ...]) -> set[str]:
    names: set[str] = set()
    for _, wrapper in wrappers:
        names.add(wrapper.name)
        names.update(wrapper.wraps)
    return names


def _selected_env_dependencies(
    config: RegistryConfig,
    runtime: RuntimeProfile,
    filters: ToolHealthFilters,
) -> tuple[EnvDependency, ...]:
    if filters.bins:
        return ()
    return tuple(
        dependency
        for dependency in config.runtime_dependencies.env
        if _dependency_applies_to_runtime(dependency.target_runtimes, runtime.name)
    )


def _dependency_applies_to_runtime(target_runtimes: tuple[str, ...], runtime_name: str) -> bool:
    return not target_runtimes or runtime_name in target_runtimes


def _retired_only_dependency(config: RegistryConfig, skill_ids: tuple[str, ...]) -> bool:
    retired_ids = {skill.id for skill in list_registry_skills(config) if skill.lifecycle == "retired"}
    return bool(skill_ids) and set(skill_ids).issubset(retired_ids)


def _dependency_applies_to_skill(required_by_skills: tuple[str, ...], selected_skills: tuple[str, ...]) -> bool:
    if not selected_skills:
        return True
    return bool(set(required_by_skills) & set(selected_skills))


def _dependency_applies_to_bin(name: str, selected_bins: tuple[str, ...]) -> bool:
    return not selected_bins or name in selected_bins


def _wrapper_applies_to_bin(wrapper: ToolWrapper, selected_bins: tuple[str, ...]) -> bool:
    if not selected_bins:
        return True
    wrapper_names = {wrapper.name, *wrapper.wraps}
    return bool(wrapper_names & set(selected_bins))


def _is_wrapper_managed_dependency(dependency: BinDependency, skills: tuple[SkillPackage, ...]) -> bool:
    if dependency.visibility_policy not in {"skill_wrapper_planned", "skill_wrapper"}:
        return False
    if not dependency.required_by_skills:
        return False

    skill_by_id = {skill.id: skill for skill in skills}
    for skill_id in dependency.required_by_skills:
        skill = skill_by_id.get(skill_id)
        if skill is None:
            continue
        for wrapper in skill.tool_wrappers:
            if dependency.name == wrapper.name or dependency.name in wrapper.wraps:
                return True
    return False


def _check_bin_dependency(runtime: RuntimeProfile, dependency: BinDependency) -> ProbeResult:
    checked_at = _checked_at()
    search_dirs = _runtime_search_dirs(runtime)
    resolved_path = _resolve_executable(dependency.name, search_dirs)
    command = dependency.probe.command or f"{dependency.name} --version"

    if not resolved_path:
        return ProbeResult(
            kind="bin",
            runtime_profile=runtime.name,
            dependency_name=dependency.name,
            skill_ids=dependency.required_by_skills,
            command_attempted=command,
            resolved_path="",
            version_output="",
            expected_pattern=dependency.probe.expected_pattern,
            status="missing",
            stderr_excerpt="",
            checked_at=checked_at,
        )

    completed = _run_probe(command, dependency.name, resolved_path, search_dirs)
    output = _trim_output((completed.stdout or "") + (completed.stderr or ""))
    stderr_excerpt = _trim_output(completed.stderr or "")
    status = _status_from_completed(completed.returncode, output, dependency.probe.expected_pattern)

    return ProbeResult(
        kind="bin",
        runtime_profile=runtime.name,
        dependency_name=dependency.name,
        skill_ids=dependency.required_by_skills,
        command_attempted=command,
        resolved_path=str(resolved_path),
        version_output=output,
        expected_pattern=dependency.probe.expected_pattern,
        status=status,
        stderr_excerpt=stderr_excerpt,
        checked_at=checked_at,
    )


def _check_tool_wrapper(runtime: RuntimeProfile, skill: SkillPackage, wrapper: ToolWrapper) -> ProbeResult:
    checked_at = _checked_at()
    search_dirs = _runtime_search_dirs(runtime)
    resolved_path = _resolve_wrapper_path(skill, wrapper)
    command = wrapper.probe.command or f"{wrapper.path} --version"

    if not resolved_path:
        return ProbeResult(
            kind="wrapper",
            runtime_profile=runtime.name,
            dependency_name=wrapper.name,
            skill_ids=(skill.id,),
            command_attempted=command,
            resolved_path="",
            version_output="",
            expected_pattern=wrapper.probe.expected_pattern,
            status="missing",
            stderr_excerpt="",
            checked_at=checked_at,
        )

    completed = _run_wrapper_probe(command, wrapper, resolved_path, search_dirs, skill.path)
    output = _trim_output((completed.stdout or "") + (completed.stderr or ""))
    stderr_excerpt = _trim_output(completed.stderr or "")
    status = _status_from_completed(completed.returncode, output, wrapper.probe.expected_pattern)

    return ProbeResult(
        kind="wrapper",
        runtime_profile=runtime.name,
        dependency_name=wrapper.name,
        skill_ids=(skill.id,),
        command_attempted=command,
        resolved_path=str(resolved_path),
        version_output=output,
        expected_pattern=wrapper.probe.expected_pattern,
        status=status,
        stderr_excerpt=stderr_excerpt,
        checked_at=checked_at,
    )


def _check_env_dependency(runtime: RuntimeProfile, dependency: EnvDependency) -> ProbeResult:
    value = os.environ.get(dependency.name)
    return ProbeResult(
        kind="env",
        runtime_profile=runtime.name,
        dependency_name=dependency.name,
        skill_ids=(),
        command_attempted="",
        resolved_path="",
        version_output="<set>" if value else "",
        expected_pattern="",
        status="available" if value else "missing",
        stderr_excerpt="",
        checked_at=_checked_at(),
    )


def _runtime_search_dirs(runtime: RuntimeProfile) -> tuple[Path, ...]:
    dirs = [Path(item).expanduser() for item in runtime.visible_bin_dirs]
    if runtime.inherits_interactive_shell_path:
        dirs.extend(Path(item).expanduser() for item in os.environ.get("PATH", "").split(os.pathsep) if item)
    return tuple(dict.fromkeys(dirs))


def _resolve_executable(name: str, search_dirs: tuple[Path, ...]) -> Path | None:
    candidate = Path(name).expanduser()
    if candidate.parent != Path("."):
        return candidate if os.access(candidate, os.X_OK) and candidate.is_file() else None

    for directory in search_dirs:
        path = directory / name
        if os.access(path, os.X_OK) and path.is_file():
            return path
    return None


def _resolve_wrapper_path(skill: SkillPackage, wrapper: ToolWrapper) -> Path | None:
    candidate = (skill.path / wrapper.path).resolve(strict=False)
    try:
        candidate.relative_to(skill.path.resolve(strict=False))
    except ValueError:
        return None
    return candidate if os.access(candidate, os.X_OK) and candidate.is_file() else None


def _run_probe(command: str, dependency_name: str, resolved_path: Path, search_dirs: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
    args = shlex.split(command)
    if not args:
        args = [str(resolved_path)]
    elif args[0] == dependency_name:
        args[0] = str(resolved_path)

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(str(path) for path in search_dirs)

    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            env=env,
            text=True,
            timeout=10,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args=args, returncode=124, stdout=exc.stdout or "", stderr=exc.stderr or "probe timed out")


def _run_wrapper_probe(
    command: str,
    wrapper: ToolWrapper,
    resolved_path: Path,
    search_dirs: tuple[Path, ...],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    args = shlex.split(command)
    if not args:
        args = [str(resolved_path)]
    elif _is_wrapper_command_token(args[0], wrapper):
        args[0] = str(resolved_path)

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(str(path) for path in search_dirs)

    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            cwd=cwd,
            env=env,
            text=True,
            timeout=10,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args=args, returncode=124, stdout=exc.stdout or "", stderr=exc.stderr or "probe timed out")


def _is_wrapper_command_token(token: str, wrapper: ToolWrapper) -> bool:
    if token == wrapper.name or token == wrapper.path:
        return True
    return Path(token) == Path(wrapper.path)


def _status_from_completed(returncode: int, output: str, expected_pattern: str) -> str:
    if returncode != 0:
        return "probe_failed"
    if expected_pattern and not re.search(expected_pattern, output):
        return "version_mismatch"
    return "available"


def _default_probe(bin_name: str):
    from .config import Probe

    return Probe(command=f"{bin_name} --version")


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim_output(value: str, limit: int = 400) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
