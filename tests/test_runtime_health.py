from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]

from skill_registry.config import load_registry
from skill_registry.deploy import apply_deploy
from skill_registry.hashing import deployable_payload_hash
from skill_registry.skills import selected_skills
from skill_registry.state import (
    DeploymentRecord,
    deployment_record_path,
    load_deployment_record,
    save_deployment_record,
)


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="skill-registry-cli-environment-") as temp:
        environment_root = Path(temp)
        home = environment_root / "home"
        config_home = environment_root / "config"
        home.mkdir()
        config_home.mkdir()

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("SKILL_REGISTRY_ROOT", None)
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(config_home)
        return subprocess.run(
            [sys.executable, "-m", "skill_registry", *args],
            cwd=cwd or REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


class RuntimeHealthTests(unittest.TestCase):
    def test_validate_accepts_generic_fixture_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fixture_registry(root, bin_dir)

            result = run_cli("--root", str(root), "validate", "--json")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def test_tool_health_reports_available_bin_in_runtime_visible_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_tool = bin_dir / "demo-tool"
            fake_tool.write_text("#!/bin/sh\nprintf '1.2.3\\n'\n", encoding="utf-8")
            fake_tool.chmod(fake_tool.stat().st_mode | stat.S_IXUSR)
            write_fixture_registry(root, bin_dir)

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "available")

    def test_tool_health_returns_runtime_failure_for_missing_bin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fixture_registry(root, bin_dir, bin_name="missing-tool")

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime")

        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "missing")

    def test_tool_health_reports_available_wrapper_without_runtime_bin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root)

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime", "--skill", "demo-skill")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["kind"], "wrapper")
        self.assertEqual(payload["results"][0]["dependency_name"], "demo-tool")
        self.assertEqual(payload["results"][0]["skill_ids"], ["demo-skill"])
        self.assertEqual(payload["results"][0]["status"], "available")
        self.assertTrue(payload["results"][0]["resolved_path"].endswith("scripts/demo-tool"))

    def test_tool_health_bin_filter_matches_wrapper_wraps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root, wrapper_name="demo-wrapper", wraps=("demo-tool",))

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime", "--bin", "demo-tool")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["kind"], "wrapper")
        self.assertEqual(payload["results"][0]["dependency_name"], "demo-wrapper")

    def test_doctor_passes_for_wrapper_managed_missing_direct_bin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root, visibility_policy="skill_wrapper_planned")

            result = run_cli("--root", str(root), "--json", "doctor", "--runtime", "demo-runtime", "--skill", "demo-skill")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["tool_health"]["ok"])
        self.assertEqual(len(payload["tool_health"]["results"]), 1)
        self.assertEqual(payload["tool_health"]["results"][0]["kind"], "wrapper")

    def test_doctor_keeps_direct_bin_check_for_non_wrapper_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root, visibility_policy="host_standard_path")

            result = run_cli("--root", str(root), "--json", "doctor", "--runtime", "demo-runtime", "--skill", "demo-skill")

        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        statuses = {(item["kind"], item["status"]) for item in payload["tool_health"]["results"]}
        self.assertIn(("bin", "missing"), statuses)
        self.assertIn(("wrapper", "available"), statuses)

    def test_validate_reports_wrapper_metadata_failures(self) -> None:
        cases = (
            ("missing-file", {"wrapper_path": "scripts/missing", "create_wrapper": False}, "wrapper_missing_file"),
            ("non-executable", {"executable": False}, "wrapper_not_executable"),
            ("absolute-path", {"wrapper_path": "/tmp/demo-tool", "create_wrapper": False}, "wrapper_absolute_path"),
            ("duplicate-name", {"duplicate_wrapper": True}, "duplicate_tool_wrapper"),
        )
        for name, kwargs, expected_code in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    write_wrapper_fixture_registry(root, **kwargs)

                    result = run_cli("--root", str(root), "--json", "validate")

                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                payload = json.loads(result.stdout)
                codes = {item["code"] for item in payload["diagnostics"]}
                self.assertIn(expected_code, codes)

    def test_validate_reports_malformed_tool_wrapper_metadata(self) -> None:
        cases = (
            ("tool_wrappers: {}\n", "tool_wrappers must be a list"),
            ("tool_wrappers:\n  - not-a-mapping\n", "tool_wrappers entries must be mappings"),
            (
                "tool_wrappers:\n  - name: demo-tool\n    path: scripts/demo-tool\n    probe:\n      - bad\n",
                "Tool wrapper demo-tool probe must be a mapping",
            ),
            (
                "tool_wrappers:\n  - name: demo-tool\n    path: scripts/demo-tool\n    probe: []\n",
                "Tool wrapper demo-tool probe must be a mapping",
            ),
            (
                'tool_wrappers:\n  - name: demo-tool\n    path: scripts/demo-tool\n    probe: ""\n',
                "Tool wrapper demo-tool probe must be a mapping",
            ),
            (
                "tool_wrappers:\n  - name: demo-tool\n    path: scripts/demo-tool\n    probe: false\n",
                "Tool wrapper demo-tool probe must be a mapping",
            ),
            (
                "tool_wrappers:\n  - name: demo-tool\n    path: scripts/demo-tool\n    wraps:\n      bad: value\n",
                "Expected a string or list of strings, got dict",
            ),
        )
        for metadata_extra, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    write_wrapper_fixture_registry(root)
                    (root / "skills" / "demo-skill" / "skill.yaml").write_text(
                        f"id: demo-skill\n{metadata_extra}",
                        encoding="utf-8",
                    )

                    result = run_cli("--root", str(root), "--json", "validate")

                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                payload = json.loads(result.stdout)
                messages = [item["message"] for item in payload["diagnostics"]]
                self.assertTrue(any(expected_message in message for message in messages), messages)

    def test_tool_health_reports_wrapper_probe_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root, script_body="#!/bin/sh\nprintf 'boom\\n' >&2\nexit 7\n")

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime", "--skill", "demo-skill")

        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["kind"], "wrapper")
        self.assertEqual(payload["results"][0]["status"], "probe_failed")

    def test_tool_health_reports_wrapper_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_wrapper_fixture_registry(root, script_body="#!/bin/sh\nprintf 'wrong\\n'\n")

            result = run_cli("--root", str(root), "--json", "tool-health", "--runtime", "demo-runtime", "--skill", "demo-skill")

        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["kind"], "wrapper")
        self.assertEqual(payload["results"][0]["status"], "version_mismatch")

    def test_validate_rejects_deprecated_runtime_managed_shims(self) -> None:
        cases = (
            (
                "managed_shims",
                """
                managed_shims:
                  allowed: true
                  scope: general
                  dirs:
                    - ./bin
                """,
                "Runtime profile broad-runtime managed_shims is deprecated",
            ),
            (
                "managed_shim_dirs",
                """
                managed_shim_dirs:
                  - ./bin
                """,
                "Runtime profile broad-runtime managed_shim_dirs is deprecated",
            ),
            (
                "supports_managed_shims",
                """
                capabilities:
                  supports_managed_shims: true
                """,
                "Runtime profile broad-runtime capability supports_managed_shims is deprecated",
            ),
        )
        for name, deprecated_yaml, expected_error in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    (root / "skills").mkdir()
                    (root / "targets").mkdir()
                    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
                    (root / "runtimes.yaml").write_text(
                        textwrap.dedent(
                            """
                            version: 1
                            runtime_profiles:
                              - name: broad-runtime
                                kind: path-list
                                visible_bin_dirs: []
                                probe:
                                  strategy: path-list
                            """
                        )
                        + textwrap.indent(textwrap.dedent(deprecated_yaml).lstrip("\n"), "    "),
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

                    result = run_cli("--root", str(root), "--json", "validate")

                self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
                payload = json.loads(result.stdout)
                self.assertIn(expected_error, payload["error"])

    def test_validate_rejects_deprecated_runtime_dependency_managed_shims(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "skills").mkdir()
            (root / "targets").mkdir()
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
                      managed_shims: []
                    """
                ),
                encoding="utf-8",
            )

            result = run_cli("--root", str(root), "--json", "validate")

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertIn("runtime_dependencies.managed_shims is deprecated", payload["error"])

    def test_doctor_combines_validation_and_tool_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fixture_registry(root, bin_dir, bin_name="missing-tool")

            result = run_cli("--root", str(root), "--json", "doctor", "--runtime", "demo-runtime")

        self.assertEqual(result.returncode, 6, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["validation"]["ok"])
        self.assertFalse(payload["tool_health"]["ok"])
        self.assertTrue(payload["deployment_status"]["ok"])
        self.assertEqual(payload["deployment_status"]["results"], [])

    def test_tool_health_rejects_unknown_runtime_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fixture_registry(root, bin_dir)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "tool-health",
                "--runtime",
                "does-not-exist",
                "--bin",
                "td",
            )

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"], ["Unknown runtime profile: does-not-exist"])

    def test_cli_helper_isolates_root_and_pointer_environment(self) -> None:
        captured_environment: dict[str, str] = {}

        def capture_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured_environment.update(kwargs["env"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        ambient = {
            "SKILL_REGISTRY_ROOT": "/ambient/registry",
            "HOME": "/ambient/home",
            "XDG_CONFIG_HOME": "/ambient/config",
        }
        with patch.dict(os.environ, ambient), patch("subprocess.run", side_effect=capture_run):
            run_cli("--help")

        self.assertNotIn("SKILL_REGISTRY_ROOT", captured_environment)
        self.assertNotEqual(captured_environment["HOME"], ambient["HOME"])
        self.assertNotEqual(captured_environment["XDG_CONFIG_HOME"], ambient["XDG_CONFIG_HOME"])
        self.assertEqual(Path(captured_environment["HOME"]).name, "home")
        self.assertEqual(Path(captured_environment["XDG_CONFIG_HOME"]).name, "config")

    def test_status_reports_missing_registry_skill_in_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "missing")

    def test_deploy_dry_run_plans_symlink_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

            self.assertFalse((target_root / "demo-skill").exists())

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["operations"][0]["operation"], "create_symlink")
        self.assertNotIn("applied", payload["operations"][0])

    def test_deploy_apply_symlink_to_absent_target_creates_link_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            target_skill = target_root / "demo-skill"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--deployed-by",
                "unit-test",
                "--reason",
                "deploy absent target",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            dry_run = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            config = load_registry(root)
            target = config.target_by_name["demo-target"]
            record = load_deployment_record(target, "demo-skill")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            operation = payload["operations"][0]
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["dry_run"])
            self.assertEqual(operation["operation"], "create_symlink")
            self.assertTrue(operation["applied"])
            self.assertEqual(operation["record_path"], str(deployment_record_path(target, "demo-skill")))
            self.assertEqual(len(operation["payload_hash"]), 64)
            self.assertEqual(operation["file_count"], 2)
            self.assertTrue(target_skill.is_symlink())
            self.assertEqual(target_skill.resolve(strict=True), source)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.operation, "deploy")
            self.assertEqual(record.deployment_mode, "symlink")
            self.assertEqual(record.deployed_by, "unit-test")
            self.assertEqual(record.reason, "deploy absent target")
            self.assertEqual(record.payload_hash, operation["payload_hash"])
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "in_sync")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr + dry_run.stdout)
            self.assertEqual(json.loads(dry_run.stdout)["operations"][0]["operation"], "skip")

    def test_deploy_apply_copy_to_absent_target_creates_payload_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            target_skill = target_root / "demo-skill"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--deployed-by",
                "unit-test",
                "--reason",
                "deploy absent target",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            dry_run = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            config = load_registry(root)
            target = config.target_by_name["demo-target"]
            record = load_deployment_record(target, "demo-skill")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            operation = payload["operations"][0]
            self.assertEqual(operation["operation"], "create_copy")
            self.assertTrue(operation["applied"])
            self.assertEqual(operation["record_path"], str(deployment_record_path(target, "demo-skill")))
            self.assertEqual(operation["file_count"], 2)
            self.assertTrue(target_skill.is_dir())
            self.assertFalse(target_skill.is_symlink())
            self.assertEqual((target_skill / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")
            self.assertEqual((target_skill / "stale.txt").read_text(encoding="utf-8"), "remove me on import\n")
            self.assertFalse((target_skill / "skill.yaml").exists())
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.operation, "deploy")
            self.assertEqual(record.deployment_mode, "copy")
            self.assertEqual(record.deployed_by, "unit-test")
            self.assertEqual(record.reason, "deploy absent target")
            self.assertEqual(record.payload_hash, operation["payload_hash"])
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "in_sync")
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr + dry_run.stdout)
            self.assertEqual(json.loads(dry_run.stdout)["operations"][0]["operation"], "skip")

    def test_guard_strict_blocks_recorded_copy_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            attempted = target_root / "demo-skill" / "SKILL.md"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(attempted),
            )

        self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision"], "block")
        self.assertEqual(payload["target"], "demo-target")
        self.assertEqual(payload["skill_id"], "demo-skill")
        self.assertEqual(payload["target_path"], str(target_root.resolve(strict=False) / "demo-skill"))
        self.assertEqual(payload["canonical_path"], str((root / "skills" / "demo-skill").resolve(strict=False)))
        self.assertEqual(payload["deployment_mode"], "copy")
        self.assertIn("registry-managed copy deployment", payload["reason"])
        self.assertIn("canonical package", payload["recommended_action"])

    def test_guard_blocks_target_symlink_path_even_when_realpath_is_registry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            target_skill = target_root / "demo-skill"
            target_skill.symlink_to(source, target_is_directory=True)
            attempted = target_skill / "SKILL.md"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "patch",
                "--path",
                str(attempted),
            )

        self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertEqual(payload["deployment_mode"], "symlink")
        self.assertEqual(payload["target_path"], str(target_root.resolve(strict=False) / "demo-skill"))
        self.assertEqual(payload["resolved_path"], str(source / "SKILL.md"))
        self.assertEqual(payload["canonical_path"], str(source))

    def test_guard_allows_original_canonical_registry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            attempted = root / "skills" / "demo-skill" / "SKILL.md"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(attempted),
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["skill_id"], "demo-skill")
        self.assertEqual(payload["canonical_path"], str((root / "skills" / "demo-skill").resolve(strict=False)))
        self.assertIsNone(payload["target"])

    def test_guard_warn_mode_permits_managed_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(target_root / "demo-skill" / "SKILL.md"),
                "--mode",
                "warn",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "warn")
        self.assertEqual(payload["skill_id"], "demo-skill")
        self.assertEqual(payload["deployment_mode"], "copy")

    def test_guard_off_mode_permits_managed_target_path_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "delete",
                "--path",
                str(target_root / "demo-skill"),
                "--mode",
                "off",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "allow")
        self.assertEqual(payload["target"], "demo-target")
        self.assertEqual(payload["skill_id"], "demo-skill")
        self.assertEqual(payload["deployment_mode"], "copy")

    def test_guard_warns_for_unmanaged_skill_like_target_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            local_skill = target_root / "local-skill"
            local_skill.mkdir()
            (local_skill / "SKILL.md").write_text("# Local Skill\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(local_skill / "SKILL.md"),
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "warn")
        self.assertEqual(payload["target"], "demo-target")
        self.assertEqual(payload["skill_id"], "local-skill")
        self.assertIsNone(payload["deployment_mode"])
        self.assertIn("unmanaged skill-like target entry", payload["reason"])

    def test_guard_allows_path_outside_target_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            root = workspace / "registry"
            root.mkdir()
            target_root = workspace / "target-skills"
            write_target_fixture_registry(root, target_root)
            outside_path = workspace / "outside" / "notes.md"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "create",
                "--path",
                str(outside_path),
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "allow")
        self.assertIsNone(payload["target"])
        self.assertIn("outside configured target roots", payload["reason"])

    def test_guard_blocks_missing_child_under_managed_target_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            missing_child = target_root / "demo-skill" / "new-reference.md"

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "create",
                "--path",
                str(missing_child),
            )

        self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision"], "block")
        self.assertEqual(payload["skill_id"], "demo-skill")
        self.assertEqual(payload["target_path"], str(target_root.resolve(strict=False) / "demo-skill"))

    def test_guard_rejects_malformed_relevant_deployment_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            config = load_registry(root)
            target = config.target_by_name["demo-target"]
            record_path = deployment_record_path(target, "demo-skill")
            record_path.parent.mkdir(parents=True)
            record_path.write_text("{not-json", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(target_root / "demo-skill" / "SKILL.md"),
            )

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("deployment record is malformed", payload["error"])

    def test_deploy_apply_creates_missing_target_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            target_root.rmdir()

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--reason",
                "deploy absent target",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(target_root.is_dir())
            self.assertTrue((target_root / "demo-skill" / "SKILL.md").is_file())
            target = load_registry(root).target_by_name["demo-target"]
            self.assertTrue(deployment_record_path(target, "demo-skill").is_file())

    def test_deploy_apply_skips_recorded_in_sync_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            target_skill = target_root / "demo-skill"
            before = snapshot_tree(target_skill)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--reason",
                "already deployed",
            )
            after = snapshot_tree(target_skill)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(before, after)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["operation"], "skip")
        self.assertFalse(payload["operations"][0]["applied"])

    def test_deploy_apply_refuses_unmanaged_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            target_skill = target_root / "demo-skill"
            before = snapshot_tree(target_skill)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--reason",
                "try unmanaged apply",
            )
            after = snapshot_tree(target_skill)
            target = load_registry(root).target_by_name["demo-target"]

            self.assertEqual(before, after)
            self.assertFalse(deployment_record_path(target, "demo-skill").exists())

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("unsafe planned operation", payload["error"])
        self.assertIn("target path exists and is not registry-managed", payload["error"])

    def test_deploy_apply_refuses_wrong_or_broken_symlink_repair(self) -> None:
        with tempfile.TemporaryDirectory() as wrong_temp:
            root = Path(wrong_temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            other = target_root / "other-skill"
            other.mkdir()
            target_skill = target_root / "demo-skill"
            target_skill.symlink_to(other, target_is_directory=True)

            wrong = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--reason",
                "try repair",
            )

            self.assertTrue(target_skill.is_symlink())
            self.assertEqual(target_skill.resolve(strict=True), other.resolve(strict=True))

        with tempfile.TemporaryDirectory() as broken_temp:
            root = Path(broken_temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            missing = target_root / "missing-skill"
            target_skill = target_root / "demo-skill"
            target_skill.symlink_to(missing, target_is_directory=True)

            broken = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--reason",
                "try repair",
            )

            self.assertTrue(target_skill.is_symlink())
            self.assertFalse(target_skill.exists())

        self.assertEqual(wrong.returncode, 1, wrong.stderr + wrong.stdout)
        self.assertEqual(broken.returncode, 1, broken.stderr + broken.stdout)
        self.assertIn("unmanaged symlink remains blocked", json.loads(wrong.stdout)["error"])
        self.assertIn("unmanaged symlink remains blocked", json.loads(broken.stdout)["error"])

    def test_deploy_apply_requires_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

            self.assertFalse((target_root / "demo-skill").exists())
            target = load_registry(root).target_by_name["demo-target"]
            self.assertFalse(deployment_record_path(target, "demo-skill").exists())

        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "deploy apply requires --reason")

    def test_payload_hash_ignores_control_and_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "demo-skill"
            source.mkdir()
            (source / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
            (source / "skill.yaml").write_text("id: demo-skill\n", encoding="utf-8")
            (source / ".DS_Store").write_bytes(b"volatile")
            (source / ".git").mkdir()
            (source / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            (source / ".skill-registry").mkdir()
            (source / ".skill-registry" / "state.json").write_text("{}", encoding="utf-8")
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "demo.pyc").write_bytes(b"cache")
            (source / "scripts").mkdir()
            (source / "scripts" / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            payload = deployable_payload_hash(source)

        self.assertEqual({item.path for item in payload.files}, {"SKILL.md", "scripts/run.sh"})
        self.assertEqual(payload.file_count, 2)
        self.assertEqual(len(payload.payload_hash), 64)

    def test_payload_hash_matches_with_different_control_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            for path in (first, second):
                (path / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
                (path / "templates").mkdir()
                (path / "templates" / "task.txt").write_text("same\n", encoding="utf-8")
            (first / "skill.yaml").write_text("id: first\n", encoding="utf-8")
            (second / "skill.yaml").write_text("id: second\ntitle: Different\n", encoding="utf-8")
            (second / ".DS_Store").write_bytes(b"ignored")

            first_payload = deployable_payload_hash(first)
            second_payload = deployable_payload_hash(second)

        self.assertEqual(first_payload.payload_hash, second_payload.payload_hash)
        self.assertEqual({item.path for item in first_payload.files}, {"SKILL.md", "templates/task.txt"})

    def test_deployment_record_save_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            record = save_demo_deployment_record(root)
            target = load_registry(root).target_by_name["demo-target"]
            record_path = deployment_record_path(target, "demo-skill")
            loaded = load_deployment_record(target, "demo-skill")

            self.assertEqual(loaded, record)
            self.assertEqual(record.file_count, len(record.files))
            self.assertTrue(str(record_path).endswith("deployments/demo-skill.json"))

    def test_status_blocks_unmanaged_directory_without_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "blocked")
        self.assertEqual(payload["results"][0]["reason"], "target path exists and is not registry-managed")

    def test_status_blocks_unmanaged_symlink_without_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            (target_root / "demo-skill").symlink_to(source, target_is_directory=True)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "blocked")
        self.assertEqual(payload["results"][0]["reason"], "target symlink is not registry-managed")

    def test_status_reports_recorded_directory_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["results"][0]["status"], "in_sync")
        self.assertIn("deployment record", payload["results"][0]["reason"])

    def test_status_reports_recorded_directory_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (target_root / "demo-skill" / "SKILL.md").write_text("# Target Drift\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][0]["status"], "drifted")
        self.assertIn("target payload hash differs", payload["results"][0]["reason"])

    def test_status_reports_recorded_directory_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (root / "skills" / "demo-skill" / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][0]["status"], "stale")
        self.assertIn("canonical payload hash differs", payload["results"][0]["reason"])

    def test_deploy_dry_run_skips_recorded_in_sync_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operations"][0]["operation"], "skip")

    def test_deploy_dry_run_blocks_recorded_drifted_and_stale_directories(self) -> None:
        with tempfile.TemporaryDirectory() as drift_temp:
            root = Path(drift_temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (target_root / "demo-skill" / "SKILL.md").write_text("# Target Drift\n", encoding="utf-8")

            drifted = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        with tempfile.TemporaryDirectory() as stale_temp:
            root = Path(stale_temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (root / "skills" / "demo-skill" / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")

            stale = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(drifted.returncode, 1, drifted.stderr + drifted.stdout)
        drifted_payload = json.loads(drifted.stdout)
        self.assertFalse(drifted_payload["ok"])
        self.assertEqual(drifted_payload["operations"][0]["operation"], "block")
        self.assertIn("recorded target is drifted", drifted_payload["operations"][0]["reason"])

        self.assertEqual(stale.returncode, 1, stale.stderr + stale.stdout)
        stale_payload = json.loads(stale.stdout)
        self.assertFalse(stale_payload["ok"])
        self.assertEqual(stale_payload["operations"][0]["operation"], "block")
        self.assertIn("recorded target is stale", stale_payload["operations"][0]["reason"])

    def test_deploy_refreshes_stale_managed_copy_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            old_payload = save_demo_deployment_record(root).payload_hash
            canonical = root / "skills" / "demo-skill"
            target_skill = target_root / "demo-skill"
            (canonical / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
                "--reason",
                "refresh stale managed copy",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            config = load_registry(root)
            target = config.target_by_name["demo-target"]
            record = load_deployment_record(target, "demo-skill")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            operation = payload["operations"][0]
            backup_path = Path(operation["backup_path"])
            self.assertEqual(operation["operation"], "refresh_copy")
            self.assertTrue(operation["applied"])
            self.assertTrue(backup_path.is_dir())
            self.assertEqual((backup_path / "entry" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")
            backup_metadata = json.loads((backup_path / "backup.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_metadata["operation"], "refresh_copy")
            self.assertEqual(backup_metadata["previous_entry_kind"], "directory")
            self.assertEqual((target_skill / "SKILL.md").read_text(encoding="utf-8"), "# Canonical Changed\n")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.operation, "refresh_copy")
            self.assertEqual(record.payload_hash, operation["payload_hash"])
            self.assertNotEqual(record.payload_hash, old_payload)
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "in_sync")

    def test_deploy_refreshes_drifted_managed_copy_only_with_drift_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            target_skill = target_root / "demo-skill"
            (target_skill / "SKILL.md").write_text("# Target Drift\n", encoding="utf-8")
            drifted_before = snapshot_tree(target_skill)

            managed_only = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
                "--reason",
                "try wrong flag",
            )
            self.assertEqual(managed_only.returncode, 1, managed_only.stderr + managed_only.stdout)
            self.assertEqual(snapshot_tree(target_skill), drifted_before)
            self.assertIn("--replace-drifted-copy", json.loads(managed_only.stdout)["error"])

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-drifted-copy",
                "--reason",
                "refresh drifted managed copy",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            operation = payload["operations"][0]
            backup_path = Path(operation["backup_path"])
            self.assertEqual(operation["operation"], "refresh_copy")
            self.assertTrue(backup_path.is_dir())
            self.assertEqual((backup_path / "entry" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"), "# Target Drift\n")
            self.assertEqual((target_skill / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "in_sync")

    def test_deploy_switches_in_sync_managed_copy_to_symlink_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            target_skill = target_root / "demo-skill"

            blocked = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
                "--reason",
                "switch managed copy to symlink",
            )
            record = load_deployment_record(load_registry(root).target_by_name["demo-target"], "demo-skill")

            self.assertEqual(blocked.returncode, 1, blocked.stderr + blocked.stdout)
            blocked_operation = json.loads(blocked.stdout)["operations"][0]
            self.assertEqual(blocked_operation["operation"], "block")
            self.assertIn("--replace-managed", blocked_operation["reason"])

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            operation = json.loads(result.stdout)["operations"][0]
            backup_path = Path(operation["backup_path"])
            self.assertEqual(operation["operation"], "replace_with_symlink")
            self.assertTrue(target_skill.is_symlink())
            self.assertEqual(target_skill.resolve(strict=True), source)
            self.assertEqual((backup_path / "entry" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.deployment_mode, "symlink")
            self.assertEqual(record.operation, "replace_with_symlink")

    def test_deploy_repairs_recorded_broken_symlink_with_replace_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            missing = target_root / "missing-skill"
            target_skill = target_root / "demo-skill"
            target_skill.symlink_to(missing, target_is_directory=True)
            save_demo_deployment_record(root, deployment_mode="symlink")

            blocked = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
                "--reason",
                "repair recorded broken symlink",
            )

            self.assertEqual(blocked.returncode, 1, blocked.stderr + blocked.stdout)
            blocked_operation = json.loads(blocked.stdout)["operations"][0]
            self.assertEqual(blocked_operation["operation"], "block")
            self.assertIn("--replace-managed", blocked_operation["reason"])

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            operation = json.loads(result.stdout)["operations"][0]
            backup_path = Path(operation["backup_path"])
            self.assertEqual(operation["operation"], "repair_symlink")
            self.assertTrue(target_skill.is_symlink())
            self.assertEqual(target_skill.resolve(strict=True), source)
            backup_metadata = json.loads((backup_path / "backup.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_metadata["previous_entry_kind"], "broken_symlink")
            self.assertEqual(backup_metadata["previous_link_target"], str(missing))

    def test_deploy_replace_flags_do_not_replace_unmanaged_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            target_skill = target_root / "demo-skill"
            before = snapshot_tree(target_skill)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
                "--replace-drifted-copy",
                "--reason",
                "try replacing unmanaged directory",
            )

            self.assertEqual(snapshot_tree(target_skill), before)
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            self.assertIn("target path exists and is not registry-managed", json.loads(result.stdout)["error"])

    def test_deploy_record_write_failure_rolls_back_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (root / "skills" / "demo-skill" / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")
            target_skill = target_root / "demo-skill"
            before = snapshot_tree(target_skill)
            config = load_registry(root)

            with patch("skill_registry.deploy.save_deployment_record", side_effect=RuntimeError("synthetic record failure")):
                with self.assertRaises(ValueError) as raised:
                    apply_deploy(
                        config,
                        mode="copy",
                        all_targets=False,
                        target_names=("demo-target",),
                        skill_ids=("demo-skill",),
                        deployed_by="unit-test",
                        reason="trigger rollback",
                        replace_managed=True,
                    )

            self.assertEqual(snapshot_tree(target_skill), before)
            message = str(raised.exception)
            self.assertIn("rolled target back from backup", message)
            self.assertIn("synthetic record failure", message)
            backups = sorted((target_root / ".skill-registry" / "backups").glob("*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "entry" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")

    def test_discover_reports_candidate_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n", encoding="utf-8")

            result = run_cli("--root", str(root), "--json", "discover", "--target", "demo-target")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"][0]["classification"], "candidate_duplicate")

    def test_discover_reports_recorded_copy_as_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            result = run_cli("--root", str(root), "--json", "discover", "--target", "demo-target")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"][0]["classification"], "managed")
        self.assertEqual(payload["entries"][0]["reason"], "target payload matches deployment record and canonical payload")

    def test_discover_reports_recorded_copy_as_stale_when_canonical_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)
            (root / "skills" / "demo-skill" / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")

            result = run_cli("--root", str(root), "--json", "discover", "--target", "demo-target")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"][0]["classification"], "stale")
        self.assertEqual(payload["entries"][0]["reason"], "canonical payload hash differs from deployment record")

    def test_discover_reports_nested_skill_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "productivity" / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Nested copy\n", encoding="utf-8")

            result = run_cli("--root", str(root), "--json", "discover", "--target", "demo-target")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entries"][0]["classification"], "candidate_duplicate")
        self.assertEqual(payload["entries"][0]["skill_id"], "demo-skill")
        self.assertTrue(payload["entries"][0]["path"].endswith("productivity/demo-skill"))

    def test_reconcile_plan_explains_candidate_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["plan"]["current_classification"], "candidate_duplicate")
        self.assertEqual(payload["plan"]["selected_action"], "propose-import")
        self.assertTrue(payload["plan"]["can_apply"])
        self.assertIn("local-only", payload["plan"]["available_actions"])
        self.assertFalse(payload["plan"]["mutates_target_files"])

    def test_reconcile_plan_uses_nested_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            nested = target_root / "productivity" / "demo-skill"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("# Nested target copy\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--source-path",
                "productivity/demo-skill",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        plan = payload["plan"]
        self.assertEqual(plan["current_classification"], "candidate_duplicate")
        self.assertEqual(plan["selected_action"], "propose-import")
        self.assertTrue(plan["can_apply"])
        self.assertTrue(plan["target_path"].endswith("productivity/demo-skill"))

    def test_reconcile_plan_rejects_nested_source_path_adopt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            nested = target_root / "productivity" / "demo-skill"
            nested.mkdir(parents=True)
            for relative_path in ("SKILL.md", "stale.txt"):
                (nested / relative_path).write_bytes((root / "skills" / "demo-skill" / relative_path).read_bytes())

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--source-path",
                "productivity/demo-skill",
                "--action",
                "adopt",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        plan = payload["plan"]
        self.assertFalse(plan["can_apply"])
        self.assertIn("default skill path", plan["reason"])

    def test_reconcile_plan_propose_import_is_applyable_for_unmanaged_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            external = root / "external-demo-skill"
            external.mkdir()
            (external / "SKILL.md").write_text("# External copy\n", encoding="utf-8")
            (target_root / "demo-skill").symlink_to(external)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "propose-import",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["plan"]["current_classification"], "unmanaged")
        self.assertTrue(payload["plan"]["can_apply"])
        self.assertFalse(payload["plan"]["mutates_target_files"])

    def test_reconcile_plan_adopt_is_applyable_for_matching_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        plan = payload["plan"]
        self.assertEqual(plan["current_classification"], "candidate_duplicate")
        self.assertEqual(plan["selected_action"], "adopt")
        self.assertTrue(plan["can_apply"])
        self.assertFalse(plan["mutates_target_files"])
        self.assertEqual(len(plan["payload_hash"]), 64)
        self.assertEqual(plan["file_count"], 2)
        self.assertIn("matches canonical payload", plan["reason"])

    def test_reconcile_plan_adopt_is_applyable_for_matching_unmanaged_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            external = root / "external-demo-skill"
            external.mkdir()
            canonical = root / "skills" / "demo-skill"
            for relative_path in ("SKILL.md", "stale.txt"):
                (external / relative_path).write_bytes((canonical / relative_path).read_bytes())
            (target_root / "demo-skill").symlink_to(external)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        plan = payload["plan"]
        self.assertEqual(plan["current_classification"], "unmanaged")
        self.assertTrue(plan["can_apply"])
        self.assertFalse(plan["mutates_target_files"])
        self.assertEqual(plan["file_count"], 2)
        self.assertIn("matches canonical payload", plan["reason"])

    def test_reconcile_plan_adopt_is_not_applyable_for_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        plan = payload["plan"]
        self.assertFalse(plan["can_apply"])
        self.assertIn("exact payload match", plan["reason"])
        self.assertIn("differs from target payload hash", plan["reason"])
        self.assertEqual(plan["payload_hash"], "")
        self.assertEqual(plan["file_count"], 0)

    def test_reconcile_apply_adopt_writes_record_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            target_skill = target_root / "demo-skill"
            before = snapshot_tree(target_skill)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "already matches canonical",
                "--adopted-by",
                "unit-test",
            )
            after = snapshot_tree(target_skill)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(before, after)
            payload = json.loads(result.stdout)
            apply_result = payload["result"]
            self.assertFalse(apply_result["mutates_target_files"])
            self.assertEqual(apply_result["action"], "adopt")
            self.assertEqual(len(apply_result["payload_hash"]), 64)
            self.assertEqual(apply_result["file_count"], 2)
            record_path = Path(apply_result["record_path"])
            self.assertEqual(
                record_path,
                (target_root / ".skill-registry" / "deployments" / "demo-skill.json").resolve(strict=False),
            )
            record = load_deployment_record(load_registry(root).target_by_name["demo-target"], "demo-skill")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.operation, "adopt")
            self.assertEqual(record.deployed_by, "unit-test")
            self.assertEqual(record.reason, "already matches canonical")
        self.assertEqual(record.deployment_mode, "symlink")
        self.assertEqual(record.payload_hash, apply_result["payload_hash"])
        self.assertEqual(record.file_count, apply_result["file_count"])

    def test_reconcile_apply_adopt_unmanaged_symlink_enables_repair_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            external = root / "external-demo-skill"
            external.mkdir()
            canonical = root / "skills" / "demo-skill"
            for relative_path in ("SKILL.md", "stale.txt"):
                (external / relative_path).write_bytes((canonical / relative_path).read_bytes())
            target_link = target_root / "demo-skill"
            target_link.symlink_to(external)
            before_target = target_link.readlink()

            adopt = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "symlink payload matches canonical",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            deploy = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--replace-managed",
            )
            after_target = target_link.readlink()

        self.assertEqual(adopt.returncode, 0, adopt.stderr + adopt.stdout)
        self.assertEqual(after_target, before_target)
        self.assertEqual(status.returncode, 1, status.stderr + status.stdout)
        self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "wrong_link")
        self.assertEqual(deploy.returncode, 0, deploy.stderr + deploy.stdout)
        self.assertEqual(json.loads(deploy.stdout)["operations"][0]["operation"], "repair_symlink")

    def test_reconcile_apply_adopt_fails_if_content_drifts_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)

            plan = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
            )
            self.assertEqual(plan.returncode, 0, plan.stderr + plan.stdout)
            self.assertTrue(json.loads(plan.stdout)["plan"]["can_apply"])

            target_skill = target_root / "demo-skill"
            (target_skill / "SKILL.md").write_text("# Drifted before apply\n", encoding="utf-8")
            drifted = snapshot_tree(target_skill)
            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "already matches canonical",
            )

            self.assertEqual(snapshot_tree(target_skill), drifted)
            self.assertFalse(deployment_record_path(load_registry(root).target_by_name["demo-target"], "demo-skill").exists())

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("exact payload match", payload["error"])

    def test_reconcile_apply_adopt_blocks_existing_deployment_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            save_demo_deployment_record(root)

            plan = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
            )
            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "already matches canonical",
            )

        self.assertEqual(plan.returncode, 0, plan.stderr + plan.stdout)
        plan_payload = json.loads(plan.stdout)
        self.assertFalse(plan_payload["plan"]["can_apply"])
        self.assertIn("existing deployment record", plan_payload["plan"]["reason"])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        apply_payload = json.loads(result.stdout)
        self.assertFalse(apply_payload["ok"])
        self.assertIn("existing deployment record", apply_payload["error"])

    def test_reconcile_apply_adopt_updates_status_and_deploy_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)

            adopt = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "already matches canonical",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            deploy = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "copy",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            (target_root / "demo-skill" / "SKILL.md").write_text("# Target Drift\n", encoding="utf-8")
            drifted = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(adopt.returncode, 0, adopt.stderr + adopt.stdout)
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        self.assertEqual(deploy.returncode, 0, deploy.stderr + deploy.stdout)
        status_payload = json.loads(status.stdout)
        deploy_payload = json.loads(deploy.stdout)
        self.assertEqual(status_payload["results"][0]["status"], "in_sync")
        self.assertEqual(deploy_payload["operations"][0]["operation"], "skip")
        self.assertEqual(drifted.returncode, 1, drifted.stderr + drifted.stdout)
        self.assertEqual(json.loads(drifted.stdout)["results"][0]["status"], "drifted")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            write_matching_target_copy(root, target_root)
            adopt = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "adopt",
                "--reason",
                "already matches canonical",
            )
            (root / "skills" / "demo-skill" / "SKILL.md").write_text("# Canonical Changed\n", encoding="utf-8")
            stale = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(adopt.returncode, 0, adopt.stderr + adopt.stdout)
        self.assertEqual(stale.returncode, 1, stale.stderr + stale.stdout)
        self.assertEqual(json.loads(stale.stdout)["results"][0]["status"], "stale")

    def test_reconcile_apply_replace_overwrites_unmanaged_copy_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root, deploy_mode="copy", supports_symlink=False)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different local copy\n", encoding="utf-8")
            (duplicate / "local-only.txt").write_text("preserve in backup\n", encoding="utf-8")

            plan = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "plan",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "replace",
            )
            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "replace",
                "--reason",
                "replace target content",
                "--adopted-by",
                "unit-test",
            )
            status = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

            self.assertEqual(plan.returncode, 0, plan.stderr + plan.stdout)
            plan_payload = json.loads(plan.stdout)["plan"]
            self.assertTrue(plan_payload["can_apply"])
            self.assertTrue(plan_payload["mutates_target_files"])

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            apply_payload = json.loads(result.stdout)["result"]
            self.assertEqual(apply_payload["action"], "replace")
            self.assertTrue(apply_payload["mutates_target_files"])
            self.assertEqual(apply_payload["file_count"], 2)
            backup_path = Path(apply_payload["backup_path"])
            self.assertTrue((backup_path / "entry" / "demo-skill" / "local-only.txt").is_file())
            self.assertFalse((target_root / "demo-skill" / "local-only.txt").exists())
            self.assertEqual(
                (target_root / "demo-skill" / "SKILL.md").read_text(encoding="utf-8"),
                "# Stale placeholder\n",
            )

            record = load_deployment_record(load_registry(root).target_by_name["demo-target"], "demo-skill")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.operation, "replace_unmanaged")
            self.assertEqual(record.deployment_mode, "copy")
            self.assertEqual(record.deployed_by, "unit-test")
            self.assertEqual(record.reason, "replace target content")
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            self.assertEqual(json.loads(status.stdout)["results"][0]["status"], "in_sync")

    def test_status_reports_recorded_symlink_mode_when_target_default_is_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root, deploy_mode="copy", supports_symlink=True)
            source = (root / "skills" / "demo-skill").resolve(strict=False)
            (target_root / "demo-skill").symlink_to(source, target_is_directory=True)
            save_demo_deployment_record(root, deployment_mode="symlink")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        status = json.loads(result.stdout)["results"][0]
        self.assertEqual(status["status"], "in_sync")
        self.assertEqual(status["deploy_mode"], "symlink")

    def test_reconcile_apply_propose_import_creates_metadata_only_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n\nUse this one.\n", encoding="utf-8")
            (duplicate / "nested").mkdir()
            (duplicate / "nested" / "data.bin").write_bytes(b"\x00\x01demo")
            before = snapshot_tree(duplicate)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "propose-import",
                "--reason",
                "review real target skill",
            )
            after = snapshot_tree(duplicate)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(before, after)
            payload = json.loads(result.stdout)
            apply_result = payload["result"]
            proposal_dir = Path(apply_result["proposal_path"])
            self.assertTrue(proposal_dir.is_dir())
            self.assertEqual(proposal_dir.parent.name, "proposals")
            self.assertEqual(apply_result["target"], "demo-target")
            self.assertEqual(apply_result["skill_id"], "demo-skill")
            self.assertEqual(apply_result["source_path"], str((target_root / "demo-skill").resolve(strict=False)))
            self.assertFalse(apply_result["mutates_target_files"])

            proposal = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))
            self.assertEqual(proposal["type"], "import")
            self.assertEqual(proposal["status"], "proposed")
            self.assertEqual(proposal["skill_id"], "demo-skill")
            self.assertEqual(proposal["source_target"], "demo-target")
            self.assertEqual(proposal["reason"], "review real target skill")
            self.assertFalse(proposal["target_mutation"])
            self.assertEqual(len(proposal["package_hash"]), 64)
            self.assertEqual({item["path"] for item in proposal["files"]}, {"SKILL.md", "nested/data.bin"})
            self.assertTrue((proposal_dir / "README.md").exists())
            self.assertTrue((proposal_dir / "diff.patch").exists())
            self.assertFalse((proposal_dir / "SKILL.md").exists())

    def test_reconcile_apply_propose_import_uses_nested_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            nested = target_root / "productivity" / "demo-skill"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("# Nested target copy\n\nUse this one.\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--source-path",
                "productivity/demo-skill",
                "--action",
                "propose-import",
                "--reason",
                "review categorized target skill",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            apply_result = payload["result"]
            self.assertTrue(apply_result["source_path"].endswith("productivity/demo-skill"))
            proposal_dir = Path(apply_result["proposal_path"])
            proposal = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))
            self.assertTrue(proposal["source_path"].endswith("productivity/demo-skill"))

            inspection = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "inspect",
                "--proposal",
                str(proposal_dir),
                "--verify-source",
            )
            self.assertEqual(inspection.returncode, 0, inspection.stderr + inspection.stdout)
            inspection_payload = json.loads(inspection.stdout)
            self.assertTrue(inspection_payload["proposal"]["verification"]["ok"])

    def test_proposal_inspect_reports_generated_proposal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "inspect",
                "--proposal",
                str(proposal_dir),
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        proposal = payload["proposal"]
        self.assertTrue(payload["ok"])
        self.assertEqual(proposal["type"], "import")
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["skill_id"], "demo-skill")
        self.assertEqual(proposal["source_target"], "demo-target")
        self.assertFalse(proposal["mutates_target_files"])
        self.assertEqual(proposal["file_count"], 1)

    def test_proposal_inspect_accepts_superseded_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)
            proposal_file = proposal_dir / "proposal.yaml"
            proposal = yaml.safe_load(proposal_file.read_text(encoding="utf-8"))
            proposal["status"] = "superseded"
            proposal["superseded_reason"] = "canonical already contains reviewed changes"
            proposal_file.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "inspect",
                "--proposal",
                str(proposal_dir),
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["proposal"]["status"], "superseded")

    def test_proposal_inspect_verify_source_reports_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "inspect",
                "--proposal",
                str(proposal_dir),
                "--verify-source",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        verification = payload["proposal"]["verification"]
        self.assertTrue(payload["ok"])
        self.assertTrue(verification["checked"])
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["errors"], [])

    def test_proposal_inspect_verify_source_accepts_legacy_package_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            source = target_root / "demo-skill"
            source.mkdir(parents=True)
            skill_text = "# Imported Skill\n"
            skill_bytes = skill_text.encode("utf-8")
            (source / "SKILL.md").write_text(skill_text, encoding="utf-8")
            file_sha = hashlib.sha256(skill_bytes).hexdigest()
            legacy_hash = legacy_proposal_package_hash((("SKILL.md", file_sha),))
            self.assertNotEqual(legacy_hash, deployable_payload_hash(source).payload_hash)

            proposal_dir = root / "proposals" / "legacy-demo-skill-import"
            proposal_dir.mkdir(parents=True)
            (proposal_dir / "proposal.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": "legacy-demo-skill-import",
                        "type": "import",
                        "status": "applied",
                        "skill_id": "demo-skill",
                        "source_target": "demo-target",
                        "source_path": str(source),
                        "canonical_path": str(root / "skills" / "demo-skill"),
                        "created_at": "2026-05-18T00:00:00+00:00",
                        "reason": "legacy hash compatibility",
                        "target_mutation": False,
                        "files": [
                            {
                                "path": "SKILL.md",
                                "size": len(skill_bytes),
                                "sha256": file_sha,
                            }
                        ],
                        "package_hash": legacy_hash,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "inspect",
                "--proposal",
                str(proposal_dir),
                "--verify-source",
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        verification = payload["proposal"]["verification"]
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["package_hash"], legacy_hash)
        self.assertEqual(verification["errors"], [])

    def test_proposal_apply_replaces_canonical_content_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root, include_source_skill_yaml=True)
            canonical = root / "skills" / "demo-skill"
            source = target_root / "demo-skill"
            source_before = snapshot_tree(source)

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )
            source_after = snapshot_tree(source)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(source_before, source_after)
            self.assertEqual((canonical / "SKILL.md").read_text(encoding="utf-8"), "# Imported Skill\n")
            self.assertFalse((canonical / "stale.txt").exists())

            metadata = yaml.safe_load((canonical / "skill.yaml").read_text(encoding="utf-8"))
            self.assertEqual(metadata["id"], "demo-skill")
            self.assertEqual(metadata["title"], "Demo Skill")
            self.assertEqual(metadata["targets"], ["demo-target"])
            self.assertEqual(metadata["lifecycle"], "canonical")
            self.assertEqual(metadata["canonical_status"], "imported")
            self.assertEqual(metadata["source_import"]["proposal_id"], proposal_dir.name)
            self.assertEqual(metadata["source_import"]["source_target"], "demo-target")
            self.assertEqual(metadata["source_import"]["approved_by"], "reviewer")
            self.assertNotIn("target_local_only", metadata)

            apply_record = yaml.safe_load((proposal_dir / "applied.yaml").read_text(encoding="utf-8"))
            self.assertEqual(apply_record["proposal_id"], proposal_dir.name)
            self.assertEqual(apply_record["approved_by"], "reviewer")
            self.assertEqual(apply_record["reason"], "reviewed source import")
            self.assertEqual(apply_record["copied_files"], ["SKILL.md"])
            self.assertEqual(apply_record["skipped_control_files"], ["skill.yaml"])
            self.assertFalse(apply_record["target_mutation"])

            proposal = yaml.safe_load((proposal_dir / "proposal.yaml").read_text(encoding="utf-8"))
            self.assertEqual(proposal["status"], "applied")

            backup = proposal_dir / "canonical-before"
            self.assertTrue((backup / "SKILL.md").exists())
            self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "# Stale placeholder\n")

            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["result"]["mutates_target_files"])
            self.assertEqual(payload["result"]["backup_path"], str(backup))
            self.assertEqual(payload["result"]["apply_record_path"], str(proposal_dir / "applied.yaml"))

    def test_proposal_apply_fails_when_source_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)
            canonical = root / "skills" / "demo-skill"
            before = snapshot_tree(canonical)
            (target_root / "demo-skill" / "SKILL.md").write_text("# Drifted Skill\n", encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )
            after = snapshot_tree(canonical)

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("source package has drifted", payload["error"])
        self.assertEqual(before, after)
        self.assertFalse((proposal_dir / "canonical-before").exists())
        self.assertFalse((proposal_dir / "applied.yaml").exists())

    def test_proposal_apply_requires_approval_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)

            missing_approved_by = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--reason",
                "reviewed source import",
            )
            missing_reason = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
            )

        self.assertNotEqual(missing_approved_by.returncode, 0)
        self.assertNotEqual(missing_reason.returncode, 0)

    def test_proposal_apply_fails_for_already_applied_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)

            first = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )
            second = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed again",
            )

        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertEqual(second.returncode, 1, second.stderr + second.stdout)
        payload = json.loads(second.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "proposal status must be proposed")

    def test_proposal_apply_fails_for_non_placeholder_canonical_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)
            metadata_path = root / "skills" / "demo-skill" / "skill.yaml"
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            metadata["lifecycle"] = "canonical"
            metadata["canonical_status"] = "imported"
            metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("canonical package is not placeholder or pending import", payload["error"])

    def test_proposal_apply_fails_when_backup_or_apply_record_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)
            (proposal_dir / "canonical-before").mkdir()

            backup_conflict = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )

        self.assertEqual(backup_conflict.returncode, 1, backup_conflict.stderr + backup_conflict.stdout)
        backup_payload = json.loads(backup_conflict.stdout)
        self.assertFalse(backup_payload["ok"])
        self.assertIn("backup directory already exists", backup_payload["error"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            proposal_dir = create_import_proposal(root, target_root)
            (proposal_dir / "applied.yaml").write_text("stale: true\n", encoding="utf-8")

            record_conflict = run_cli(
                "--root",
                str(root),
                "--json",
                "proposal",
                "apply",
                "--proposal",
                str(proposal_dir),
                "--approved-by",
                "reviewer",
                "--reason",
                "reviewed source import",
            )

        self.assertEqual(record_conflict.returncode, 1, record_conflict.stderr + record_conflict.stdout)
        record_payload = json.loads(record_conflict.stdout)
        self.assertFalse(record_payload["ok"])
        self.assertIn("apply record already exists", record_payload["error"])

    def test_reconcile_apply_local_only_updates_status_and_deploy_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n", encoding="utf-8")

            apply_result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "local-only",
                "--reason",
                "owned by the demo target",
            )
            status_result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )
            deploy_result = run_cli(
                "--root",
                str(root),
                "--json",
                "deploy",
                "--dry-run",
                "--mode",
                "symlink",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

            self.assertTrue((target_root / ".skill-registry" / "classifications" / "demo-skill.json").exists())
            self.assertTrue((target_root / "demo-skill").is_dir())

        self.assertEqual(apply_result.returncode, 0, apply_result.stderr + apply_result.stdout)
        self.assertEqual(status_result.returncode, 0, status_result.stderr + status_result.stdout)
        self.assertEqual(deploy_result.returncode, 0, deploy_result.stderr + deploy_result.stdout)
        status_payload = json.loads(status_result.stdout)
        deploy_payload = json.loads(deploy_result.stdout)
        self.assertEqual(status_payload["results"][0]["status"], "local_only")
        self.assertEqual(deploy_payload["operations"][0]["operation"], "skip")

    def test_reconcile_apply_keep_blocked_preserves_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
            duplicate = target_root / "demo-skill"
            duplicate.mkdir(parents=True)
            (duplicate / "SKILL.md").write_text("# Different copy\n", encoding="utf-8")

            apply_result = run_cli(
                "--root",
                str(root),
                "--json",
                "reconcile",
                "apply",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
                "--action",
                "keep-blocked",
                "--reason",
                "needs manual review",
            )
            status_result = run_cli(
                "--root",
                str(root),
                "--json",
                "status",
                "--target",
                "demo-target",
                "--skill",
                "demo-skill",
            )

        self.assertEqual(apply_result.returncode, 0, apply_result.stderr + apply_result.stdout)
        self.assertEqual(status_result.returncode, 1, status_result.stderr + status_result.stdout)
        payload = json.loads(status_result.stdout)
        self.assertEqual(payload["results"][0]["status"], "blocked")
        self.assertEqual(payload["results"][0]["reason"], "needs manual review")

    def test_deploy_skill_set_uses_default_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)
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

            result = run_cli("--root", str(root), "--json", "deploy", "--dry-run", "--skill-set", "demo-set")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["operations"]), 1)
        self.assertEqual(payload["operations"][0]["target"], "demo-target")
        self.assertEqual(payload["operations"][0]["skill_id"], "demo-skill")

    def test_deploy_unknown_skill_set_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_root = root / "target-skills"
            write_target_fixture_registry(root, target_root)

            result = run_cli("--root", str(root), "--json", "deploy", "--dry-run", "--skill-set", "missing-set")

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertIn("Unknown skill set: missing-set", payload["errors"])


def write_wrapper_fixture_registry(
    root: Path,
    *,
    wrapper_name: str = "demo-tool",
    wrapper_path: str = "scripts/demo-tool",
    wraps: tuple[str, ...] = ("demo-tool",),
    script_body: str = "#!/bin/sh\nprintf '2.0\\n'\n",
    executable: bool = True,
    create_wrapper: bool = True,
    duplicate_wrapper: bool = False,
    visibility_policy: str = "",
) -> None:
    bin_dir = root / "bin"
    skills_dir = root / "skills"
    skill_dir = skills_dir / "demo-skill"
    wrapper_file = skill_dir / wrapper_path
    (root / "targets").mkdir(exist_ok=True)
    bin_dir.mkdir()
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")

    if create_wrapper and not Path(wrapper_path).is_absolute():
        wrapper_file.parent.mkdir(parents=True, exist_ok=True)
        wrapper_file.write_text(script_body, encoding="utf-8")
        if executable:
            wrapper_file.chmod(wrapper_file.stat().st_mode | stat.S_IXUSR)

    wrapper_entries = [
        {
            "name": wrapper_name,
            "path": wrapper_path,
            "wraps": list(wraps),
            "probe": {
                "expected_pattern": "^2\\.",
            },
        }
    ]
    if duplicate_wrapper:
        second_wrapper = skill_dir / "scripts" / "demo-tool-2"
        second_wrapper.parent.mkdir(parents=True, exist_ok=True)
        second_wrapper.write_text(script_body, encoding="utf-8")
        second_wrapper.chmod(second_wrapper.stat().st_mode | stat.S_IXUSR)
        wrapper_entries.append(
            {
                "name": wrapper_name,
                "path": "scripts/demo-tool-2",
                "wraps": list(wraps),
                "probe": {
                    "expected_pattern": "^2\\.",
                },
            }
        )

    skill_metadata = {
        "id": "demo-skill",
        "title": "Demo Skill",
        "tool_wrappers": wrapper_entries,
    }
    (skill_dir / "skill.yaml").write_text(yaml.safe_dump(skill_metadata, sort_keys=False), encoding="utf-8")
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        textwrap.dedent(
            f"""
            version: 1
            runtime_profiles:
              - name: demo-runtime
                kind: path-list
                visible_bin_dirs:
                  - "{bin_dir}"
                probe:
                  strategy: path-list
            """
        ),
        encoding="utf-8",
    )

    dependency_block = "[]"
    if visibility_policy:
        dependency_block = textwrap.dedent(
            f"""
            - name: demo-tool
              required: true
              required_by_skills:
                - demo-skill
              target_runtimes:
                - demo-runtime
              probe:
                command: "demo-tool --version"
                expected_pattern: "^2\\\\."
              visibility_policy: {visibility_policy}
            """
        ).rstrip()

    dependencies_yaml = (
        "version: 1\n"
        "runtime_dependencies:\n"
        "  bins:\n"
        f"{textwrap.indent(dependency_block, '    ')}\n"
        "  env: []\n"
    )
    (root / "runtime-dependencies.yaml").write_text(dependencies_yaml, encoding="utf-8")


def write_fixture_registry(root: Path, bin_dir: Path, bin_name: str = "demo-tool") -> None:
    (root / "skills").mkdir(exist_ok=True)
    (root / "targets").mkdir(exist_ok=True)
    (root / "registry.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        textwrap.dedent(
            f"""
            version: 1
            runtime_profiles:
              - name: demo-runtime
                kind: path-list
                visible_bin_dirs:
                  - "{bin_dir}"
                probe:
                  strategy: path-list
            """
        ),
        encoding="utf-8",
    )
    (root / "runtime-dependencies.yaml").write_text(
        textwrap.dedent(
            f"""
            version: 1
            runtime_dependencies:
              bins:
                - name: {bin_name}
                  required: true
                  target_runtimes:
                    - demo-runtime
                  probe:
                    command: "{bin_name} --version"
                    expected_pattern: "^1\\\\."
              env: []
            """
        ),
        encoding="utf-8",
    )


def write_target_fixture_registry(
    root: Path,
    target_root: Path,
    *,
    deploy_mode: str = "symlink",
    supports_symlink: bool = True,
) -> None:
    skills_dir = root / "skills"
    targets_dir = root / "targets"
    skill_dir = skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    targets_dir.mkdir()
    target_root.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "# Stale placeholder\n",
        encoding="utf-8",
    )
    (skill_dir / "stale.txt").write_text("remove me on import\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        textwrap.dedent(
            """
            id: demo-skill
            title: Demo Skill
            lifecycle: placeholder
            canonical_status: pending_import
            targets:
              - demo-target
            """
        ),
        encoding="utf-8",
    )
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
            f"""
            targets:
              - name: demo-target
                system: demo
                root: "{target_root}"
                deploy_mode: {deploy_mode}
                include_in_all: true
                supports_symlink: {str(supports_symlink).lower()}
                runtime_profiles:
                  - demo-runtime
            """
        ),
        encoding="utf-8",
    )


def write_matching_target_copy(root: Path, target_root: Path) -> None:
    canonical = root / "skills" / "demo-skill"
    target = target_root / "demo-skill"
    target.mkdir(parents=True)
    for relative_path in ("SKILL.md", "stale.txt"):
        (target / relative_path).write_bytes((canonical / relative_path).read_bytes())


def save_demo_deployment_record(root: Path, deployment_mode: str = "copy") -> DeploymentRecord:
    config = load_registry(root)
    target = config.target_by_name["demo-target"]
    skill = selected_skills(config, ("demo-skill",))[0]
    payload = deployable_payload_hash(skill.path)
    record = DeploymentRecord(
        schema_version=1,
        target=target.name,
        skill_id=skill.id,
        deployment_mode=deployment_mode,
        source_path=str(skill.path),
        target_path=str(target.root_path / skill.id),
        payload_hash=payload.payload_hash,
        file_count=payload.file_count,
        files=payload.files,
        deployed_at="2026-05-18T00:00:00+00:00",
        deployed_by="unit-test",
        operation="deploy",
        registry_commit="test-commit",
    )
    return save_deployment_record(target, record)


def legacy_proposal_package_hash(files: tuple[tuple[str, str], ...]) -> str:
    digest = hashlib.sha256()
    for relative_path, file_sha in sorted(files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def create_import_proposal(root: Path, target_root: Path, *, include_source_skill_yaml: bool = False) -> Path:
    write_target_fixture_registry(root, target_root)
    source = target_root / "demo-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Imported Skill\n", encoding="utf-8")
    if include_source_skill_yaml:
        (source / "skill.yaml").write_text(
            textwrap.dedent(
                """
                id: demo-skill
                title: Target Local Demo Skill
                target_local_only: true
                """
            ),
            encoding="utf-8",
        )

    result = run_cli(
        "--root",
        str(root),
        "--json",
        "reconcile",
        "apply",
        "--target",
        "demo-target",
        "--skill",
        "demo-skill",
        "--action",
        "propose-import",
        "--reason",
        "review real target skill",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr + result.stdout)
    payload = json.loads(result.stdout)
    return Path(payload["result"]["proposal_path"])


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

if __name__ == "__main__":
    unittest.main()
