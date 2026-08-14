from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]

from skill_registry.config import load_registry
from skill_registry.hashing import deployable_payload_hash
from skill_registry.skills import selected_skills
from skill_registry.state import DeploymentRecord, save_deployment_record


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "skill_registry", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class HermesBundleExportTests(unittest.TestCase):
    def test_dry_run_reports_yaml_metadata_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            bundle_path = hermes_home / "skill-bundles" / "demo-set.yaml"
            expected_bundle_path = str(bundle_path.resolve(strict=False))
            write_hermes_fixture_registry(root, hermes_home)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--dry-run",
                "--instruction",
                "Use together",
                "--reason",
                "preview",
            )
            bundle_exists = bundle_path.exists()

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        expected_yaml = (
            "name: Demo Set\n"
            "description: Demo Set\n"
            "skills:\n"
            "  - demo-skill\n"
            "instruction: Use together\n"
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["operation"], "would_create")
        self.assertEqual(payload["skill_set_id"], "demo-set")
        self.assertEqual(payload["target"], "hermes-global")
        self.assertEqual(payload["bundle_path"], expected_bundle_path)
        self.assertEqual(payload["slug"], "demo-set")
        self.assertEqual(payload["skills"], ["demo-skill"])
        self.assertEqual(payload["yaml_sha256"], hashlib.sha256(expected_yaml.encode("utf-8")).hexdigest())
        self.assertEqual(payload["reason"], "preview")
        self.assertFalse(bundle_exists)

    def test_apply_writes_expected_yaml_to_temporary_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            bundle_path = hermes_home / "skill-bundles" / "demo-set.yaml"
            expected_bundle_path = str(bundle_path.resolve(strict=False))
            write_hermes_fixture_registry(root, hermes_home)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--reason",
                "create bundle",
                "--exported-by",
                "unit-test",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["dry_run"])
            self.assertEqual(payload["operation"], "created")
            self.assertEqual(payload["bundle_path"], expected_bundle_path)
            self.assertEqual(payload["exported_by"], "unit-test")
            self.assertTrue(bundle_path.is_file())
            text = bundle_path.read_text(encoding="utf-8")

        self.assertEqual(
            text,
            "name: Demo Set\n"
            "description: Demo Set\n"
            "skills:\n"
            "  - demo-skill\n",
        )
        self.assertEqual(yaml.safe_load(text), {"name": "Demo Set", "description": "Demo Set", "skills": ["demo-skill"]})
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_unknown_skill_set_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "missing-set",
                "--target",
                "hermes-global",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("Unknown skill set: missing-set", payload["reason"])

    def test_unknown_target_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "missing-target",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("Unknown target: missing-target", payload["reason"])

    def test_non_hermes_global_target_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home, target_system="codex")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("must use system hermes", payload["reason"])

    def test_non_global_hermes_target_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(
                root,
                hermes_home,
                extra_targets=(
                    {
                        "name": "hermes-profile-demo",
                        "system": "hermes",
                        "root": str(hermes_home / "profiles" / "demo" / "skills"),
                        "state_dir": str(hermes_home / "profiles" / "demo" / "skill-registry-state"),
                    },
                ),
            )

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-profile-demo",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("or a subtarget under", payload["reason"])

    def test_global_category_target_writes_bundle_to_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            category_root = hermes_home / "skills" / "examples"
            bundle_path = hermes_home / "skill-bundles" / "demo-set.yaml"
            write_hermes_fixture_registry(
                root,
                hermes_home,
                create_in_sync_member=False,
                extra_targets=(
                    {
                        "name": "hermes-global-example",
                        "system": "hermes",
                        "root": str(category_root),
                        "state_dir": str(hermes_home / "skill-registry-state" / "example"),
                    },
                ),
                skill_targets=("hermes-global-example",),
            )
            write_matching_target_copy(root, category_root)
            save_demo_deployment_record(root, target_name="hermes-global-example")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global-example",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["target"], "hermes-global-example")
            self.assertEqual(payload["bundle_path"], str(bundle_path.resolve(strict=False)))
            self.assertTrue(bundle_path.is_file())

    def test_unknown_member_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home, members=("demo-skill", "missing-skill"))

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("Unknown skill-set member(s): missing-skill", payload["reason"])

    def test_not_applicable_member_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home, skill_targets=("codex",))

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("not applicable to hermes-global: demo-skill", payload["reason"])

    def test_member_not_in_sync_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            write_hermes_fixture_registry(root, hermes_home, create_in_sync_member=False)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
                "--dry-run",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("demo-skill is not in_sync", payload["reason"])

    def test_existing_bundle_file_blocks_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "registry"
            hermes_home = Path(temp) / ".hermes"
            bundle_path = hermes_home / "skill-bundles" / "demo-set.yaml"
            write_hermes_fixture_registry(root, hermes_home)
            bundle_path.parent.mkdir(parents=True)
            bundle_path.write_text("existing: true\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "export",
                "hermes-bundle",
                "--skill-set",
                "demo-set",
                "--target",
                "hermes-global",
            )
            after = bundle_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "blocked")
        self.assertIn("Bundle file already exists", payload["reason"])
        self.assertEqual(after, "existing: true\n")


