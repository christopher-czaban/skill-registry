from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]

from skill_registry.cli import main
from skill_registry.root import RegistryRootError, ResolvedRegistryRoot, resolve_registry_root


class RegistryRootTests(unittest.TestCase):
    def test_explicit_root_is_resolved_and_result_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")

            result = resolve_registry_root(root, cwd=base, environ=isolated_environment(base))

        self.assertEqual(result, ResolvedRegistryRoot(path=root.resolve(), source="explicit"))
        with self.assertRaises(FrozenInstanceError):
            result.source = "cwd"  # type: ignore[misc]

    def test_explicit_root_expands_home_and_relative_paths_use_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = base / "home"
            home_root = write_registry(home / "home-registry")
            relative_root = write_registry(base / "relative-registry")
            environment = isolated_environment(base, home=home)

            home_result = resolve_registry_root(
                "~/home-registry",
                cwd=base,
                environ=environment,
            )
            relative_result = resolve_registry_root(
                "relative-registry",
                cwd=base,
                environ=environment,
            )

        self.assertEqual(home_result.path, home_root.resolve())
        self.assertEqual(relative_result.path, relative_root.resolve())

    def test_environment_root_is_used_when_explicit_root_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "environment-registry")
            environment = isolated_environment(base)
            environment["SKILL_REGISTRY_ROOT"] = str(root)

            result = resolve_registry_root(cwd=base, environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=root.resolve(), source="environment"))

    def test_nearest_cwd_ancestor_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            outer = write_registry(base / "outer")
            inner = write_registry(outer / "inner")
            nested = inner / "one" / "two"
            nested.mkdir(parents=True)

            result = resolve_registry_root(cwd=nested, environ=isolated_environment(base))

        self.assertEqual(result, ResolvedRegistryRoot(path=inner.resolve(), source="cwd"))

    def test_cwd_discovery_does_not_select_a_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_registry(base / "registry-sibling")
            unrelated = base / "unrelated"
            unrelated.mkdir()

            with self.assertRaisesRegex(RegistryRootError, "Could not resolve a skill registry root"):
                resolve_registry_root(cwd=unrelated, environ=isolated_environment(base))

    def test_explicit_root_has_precedence_over_all_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            explicit = write_registry(base / "explicit")
            environment_root = write_registry(base / "environment")
            cwd_root = write_registry(base / "cwd")
            default_root = write_registry(base / "default")
            environment = isolated_environment(base)
            environment["SKILL_REGISTRY_ROOT"] = str(environment_root)
            write_default_pointer(environment, default_root)

            result = resolve_registry_root(explicit, cwd=cwd_root, environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=explicit.resolve(), source="explicit"))

    def test_environment_root_has_precedence_over_cwd_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment_root = write_registry(base / "environment")
            cwd_root = write_registry(base / "cwd")
            default_root = write_registry(base / "default")
            environment = isolated_environment(base)
            environment["SKILL_REGISTRY_ROOT"] = str(environment_root)
            write_default_pointer(environment, default_root)

            result = resolve_registry_root(cwd=cwd_root, environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=environment_root.resolve(), source="environment"))

    def test_cwd_root_has_precedence_over_default_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cwd_root = write_registry(base / "cwd")
            default_root = write_registry(base / "default")
            environment = isolated_environment(base)
            write_default_pointer(environment, default_root)

            result = resolve_registry_root(cwd=cwd_root, environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=cwd_root.resolve(), source="cwd"))

    def test_invalid_explicit_root_fails_without_falling_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cwd_root = write_registry(base / "cwd")
            environment = isolated_environment(base)
            environment["SKILL_REGISTRY_ROOT"] = str(cwd_root)

            with self.assertRaisesRegex(RegistryRootError, "Explicit registry root does not contain"):
                resolve_registry_root(base / "missing", cwd=cwd_root, environ=environment)

    def test_explicit_root_marker_inspection_permission_error_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "explicit")
            marker = (root / "registry.yaml").resolve()
            original_stat = Path.stat

            def guarded_stat(path: Path, *args, **kwargs):
                if path == marker:
                    raise PermissionError("explicit marker denied")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", guarded_stat):
                with self.assertRaisesRegex(
                    RegistryRootError,
                    "Could not inspect registry.yaml for Explicit registry root",
                ):
                    resolve_registry_root(root, cwd=base, environ=isolated_environment(base))

    def test_empty_or_invalid_environment_root_fails_without_falling_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cwd_root = write_registry(base / "cwd")

            empty_environment = isolated_environment(base)
            empty_environment["SKILL_REGISTRY_ROOT"] = "  "
            with self.assertRaisesRegex(RegistryRootError, "SKILL_REGISTRY_ROOT is empty"):
                resolve_registry_root(cwd=cwd_root, environ=empty_environment)

            invalid_environment = isolated_environment(base)
            invalid_environment["SKILL_REGISTRY_ROOT"] = str(base / "missing")
            with self.assertRaisesRegex(RegistryRootError, "SKILL_REGISTRY_ROOT does not contain"):
                resolve_registry_root(cwd=cwd_root, environ=invalid_environment)

    def test_environment_root_marker_inspection_permission_error_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "environment")
            marker = (root / "registry.yaml").resolve()
            environment = isolated_environment(base)
            environment["SKILL_REGISTRY_ROOT"] = str(root)
            original_stat = Path.stat

            def guarded_stat(path: Path, *args, **kwargs):
                if path == marker:
                    raise PermissionError("environment marker denied")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", guarded_stat):
                with self.assertRaisesRegex(
                    RegistryRootError,
                    "Could not inspect registry.yaml for SKILL_REGISTRY_ROOT",
                ):
                    resolve_registry_root(cwd=base, environ=environment)

    def test_xdg_default_pointer_is_used_and_overrides_home_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            xdg_root = write_registry(base / "xdg-registry")
            home_root = write_registry(base / "home-registry")
            environment = isolated_environment(base)
            environment["XDG_CONFIG_HOME"] = str(base / "xdg")
            write_default_pointer(environment, xdg_root)

            home_pointer = Path(environment["HOME"]) / ".config" / "skill-registry" / "default-root"
            home_pointer.parent.mkdir(parents=True)
            home_pointer.write_text(f"{home_root}\n", encoding="utf-8")

            result = resolve_registry_root(cwd=base / "unrelated", environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=xdg_root.resolve(), source="default"))

    def test_home_fallback_default_pointer_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "default-registry")
            unrelated = base / "unrelated"
            unrelated.mkdir()
            environment = isolated_environment(base)
            environment["XDG_CONFIG_HOME"] = ""
            write_default_pointer(environment, root)

            result = resolve_registry_root(cwd=unrelated, environ=environment)

        self.assertEqual(result, ResolvedRegistryRoot(path=root.resolve(), source="default"))

    def test_relative_pointer_target_is_resolved_from_pointer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            pointer = default_pointer_path(environment).resolve(strict=False)
            root = write_registry(pointer.parent / "registry")
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text("registry\n", encoding="utf-8")
            unrelated = base / "unrelated"
            unrelated.mkdir()

            result = resolve_registry_root(cwd=unrelated, environ=environment)

        self.assertEqual(result.path, root.resolve())

    def test_missing_pointer_is_absent_and_no_root_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            unrelated = base / "unrelated"
            unrelated.mkdir()

            with self.assertRaises(RegistryRootError) as raised:
                resolve_registry_root(cwd=unrelated, environ=isolated_environment(base))

        message = str(raised.exception)
        self.assertIn("--root PATH", message)
        self.assertIn("SKILL_REGISTRY_ROOT", message)
        self.assertIn("inside a registry checkout", message)
        self.assertIn("default-root pointer", message)

    def test_pointer_metadata_inspection_permission_error_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            unrelated = base / "unrelated"
            unrelated.mkdir()
            environment = isolated_environment(base)
            pointer = default_pointer_path(environment).resolve(strict=False)
            original_lstat = Path.lstat

            def guarded_lstat(path: Path, *args, **kwargs):
                if path == pointer:
                    raise PermissionError("pointer metadata denied")
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", guarded_lstat):
                with self.assertRaisesRegex(
                    RegistryRootError,
                    "Could not inspect default registry root pointer",
                ):
                    resolve_registry_root(cwd=unrelated, environ=environment)

    def test_current_working_directory_inspection_permission_error_is_guarded(self) -> None:
        with patch("skill_registry.root.Path.cwd", side_effect=PermissionError("cwd denied")):
            with self.assertRaisesRegex(RegistryRootError, "Could not inspect current working directory"):
                resolve_registry_root(environ={})

    def test_unreadable_pointer_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            pointer = default_pointer_path(environment)
            pointer.parent.mkdir(parents=True)
            pointer.write_text("/unused\n", encoding="utf-8")

            with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(RegistryRootError, "Could not read default registry root pointer"):
                    resolve_registry_root(cwd=base, environ=environment)

    def test_empty_multiline_and_invalid_pointer_targets_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            environment = isolated_environment(base)
            pointer = default_pointer_path(environment)
            pointer.parent.mkdir(parents=True)

            pointer.write_text(" \n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryRootError, "pointer is empty"):
                resolve_registry_root(cwd=base, environ=environment)

            pointer.write_text("/first\n/second\n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryRootError, "exactly one path"):
                resolve_registry_root(cwd=base, environ=environment)

            pointer.write_text(f"{base / 'missing'}\n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryRootError, "Default registry root does not contain"):
                resolve_registry_root(cwd=base, environ=environment)

    def test_registry_yaml_must_be_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "registry"
            (root / "registry.yaml").mkdir(parents=True)

            with self.assertRaisesRegex(RegistryRootError, "does not contain a regular registry.yaml"):
                resolve_registry_root(root, cwd=base, environ=isolated_environment(base))


class RegistryRootCliTests(unittest.TestCase):
    def test_validate_with_explicit_root_from_unrelated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")
            unrelated = base / "unrelated"
            unrelated.mkdir()

            result = run_cli("--root", str(root), "--json", "validate", cwd=unrelated, home=base / "home")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_validate_with_omitted_root_from_instance_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")

            result = run_cli("validate", "--json", cwd=root, home=base / "home")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_validate_with_omitted_root_from_nested_instance_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")
            nested = root / "nested" / "directory"
            nested.mkdir(parents=True)

            result = run_cli("--json", "validate", cwd=nested, home=base / "home")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_validate_with_environment_root_from_unrelated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")
            unrelated = base / "unrelated"
            unrelated.mkdir()

            result = run_cli(
                "--json",
                "validate",
                cwd=unrelated,
                home=base / "home",
                env_updates={"SKILL_REGISTRY_ROOT": str(root)},
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_explicit_root_wins_over_invalid_environment_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")
            unrelated = base / "unrelated"
            unrelated.mkdir()

            result = run_cli(
                "--root",
                str(root),
                "--json",
                "validate",
                cwd=unrelated,
                home=base / "home",
                env_updates={"SKILL_REGISTRY_ROOT": str(base / "missing")},
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_no_resolvable_root_returns_json_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            unrelated = base / "unrelated"
            unrelated.mkdir()

            result = run_cli("--json", "validate", cwd=unrelated, home=base / "home")

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("Could not resolve a skill registry root", payload["error"])

    def test_inaccessible_explicit_source_returns_json_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = write_registry(base / "registry")
            marker = (root / "registry.yaml").resolve()
            original_stat = Path.stat
            stdout = StringIO()
            stderr = StringIO()

            def guarded_stat(path: Path, *args, **kwargs):
                if path == marker:
                    raise PermissionError("CLI marker denied")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", guarded_stat):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    return_code = main(["--root", str(root), "--json", "validate"])

        self.assertEqual(return_code, 3)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("Traceback", stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload), {"error", "ok"})
        self.assertFalse(payload["ok"])
        self.assertIn("Could not inspect registry.yaml for Explicit registry root", payload["error"])


def write_registry(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.yaml").write_text("{}\n", encoding="utf-8")
    (root / "runtimes.yaml").write_text(
        "runtime_profiles:\n"
        "  - name: test-runtime\n"
        "    probe:\n"
        "      strategy: path-list\n",
        encoding="utf-8",
    )
    (root / "skills").mkdir(exist_ok=True)
    (root / "targets").mkdir(exist_ok=True)
    return root


def isolated_environment(base: Path, *, home: Path | None = None) -> dict[str, str]:
    return {"HOME": str(home or base / "home")}


def default_pointer_path(environment: dict[str, str]) -> Path:
    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "skill-registry" / "default-root"
    return Path(environment["HOME"]) / ".config" / "skill-registry" / "default-root"


def write_default_pointer(environment: dict[str, str], root: Path) -> Path:
    pointer = default_pointer_path(environment)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{root}\n", encoding="utf-8")
    return pointer


def run_cli(
    *args: str,
    cwd: Path,
    home: Path,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["HOME"] = str(home)
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("SKILL_REGISTRY_ROOT", None)
    if env_updates:
        environment.update(env_updates)
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
