from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from skill_registry.config import load_registry
from skill_registry.validation import validate_registry


class LocalTargetOverrideTests(unittest.TestCase):
    def test_local_target_overrides_tracked_target_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_skills = root.parent / "external-skills"
            external_state = root.parent / "external-state"
            write_registry_fixture(root)
            write_local_target(
                root,
                "wsl-codex.yaml",
                f"""
                targets:
                  - name: codex
                    system: codex
                    root: "{external_skills}"
                    state_dir: "{external_state}"
                    deploy_mode: copy
                    include_in_all: false
                    supports_symlink: false
                    runtime_profiles:
                      - codex-desktop
                """,
            )

            config = load_registry(root)

        self.assertEqual(len(config.targets), 1)
        self.assertEqual(
            config.target_by_name["codex"].root,
            str(external_skills),
        )
        self.assertEqual(
            config.target_by_name["codex"].state_dir,
            str(external_state),
        )
        self.assertEqual(config.target_by_name["codex"].deploy_mode, "copy")
        self.assertFalse(config.target_by_name["codex"].include_in_all)
        self.assertFalse(config.target_by_name["codex"].supports_symlink)
        self.assertEqual(config.local_targets_dir, config.root / "targets.local")
        self.assertIn(config.local_targets_dir / "wsl-codex.yaml", config.loaded_files)

    def test_local_target_can_add_machine_local_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external_skills = root.parent / "additional-external-skills"
            write_registry_fixture(root)
            write_local_target(
                root,
                "extra.yaml",
                f"""
                targets:
                  - name: codex-desktop-wsl
                    system: codex
                    root: "{external_skills}"
                    deploy_mode: copy
                    include_in_all: false
                    supports_symlink: false
                    runtime_profiles:
                      - codex-desktop
                """,
            )

            config = load_registry(root)

        self.assertEqual(
            [target.name for target in config.targets],
            ["codex", "codex-desktop-wsl"],
        )
        self.assertEqual(config.target_by_name["codex"].deploy_mode, "symlink")
        self.assertEqual(config.target_by_name["codex-desktop-wsl"].deploy_mode, "copy")

    def test_duplicate_local_target_names_are_reported_by_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_registry_fixture(root)
            write_local_target(
                root,
                "duplicates.yaml",
                """
                targets:
                  - name: codex-local
                    system: codex
                    root: ./one
                  - name: codex-local
                    system: codex
                    root: ./two
                """,
            )

            result = validate_registry(load_registry(root))

        self.assertIn("duplicate_target", {item.code for item in result.diagnostics})


def write_registry_fixture(root: Path) -> None:
    (root / "skills").mkdir()
    (root / "targets").mkdir()
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            runtime_profiles:
              - name: codex-desktop
                kind: path-list
                visible_bin_dirs: []
                probe:
                  strategy: path-list
            """
        ).lstrip(),
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
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "targets" / "codex.yaml").write_text(
        textwrap.dedent(
            """
            targets:
              - name: codex
                system: codex
                root: ~/.codex/skills
                state_dir: ~/.codex/skill-registry-state
                deploy_mode: symlink
                include_in_all: true
                supports_symlink: true
                runtime_profiles:
                  - codex-desktop
            """
        ).lstrip(),
        encoding="utf-8",
    )


def write_local_target(root: Path, filename: str, content: str) -> None:
    local_targets_dir = root / "targets.local"
    local_targets_dir.mkdir(exist_ok=True)
    (local_targets_dir / filename).write_text(
        textwrap.dedent(content).lstrip(),
        encoding="utf-8",
    )