def write_hermes_fixture_registry(
    root: Path,
    hermes_home: Path,
    *,
    create_in_sync_member: bool = True,
    extra_targets: tuple[dict[str, str], ...] = (),
    members: tuple[str, ...] = ("demo-skill",),
    skill_targets: tuple[str, ...] = ("hermes-global",),
    target_system: str = "hermes",
) -> None:
    skills_dir = root / "skills"
    targets_dir = root / "targets"
    skill_dir = skills_dir / "demo-skill"
    target_root = hermes_home / "skills"
    state_dir = hermes_home / "skill-registry-state"
    skill_dir.mkdir(parents=True)
    targets_dir.mkdir(parents=True)
    target_root.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")
    skill_targets_yaml = "\n".join(f"  - {target}" for target in skill_targets)
    (skill_dir / "skill.yaml").write_text(
        f"id: demo-skill\ntitle: Demo Skill\ntargets:\n{skill_targets_yaml}\n",
        encoding="utf-8",
    )
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            runtime_profiles:
              - name: hermes
                kind: path-list
                visible_bin_dirs: []
                probe:
                  strategy: path-list
            """
        ),
        encoding="utf-8",
    )
    (root / "runtime-dependencies.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            runtime_dependencies:
              bins: []
              env: []
            """
        ),
        encoding="utf-8",
    )
    target_entries = [
        {
            "name": "hermes-global",
            "system": target_system,
            "root": str(target_root),
            "state_dir": str(state_dir),
        },
        *extra_targets,
    ]
    target_yaml = "targets:\n" + "".join(
        textwrap.dedent(
            f"""
              - name: {target["name"]}
                system: {target["system"]}
                root: "{target["root"]}"
                state_dir: "{target["state_dir"]}"
                deploy_mode: copy
                include_in_all: true
                supports_symlink: true
                runtime_profiles:
                  - hermes
            """
        )
        for target in target_entries
    )
    (targets_dir / "hermes.yaml").write_text(target_yaml, encoding="utf-8")
    members_yaml = "\n".join(f"                  - {member}" for member in members)
    (root / "skill-sets.yaml").write_text(
        textwrap.dedent(
            f"""
            version: 1
            skill_sets:
              - id: demo-set
                title: Demo Set
                members:
{members_yaml}
                default_targets:
                  - hermes-global
            """
        ),
        encoding="utf-8",
    )
    if create_in_sync_member:
        write_matching_target_copy(root, target_root)
        save_demo_deployment_record(root)


def write_matching_target_copy(root: Path, target_root: Path) -> None:
    target = target_root / "demo-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes((root / "skills" / "demo-skill" / "SKILL.md").read_bytes())


def save_demo_deployment_record(root: Path, *, target_name: str = "hermes-global") -> DeploymentRecord:
    config = load_registry(root)
    target = config.target_by_name[target_name]
    skill = selected_skills(config, ("demo-skill",))[0]
    payload = deployable_payload_hash(skill.path)
    record = DeploymentRecord(
        schema_version=1,
        target=target.name,
        skill_id=skill.id,
        deployment_mode="copy",
        source_path=str(skill.path),
        target_path=str(target.root_path / skill.id),
        payload_hash=payload.payload_hash,
        file_count=payload.file_count,
        files=payload.files,
        deployed_at="2026-06-23T00:00:00+00:00",
        deployed_by="unit-test",
        operation="deploy",
        registry_commit="test-commit",
    )
    return save_deployment_record(target, record)
