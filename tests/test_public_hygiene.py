from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_PATH = REPO_ROOT / "scripts" / "check_public_hygiene.py"
SPEC = importlib.util.spec_from_file_location("public_hygiene", HYGIENE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load hygiene module from {HYGIENE_PATH}")
HYGIENE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HYGIENE
SPEC.loader.exec_module(HYGIENE)


def write_expected_tree(root: Path) -> None:
    for relative in HYGIENE.EXPECTED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


class PublicHygieneTests(unittest.TestCase):
    def test_repository_matches_exact_public_inventory_and_passes_hygiene(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HYGIENE_PATH), "--root", str(REPO_ROOT)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        actual = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in HYGIENE.collect_public_files(REPO_ROOT)
        }
        self.assertEqual(actual, set(HYGIENE.EXPECTED_FILES))

    def test_sensitive_rules_report_path_and_rule_without_matching_content(self) -> None:
        cases = (
            ("/" + "Users" + "/example/private/file.txt", "absolute-posix-home"),
            ("C:" + "\\Users\\example\\private.txt", "absolute-windows-profile"),
            (HYGIENE.INVALID_RESOURCE_NAME, "invalid-skill-set-resource"),
            ("-----BEGIN " + "PRIVATE KEY-----", "private-key-marker"),
        )
        for content, expected_rule in cases:
            with self.subTest(expected_rule=expected_rule):
                findings = HYGIENE.scan_text("fixture.txt", content)
                self.assertIn(expected_rule, {finding.rule for finding in findings})
                rendered = "\n".join(finding.render() for finding in findings)
                self.assertIn("fixture.txt", rendered)
                self.assertNotIn(content, rendered)

    def test_archive_path_policy_rejects_private_content_and_plural_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "package.whl"
            private = HYGIENE._safe_member_name(archive, "package/skills/example/SKILL.md")
            plural = HYGIENE._safe_member_name(archive, f"package/{HYGIENE.INVALID_RESOURCE_NAME}")

        self.assertIn("forbidden-archive-path", {finding.rule for finding in private})
        self.assertIn("invalid-skill-set-resource", {finding.rule for finding in plural})

    def test_no_git_staging_rejects_entries_under_previously_ignored_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_expected_tree(root)
            (root / "build" / "nested").mkdir(parents=True)
            (root / "build" / "nested" / "private.txt").write_text("unexpected\n", encoding="utf-8")

            findings, _, _ = HYGIENE.scan_source(root, require_no_git=True)

        rules_by_path = {(finding.path, finding.rule) for finding in findings}
        self.assertIn(("build", "unexpected-directory"), rules_by_path)
        self.assertIn(("build/nested", "unexpected-directory"), rules_by_path)
        self.assertIn(("build/nested/private.txt", "unexpected-file"), rules_by_path)

    def test_git_backed_scan_uses_exact_tracked_inventory_in_ignored_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_expected_tree(root)
            (root / ".git").mkdir()
            tracked_private = root / "build" / "tracked-private.txt"
            tracked_private.parent.mkdir()
            tracked_private.write_bytes(b"reviewed-prefix\x00reviewed-suffix")
            tracked_names = sorted(HYGIENE.EXPECTED_FILES | {"build/tracked-private.txt"})
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"\0".join(name.encode("utf-8") for name in tracked_names) + b"\0",
                stderr=b"",
            )

            with patch.object(HYGIENE.subprocess, "run", return_value=completed) as mocked_run:
                findings, file_count, _ = HYGIENE.scan_source(root, require_no_git=False)

        mocked_run.assert_called_once_with(
            ["git", "-C", str(root), "ls-files", "-z", "--"],
            capture_output=True,
            check=False,
        )
        rules_by_path = {(finding.path, finding.rule) for finding in findings}
        self.assertEqual(file_count, len(HYGIENE.EXPECTED_FILES) + 1)
        self.assertIn(("build/tracked-private.txt", "unexpected-file"), rules_by_path)
        self.assertIn(("build/tracked-private.txt", "disallowed-control-byte"), rules_by_path)

    def test_git_backed_scan_fails_closed_when_tracked_inventory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            completed = subprocess.CompletedProcess(args=[], returncode=128, stdout=b"", stderr=b"failure")

            with patch.object(HYGIENE.subprocess, "run", return_value=completed):
                findings, file_count, text_bytes = HYGIENE.scan_source(root, require_no_git=False)

        self.assertEqual(file_count, 0)
        self.assertEqual(text_bytes, 0)
        self.assertEqual({finding.rule for finding in findings}, {"git-inventory-failed"})

    def test_source_payload_rejects_utf8_decodable_nul_and_control_bytes(self) -> None:
        for payload in (b"prefix\x00suffix", b"prefix\x1bsuffix", "prefix\u0085suffix".encode("utf-8")):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    write_expected_tree(root)
                    (root / "README.md").write_bytes(payload)

                    findings, _, _ = HYGIENE.scan_source(root, require_no_git=True)

                readme_rules = {finding.rule for finding in findings if finding.path == "README.md"}
                self.assertIn("disallowed-control-byte", readme_rules)

    def test_wheel_and_sdist_payloads_reject_utf8_decodable_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            wheel = base / "package.whl"
            with zipfile.ZipFile(wheel, mode="w") as package:
                package.writestr("package/data.txt", b"prefix\x00suffix")

            sdist = base / "package.tar.gz"
            payload = "prefix\u0085suffix".encode("utf-8")
            member = tarfile.TarInfo("package/data.txt")
            member.size = len(payload)
            with tarfile.open(sdist, mode="w:gz") as package:
                package.addfile(member, io.BytesIO(payload))

            wheel_findings, _ = HYGIENE.scan_zip(wheel)
            sdist_findings, _ = HYGIENE.scan_tar(sdist)

        self.assertIn("disallowed-control-byte", {finding.rule for finding in wheel_findings})
        self.assertIn("disallowed-control-byte", {finding.rule for finding in sdist_findings})

    def test_package_data_uses_exact_singular_resource_name(self) -> None:
        metadata = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        template_names = {
            path.name
            for path in (REPO_ROOT / "src" / "skill_registry" / "templates" / "registry-instance").iterdir()
        }

        self.assertIn('"templates/registry-instance/skill-sets.yaml"', metadata)
        self.assertNotIn(HYGIENE.INVALID_RESOURCE_NAME, metadata)
        self.assertIn("skill-sets.yaml", template_names)
        self.assertNotIn(HYGIENE.INVALID_RESOURCE_NAME, template_names)

    def test_readme_creates_lock_before_using_frozen_sync_on_later_machines(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        first_sync = readme.index("\nuv sync\n")
        commit_lock = readme.index("commit that lock")
        later_sync = readme.index("`uv sync --frozen`")
        self.assertLess(first_sync, commit_lock)
        self.assertLess(commit_lock, later_sync)

    def test_ci_workflow_has_exact_read_only_triggers_matrix_and_pins(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(workflow["on"]), {"pull_request", "push", "workflow_dispatch"})
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "true")
        matrix = workflow["jobs"]["tests"]["strategy"]["matrix"]["include"]
        self.assertEqual(
            matrix,
            [
                {"os": "ubuntu-latest", "python-version": "3.9"},
                {"os": "ubuntu-latest", "python-version": "3.14"},
                {"os": "macos-latest", "python-version": "3.14"},
            ],
        )
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", text)
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0", text)
        self.assertIn("astral-sh/setup-uv@ae62891fec2bb8e7d6c99fc78c9fec3a63790f8d # v10.0.0", text)
        self.assertIn("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1", text)
        self.assertNotIn("id-token", text)

    def test_release_workflow_is_manual_build_or_release_only_oidc_publish(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "release.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertEqual(set(workflow["on"]), {"release", "workflow_dispatch"})
        self.assertEqual(workflow["on"]["release"]["types"], ["published"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        publish = workflow["jobs"]["publish"]
        self.assertEqual(publish["if"], "github.event_name == 'release'")
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"], {"contents": "read", "id-token": "write"})
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1", text)
        self.assertIn("pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2", text)
        self.assertNotRegex(text, r"(?m)^\s+(?:password|user|api-token):")
        self.assertEqual(text.count("uv build --wheel --sdist"), 1)


if __name__ == "__main__":
    unittest.main()
