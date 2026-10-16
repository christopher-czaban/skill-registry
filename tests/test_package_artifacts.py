from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from skill_registry.bootstrap import installed_engine_distribution


REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_NAMES = {
    ".gitignore",
    "pyproject.toml",
    "registry.yaml",
    "runtime-dependencies.yaml",
    "runtimes.yaml",
    "skill-sets.yaml",
}


@unittest.skipIf(
    os.environ.get("SKILL_REGISTRY_SKIP_ARTIFACT_E2E") == "1",
    "artifact E2E runs after the release job's single package build",
)
class PackageArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="skill-registry-package-artifacts-")
        cls.base = Path(cls._temporary.name)
        prebuilt_dist = os.environ.get("SKILL_REGISTRY_ARTIFACT_DIR", "")
        cls.dist = Path(prebuilt_dist).resolve() if prebuilt_dist else cls.base / "dist"
        cls.cache = cls.base / "uv-cache"
        if not prebuilt_dist:
            cls.source = cls.base / "source"
            cls.source.mkdir()
            (cls.source / "src").mkdir()
            shutil.copytree(REPO_ROOT / "src" / "skill_registry", cls.source / "src" / "skill_registry")
            shutil.copy2(REPO_ROOT / "pyproject.toml", cls.source / "pyproject.toml")
            shutil.copy2(REPO_ROOT / "README.md", cls.source / "README.md")
            shutil.copy2(REPO_ROOT / "LICENSE", cls.source / "LICENSE")
            environment = isolated_process_environment(cls.base / "build-home", cls.cache)
            result = subprocess.run(
                ["uv", "build", "--wheel", "--sdist", "--out-dir", str(cls.dist)],
                cwd=cls.source,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=180,
            )
            if result.returncode != 0:
                raise AssertionError(f"uv build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        wheels = tuple(cls.dist.glob("*.whl"))
        sdists = tuple(cls.dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise AssertionError(f"Expected one wheel and one sdist, found {wheels!r} and {sdists!r}")
        cls.wheel = wheels[0]
        cls.sdist = sdists[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_artifact_names_metadata_and_console_script_match_distribution_contract(self) -> None:
        self.assertEqual(self.wheel.name, "agent_skill_registry-0.2.0-py3-none-any.whl")
        self.assertEqual(self.sdist.name, "agent_skill_registry-0.2.0.tar.gz")

        with zipfile.ZipFile(self.wheel) as archive:
            metadata_members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            entry_point_members = [
                name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
            ]
            self.assertEqual(len(metadata_members), 1)
            self.assertEqual(len(entry_point_members), 1)
            metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_members[0]))
            entry_points = configparser.ConfigParser()
            entry_points.read_string(archive.read(entry_point_members[0]).decode("utf-8"))

        self.assertEqual(metadata["Name"], "agent-skill-registry")
        self.assertEqual(metadata["Version"], "0.2.0")
        self.assertEqual(metadata["Requires-Python"], ">=3.9")
        self.assertEqual(
            set(metadata.get_all("Project-URL", [])),
            {
                "Homepage, https://github.com/christopher-czaban/skill-registry",
                "Documentation, https://github.com/christopher-czaban/skill-registry#readme",
                "Issues, https://github.com/christopher-czaban/skill-registry/issues",
                "Source, https://github.com/christopher-czaban/skill-registry",
            },
        )
        self.assertEqual(
            dict(entry_points["console_scripts"]),
            {"skill-registry": "skill_registry.cli:main"},
        )

    def test_wheel_contains_every_registry_instance_resource(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        expected = {
            f"skill_registry/templates/registry-instance/{resource}"
            for resource in RESOURCE_NAMES
        }
        self.assertEqual(expected - names, set())

    def test_sdist_contains_every_registry_instance_resource(self) -> None:
        with tarfile.open(self.sdist, mode="r:gz") as archive:
            names = set(archive.getnames())
        for resource in RESOURCE_NAMES:
            suffix = f"/src/skill_registry/templates/registry-instance/{resource}"
            self.assertTrue(any(name.endswith(suffix) for name in names), resource)

    def test_generated_instance_syncs_and_validates_from_local_wheel_without_engine_source(self) -> None:
        generated = self.base / "generated-instance"
        runner = self.base / "e2e-runner"
        bootstrap_venv = self.base / "bootstrap-venv"
        identity = installed_engine_distribution()
        environment = isolated_process_environment(self.base / "e2e-home", self.base / "e2e-cache")
        create_venv = subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(bootstrap_venv)],
            cwd=self.base,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(create_venv.returncode, 0, create_venv.stderr + create_venv.stdout)
        bootstrap_python = bootstrap_venv / "bin" / "python"
        install = subprocess.run(
            ["uv", "pip", "install", "--python", str(bootstrap_python), str(self.wheel)],
            cwd=self.base,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
        bootstrap_cli = bootstrap_venv / "bin" / "skill-registry"
        self.assertFalse((bootstrap_venv / "bin" / "agent-skill-registry").exists())

        help_result = subprocess.run(
            [str(bootstrap_cli), "--help"],
            cwd=self.base,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr + help_result.stdout)
        self.assertIn("usage: skill-registry", help_result.stdout)

        init = subprocess.run(
            [
                str(bootstrap_cli),
                "--json",
                "init",
                str(generated),
                "--target",
                "codex",
                "--target",
                "agents",
            ],
            cwd=self.base,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
        init_payload = json.loads(init.stdout)
        self.assertTrue(init_payload["ok"])
        self.assertEqual(init_payload["engine_distribution"], "agent-skill-registry")
        self.assertEqual(init_payload["engine_version"], "0.2.0")
        self.assertIn(
            '"agent-skill-registry==0.2.0"',
            (generated / "pyproject.toml").read_text(encoding="utf-8"),
        )
        assert_no_development_source(generated)
        self.assertFalse((generated / "uv.lock").exists())
        self.assertTrue((generated / "skill-sets.yaml").is_file())
        self.assertFalse((generated / ("skillsets" + ".yaml")).exists())

        shutil.copytree(generated, runner)
        skill_dir = runner / "skills" / "example-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Example Skill\n", encoding="utf-8")
        (skill_dir / "skill.yaml").write_text(
            "id: example-skill\ntitle: Example Skill\ntargets:\n  - codex\n",
            encoding="utf-8",
        )
        environment["UV_FIND_LINKS"] = str(self.dist)
        sync = subprocess.run(
            ["uv", "sync", "--no-sources"],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(sync.returncode, 0, sync.stderr + sync.stdout)

        frozen_sync = subprocess.run(
            ["uv", "sync", "--frozen"],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(frozen_sync.returncode, 0, frozen_sync.stderr + frozen_sync.stdout)

        validation = subprocess.run(
            ["uv", "run", "--frozen", "skill-registry", "validate", "--json"],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(validation.returncode, 0, validation.stderr + validation.stdout)
        self.assertTrue(json.loads(validation.stdout)["ok"])

        deploy = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "skill-registry",
                "--json",
                "deploy",
                "--dry-run",
                "--target",
                "codex",
                "--skill",
                "example-skill",
            ],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(deploy.returncode, 0, deploy.stderr + deploy.stdout)
        self.assertTrue(json.loads(deploy.stdout)["ok"])

        status = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "skill-registry",
                "--json",
                "status",
                "--target",
                "codex",
                "--skill",
                "example-skill",
            ],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(status.returncode, 1, status.stderr + status.stdout)
        self.assertFalse(json.loads(status.stdout)["ok"])

        guard = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "skill-registry",
                "--json",
                "guard",
                "check",
                "--operation",
                "edit",
                "--path",
                str(runner / "registry.yaml"),
            ],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(guard.returncode, 0, guard.stderr + guard.stdout)
        self.assertEqual(json.loads(guard.stdout)["decision"], "allow")

        def installed_command(*arguments: str):
            return subprocess.run(
                ["uv", "run", "--frozen", "skill-registry", "--json", *arguments],
                cwd=runner, env=environment, text=True, capture_output=True,
                check=False, timeout=180,
            )

        installed = installed_command("deploy", "--target", "codex", "--skill", "example-skill",
                                      "--reason", "installed-wheel smoke")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        preview = installed_command("undeploy", "--target", "codex", "--skill", "example-skill", "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        metadata_path = skill_dir / "skill.yaml"
        metadata_path.write_text(metadata_path.read_text(encoding="utf-8") + "lifecycle: retired\n", encoding="utf-8")
        prohibited = installed_command("deploy", "--target", "codex", "--skill", "example-skill", "--dry-run")
        self.assertEqual(prohibited.returncode, 1, prohibited.stdout + prohibited.stderr)
        removed = installed_command("undeploy", "--target", "codex", "--skill", "example-skill",
                                    "--reason", "retired installed-wheel smoke")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertTrue(json.loads(removed.stdout)["operations"][0]["receipt_path"])
        final_status = installed_command("status", "--target", "codex", "--skill", "example-skill")
        self.assertEqual(final_status.returncode, 0, final_status.stdout + final_status.stderr)
        self.assertEqual(json.loads(final_status.stdout)["results"][0]["status"], "undeployed")
        self.assertTrue((skill_dir / "SKILL.md").is_file())

        installation = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-c",
                (
                    "import importlib.metadata, pathlib, skill_registry; "
                    "print(importlib.metadata.version('agent-skill-registry')); "
                    "print(pathlib.Path(skill_registry.__file__).resolve())"
                ),
            ],
            cwd=runner,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(installation.returncode, 0, installation.stderr + installation.stdout)
        lines = installation.stdout.splitlines()
        self.assertEqual(lines[0], identity.version)
        installed_path = Path(lines[1])
        self.assertTrue(installed_path.is_relative_to((runner / ".venv").resolve()))
        self.assertFalse(installed_path.is_relative_to(REPO_ROOT))

        # The initializer's canonical output stays untouched by test-only package discovery.
        assert_no_development_source(generated)
        self.assertFalse((generated / "uv.lock").exists())


def isolated_process_environment(home: Path, cache: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["UV_CACHE_DIR"] = str(cache)
    environment["UV_NO_PROGRESS"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("SKILL_REGISTRY_ROOT", None)
    environment.pop("XDG_CONFIG_HOME", None)
    environment.pop("UV_FIND_LINKS", None)
    environment.pop("UV_INDEX", None)
    environment.pop("UV_INDEX_URL", None)
    environment.pop("UV_EXTRA_INDEX_URL", None)
    return environment


def assert_no_development_source(root: Path) -> None:
    forbidden = (
        str(REPO_ROOT),
        "file://",
        "[tool.uv.sources]",
        "UV_FIND_LINKS",
        "find-links",
        "test-index",
    )
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        for value in forbidden:
            if value:
                if value in content:
                    raise AssertionError(f"Forbidden development source {value!r} in {path}")


if __name__ == "__main__":
    unittest.main()
