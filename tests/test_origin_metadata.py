from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from skill_registry.config import ConfigError, load_registry
from skill_registry.skills import load_skill
from skill_registry.validation import ValidationResult, validate_registry


class OriginMetadataTests(unittest.TestCase):
    def test_valid_external_skill_origin_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = write_origin_fixture_registry(
                root,
                textwrap.dedent(
                    """
                    origin:
                      kind: external_skill
                      url: https://github.com/example/skills
                      retrieved_commit: 0123456789abcdef0123456789abcdef01234567
                    """
                ),
            )

            result = validate_registry(load_registry(root))
            skill = load_skill(skill_dir)

        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(skill.origin["kind"], "external_skill")
        with self.assertRaises(TypeError):
            skill.origin["kind"] = "manual"  # type: ignore[index]

    def test_missing_origin_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root)

            result = validate_registry(load_registry(root))

        self.assertTrue(result.ok, result.diagnostics)

    def test_explicit_empty_origin_reports_missing_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root, "origin: {}\n")

            result = validate_registry(load_registry(root))

        self.assertIn("skill_origin_missing_kind", diagnostic_codes(result))

    def test_non_mapping_origin_fails_config_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = write_origin_fixture_registry(
                root,
                textwrap.dedent(
                    """
                    origin:
                      - not-a-mapping
                    """
                ),
            )

            with self.assertRaises(ConfigError):
                load_skill(skill_dir)

    def test_missing_origin_kind_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(
                root,
                textwrap.dedent(
                    """
                    origin:
                      url: https://github.com/example/skills
                    """
                ),
            )

            result = validate_registry(load_registry(root))

        self.assertIn("skill_origin_missing_kind", diagnostic_codes(result))

    def test_malformed_retrieved_commit_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(
                root,
                textwrap.dedent(
                    """
                    origin:
                      kind: external_skill
                      url: https://github.com/example/skills
                      retrieved_commit: not-a-commit
                    """
                ),
            )

            result = validate_registry(load_registry(root))

        self.assertIn("skill_origin_invalid_retrieved_commit", diagnostic_codes(result))

    def test_local_zip_without_source_path_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(
                root,
                textwrap.dedent(
                    """
                    origin:
                      kind: local_zip
                    """
                ),
            )

            result = validate_registry(load_registry(root))

        self.assertIn("skill_origin_missing_source_path", diagnostic_codes(result))

    def test_valid_skill_dependencies_pass_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root, "skill_dependencies:\n  - helper-skill\n")
            write_skill(root, "helper-skill")

            result = validate_registry(load_registry(root))
            skill = load_skill(root / "skills" / "demo-skill")

        self.assertTrue(result.ok, result.diagnostics)
        self.assertEqual(skill.skill_dependencies, ("helper-skill",))

    def test_missing_skill_dependency_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root, "skill_dependencies:\n  - missing-skill\n")

            result = validate_registry(load_registry(root))

        self.assertIn("unknown_skill_dependency", diagnostic_codes(result))

    def test_self_skill_dependency_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root, "skill_dependencies:\n  - demo-skill\n")

            result = validate_registry(load_registry(root))

        self.assertIn("skill_dependency_self_reference", diagnostic_codes(result))

    def test_valid_skill_set_passes_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root)
            (root / "skill-sets.yaml").write_text(
                textwrap.dedent(
                    """
                    version: 1
                    skill_sets:
                      - id: demo-set
                        title: Demo Set
                        members:
                          - demo-skill
                        default_targets:
                          - demo-target
                    """
                ),
                encoding="utf-8",
            )

            result = validate_registry(load_registry(root))

        self.assertTrue(result.ok, result.diagnostics)

    def test_unknown_skill_set_member_and_target_report_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_origin_fixture_registry(root)
            (root / "skill-sets.yaml").write_text(
                textwrap.dedent(
                    """
                    version: 1
                    skill_sets:
                      - id: demo-set
                        members:
                          - missing-skill
                        default_targets:
                          - missing-target
                    """
                ),
                encoding="utf-8",
            )

            result = validate_registry(load_registry(root))

        codes = diagnostic_codes(result)
        self.assertIn("unknown_skill_set_member", codes)
        self.assertIn("unknown_skill_set_default_target", codes)


def diagnostic_codes(result: ValidationResult) -> set[str]:
    return {item.code for item in result.diagnostics}


def write_origin_fixture_registry(root: Path, metadata_extra: str = "") -> Path:
    skills_dir = root / "skills"
    targets_dir = root / "targets"
    skill_dir = skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    targets_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")
    metadata = "id: demo-skill\ntitle: Demo Skill\n"
    if metadata_extra:
        metadata += textwrap.dedent(metadata_extra).lstrip("\n")
    (skill_dir / "skill.yaml").write_text(metadata, encoding="utf-8")
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            runtime_profiles:
              - name: demo-runtime
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
    (targets_dir / "demo.yaml").write_text(
        textwrap.dedent(
            """
            targets:
              - name: demo-target
                system: demo
                root: ./target-skills
                deploy_mode: symlink
                include_in_all: true
                supports_symlink: true
                runtime_profiles:
                  - demo-runtime
            """
        ),
        encoding="utf-8",
    )
    return skill_dir


def write_skill(root: Path, skill_id: str) -> Path:
    skill_dir = root / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {skill_id}\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(f"id: {skill_id}\ntitle: {skill_id}\n", encoding="utf-8")
    return skill_dir
