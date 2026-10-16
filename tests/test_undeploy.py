from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from skill_registry.config import load_registry
from skill_registry.deploy import apply_deploy, plan_deploy
from skill_registry.health import ToolHealthFilters, check_tool_health
from skill_registry.hermes_bundle_export import export_hermes_bundle
from skill_registry.reconcile import plan_reconciliation
from skill_registry.state import deployment_record_path, load_deployment_record
from skill_registry.status import check_status
from skill_registry.undeploy import (
    UndeployError, apply_undeploy, pending_path, plan_undeploy, read_receipt, receipt_path,
)
from skill_registry.validation import validate_registry
from test_runtime_health import run_cli, write_wrapper_fixture_registry


class UndeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        write_wrapper_fixture_registry(self.root)
        targets = [{"name": name, "system": "codex", "root": str(self.root / "installed" / name),
                    "state_dir": str(self.root / "state" / name), "supports_symlink": True,
                    "runtime_profiles": ["demo-runtime"]} for name in ("demo", "other")]
        (self.root / "targets" / "demo.yaml").write_text(yaml.safe_dump({"targets": targets}))
        self.config = load_registry(self.root)
        self.target = self.config.target_by_name["demo"]
        self.source = self.root / "skills" / "demo-skill"
        self.entry = self.target.root_path / "demo-skill"
        self.record = deployment_record_path(self.target, "demo-skill")

    def deploy(self, mode="symlink", targets=("demo",)):
        return apply_deploy(self.config, mode=mode, all_targets=False, target_names=targets,
                            skill_ids=("demo-skill",), deployed_by="test", reason="fixture")

    def remove(self, ids=("demo-skill",)):
        return apply_undeploy(self.config, target_name="demo", skill_ids=ids,
                              reason="Retire demo workflow", undeployed_by="operator")

    def metadata(self, **values):
        path = self.source / "skill.yaml"
        data = yaml.safe_load(path.read_text())
        data.update(values)
        path.write_text(yaml.safe_dump(data))
        self.config = load_registry(self.root)

    def snapshot(self):
        return {str(p.relative_to(self.root)): ("link", os.readlink(p)) if p.is_symlink() else
                ("file", p.read_bytes()) for p in self.root.rglob("*") if p.is_file() or p.is_symlink()}

    def test_dry_run_is_non_mutating(self):
        self.deploy()
        before = self.snapshot()
        result = run_cli("--root", str(self.root), "undeploy", "--target", "demo",
                         "--skill", "demo-skill", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["operations"][0]["operation"], "remove_symlink")
        self.assertEqual(self.snapshot(), before)

    def test_symlink_removal_preserves_source_and_other_target(self):
        self.deploy(targets=("demo", "other"))
        source_before = (self.source / "SKILL.md").read_bytes()
        result = self.remove()[0]
        self.assertTrue(result.applied)
        self.assertFalse(self.entry.is_symlink())
        self.assertFalse(self.record.exists())
        self.assertEqual((self.source / "SKILL.md").read_bytes(), source_before)
        self.assertTrue((self.config.target_by_name["other"].root_path / "demo-skill").is_symlink())
        receipt = read_receipt(self.config, self.target, "demo-skill")
        self.assertEqual(receipt["reason"], "Retire demo workflow")
        self.assertEqual(receipt["undeployed_by"], "operator")
        self.assertTrue((Path(receipt["backup_path"]) / "deployment-record.json").is_file())
        self.assertTrue((Path(receipt["backup_path"]) / "entry" / "demo-skill").is_symlink())
        self.assertEqual(check_status(self.config, ("demo",), ("demo-skill",))[0].status, "undeployed")

    def test_stale_copy_is_removable_and_backup_keeps_excluded_files(self):
        self.deploy(mode="copy")
        cache = self.entry / "__pycache__"
        cache.mkdir()
        (cache / "local.pyc").write_bytes(b"keep this excluded file")
        (self.source / "SKILL.md").write_text("New canonical content")
        result = self.remove()[0]
        self.assertFalse(self.entry.exists())
        backup = Path(result.backup_path) / "entry" / "demo-skill"
        self.assertEqual((backup / "__pycache__" / "local.pyc").read_bytes(), b"keep this excluded file")
        self.assertNotEqual((backup / "SKILL.md").read_text(), (self.source / "SKILL.md").read_text())

    def test_locally_drifted_copy_is_blocked(self):
        self.deploy(mode="copy")
        (self.entry / "SKILL.md").write_text("unique local improvement")
        before = self.snapshot()
        with self.assertRaisesRegex(UndeployError, "local drift"):
            self.remove()
        self.assertEqual(self.snapshot(), before)

    def test_wrong_symlink_is_blocked(self):
        self.deploy()
        self.entry.unlink()
        elsewhere = self.root / "unrelated"
        elsewhere.mkdir()
        self.entry.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(UndeployError, "different source"):
            self.remove()
        self.assertTrue(self.entry.is_symlink())
        self.assertTrue(elsewhere.exists())

    def test_unmanaged_matching_symlink_is_blocked(self):
        self.target.root_path.mkdir(parents=True)
        self.entry.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(UndeployError, "no valid deployment record"):
            self.remove()
        self.assertTrue(self.entry.is_symlink())

    def test_record_mode_type_mismatch_is_blocked(self):
        self.deploy()
        self.entry.unlink()
        shutil.copytree(self.source, self.entry)
        with self.assertRaisesRegex(UndeployError, "replaced"):
            self.remove()
        self.assertTrue(self.entry.is_dir())

    def test_malformed_record_is_blocked(self):
        self.deploy()
        self.record.write_text("not json")
        with self.assertRaisesRegex(UndeployError, "malformed"):
            self.remove()
        self.assertTrue(self.entry.is_symlink())

    def test_exact_entry_identity_is_required_not_resolved_alias(self):
        self.deploy()
        data = json.loads(self.record.read_text())
        data["target_path"] = str(self.source)
        self.record.write_text(json.dumps(data))
        with self.assertRaisesRegex(UndeployError, "target_path mismatch"):
            self.remove()

    def test_source_missing_symlink_can_be_removed_and_status_tracks_receipt(self):
        self.deploy()
        shutil.rmtree(self.source)
        self.config = load_registry(self.root)
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "orphaned")
        self.remove()
        result = run_cli("--root", str(self.root), "status", "--target", "demo", "--skill", "demo-skill", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "undeployed")

    def test_source_missing_copy_can_be_removed(self):
        self.deploy(mode="copy")
        shutil.rmtree(self.source)
        self.config = load_registry(self.root)
        self.remove()
        self.assertFalse(self.entry.exists())
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "undeployed")

    def test_removed_allowlist_entry_does_not_hide_installation(self):
        self.deploy()
        self.metadata(targets=["other"])
        self.assertEqual(len(check_status(self.config, ("demo",))), 1)
        self.remove()

    def test_missing_entry_finalizes_record_and_is_idempotent(self):
        self.deploy()
        self.entry.unlink()
        self.assertEqual(self.remove()[0].operation, "record_absent")
        before = self.snapshot()
        self.assertFalse(self.remove()[0].applied)
        self.assertEqual(before, self.snapshot())

    def test_known_absent_skill_is_noop_unknown_skill_blocks(self):
        before = self.snapshot()
        self.assertEqual(self.remove()[0].operation, "skip")
        self.assertEqual(before, self.snapshot())
        with self.assertRaisesRegex(UndeployError, "Unknown skill"):
            self.remove(("unknown",))

    def test_batch_preflight_blocks_all_before_mutation(self):
        self.deploy()
        with self.assertRaises(UndeployError):
            self.remove(("demo-skill", "unknown"))
        self.assertTrue(self.entry.is_symlink())
        self.assertTrue(self.record.exists())

    def test_unsafe_selector_and_missing_reason_are_rejected(self):
        self.deploy()
        for args, code in ((("--skill", "../escape", "--dry-run"), 3),
                           (("--skill", "demo-skill"), 2)):
            result = run_cli("--root", str(self.root), "undeploy", "--target", "demo", *args, "--json")
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertTrue(self.entry.is_symlink())

    def test_unsafe_state_symlink_is_blocked(self):
        self.deploy()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target.state_path / "backups").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UndeployError, "unsafe state"):
            self.remove()
        self.assertEqual(list(outside.iterdir()), [])

    def test_metadata_write_failure_restores_original_entry_and_record(self):
        self.deploy(mode="copy")
        original_record = self.record.read_bytes()
        with patch("skill_registry.undeploy._atomic_bytes", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(UndeployError, "rolled back"):
                self.remove()
        self.assertTrue(self.entry.is_dir())
        self.assertEqual(self.record.read_bytes(), original_record)
        self.assertFalse(receipt_path(self.target, "demo-skill").exists())
        self.assertFalse(pending_path(self.target, "demo-skill").exists())

    def test_record_removal_failure_restores_receipt_and_entry(self):
        self.deploy()
        original_unlink = Path.unlink
        def fail_record(path, *args, **kwargs):
            if path == self.record:
                raise OSError("record locked")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_record):
            with self.assertRaisesRegex(UndeployError, "rolled back"):
                self.remove()
        self.assertTrue(self.entry.is_symlink())
        self.assertTrue(self.record.exists())
        self.assertFalse(receipt_path(self.target, "demo-skill").exists())

    def test_pending_operation_blocks_status_deploy_and_undeploy(self):
        self.deploy()
        journal = pending_path(self.target, "demo-skill")
        journal.parent.mkdir(parents=True)
        journal.write_text("{}")
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "blocked")
        with self.assertRaisesRegex(UndeployError, "pending"):
            self.remove()
        with self.assertRaisesRegex(ValueError, "pending"):
            self.deploy()

    def test_target_recreation_after_undeploy_is_not_healthy(self):
        self.deploy()
        self.remove()
        self.entry.mkdir()
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "blocked")
        with self.assertRaisesRegex(UndeployError, "unmanaged"):
            self.remove()

    def test_explicit_redeployment_supersedes_old_receipt(self):
        self.deploy()
        self.remove()
        implicit = plan_deploy(self.config, mode="symlink", all_targets=False, target_names=("demo",), skill_ids=())
        self.assertEqual(implicit[0].operation, "skip")
        self.deploy()
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "in_sync")
        self.entry.unlink()
        self.assertNotEqual(check_status(self.config, ("demo",))[0].status, "undeployed")

    def test_retired_skill_cannot_be_installed_but_can_be_undeployed(self):
        self.deploy()
        self.metadata(lifecycle="retired")
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "retired_deployed")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.deploy()
        self.remove()
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "undeployed")
        with self.assertRaisesRegex(ValueError, "retired"):
            self.deploy()
        self.assertEqual(plan_deploy(self.config, mode="symlink", all_targets=False,
                                     target_names=("demo",), skill_ids=()), ())

    def test_retired_uninstalled_status_and_doctor_are_healthy(self):
        self.metadata(lifecycle="retired")
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "retired")
        result = run_cli("--root", str(self.root), "doctor", "--target", "demo", "--skill", "demo-skill", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["tool_health"]["results"], [])

    def test_retired_wrappers_are_never_probed(self):
        (self.source / "scripts" / "demo-tool").write_text("#!/bin/sh\nexit 42\n")
        self.metadata(lifecycle="retired")
        self.assertEqual(check_tool_health(self.config, ToolHealthFilters()), ())
        self.assertEqual(check_tool_health(self.config, ToolHealthFilters(skills=("demo-skill",))), ())

    def test_retired_reconcile_replacement_is_blocked(self):
        self.metadata(lifecycle="retired")
        with self.assertRaisesRegex(ValueError, "retired"):
            plan_reconciliation(self.config, target_name="demo", skill_id="demo-skill", action="replace")

    def test_invalid_lifecycle_is_validation_error(self):
        self.metadata(lifecycle="typo")
        self.assertIn("invalid_skill_lifecycle", {d.code for d in validate_registry(self.config).errors})

    def test_active_dependencies_and_sets_cannot_reference_retired_skill(self):
        other = self.root / "skills" / "other-skill"
        other.mkdir()
        (other / "SKILL.md").write_text("# Another skill")
        (other / "skill.yaml").write_text("id: other-skill\nskill_dependencies: [demo-skill]\n")
        (self.root / "skill-sets.yaml").write_text("skill_sets:\n  - id: demo\n    members: [demo-skill]\n")
        self.metadata(lifecycle="retired")
        codes = {d.code for d in validate_registry(self.config).errors}
        self.assertIn("retired_skill_dependency", codes)
        self.assertIn("retired_skill_set_member", codes)

    def test_retirement_can_be_reactivated_by_reviewed_metadata(self):
        self.metadata(lifecycle="retired")
        self.metadata(lifecycle="canonical")
        self.deploy()
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "in_sync")

    def test_legacy_placeholder_remains_compatible(self):
        self.metadata(lifecycle="placeholder")
        self.assertTrue(validate_registry(self.config).ok)
        self.deploy()

    def test_shared_target_entry_is_not_removed(self):
        self.deploy()
        path = self.root / "targets" / "demo.yaml"
        data = yaml.safe_load(path.read_text())
        data["targets"][1]["root"] = data["targets"][0]["root"]
        path.write_text(yaml.safe_dump(data))
        self.config = load_registry(self.root)
        with self.assertRaisesRegex(UndeployError, "another target"):
            self.remove()
        self.assertTrue(self.entry.is_symlink())

    def test_concurrent_copy_edit_during_backup_is_preserved(self):
        from skill_registry import undeploy
        self.deploy(mode="copy")
        original_copy = undeploy._copy_entry
        def edit_during_copy(source, destination):
            original_copy(source, destination)
            (source / "SKILL.md").write_text("new concurrent work")
        with patch("skill_registry.undeploy._copy_entry", side_effect=edit_during_copy):
            with self.assertRaisesRegex(UndeployError, "rolled back"):
                self.remove()
        self.assertEqual((self.entry / "SKILL.md").read_text(), "new concurrent work")
        self.assertTrue(self.record.exists())

    def test_malformed_receipt_and_pending_postcommit_are_not_hidden(self):
        self.deploy()
        self.remove()
        journal = pending_path(self.target, "demo-skill")
        journal.write_text("{}")
        plan = plan_deploy(self.config, mode="symlink", all_targets=False, target_names=("demo",), skill_ids=())
        self.assertEqual(plan[0].operation, "block")
        journal.unlink()
        receipt_path(self.target, "demo-skill").write_text("{}")
        self.assertEqual(check_status(self.config, ("demo",))[0].status, "blocked")

    def test_retired_skill_set_cannot_be_deployed(self):
        (self.root / "skill-sets.yaml").write_text("skill_sets:\n  - id: demo\n    members: [demo-skill]\n")
        self.metadata(lifecycle="retired")
        result = run_cli("--root", str(self.root), "deploy", "--target", "demo", "--skill-set", "demo", "--dry-run", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.entry.exists())

    def test_late_batch_failure_reports_completed_relations(self):
        from skill_registry import undeploy
        second = self.root / "skills" / "second-skill"
        shutil.copytree(self.source, second)
        metadata = yaml.safe_load((second / "skill.yaml").read_text())
        metadata["id"] = "second-skill"
        (second / "skill.yaml").write_text(yaml.safe_dump(metadata))
        self.config = load_registry(self.root)
        apply_deploy(self.config, mode="symlink", all_targets=False, target_names=("demo",),
                     skill_ids=("demo-skill", "second-skill"), deployed_by="test", reason="fixture")
        original = undeploy._apply_one
        def fail_second(config, target, plan, reason, actor):
            if plan.skill_id == "second-skill":
                raise OSError("second relation locked")
            return original(config, target, plan, reason, actor)
        with patch("skill_registry.undeploy._apply_one", side_effect=fail_second):
            with self.assertRaises(UndeployError) as failure:
                self.remove(("demo-skill", "second-skill"))
        self.assertEqual([r.skill_id for r in failure.exception.completed], ["demo-skill"])
        self.assertFalse(self.entry.is_symlink())
        self.assertTrue((self.target.root_path / "second-skill").is_symlink())

    def test_final_journal_failure_restores_entry_and_record(self):
        self.deploy(mode="copy")
        original_unlink = Path.unlink
        failed = False
        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path == pending_path(self.target, "demo-skill") and not failed:
                failed = True
                raise OSError("journal locked")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_once):
            with self.assertRaisesRegex(UndeployError, "rolled back"):
                self.remove()
        self.assertTrue(self.entry.is_dir())
        self.assertTrue(self.record.exists())
        self.assertFalse(receipt_path(self.target, "demo-skill").exists())
        self.assertFalse(pending_path(self.target, "demo-skill").exists())


if __name__ == "__main__":
    unittest.main()
