from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import RegistryConfig, TargetConfig
from .hashing import deployable_payload_hash
from .skills import list_registry_skills
from .state import classification_record_path, deployment_record_path, load_classification, load_deployment_record


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")


@dataclass(frozen=True)
class UndeployOperation:
    target: str
    skill_id: str
    operation: str
    target_path: str
    source_path: str
    mode: str = ""
    reason: str = ""
    fingerprint: str = ""
    backup_path: str = ""
    receipt_path: str = ""
    applied: bool = False

    @property
    def ok(self) -> bool:
        return self.operation != "block"


class UndeployError(ValueError):
    def __init__(self, message: str, completed: tuple[UndeployOperation, ...] = ()):
        super().__init__(message)
        self.completed = completed


def safe_skill_id(skill_id: str) -> bool:
    return bool(_SAFE_ID.fullmatch(skill_id))


def receipt_path(target: TargetConfig, skill_id: str) -> Path:
    return target.state_path / "undeployments" / f"{skill_id}.json"


def pending_path(target: TargetConfig, skill_id: str) -> Path:
    return target.state_path / "undeployments" / f".pending-{skill_id}.json"


def local_skill_ids(target: TargetConfig) -> set[str]:
    ids: set[str] = set()
    for directory in ("deployments", "undeployments"):
        for path in (target.state_path / directory).glob("*.json"):
            name = path.stem
            if name.startswith(".pending-"):
                name = name[len(".pending-"):]
            if safe_skill_id(name):
                ids.add(name)
    return ids


