from __future__ import annotations

import errno
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from skill_registry import bootstrap
from skill_registry.bootstrap import (
    BootstrapError,
    EngineDistribution,
    clear_default_registry_root,
    initialize_registry_instance,
    installed_engine_distribution,
    normalize_project_name,
    set_default_registry_root,
)
from skill_registry.cli import main
from skill_registry.config import load_registry
from skill_registry.root import RegistryRootError, resolve_registry_root
from skill_registry.validation import Diagnostic, ValidationResult, validate_registry


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ENGINE = EngineDistribution("agent-skill-registry", "9.8.7")
PRESET_EXPECTATIONS = {
    "codex": ("codex", "~/.codex/skills", "~/.codex/skill-registry-state", "codex-desktop"),
    "claude-code": (
        "claude-code",
        "~/.claude/skills",
        "~/.claude/skill-registry-state",
        "claude-code",
    ),
    "agents": ("agents", "~/.agents/skills", "~/.agents/skill-registry-state", "agents-shared"),
    "hermes-global": ("hermes", "~/.hermes/skills", "~/.hermes/skill-registry-state", "hermes"),
    "openclaw-global": (
        "openclaw",
        "~/.openclaw/skills",
        "~/.openclaw/skill-registry-state",
        "openclaw-codex-harness",
    ),
}