def read_receipt(config: RegistryConfig, target: TargetConfig, skill_id: str) -> dict | None:
    path = receipt_path(target, skill_id)
    if not path.exists() and not path.is_symlink():
        return None
    _regular_file(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = {"schema_version": 1, "target": target.name, "skill_id": skill_id,
                "target_path": str(target.root_path / skill_id),
                "source_path": str((config.skills_dir / skill_id).resolve(strict=False)),
                "status": "completed"}
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise ValueError(f"invalid undeployment receipt: {path}")
    for key in ("undeployed_at", "undeployed_by", "reason", "backup_path"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"undeployment receipt missing {key}: {path}")
    if data.get("deployment_mode") not in {"symlink", "copy"}:
        raise ValueError(f"invalid undeployment receipt mode: {path}")
    return data


def known_skill_ids(config: RegistryConfig) -> set[str]:
    ids = {skill.id for skill in list_registry_skills(config)}
    for target in config.targets:
        ids.update(local_skill_ids(target))
    return ids


def plan_undeploy(config: RegistryConfig, *, target_name: str,
                  skill_ids: tuple[str, ...]) -> tuple[UndeployOperation, ...]:
    if target_name not in config.target_by_name:
        raise ValueError(f"Unknown target: {target_name}")
    if not skill_ids:
        raise ValueError("undeploy requires explicit skill IDs")
    target = config.target_by_name[target_name]
    return tuple(_plan_one(config, target, skill_id) for skill_id in dict.fromkeys(skill_ids))


def _plan_one(config: RegistryConfig, target: TargetConfig, skill_id: str) -> UndeployOperation:
    if not safe_skill_id(skill_id):
        return UndeployOperation(target.name, skill_id, "block", "", "", reason="unsafe skill ID")
    entry = target.root_path / skill_id
    source = (config.skills_dir / skill_id).resolve(strict=False)
    result = UndeployOperation(target.name, skill_id, "block", str(entry), str(source))
    try:
        _safe_layout(config, target, skill_id)
        if pending_path(target, skill_id).exists():
            raise ValueError(f"pending undeployment needs recovery: {pending_path(target, skill_id)}")
        classification = load_classification(target, skill_id)
        if classification:
            raise ValueError(f"target has {classification.classification} classification; review it before removal")
        record_path = deployment_record_path(target, skill_id)
        if record_path.exists() or record_path.is_symlink():
            _regular_file(record_path)
        record = load_deployment_record(target, skill_id)
        exists = entry.exists() or entry.is_symlink()
        if record is None:
            old_receipt = read_receipt(config, target, skill_id)
            if exists:
                raise ValueError("target entry has no valid deployment record; unmanaged removal is prohibited")
            if old_receipt or skill_id in {s.id for s in list_registry_skills(config)}:
                return replace(result, operation="skip", reason="already absent; no mutation required")
            raise ValueError(f"Unknown skill: {skill_id}")
        if record.target != target.name or record.skill_id != skill_id:
            raise ValueError("deployment record target or skill mismatch")
        if _lexical_path(record.target_path) != entry:
            raise ValueError("deployment record target_path mismatch")
        if Path(record.source_path).expanduser().resolve(strict=False) != source:
            raise ValueError("deployment record belongs to another canonical source/instance")
        result = replace(result, mode=record.deployment_mode)
        if not exists:
            return replace(result, operation="record_absent", reason="managed entry already absent; finalize local state",
                           fingerprint=_fingerprint(target, skill_id))
        if record.deployment_mode == "symlink":
            if not entry.is_symlink() or entry.resolve(strict=False) != source:
                raise ValueError("managed symlink was replaced or points at a different source")
            operation = "remove_symlink"
        else:
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("managed copy was replaced by another entry type")
            if deployable_payload_hash(entry).payload_hash != record.payload_hash:
                raise ValueError("managed copy has local drift; preserve/reconcile it before removal")
            operation = "remove_copy"
        return replace(result, operation=operation, reason="valid deployment record proves ownership",
                       fingerprint=_fingerprint(target, skill_id))
    except (OSError, ValueError, RuntimeError) as exc:
        return replace(result, reason=str(exc))


def apply_undeploy(config: RegistryConfig, *, target_name: str, skill_ids: tuple[str, ...],
                   reason: str, undeployed_by: str = "skill-registry") -> tuple[UndeployOperation, ...]:
    if not reason.strip():
        raise ValueError("undeploy apply requires a non-empty reason")
    plans = plan_undeploy(config, target_name=target_name, skill_ids=skill_ids)
    blocked = [p for p in plans if not p.ok]
    if blocked:
        raise UndeployError("undeploy blocked: " + "; ".join(f"{p.skill_id}: {p.reason}" for p in blocked))
    target = config.target_by_name[target_name]
    completed: list[UndeployOperation] = []
    for plan in plans:
        try:
            current = _plan_one(config, target, plan.skill_id)
            if current != plan:
                raise ValueError(f"target changed since preflight: {plan.skill_id}")
            if plan.operation == "skip":
                completed.append(plan)
            else:
                completed.append(_apply_one(config, target, plan, reason.strip(), undeployed_by.strip() or "skill-registry"))
        except (OSError, ValueError, RuntimeError) as exc:
            raise UndeployError(str(exc), tuple(completed)) from exc
    return tuple(completed)


def _apply_one(config: RegistryConfig, target: TargetConfig, plan: UndeployOperation,
               reason: str, actor: str) -> UndeployOperation:
    entry = Path(plan.target_path)
    record_path = deployment_record_path(target, plan.skill_id)
    marker = receipt_path(target, plan.skill_id)
    journal = pending_path(target, plan.skill_id)
    record_bytes = record_path.read_bytes()
    prior_receipt = marker.read_bytes() if marker.exists() else None
    operation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:8]
    backup = target.state_path / "backups" / f"{operation_id}-{plan.skill_id}-undeploy"
    quarantine = entry.with_name(f".skill-registry-undeploy-{operation_id}")
    backup_entry = backup / "entry" / plan.skill_id
    moved = False
    record_removed = False
    receipt_written = False
    journal_created = False
    try:
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        (backup / "deployment-record.json").write_bytes(record_bytes)
        if prior_receipt is not None:
            (backup / "previous-receipt.json").write_bytes(prior_receipt)
        marker.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("x", encoding="utf-8") as handle:
            journal_created = True
            json.dump({"target_path": str(entry), "backup_path": str(backup),
                       "quarantine_path": str(quarantine), "operation": asdict(plan)}, handle, indent=2)
        if _fingerprint(target, plan.skill_id) != plan.fingerprint:
            raise ValueError("target or deployment state changed immediately before removal")
        if plan.operation != "record_absent":
            entry.rename(quarantine)
            moved = True
            if _fingerprint(target, plan.skill_id, entry=quarantine) != plan.fingerprint:
                raise ValueError("target changed while preparing removal")
            backup_entry.parent.mkdir()
            _copy_entry(quarantine, backup_entry)
            if _fingerprint(target, plan.skill_id, entry=quarantine) != plan.fingerprint:
                raise ValueError("target or deployment state changed during backup")
        data = {"schema_version": 1, "status": "completed", "target": target.name,
                "skill_id": plan.skill_id, "source_path": plan.source_path,
                "target_path": plan.target_path, "deployment_mode": plan.mode,
                "undeployed_at": datetime.now(timezone.utc).isoformat(),
                "undeployed_by": actor, "reason": reason, "backup_path": str(backup)}
        (backup / "backup.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if entry.exists() or entry.is_symlink():
            raise ValueError("another process recreated the target entry during removal")
        if record_path.read_bytes() != record_bytes:
            raise ValueError("deployment record changed during removal")
        _atomic_bytes(marker, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())
        receipt_written = True
        record_path.unlink()
        record_removed = True
        if moved:
            _remove_entry(quarantine)
        journal.unlink()
        return replace(plan, applied=True, reason=reason, backup_path=str(backup), receipt_path=str(marker))
    except Exception as exc:
        try:
            if moved:
                if entry.exists() or entry.is_symlink():
                    raise ValueError("target was recreated; will not overwrite the new entry")
                if quarantine.exists() or quarantine.is_symlink():
                    quarantine.rename(entry)
                else:
                    _copy_entry(backup_entry, entry)
            if record_removed:
                if record_path.exists() or record_path.is_symlink():
                    raise ValueError("deployment record was recreated; will not overwrite it")
                _atomic_bytes(record_path, record_bytes)
            if receipt_written:
                if prior_receipt is None:
                    marker.unlink()
                else:
                    _atomic_bytes(marker, prior_receipt)
            if journal_created and journal.exists():
                journal.unlink()
        except Exception as recovery:
            raise ValueError(f"undeploy failed; manual recovery required from {backup}; "
                             f"pending journal {journal}: {recovery}; original error: {exc}") from exc
        raise ValueError(f"undeploy failed and was rolled back; evidence at {backup}: {exc}") from exc


def _fingerprint(target: TargetConfig, skill_id: str, *, entry: Path | None = None) -> str:
    path = entry if entry is not None else target.root_path / skill_id
    values: list[object] = [hashlib.sha256(deployment_record_path(target, skill_id).read_bytes()).hexdigest()]
    for state_file in (receipt_path(target, skill_id), classification_record_path(target, skill_id)):
        values.append(hashlib.sha256(state_file.read_bytes()).hexdigest() if state_file.exists() else None)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        values.extend((info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns))
        if path.is_symlink():
            values.append(os.readlink(path))
        elif path.is_dir():
            values.append(deployable_payload_hash(path).payload_hash)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _safe_layout(config: RegistryConfig, target: TargetConfig, skill_id: str) -> None:
    entry = target.root_path / skill_id
    skills_root = config.skills_dir.resolve(strict=False)
    source = (skills_root / skill_id).resolve(strict=False)
    if _within(entry, skills_root) or _within(config.root, entry) or not _within(source, skills_root):
        raise ValueError("target overlaps the registry source or canonical source escapes its root")
    if any(other.name != target.name and other.root_path / skill_id == entry for other in config.targets):
        raise ValueError("registered targets share this entry; removal would affect another target")
    state = target.state_path
    if _within(state, entry) or _within(entry, state):
        raise ValueError("target and state paths overlap")
    if _within(state, skills_root) or _within(skills_root, state):
        raise ValueError("state and canonical source paths overlap")
    for directory in (state / "backups", state / "deployments", state / "undeployments", state / "classifications"):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"unsafe state directory: {directory}")
    for path in (deployment_record_path(target, skill_id), classification_record_path(target, skill_id),
                 receipt_path(target, skill_id), pending_path(target, skill_id)):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"unsafe state file: {path}")


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _lexical_path(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular state file: {path}")


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def _remove_entry(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)