class InstanceBootstrapTests(unittest.TestCase):
    def test_project_name_normalization_and_empty_rejection(self) -> None:
        self.assertEqual(normalize_project_name("My Registry__2026"), "my-registry-2026")
        self.assertEqual(normalize_project_name("Über Demo"), "ber-demo")
        with self.assertRaisesRegex(BootstrapError, "empty after normalization"):
            normalize_project_name("___")

    def test_default_name_comes_from_destination_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Demo Instance"
            result = initialize_registry_instance(
                root,
                environ=isolated_environment(base),
                cwd=base,
                engine_identity=TEST_ENGINE,
            )
        self.assertEqual(result.name, "demo-instance")

    def test_project_name_cannot_shadow_engine_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "agent_skill_registry"
            with self.assertRaisesRegex(BootstrapError, "conflicts with the engine distribution"):
                initialize_registry_instance(
                    root,
                    environ=isolated_environment(base),
                    cwd=base,
                    engine_identity=TEST_ENGINE,
                )
            self.assertFalse(root.exists())

    def test_installed_distribution_identity_uses_metadata_name_and_version(self) -> None:
        distribution = SimpleNamespace(metadata={"Name": "Canonical-Engine"}, version="2.4.6")
        with patch("skill_registry.bootstrap.importlib.metadata.distribution", return_value=distribution) as lookup:
            identity = installed_engine_distribution()
        lookup.assert_called_once_with("agent-skill-registry")
        self.assertEqual(identity, EngineDistribution("Canonical-Engine", "2.4.6"))

    def test_missing_or_incomplete_distribution_metadata_is_actionable(self) -> None:
        with patch(
            "skill_registry.bootstrap.importlib.metadata.distribution",
            side_effect=bootstrap.importlib.metadata.PackageNotFoundError("agent-skill-registry"),
        ):
            with self.assertRaisesRegex(BootstrapError, "metadata was not found"):
                installed_engine_distribution()
        with patch(
            "skill_registry.bootstrap.importlib.metadata.distribution",
            return_value=SimpleNamespace(metadata={}, version=""),
        ):
            with self.assertRaisesRegex(BootstrapError, "missing Name or Version"):
                installed_engine_distribution()

    def test_generated_project_is_non_package_and_exactly_pins_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            initialize_registry_instance(
                root,
                name="Customer Skills",
                environ=isolated_environment(base),
                cwd=base,
                engine_identity=TEST_ENGINE,
            )
            pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('name = "customer-skills"', pyproject)
        self.assertIn('requires-python = ">=3.9"', pyproject)
        self.assertIn('"agent-skill-registry==9.8.7"', pyproject)
        self.assertIn("[tool.uv]\npackage = false", pyproject)
        self.assertNotIn("[build-system]", pyproject)
        self.assertNotIn("[tool.uv.sources]", pyproject)
        self.assertNotIn("file://", pyproject)

    def test_no_target_instance_has_complete_git_ready_shape_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            result = initialize_registry_instance(
                root,
                environ=isolated_environment(base),
                cwd=base,
                engine_identity=TEST_ENGINE,
            )
            validation = validate_registry(load_registry(root))
            runtime_data = yaml.safe_load((root / "runtimes.yaml").read_text(encoding="utf-8"))

            for filename in (
                "registry.yaml",
                "skill-sets.yaml",
                "runtimes.yaml",
                "runtime-dependencies.yaml",
                "pyproject.toml",
                ".gitignore",
            ):
                self.assertTrue((root / filename).is_file(), filename)
            for directory in ("skills", "targets", "proposals"):
                self.assertTrue((root / directory).is_dir(), directory)
            self.assertTrue((root / "skills" / ".gitkeep").is_file())
            self.assertTrue((root / "targets" / ".gitkeep").is_file())
            self.assertTrue((root / "proposals" / ".gitkeep").is_file())
            self.assertFalse((root / "uv.lock").exists())

        self.assertEqual(result.targets, ())
        self.assertEqual(runtime_data["runtime_profiles"], [])
        self.assertTrue(validation.ok, validation.diagnostics)

    def test_every_preset_writes_exact_target_contract_and_singular_symlink_key(self) -> None:
        for preset, (system, target_root, state_dir, runtime) in PRESET_EXPECTATIONS.items():
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "registry"
                initialize_registry_instance(
                    root,
                    targets=(preset,),
                    environ=isolated_environment(base),
                    cwd=base,
                    engine_identity=TEST_ENGINE,
                )
                target_data = yaml.safe_load((root / "targets" / f"{preset}.yaml").read_text(encoding="utf-8"))
                raw_target = target_data["targets"][0]
                parsed_target = load_registry(root).targets[0]

                self.assertEqual(raw_target["name"], preset)
                self.assertEqual(raw_target["system"], system)
                self.assertEqual(raw_target["root"], target_root)
                self.assertEqual(raw_target["state_dir"], state_dir)
                self.assertEqual(raw_target["deploy_mode"], "symlink")
                self.assertIs(raw_target["include_in_all"], False)
                self.assertIn("supports_symlink", raw_target)
                self.assertIs(raw_target["supports_symlink"], True)
                self.assertNotIn("supports_symlinks", raw_target)
                self.assertEqual(raw_target["runtime_profiles"], [runtime])
                self.assertIs(parsed_target.supports_symlink, True)
                self.assertTrue(validate_registry(load_registry(root)).ok)

    def test_duplicate_presets_are_deduplicated_in_first_occurrence_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            result = initialize_registry_instance(
                root,
                targets=("agents", "codex", "agents", "codex", "claude-code"),
                environ=isolated_environment(base),
                cwd=base,
                engine_identity=TEST_ENGINE,
            )
            target_files = sorted(path.name for path in (root / "targets").glob("*.yaml"))
            runtimes = yaml.safe_load((root / "runtimes.yaml").read_text(encoding="utf-8"))["runtime_profiles"]

        self.assertEqual(result.targets, ("agents", "codex", "claude-code"))
        self.assertEqual(target_files, ["agents.yaml", "claude-code.yaml", "codex.yaml"])
        self.assertEqual([item["name"] for item in runtimes], ["agents-shared", "codex-desktop", "claude-code"])

    def test_unknown_preset_fails_before_writing_and_lists_supported_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with self.assertRaises(BootstrapError) as raised:
                initialize_registry_instance(
                    root,
                    targets=("unknown",),
                    environ=isolated_environment(base),
                    cwd=base,
                    engine_identity=TEST_ENGINE,
                )
            self.assertFalse(root.exists())
            self.assertFalse(any(base.glob(".registry.skill-registry-init-*")))

        message = str(raised.exception)
        self.assertIn("Unknown target preset(s): unknown", message)
        for preset in bootstrap.SUPPORTED_TARGET_PRESETS:
            self.assertIn(preset, message)

    def test_regular_file_symlink_and_nonempty_destinations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)

            file_destination = base / "file"
            file_destination.write_text("owned\n", encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "not a directory"):
                initialize_registry_instance(
                    file_destination, environ=environment, cwd=base, engine_identity=TEST_ENGINE
                )
            self.assertEqual(file_destination.read_text(encoding="utf-8"), "owned\n")

            nonempty = base / "nonempty"
            nonempty.mkdir()
            (nonempty / "owned.txt").write_text("owned\n", encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "not empty"):
                initialize_registry_instance(nonempty, environ=environment, cwd=base, engine_identity=TEST_ENGINE)
            self.assertEqual((nonempty / "owned.txt").read_text(encoding="utf-8"), "owned\n")

            real = base / "real"
            real.mkdir()
            link = base / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(BootstrapError, "symbolic link"):
                initialize_registry_instance(link, environ=environment, cwd=base, engine_identity=TEST_ENGINE)

    def test_destination_metadata_permission_failure_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            root.mkdir()
            original_lstat = Path.lstat

            def guarded_lstat(path: Path, *args, **kwargs):
                if path == root:
                    raise PermissionError("destination denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", guarded_lstat):
                with self.assertRaisesRegex(BootstrapError, "Could not inspect registry instance destination"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

    def test_stage_validation_error_preserves_absent_destination(self) -> None:
        invalid = ValidationResult((Diagnostic("error", "test_error", "forced stage failure"),))
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with patch("skill_registry.bootstrap.validate_registry", return_value=invalid):
                with self.assertRaisesRegex(BootstrapError, "Staged registry instance failed validation"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )
            self.assertFalse(root.exists())
            self.assertFalse(any(base.glob(".registry.skill-registry-init-*")))

    def test_stage_cleanup_failure_is_appended_to_primary_error(self) -> None:
        invalid = ValidationResult((Diagnostic("error", "test_error", "forced stage failure"),))
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with patch("skill_registry.bootstrap.validate_registry", return_value=invalid), \
                patch("skill_registry.bootstrap._remove_staging", return_value="cleanup denied"):
                with self.assertRaises(BootstrapError) as raised:
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

        self.assertIn("forced stage failure", str(raised.exception))
        self.assertIn("staging cleanup also failed: cleanup denied", str(raised.exception))

    def test_validation_warnings_are_reported_but_do_not_block_publication(self) -> None:
        warning = Diagnostic("warning", "test_warning", "review this")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with patch(
                "skill_registry.bootstrap.validate_registry",
                side_effect=(ValidationResult((warning,)), ValidationResult((warning,))),
            ):
                result = initialize_registry_instance(
                    root,
                    environ=isolated_environment(base),
                    cwd=base,
                    engine_identity=TEST_ENGINE,
                )

            self.assertTrue(root.is_dir())
            self.assertEqual(result.warnings, ("test_warning: review this",))

    def test_final_validation_error_rolls_back_and_preserves_caller_empty_directory(self) -> None:
        valid = ValidationResult(())
        invalid = ValidationResult((Diagnostic("error", "test_error", "forced final failure"),))
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            root.mkdir()
            original_inode = root.stat().st_ino
            with patch("skill_registry.bootstrap.validate_registry", side_effect=(valid, invalid)):
                with self.assertRaisesRegex(BootstrapError, "Published registry instance failed validation"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )
            self.assertTrue(root.is_dir())
            self.assertEqual(root.stat().st_ino, original_inode)
            self.assertEqual(list(root.iterdir()), [])

    def test_copy_failure_rolls_back_owned_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with patch("skill_registry.bootstrap._copy_file", side_effect=OSError("copy denied")):
                with self.assertRaisesRegex(BootstrapError, "Could not publish registry instance"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )
            self.assertFalse(root.exists())
            self.assertFalse(any(base.glob(".registry.skill-registry-init-*")))

    def test_file_ownership_acquisition_failures_are_transactional(self) -> None:
        for failure in ("fstat", "dup"):
            for caller_owned_root in (False, True):
                for substitute_foreign in (False, True):
                    with self.subTest(
                        failure=failure,
                        caller_owned_root=caller_owned_root,
                        substitute_foreign=substitute_foreign,
                    ):
                        self._assert_file_ownership_acquisition_failure(
                            failure,
                            caller_owned_root=caller_owned_root,
                            substitute_foreign=substitute_foreign,
                        )

    def _assert_file_ownership_acquisition_failure(
        self,
        failure: str,
        *,
        caller_owned_root: bool,
        substitute_foreign: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            caller_inode: int | None = None
            if caller_owned_root:
                root.mkdir()
                caller_inode = root.stat().st_ino

            original_capture = bootstrap._capture_open_file_entry
            original_fstat = os.fstat
            original_retain = bootstrap._retain_ownership_descriptor
            target_descriptors: list[int] = []
            failed = False
            foreign_inode: int | None = None

            def fail_during_capture(path: Path, target, *args) -> bootstrap._PublishedEntry:
                nonlocal failed, foreign_inode
                if path.name != ".gitignore" or failed:
                    return original_capture(path, target, *args)

                descriptor = target.fileno()
                target_descriptors.append(descriptor)
                if substitute_foreign:
                    path.unlink()
                    path.write_text("foreign acquisition replacement\n", encoding="utf-8")
                    foreign_inode = path.stat().st_ino

                if failure == "fstat":
                    def fail_first_target_fstat(selected: int) -> os.stat_result:
                        nonlocal failed
                        if selected == descriptor and not failed:
                            failed = True
                            raise OSError(errno.EMFILE, "ownership fstat denied")
                        return original_fstat(selected)

                    with patch("skill_registry.bootstrap.os.fstat", side_effect=fail_first_target_fstat):
                        return original_capture(path, target, *args)

                failed = True
                with patch(
                    "skill_registry.bootstrap._retain_ownership_descriptor",
                    side_effect=OSError(errno.EMFILE, "ownership dup denied"),
                ):
                    return original_capture(path, target, *args)

            with patch(
                "skill_registry.bootstrap._capture_open_file_entry",
                side_effect=fail_during_capture,
            ):
                with self.assertRaises(BootstrapError) as raised:
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            self.assertTrue(failed)
            self.assertIn("Could not acquire publication ownership handle", str(raised.exception))
            self.assertTrue(target_descriptors)
            for descriptor in target_descriptors:
                with self.assertRaises(OSError) as closed:
                    original_fstat(descriptor)
                self.assertEqual(closed.exception.errno, errno.EBADF)

            if substitute_foreign:
                foreign = root / ".gitignore"
                self.assertEqual(foreign.stat().st_ino, foreign_inode)
                self.assertEqual(
                    foreign.read_text(encoding="utf-8"),
                    "foreign acquisition replacement\n",
                )
                self.assertEqual([path.name for path in root.iterdir()], [".gitignore"])
            elif caller_owned_root:
                self.assertEqual(root.stat().st_ino, caller_inode)
                self.assertEqual(list(root.iterdir()), [])
            else:
                self.assertFalse(root.exists())

            if caller_inode is not None:
                self.assertEqual(root.stat().st_ino, caller_inode)
            self.assertEqual(list(base.glob(".registry.skill-registry-file-acquisition-*")), [])

    def test_file_ownership_acquisition_restore_collision_preserves_both_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            root.mkdir()
            root_inode = root.stat().st_ino
            original_capture = bootstrap._capture_open_file_entry
            original_restore = bootstrap._restore_quarantined_path
            target_descriptor: int | None = None
            collided = False

            def substitute_then_fail(path: Path, target, *args) -> bootstrap._PublishedEntry:
                nonlocal target_descriptor
                if path.name != ".gitignore":
                    return original_capture(path, target, *args)
                target_descriptor = target.fileno()
                path.unlink()
                path.write_text("foreign quarantined file\n", encoding="utf-8")
                with patch(
                    "skill_registry.bootstrap._retain_ownership_descriptor",
                    side_effect=OSError(errno.EMFILE, "ownership dup denied"),
                ):
                    return original_capture(path, target, *args)

            def collide_before_restore(
                quarantined_path: Path,
                public_path: Path,
                expected: os.stat_result | None,
            ) -> str:
                nonlocal collided
                if public_path.name == ".gitignore" and not collided:
                    collided = True
                    public_path.write_text("foreign public collision\n", encoding="utf-8")
                return original_restore(quarantined_path, public_path, expected)

            with patch(
                "skill_registry.bootstrap._capture_open_file_entry",
                side_effect=substitute_then_fail,
            ), patch(
                "skill_registry.bootstrap._restore_quarantined_path",
                side_effect=collide_before_restore,
            ):
                with self.assertRaises(BootstrapError) as raised:
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            quarantine = next(base.glob(".registry.skill-registry-file-acquisition-*"))
            quarantined_file = quarantine / "entry"
            self.assertTrue(collided)
            self.assertIn("could not restore", str(raised.exception))
            self.assertIn(f"preserved at {quarantined_file}", str(raised.exception))
            self.assertIn("file-acquisition quarantine retained", str(raised.exception))
            self.assertEqual(root.stat().st_ino, root_inode)
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "foreign public collision\n")
            self.assertEqual(quarantined_file.read_text(encoding="utf-8"), "foreign quarantined file\n")
            self.assertIsNotNone(target_descriptor)
            with self.assertRaises(OSError) as closed:
                os.fstat(target_descriptor)
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_file_ownership_acquisition_reports_target_close_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            original_capture = bootstrap._capture_open_file_entry
            original_fstat = os.fstat
            original_close = bootstrap._close_open_publication_file
            target_descriptor: int | None = None
            failed = False

            def fail_initial_fstat(path: Path, target, *args) -> bootstrap._PublishedEntry:
                nonlocal target_descriptor, failed
                if path.name != ".gitignore":
                    return original_capture(path, target, *args)
                target_descriptor = target.fileno()

                def fail_once(selected: int) -> os.stat_result:
                    nonlocal failed
                    if selected == target_descriptor and not failed:
                        failed = True
                        raise OSError(errno.EMFILE, "ownership fstat denied")
                    return original_fstat(selected)

                with patch("skill_registry.bootstrap.os.fstat", side_effect=fail_once):
                    return original_capture(path, target, *args)

            def close_then_fail(target, path: Path) -> None:
                original_close(target, path)
                raise OSError(errno.EIO, "target close denied")

            with patch(
                "skill_registry.bootstrap._capture_open_file_entry",
                side_effect=fail_initial_fstat,
            ), patch(
                "skill_registry.bootstrap._close_open_publication_file",
                side_effect=close_then_fail,
            ):
                with self.assertRaises(BootstrapError) as raised:
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            message = str(raised.exception)
            self.assertIn("ownership fstat denied", message)
            self.assertIn("Could not close exclusive publication handle", message)
            self.assertIn("target close denied", message)
            self.assertFalse(root.exists())
            self.assertIsNotNone(target_descriptor)
            with self.assertRaises(OSError) as closed:
                original_fstat(target_descriptor)
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertEqual(list(base.glob(".registry.skill-registry-file-acquisition-*")), [])

    def test_file_substitution_before_descriptor_capture_preserves_foreign_file_for_absent_destination(self) -> None:
        self._assert_file_substitution_preserved(caller_owned_root=False)

    def test_file_substitution_before_descriptor_capture_preserves_foreign_file_and_caller_root(self) -> None:
        self._assert_file_substitution_preserved(caller_owned_root=True)

    def test_directory_substitution_before_publication_identity_check_preserves_foreign_directory_for_absent_destination(
        self,
    ) -> None:
        self._assert_directory_substitution_preserved(caller_owned_root=False)

    def test_directory_substitution_before_publication_identity_check_preserves_foreign_directory_and_caller_root(
        self,
    ) -> None:
        self._assert_directory_substitution_preserved(caller_owned_root=True)

    def _assert_file_substitution_preserved(self, *, caller_owned_root: bool) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            caller_inode: int | None = None
            if caller_owned_root:
                root.mkdir()
                caller_inode = root.stat().st_ino

            original_capture = bootstrap._capture_open_file_entry
            foreign_inode: int | None = None
            substituted = False

            def substitute_then_capture(path: Path, target, *args) -> bootstrap._PublishedEntry:
                nonlocal foreign_inode, substituted
                if path.name == "registry.yaml" and not substituted:
                    substituted = True
                    path.unlink()
                    path.write_text("version: 1\n", encoding="utf-8")
                    foreign_inode = path.stat().st_ino
                return original_capture(path, target, *args)

            with patch("skill_registry.bootstrap._capture_open_file_entry", side_effect=substitute_then_capture):
                with self.assertRaisesRegex(BootstrapError, "changed during publication"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            foreign = root / "registry.yaml"
            self.assertTrue(substituted)
            self.assertEqual(foreign.read_text(encoding="utf-8"), "version: 1\n")
            self.assertEqual(foreign.stat().st_ino, foreign_inode)
            self.assertEqual([path.name for path in root.iterdir()], ["registry.yaml"])
            if caller_inode is not None:
                self.assertEqual(root.stat().st_ino, caller_inode)

    def _assert_directory_substitution_preserved(self, *, caller_owned_root: bool) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            caller_inode: int | None = None
            if caller_owned_root:
                root.mkdir()
                caller_inode = root.stat().st_ino

            original_stat = bootstrap._stat_published_path
            foreign_inode: int | None = None
            substituted = False

            def substitute_then_stat(path: Path) -> os.stat_result:
                nonlocal foreign_inode, substituted
                if path.name == "skills" and not substituted:
                    substituted = True
                    path.rmdir()
                    path.mkdir()
                    foreign_inode = path.stat().st_ino
                    (path / "foreign.txt").write_text("foreign directory content\n", encoding="utf-8")
                return original_stat(path)

            with patch("skill_registry.bootstrap._stat_published_path", side_effect=substitute_then_stat):
                with self.assertRaisesRegex(BootstrapError, "directory changed during publication"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            foreign = root / "skills"
            self.assertTrue(substituted)
            self.assertEqual(foreign.stat().st_ino, foreign_inode)
            self.assertEqual((foreign / "foreign.txt").read_text(encoding="utf-8"), "foreign directory content\n")
            self.assertEqual([path.name for path in root.iterdir()], ["skills"])
            if caller_inode is not None:
                self.assertEqual(root.stat().st_ino, caller_inode)

    def test_file_substitution_at_rollback_quarantine_preserves_foreign_file_for_absent_destination(self) -> None:
        self._assert_rollback_substitution_preserved("file", caller_owned_root=False)

    def test_file_substitution_at_rollback_quarantine_preserves_foreign_file_and_caller_root(self) -> None:
        self._assert_rollback_substitution_preserved("file", caller_owned_root=True)

    def test_child_directory_substitution_at_rollback_quarantine_preserves_foreign_directory_for_absent_destination(
        self,
    ) -> None:
        self._assert_rollback_substitution_preserved("directory", caller_owned_root=False)

    def test_child_directory_substitution_at_rollback_quarantine_preserves_foreign_directory_and_caller_root(
        self,
    ) -> None:
        self._assert_rollback_substitution_preserved("directory", caller_owned_root=True)

    def test_created_root_substitution_at_rollback_quarantine_preserves_foreign_root(self) -> None:
        self._assert_rollback_substitution_preserved("root", caller_owned_root=False)

    def test_rollback_restoration_collision_preserves_public_and_quarantined_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            root.mkdir()
            public_path = root / "registry.yaml"
            public_path.write_text("foreign moved to quarantine\n", encoding="utf-8")
            metadata = public_path.stat()
            publication = bootstrap._Publication(
                root=root,
                root_created=False,
                root_device=root.stat().st_dev,
                root_inode=root.stat().st_ino,
                entries=(
                    bootstrap._PublishedEntry(
                        path=public_path,
                        device=metadata.st_dev,
                        inode=metadata.st_ino + 1,
                        is_directory=False,
                    ),
                ),
            )
            original_lstat = Path.lstat
            collision_inode: int | None = None

            def create_collision_before_quarantine_inspection(path: Path, *args, **kwargs):
                nonlocal collision_inode
                if path.name == "entry-0000" and "skill-registry-rollback" in path.parent.name:
                    public_path.write_text("foreign public collision\n", encoding="utf-8")
                    collision_inode = public_path.stat().st_ino
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", create_collision_before_quarantine_inspection):
                error = bootstrap._rollback_publication(publication)

            quarantine = next(base.glob(".registry.skill-registry-rollback-*"))
            quarantined_path = quarantine / "entry-0000"
            self.assertIn("could not restore", error)
            self.assertIn(f"preserved at {quarantined_path}", error)
            self.assertEqual(public_path.stat().st_ino, collision_inode)
            self.assertEqual(public_path.read_text(encoding="utf-8"), "foreign public collision\n")
            self.assertEqual(
                quarantined_path.read_text(encoding="utf-8"),
                "foreign moved to quarantine\n",
            )

    def _assert_rollback_substitution_preserved(self, kind: str, *, caller_owned_root: bool) -> None:
        valid = ValidationResult(())
        invalid = ValidationResult((Diagnostic("error", "test_error", "force rollback"),))
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            caller_inode: int | None = None
            if caller_owned_root:
                root.mkdir()
                caller_inode = root.stat().st_ino

            if kind == "file":
                target = root / "registry.yaml"
            elif kind == "directory":
                target = root / "skills"
            else:
                target = root

            original_quarantine = bootstrap._quarantine_public_path
            original_retain = bootstrap._retain_ownership_descriptor
            retained_descriptors: list[int] = []
            foreign_inode: int | None = None
            substituted = False

            def track_retained_descriptor(descriptor: int) -> int:
                retained = original_retain(descriptor)
                retained_descriptors.append(retained)
                return retained

            def substitute_then_quarantine(source: Path, destination: Path) -> None:
                nonlocal foreign_inode, substituted
                if source == target and not substituted:
                    substituted = True
                    source_metadata = source.lstat()
                    self.assertTrue(
                        any(
                            metadata.st_dev == source_metadata.st_dev
                            and metadata.st_ino == source_metadata.st_ino
                            for metadata in (os.fstat(descriptor) for descriptor in retained_descriptors)
                        ),
                        f"no retained ownership descriptor for {source}",
                    )
                    if kind == "file":
                        source.unlink()
                        source.write_text("foreign rollback file\n", encoding="utf-8")
                    else:
                        source.rmdir()
                        source.mkdir()
                        (source / "foreign.txt").write_text("foreign rollback directory\n", encoding="utf-8")
                    foreign_inode = source.stat().st_ino
                original_quarantine(source, destination)

            with patch("skill_registry.bootstrap.validate_registry", side_effect=(valid, invalid)), \
                patch(
                    "skill_registry.bootstrap._retain_ownership_descriptor",
                    side_effect=track_retained_descriptor,
                ), \
                patch(
                    "skill_registry.bootstrap._quarantine_public_path",
                    side_effect=substitute_then_quarantine,
                ):
                with self.assertRaisesRegex(BootstrapError, "publication rollback also failed"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            self.assertTrue(substituted)
            self.assertEqual(target.stat().st_ino, foreign_inode)
            if kind == "file":
                self.assertEqual(target.read_text(encoding="utf-8"), "foreign rollback file\n")
            else:
                self.assertEqual(
                    (target / "foreign.txt").read_text(encoding="utf-8"),
                    "foreign rollback directory\n",
                )
            if caller_inode is not None:
                self.assertEqual(root.stat().st_ino, caller_inode)
            self.assertEqual(list(base.glob(".registry.skill-registry-rollback-*")), [])

    def test_publication_ownership_descriptors_close_on_success_and_rollback(self) -> None:
        valid = ValidationResult(())
        invalid = ValidationResult((Diagnostic("error", "test_error", "force rollback"),))
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "registry"
                retained_descriptors: list[int] = []
                original_retain = bootstrap._retain_ownership_descriptor

                def track_retained_descriptor(descriptor: int) -> int:
                    retained = original_retain(descriptor)
                    retained_descriptors.append(retained)
                    return retained

                validation = (
                    patch("skill_registry.bootstrap.validate_registry", side_effect=(valid, invalid))
                    if rollback
                    else patch("skill_registry.bootstrap.validate_registry", return_value=valid)
                )
                with validation, patch(
                    "skill_registry.bootstrap._retain_ownership_descriptor",
                    side_effect=track_retained_descriptor,
                ):
                    if rollback:
                        with self.assertRaisesRegex(BootstrapError, "Published registry instance failed validation"):
                            initialize_registry_instance(
                                root,
                                environ=isolated_environment(base),
                                cwd=base,
                                engine_identity=TEST_ENGINE,
                            )
                    else:
                        initialize_registry_instance(
                            root,
                            environ=isolated_environment(base),
                            cwd=base,
                            engine_identity=TEST_ENGINE,
                        )

                self.assertTrue(retained_descriptors)
                for descriptor in retained_descriptors:
                    with self.assertRaises(OSError) as raised:
                        os.fstat(descriptor)
                    self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_created_root_post_rename_probe_failure_uses_immediate_ownership_ledger(self) -> None:
        self._assert_post_rename_probe_failure(caller_owned_root=False, fail_root=True)

    def test_child_directory_post_rename_probe_failure_rolls_back_absent_destination(self) -> None:
        self._assert_post_rename_probe_failure(caller_owned_root=False, fail_root=False)

    def test_child_directory_post_rename_probe_failure_preserves_caller_root(self) -> None:
        self._assert_post_rename_probe_failure(caller_owned_root=True, fail_root=False)

    def _assert_post_rename_probe_failure(self, *, caller_owned_root: bool, fail_root: bool) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            caller_inode: int | None = None
            if caller_owned_root:
                root.mkdir()
                caller_inode = root.stat().st_ino

            original_stat = bootstrap._stat_published_path
            failed = False

            def fail_selected_probe(path: Path) -> os.stat_result:
                nonlocal failed
                selected = path == root if fail_root else path == root / "skills"
                if selected and not failed:
                    failed = True
                    raise PermissionError("post-rename probe denied")
                return original_stat(path)

            with patch("skill_registry.bootstrap._stat_published_path", side_effect=fail_selected_probe):
                with self.assertRaisesRegex(BootstrapError, "post-rename probe denied"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            self.assertTrue(failed)
            if caller_inode is None:
                self.assertFalse(root.exists())
            else:
                self.assertEqual(root.stat().st_ino, caller_inode)
                self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(base.glob(".registry.skill-registry-rollback-*")), [])

    def test_native_no_replace_library_and_symbol_failures_are_bootstrap_errors(self) -> None:
        source = Path("source")
        destination = Path("destination")
        for platform_name, label, symbol in (
            ("darwin", "Darwin", "renamex_np"),
            ("linux", "Linux", "renameat2"),
        ):
            with self.subTest(platform=platform_name, failure="library"), \
                patch.object(bootstrap.sys, "platform", platform_name), \
                patch.object(bootstrap.ctypes, "CDLL", side_effect=OSError("native library denied")):
                with self.assertRaisesRegex(BootstrapError, f"load native atomic no-replace rename support on {label}"):
                    bootstrap._rename_noreplace(source, destination)

            with self.subTest(platform=platform_name, failure="symbol"), \
                patch.object(bootstrap.sys, "platform", platform_name), \
                patch.object(bootstrap.ctypes, "CDLL", return_value=SimpleNamespace()):
                with self.assertRaisesRegex(BootstrapError, f"symbol {symbol} is unavailable on {label}"):
                    bootstrap._rename_noreplace(source, destination)

    def test_native_no_replace_unsupported_platform_is_bootstrap_error(self) -> None:
        with patch.object(bootstrap.sys, "platform", "win32"), \
            patch.object(bootstrap.ctypes, "CDLL") as load_library:
            with self.assertRaisesRegex(BootstrapError, "unsupported on platform win32"):
                bootstrap._rename_noreplace(Path("source"), Path("destination"))
        load_library.assert_not_called()

    def test_missing_native_symbol_is_cli_json_exit_three_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(bootstrap.sys, "platform", "darwin"), \
                patch.object(bootstrap.ctypes, "CDLL", return_value=SimpleNamespace()), \
                patch("skill_registry.bootstrap.installed_engine_distribution", return_value=TEST_ENGINE), \
                redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = main(["init", str(root), "--json"])

            self.assertEqual(return_code, 3)
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn("Traceback", stdout.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(set(payload), {"error", "ok"})
            self.assertFalse(payload["ok"])
            self.assertIn("renamex_np", payload["error"])
            self.assertFalse(root.exists())

    def test_publication_race_does_not_merge_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            destination = base / "registry"
            state = bootstrap._inspect_destination(destination)
            staging = base / "staging"
            staging.mkdir()
            (staging / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
            destination.mkdir()
            (destination / "foreign.txt").write_text("foreign\n", encoding="utf-8")

            with self.assertRaisesRegex(BootstrapError, "appeared during publication"):
                bootstrap._publish_staging(staging, destination, state)

            self.assertEqual((destination / "foreign.txt").read_text(encoding="utf-8"), "foreign\n")

    def test_foreign_content_added_during_publication_is_preserved_and_init_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            calls = 0

            def racing_validation(path: Path, *, label: str) -> tuple[str, ...]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    (path / "foreign.txt").write_text("foreign\n", encoding="utf-8")
                return ()

            with patch("skill_registry.bootstrap._validated_warnings", side_effect=racing_validation):
                with self.assertRaisesRegex(BootstrapError, "changed during publication"):
                    initialize_registry_instance(
                        root,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )

            self.assertEqual((root / "foreign.txt").read_text(encoding="utf-8"), "foreign\n")
            self.assertEqual([path.name for path in root.iterdir()], ["foreign.txt"])

    def test_repeated_init_refuses_to_overwrite_first_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            environment = isolated_environment(base)
            initialize_registry_instance(root, environ=environment, cwd=base, engine_identity=TEST_ENGINE)
            original = (root / "pyproject.toml").read_text(encoding="utf-8")
            with self.assertRaisesRegex(BootstrapError, "not empty"):
                initialize_registry_instance(root, environ=environment, cwd=base, engine_identity=TEST_ENGINE)
            self.assertEqual((root / "pyproject.toml").read_text(encoding="utf-8"), original)

    def test_pointer_failure_after_scaffold_preserves_valid_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            with patch(
                "skill_registry.bootstrap.set_default_registry_root",
                side_effect=RegistryRootError("pointer denied"),
            ):
                with self.assertRaisesRegex(BootstrapError, "was created and validated") as raised:
                    initialize_registry_instance(
                        root,
                        set_default=True,
                        environ=isolated_environment(base),
                        cwd=base,
                        engine_identity=TEST_ENGINE,
                    )
            self.assertTrue(validate_registry(load_registry(root)).ok)
            self.assertIn("skill-registry root set-default", str(raised.exception))

    def test_init_does_not_invoke_subprocess_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.object(subprocess, "run") as run, \
                patch.object(subprocess, "Popen") as popen, \
                patch.object(socket, "create_connection") as connect, \
                patch.object(urllib.request, "urlopen") as urlopen:
                initialize_registry_instance(
                    base / "registry",
                    targets=("codex",),
                    environ=isolated_environment(base),
                    cwd=base,
                    engine_identity=TEST_ENGINE,
                )
            run.assert_not_called()
            popen.assert_not_called()
            connect.assert_not_called()
            urlopen.assert_not_called()

    def test_unknown_runtime_reference_remains_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            initialize_registry_instance(
                root,
                environ=isolated_environment(base),
                cwd=base,
                engine_identity=TEST_ENGINE,
            )
            (root / "targets" / ".gitkeep").unlink()
            (root / "targets" / "broken.yaml").write_text(
                "targets:\n"
                "  - name: broken\n"
                "    system: demo\n"
                "    root: ~/.demo/skills\n"
                "    runtime_profiles:\n"
                "      - missing-runtime\n",
                encoding="utf-8",
            )
            validation = validate_registry(load_registry(root))

        self.assertFalse(validation.ok)
        self.assertIn("missing_runtime_profiles", {item.code for item in validation.errors})
        self.assertIn("unknown_runtime_profile", {item.code for item in validation.errors})


class DefaultPointerTests(unittest.TestCase):
    def test_set_default_writes_normalized_absolute_path_and_exact_newline_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base, use_xdg=True)
            root = write_marker_registry(base / "registry")
            with patch("skill_registry.bootstrap.os.replace", wraps=os.replace) as replace:
                result = set_default_registry_root(root, environ=environment, cwd=base)

            self.assertTrue(result.changed)
            self.assertEqual(result.root, root.resolve())
            self.assertEqual(
                result.pointer.parent,
                (Path(environment["XDG_CONFIG_HOME"]) / "skill-registry").resolve(strict=False),
            )
            self.assertEqual(result.pointer.read_bytes(), f"{root.resolve()}\n".encode())
            replace.assert_called_once()
            source, destination = replace.call_args.args
            self.assertEqual(Path(source).parent, result.pointer.parent)
            self.assertEqual(Path(destination), result.pointer)

    def test_set_default_is_idempotent_and_resolves_through_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            root = write_marker_registry(base / "registry")
            first = set_default_registry_root(root, environ=environment, cwd=base)
            second = set_default_registry_root(root, environ=environment, cwd=base)
            unrelated = base / "unrelated"
            unrelated.mkdir()
            resolved = resolve_registry_root(cwd=unrelated, environ=environment)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(resolved.path, root.resolve())
        self.assertEqual(resolved.source, "default")

    def test_clear_default_is_idempotent_and_preserves_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            root = write_marker_registry(base / "registry")
            pointer = set_default_registry_root(root, environ=environment, cwd=base).pointer
            first = clear_default_registry_root(environ=environment, cwd=base)
            second = clear_default_registry_root(environ=environment, cwd=base)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertFalse(pointer.exists())
            self.assertTrue((root / "registry.yaml").is_file())

    def test_pointer_metadata_and_write_permission_failures_are_source_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            root = write_marker_registry(base / "registry")
            pointer = (
                Path(environment["HOME"]) / ".config" / "skill-registry" / "default-root"
            ).resolve(strict=False)
            original_lstat = Path.lstat

            def guarded_lstat(path: Path, *args, **kwargs):
                if path == pointer:
                    raise PermissionError("pointer metadata denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", guarded_lstat):
                with self.assertRaisesRegex(RegistryRootError, "Could not inspect default registry root pointer"):
                    set_default_registry_root(root, environ=environment, cwd=base)

            with patch("skill_registry.bootstrap.os.replace", side_effect=PermissionError("replace denied")):
                with self.assertRaisesRegex(RegistryRootError, "Could not write default registry root pointer"):
                    set_default_registry_root(root, environ=environment, cwd=base)
            self.assertFalse(pointer.exists())
            if pointer.parent.exists():
                self.assertEqual(list(pointer.parent.iterdir()), [])

    def test_pointer_temp_cleanup_failure_is_appended_to_write_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            root = write_marker_registry(base / "registry")
            original_unlink = Path.unlink

            def denied_temp_unlink(path: Path, *args, **kwargs):
                if path.name.startswith(".default-root."):
                    raise PermissionError("temp cleanup denied")
                return original_unlink(path, *args, **kwargs)

            with patch("skill_registry.bootstrap.os.replace", side_effect=PermissionError("replace denied")), \
                patch.object(Path, "unlink", denied_temp_unlink):
                with self.assertRaises(RegistryRootError) as raised:
                    set_default_registry_root(root, environ=environment, cwd=base)

        self.assertIn("replace denied", str(raised.exception))
        self.assertIn("temporary pointer cleanup also failed: temp cleanup denied", str(raised.exception))

    def test_missing_home_fails_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_marker_registry(base / "registry")
            with self.assertRaisesRegex(RegistryRootError, "HOME is not set"):
                set_default_registry_root(root, environ={}, cwd=base)

    def test_invalid_registry_root_is_rejected_before_pointer_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            invalid = base / "invalid"
            invalid.mkdir()
            with self.assertRaisesRegex(RegistryRootError, "does not contain a regular registry.yaml"):
                set_default_registry_root(invalid, environ=environment, cwd=base)
            pointer = Path(environment["HOME"]) / ".config" / "skill-registry" / "default-root"
            self.assertFalse(pointer.exists())


class BootstrapCliTests(unittest.TestCase):
    def test_init_json_contract_and_implicit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            result = run_cli(
                "--json",
                "init",
                str(root),
                "--name",
                "CLI Demo",
                "--target",
                "codex",
                "--target",
                "agents",
                cwd=base,
                home=base / "home",
            )
            validation = run_cli("validate", "--json", cwd=root, home=base / "home")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload),
            {
                "ok",
                "root",
                "name",
                "engine_distribution",
                "engine_version",
                "targets",
                "default_set",
                "warnings",
                "next_steps",
            },
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "cli-demo")
        self.assertEqual(payload["targets"], ["codex", "agents"])
        self.assertFalse(payload["default_set"])
        self.assertEqual(validation.returncode, 0, validation.stderr + validation.stdout)
        self.assertTrue(json.loads(validation.stdout)["ok"])

    def test_init_human_output_contains_required_fields_and_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            result = run_cli("init", "registry", cwd=base, home=base / "home")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("root:", result.stdout)
        self.assertIn("name: registry", result.stdout)
        self.assertIn("engine: agent-skill-registry==", result.stdout)
        self.assertIn("targets: (none)", result.stdout)
        self.assertIn("uv sync", result.stdout)
        self.assertIn("uv run skill-registry validate", result.stdout)

    def test_unknown_preset_json_error_is_exit_three_with_empty_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            result = run_cli(
                "init",
                str(root),
                "--target",
                "inaccessible",
                "--json",
                cwd=base,
                home=base / "home",
            )

        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("Traceback", result.stdout)
        self.assertEqual(set(json.loads(result.stdout)), {"error", "ok"})
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertFalse(root.exists())

    def test_root_show_set_and_clear_json_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_marker_registry(base / "registry")
            home = base / "home"
            set_result = run_cli("root", "set-default", str(root), "--json", cwd=base, home=home)
            unrelated = base / "unrelated"
            unrelated.mkdir()
            show_result = run_cli("root", "show", "--json", cwd=unrelated, home=home)
            explicit_show = run_cli(
                "--root", str(root), "root", "show", "--json", cwd=unrelated, home=home
            )
            clear_result = run_cli("root", "clear-default", "--json", cwd=unrelated, home=home)
            second_clear = run_cli("root", "clear-default", "--json", cwd=unrelated, home=home)

        self.assertEqual(set_result.returncode, 0, set_result.stderr + set_result.stdout)
        self.assertEqual(json.loads(set_result.stdout)["source"], "default-pointer")
        self.assertTrue(json.loads(set_result.stdout)["changed"])
        self.assertEqual(show_result.returncode, 0, show_result.stderr + show_result.stdout)
        self.assertEqual(json.loads(show_result.stdout)["source"], "default")
        self.assertEqual(json.loads(explicit_show.stdout)["source"], "explicit")
        self.assertEqual(json.loads(clear_result.stdout)["root"], None)
        self.assertTrue(json.loads(clear_result.stdout)["changed"])
        self.assertFalse(json.loads(second_clear.stdout)["changed"])

    def test_inaccessible_root_source_is_json_exit_three_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_marker_registry(base / "registry")
            marker = (root / "registry.yaml").resolve()
            original_stat = Path.stat
            stdout = io.StringIO()
            stderr = io.StringIO()

            def guarded_stat(path: Path, *args, **kwargs):
                if path == marker:
                    raise PermissionError("root marker denied")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", guarded_stat):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    return_code = main(["root", "set-default", str(root), "--json"])

        self.assertEqual(return_code, 3)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("Traceback", stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload), {"error", "ok"})
        self.assertFalse(payload["ok"])
        self.assertIn("Could not inspect registry.yaml for Explicit registry root", payload["error"])

    def test_set_default_init_sets_pointer_only_after_valid_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            home = base / "home"
            result = run_cli(
                "init", str(root), "--set-default", "--json", cwd=base, home=home
            )
            pointer = home / ".config" / "skill-registry" / "default-root"
            pointer_content = pointer.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["default_set"])
        self.assertEqual(pointer_content, f"{root.resolve()}\n")


def isolated_environment(base: Path, *, use_xdg: bool = False) -> dict[str, str]:
    environment = {"HOME": str(base / "home")}
    if use_xdg:
        environment["XDG_CONFIG_HOME"] = str(base / "xdg")
    return environment


def write_marker_registry(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    return root


def run_cli(*args: str, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["HOME"] = str(home)
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("SKILL_REGISTRY_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "skill_registry", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


if __name__ == "__main__":
    unittest.main()
