from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import TextIO

from .bootstrap import (
    BootstrapError,
    clear_default_registry_root,
    initialize_registry_instance,
    set_default_registry_root,
)
from .config import ConfigError, load_registry
from .deploy import apply_deploy, plan_deploy, plan_ok
from .discovery import discover
from .guardrail import (
    GUARDRAIL_MODES,
    SUPPORTED_OPERATIONS,
    GuardrailConfigError,
    check_guardrail,
)
from .health import ToolHealthFilters, check_tool_health, health_ok
from .hermes_bundle_export import HermesBundleExportResult, export_hermes_bundle
from .proposals import ProposalError, apply_import_proposal, inspect_import_proposal
from .reconcile import ALL_ACTIONS, apply_reconciliation, plan_reconciliation
from .root import RegistryRootError, resolve_registry_root
from .skills import list_registry_skills
from .status import check_status, status_ok
from .targets import target_filter_errors
from .undeploy import UndeployError, apply_undeploy, known_skill_ids, plan_undeploy, safe_skill_id
from .validation import validate_registry


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    if args.command == "init":
        try:
            return _cmd_init(args, sys.stdout)
        except BootstrapError as exc:
            _print_error(str(exc), args.json)
            return 3

    if args.command == "root":
        try:
            return _cmd_root(args, sys.stdout)
        except RegistryRootError as exc:
            _print_error(str(exc), args.json)
            return 3

    try:
        resolved_root = resolve_registry_root(args.root)
        config = load_registry(resolved_root.path)
    except (RegistryRootError, ConfigError) as exc:
        _print_error(str(exc), args.json)
        return 3

    if args.command == "validate":
        return _cmd_validate(config, args, sys.stdout)
    if args.command == "tool-health":
        return _cmd_tool_health(config, args, sys.stdout)
    if args.command == "doctor":
        return _cmd_doctor(config, args, sys.stdout)
    if args.command == "discover":
        return _cmd_discover(config, args, sys.stdout)
    if args.command == "status":
        return _cmd_status(config, args, sys.stdout)
    if args.command == "deploy":
        return _cmd_deploy(config, args, sys.stdout)
    if args.command == "undeploy":
        return _cmd_undeploy(config, args, sys.stdout)
    if args.command == "export":
        return _cmd_export(config, args, sys.stdout)
    if args.command == "reconcile":
        return _cmd_reconcile(config, args, sys.stdout)
    if args.command == "proposal":
        return _cmd_proposal(config, args, sys.stdout)
    if args.command == "guard":
        return _cmd_guard(config, args, sys.stdout)

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-registry")
    parser.add_argument("--root", default=None, help="Registry root directory.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    subcommands = parser.add_subparsers(dest="command")

    init_parser = subcommands.add_parser("init", help="Create a new user-owned registry instance.")
    _add_json_option(init_parser)
    init_parser.add_argument("path", help="Destination path for the new registry instance.")
    init_parser.add_argument("--name", default=None, help="Project name; defaults to the destination basename.")
    init_parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Generic target preset to include. Repeatable.",
    )
    init_parser.add_argument(
        "--set-default",
        action="store_true",
        help="Set the machine-local default-root pointer after successful validation.",
    )

    root_parser = subcommands.add_parser("root", help="Inspect or update the machine-local default root.")
    root_subcommands = root_parser.add_subparsers(dest="root_command", required=True)

    root_show = root_subcommands.add_parser("show", help="Show the resolved registry root and source.")
    _add_json_option(root_show)

    root_set = root_subcommands.add_parser("set-default", help="Set the machine-local default-root pointer.")
    _add_json_option(root_set)
    root_set.add_argument("path", help="Valid registry root to store in the default pointer.")

    root_clear = root_subcommands.add_parser("clear-default", help="Clear the machine-local default-root pointer.")
    _add_json_option(root_clear)

    validate = subcommands.add_parser("validate", help="Validate registry metadata and runtime policy.")
    _add_json_option(validate)

    tool_health = subcommands.add_parser("tool-health", help="Check runtime command/env availability.")
    _add_json_option(tool_health)
    tool_health.add_argument("--runtime", action="append", default=[], help="Runtime profile to check. Repeatable.")
    tool_health.add_argument("--skill", action="append", default=[], help="Skill ID filter. Repeatable.")
    tool_health.add_argument("--bin", action="append", default=[], help="Binary dependency filter or ad-hoc probe. Repeatable.")

    doctor = subcommands.add_parser("doctor", help="Run the human-facing combined registry health check.")
    _add_json_option(doctor)
    doctor.add_argument("--runtime", action="append", default=[], help="Runtime profile to check. Repeatable.")
    doctor.add_argument("--target", action="append", default=[], help="Target to include in deployment status. Repeatable.")
    doctor.add_argument("--skill", action="append", default=[], help="Skill ID filter. Repeatable.")
    doctor.add_argument("--bin", action="append", default=[], help="Binary dependency filter or ad-hoc probe. Repeatable.")

    discover_parser = subcommands.add_parser("discover", help="Inventory skill-like entries in configured targets.")
    _add_json_option(discover_parser)
    discover_parser.add_argument("--target", action="append", default=[], help="Target to discover. Repeatable.")

    status_parser = subcommands.add_parser("status", help="Check registry-managed deployment status.")
    _add_json_option(status_parser)
    status_parser.add_argument("--target", action="append", default=[], help="Target to check. Repeatable.")
    status_parser.add_argument("--skill", action="append", default=[], help="Skill ID filter. Repeatable.")

    deploy_parser = subcommands.add_parser("deploy", help="Plan or apply skill deployment.")
    _add_json_option(deploy_parser)
    deploy_parser.add_argument("--dry-run", action="store_true", help="Show planned operations without writing.")
    deploy_parser.add_argument(
        "--mode",
        choices=("target-default", "symlink", "copy"),
        default="target-default",
        help="Deployment mode. target-default uses each target's configured deploy_mode.",
    )
    deploy_target_group = deploy_parser.add_mutually_exclusive_group()
    deploy_target_group.add_argument("--all-targets", action="store_true", help="Plan for targets marked include_in_all.")
    deploy_target_group.add_argument("--target", action="append", default=[], help="Target to deploy to. Repeatable.")
    deploy_parser.add_argument("--skill", action="append", default=[], help="Skill ID filter. Repeatable.")
    deploy_parser.add_argument(
        "--skill-set",
        action="append",
        default=[],
        help="Deploy all members of a registry skill set. Repeatable.",
    )
    deploy_parser.add_argument(
        "--deployed-by",
        default="skill-registry",
        help="Actor recorded for deployment records in apply mode.",
    )
    deploy_parser.add_argument("--reason", default="", help="Human reason recorded in apply mode.")
    deploy_parser.add_argument(
        "--replace-managed",
        action="store_true",
        help="Repair or replace an existing target entry only when a valid deployment record proves registry ownership.",
    )
    deploy_parser.add_argument(
        "--replace-drifted-copy",
        action="store_true",
        help="Overwrite a drifted registry-managed copy from the canonical package.",
    )

    undeploy_parser = subcommands.add_parser("undeploy", help="Remove explicitly selected registry-managed local deployments.")
    _add_json_option(undeploy_parser)
    undeploy_parser.add_argument("--target", required=True, help="One registered local target.")
    undeploy_parser.add_argument("--skill", action="append", required=True, help="Explicit skill ID. Repeatable.")
    undeploy_parser.add_argument("--dry-run", action="store_true", help="Preview removal without writing.")
    undeploy_parser.add_argument("--reason", default="", help="Required audit reason in apply mode.")
    undeploy_parser.add_argument("--undeployed-by", default="skill-registry", help="Actor recorded in the local receipt.")

    export_parser = subcommands.add_parser("export", help="Export derived native target artifacts.")
    export_subcommands = export_parser.add_subparsers(dest="export_command")

    hermes_bundle = export_subcommands.add_parser(
        "hermes-bundle",
        help="Generate a thin global Hermes Skill Bundle YAML file from a registry skill set.",
    )
    _add_json_option(hermes_bundle)
    hermes_bundle.add_argument("--skill-set", required=True, help="Registry skill set to export.")
    hermes_bundle.add_argument("--target", required=True, help="Export target. v1 supports only hermes-global.")
    hermes_bundle.add_argument("--dry-run", action="store_true", help="Show export result without writing.")
    hermes_bundle.add_argument("--instruction", default="", help="Optional bundle-level Hermes instruction text.")
    hermes_bundle.add_argument("--reason", default="", help="Human reason included in export output.")
    hermes_bundle.add_argument(
        "--exported-by",
        default="skill-registry",
        help="Actor included in export output.",
    )

    reconcile_parser = subcommands.add_parser("reconcile", help="Plan or apply explicit reconciliation decisions.")
    reconcile_subcommands = reconcile_parser.add_subparsers(dest="reconcile_command")

    reconcile_plan = reconcile_subcommands.add_parser("plan", help="Explain reconciliation options without writing.")
    _add_json_option(reconcile_plan)
    reconcile_plan.add_argument("--target", required=True, help="Target to reconcile.")
    reconcile_plan.add_argument("--skill", required=True, help="Skill ID to reconcile.")
    reconcile_plan.add_argument(
        "--source-path",
        default="",
        help="Optional target-local source path to reconcile, relative to the target root or absolute under it.",
    )
    reconcile_plan.add_argument("--action", choices=ALL_ACTIONS, default="", help="Preview one explicit action.")

    reconcile_apply = reconcile_subcommands.add_parser("apply", help="Apply a safe reconciliation action.")
    _add_json_option(reconcile_apply)
    reconcile_apply.add_argument("--target", required=True, help="Target to reconcile.")
    reconcile_apply.add_argument("--skill", required=True, help="Skill ID to reconcile.")
    reconcile_apply.add_argument(
        "--source-path",
        default="",
        help="Optional target-local source path to reconcile, relative to the target root or absolute under it.",
    )
    reconcile_apply.add_argument(
        "--action",
        choices=tuple(action for action in ALL_ACTIONS if action in {"adopt", "local-only", "keep-blocked", "propose-import", "replace"}),
        required=True,
        help="Safe reconciliation action.",
    )
    reconcile_apply.add_argument("--reason", required=True, help="Human reason recorded in registry state.")
    reconcile_apply.add_argument(
        "--adopted-by",
        default="skill-registry",
        help="Actor recorded for adoption or replacement deployment records.",
    )

    proposal_parser = subcommands.add_parser("proposal", help="Inspect or apply reviewed proposal artifacts.")
    proposal_subcommands = proposal_parser.add_subparsers(dest="proposal_command")

    proposal_inspect = proposal_subcommands.add_parser("inspect", help="Inspect an import proposal without writing.")
    _add_json_option(proposal_inspect)
    proposal_inspect.add_argument("--proposal", required=True, help="Proposal directory or proposal.yaml path.")
    proposal_inspect.add_argument("--verify-source", action="store_true", help="Recompute source inventory and hash.")

    proposal_apply = proposal_subcommands.add_parser("apply", help="Apply a reviewed import proposal.")
    _add_json_option(proposal_apply)
    proposal_apply.add_argument("--proposal", required=True, help="Proposal directory or proposal.yaml path.")
    proposal_apply.add_argument("--approved-by", required=True, help="Reviewer approving this proposal.")
    proposal_apply.add_argument("--reason", required=True, help="Review reason recorded in the apply audit record.")

    guard_parser = subcommands.add_parser("guard", help="Check guardrail decisions for write-like operations.")
    guard_subcommands = guard_parser.add_subparsers(dest="guard_command")

    guard_check = guard_subcommands.add_parser("check", help="Classify a write-like operation against target roots.")
    _add_json_option(guard_check)
    guard_check.add_argument("--operation", choices=SUPPORTED_OPERATIONS, required=True, help="Write-like operation.")
    guard_check.add_argument("--path", required=True, help="Attempted write path.")
    guard_check.add_argument(
        "--mode",
        choices=GUARDRAIL_MODES,
        default="strict",
        help="Guardrail mode.",
    )

    return parser


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON.")


def _cmd_init(args: argparse.Namespace, stdout: TextIO) -> int:
    result = initialize_registry_instance(
        args.path,
        name=args.name,
        targets=tuple(args.target),
        set_default=args.set_default,
    )
    if args.json:
        _dump_json(
            {
                "ok": True,
                "root": str(result.root),
                "name": result.name,
                "engine_distribution": result.engine_distribution,
                "engine_version": result.engine_version,
                "targets": list(result.targets),
                "default_set": result.default_set,
                "warnings": list(result.warnings),
                "next_steps": list(result.next_steps),
            },
            stdout,
        )
        return 0

    print("Initialized registry instance.", file=stdout)
    print(f"root: {result.root}", file=stdout)
    print(f"name: {result.name}", file=stdout)
    print(f"engine: {result.engine_distribution}=={result.engine_version}", file=stdout)
    print(f"targets: {', '.join(result.targets) if result.targets else '(none)'}", file=stdout)
    print(f"default root set: {'yes' if result.default_set else 'no'}", file=stdout)
    print("validation: passed", file=stdout)
    if result.warnings:
        print("warnings:", file=stdout)
        for warning in result.warnings:
            print(f"  - {warning}", file=stdout)
    print("next steps:", file=stdout)
    for step in result.next_steps:
        print(f"  {step}", file=stdout)
    return 0


def _cmd_root(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.root_command == "show":
        resolved = resolve_registry_root(args.root)
        if args.json:
            _dump_json({"ok": True, "root": str(resolved.path), "source": resolved.source}, stdout)
        else:
            print(f"root: {resolved.path}", file=stdout)
            print(f"source: {resolved.source}", file=stdout)
        return 0

    if args.root_command == "set-default":
        result = set_default_registry_root(args.path)
        payload = {
            "ok": True,
            "pointer": str(result.pointer),
            "changed": result.changed,
            "root": str(result.root),
            "source": "default-pointer",
        }
        if args.json:
            _dump_json(payload, stdout)
        else:
            print(f"pointer: {result.pointer}", file=stdout)
            print(f"root: {result.root}", file=stdout)
            print(f"changed: {'yes' if result.changed else 'no'}", file=stdout)
        return 0

    if args.root_command == "clear-default":
        result = clear_default_registry_root()
        payload = {
            "ok": True,
            "pointer": str(result.pointer),
            "changed": result.changed,
            "root": None,
            "source": "default-pointer",
        }
        if args.json:
            _dump_json(payload, stdout)
        else:
            print(f"pointer: {result.pointer}", file=stdout)
            print(f"changed: {'yes' if result.changed else 'no'}", file=stdout)
        return 0

    raise RegistryRootError("A root subcommand is required: show, set-default, or clear-default.")


def _cmd_validate(config, args: argparse.Namespace, stdout: TextIO) -> int:
    result = validate_registry(config)
    if args.json:
        _dump_json(
            {
                "ok": result.ok,
                "diagnostics": [asdict(item) for item in result.diagnostics],
            },
            stdout,
        )
    else:
        _print_validation(result, stdout)
    return 0 if result.ok else 1


def _cmd_tool_health(config, args: argparse.Namespace, stdout: TextIO) -> int:
    validation = validate_registry(config)
    filter_errors = _runtime_filter_errors(config, args.runtime)
    if validation.errors:
        if args.json:
            _dump_json({"ok": False, "validation": [asdict(item) for item in validation.diagnostics], "results": []}, stdout)
        else:
            _print_validation(validation, stdout)
        return 3
    if filter_errors:
        if args.json:
            _dump_json({"ok": False, "errors": filter_errors, "results": []}, stdout)
        else:
            for error in filter_errors:
                print(f"error: {error}", file=stdout)
        return 3

    filters = ToolHealthFilters(tuple(args.runtime), tuple(args.skill), tuple(args.bin))
    results = check_tool_health(config, filters)
    ok = health_ok(results)

    if args.json:
        _dump_json({"ok": ok, "results": [asdict(item) for item in results]}, stdout)
    else:
        _print_tool_health(results, stdout)
    return 0 if ok else 6


def _cmd_doctor(config, args: argparse.Namespace, stdout: TextIO) -> int:
    validation = validate_registry(config)
    filter_errors = _runtime_filter_errors(config, args.runtime)
    filter_errors.extend(target_filter_errors(config, tuple(args.target)))
    filter_errors.extend(_skill_filter_errors(config, tuple(args.skill), include_local=True))
    filters = ToolHealthFilters(tuple(args.runtime), tuple(args.skill), tuple(args.bin))
    results = () if validation.errors or filter_errors else check_tool_health(config, filters)
    deployment_statuses = () if validation.errors or filter_errors else check_status(config, tuple(args.target), tuple(args.skill))
    ok = validation.ok and not filter_errors and health_ok(results) and status_ok(deployment_statuses)

    if args.json:
        _dump_json(
            {
                "ok": ok,
                "validation": {
                    "ok": validation.ok,
                    "diagnostics": [asdict(item) for item in validation.diagnostics],
                },
                "tool_health": {
                    "ok": not filter_errors and health_ok(results),
                    "results": [asdict(item) for item in results],
                },
                "deployment_status": {
                    "ok": status_ok(deployment_statuses),
                    "results": [asdict(item) for item in deployment_statuses],
                },
                "errors": filter_errors,
            },
            stdout,
        )
    else:
        _print_doctor(validation, results, deployment_statuses, filter_errors, stdout)

    if not validation.ok:
        return 3
    if filter_errors:
        return 3
    if not health_ok(results):
        return 6
    return 0 if status_ok(deployment_statuses) else 1


def _cmd_discover(config, args: argparse.Namespace, stdout: TextIO) -> int:
    filter_errors = target_filter_errors(config, tuple(args.target))
    if filter_errors:
        if args.json:
            _dump_json({"ok": False, "errors": filter_errors, "entries": []}, stdout)
        else:
            for error in filter_errors:
                print(f"error: {error}", file=stdout)
        return 3

    entries = discover(config, tuple(args.target))
    if args.json:
        _dump_json({"ok": True, "entries": [asdict(item) for item in entries]}, stdout)
    else:
        _print_discovery(entries, stdout)
    return 0


def _cmd_status(config, args: argparse.Namespace, stdout: TextIO) -> int:
    filter_errors = target_filter_errors(config, tuple(args.target))
    filter_errors.extend(_skill_filter_errors(config, tuple(args.skill), include_local=True))
    if filter_errors:
        if args.json:
            _dump_json({"ok": False, "errors": filter_errors, "results": []}, stdout)
        else:
            for error in filter_errors:
                print(f"error: {error}", file=stdout)
        return 3

    results = check_status(config, tuple(args.target), tuple(args.skill))
    ok = status_ok(results)
    if args.json:
        _dump_json({"ok": ok, "results": [asdict(item) for item in results]}, stdout)
    else:
        _print_status(results, stdout)
    return 0 if ok else 1


def _cmd_deploy(config, args: argparse.Namespace, stdout: TextIO) -> int:
    skill_ids, skill_set_errors = _expanded_skill_filters(config, tuple(args.skill), tuple(args.skill_set))
    target_names, target_selection_errors = _deploy_target_selection(config, args)
    filter_errors = target_filter_errors(config, target_names)
    filter_errors.extend(_skill_filter_errors(config, skill_ids))
    filter_errors.extend(skill_set_errors)
    filter_errors.extend(target_selection_errors)
    if filter_errors:
        if args.json:
            _dump_json({"ok": False, "errors": filter_errors, "operations": []}, stdout)
        else:
            for error in filter_errors:
                print(f"error: {error}", file=stdout)
        return 3

    if not args.dry_run:
        if not args.reason.strip():
            message = "deploy apply requires --reason"
            if args.json:
                _dump_json({"ok": False, "dry_run": False, "error": message, "operations": []}, stdout)
            else:
                print(f"error: {message}", file=stdout)
            return 2
        try:
            operations = apply_deploy(
                config,
                mode=args.mode,
                all_targets=bool(args.all_targets),
                target_names=target_names,
                skill_ids=skill_ids,
                deployed_by=args.deployed_by,
                reason=args.reason,
                replace_managed=bool(args.replace_managed),
                replace_drifted_copy=bool(args.replace_drifted_copy),
            )
        except ValueError as exc:
            if args.json:
                _dump_json({"ok": False, "dry_run": False, "error": str(exc), "operations": []}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 1

        if args.json:
            _dump_json({"ok": True, "dry_run": False, "operations": [asdict(item) for item in operations]}, stdout)
        else:
            _print_deploy_apply(operations, stdout)
        return 0

    operations = plan_deploy(
        config,
        mode=args.mode,
        all_targets=bool(args.all_targets),
        target_names=target_names,
        skill_ids=skill_ids,
        replace_managed=bool(args.replace_managed),
        replace_drifted_copy=bool(args.replace_drifted_copy),
    )
    ok = plan_ok(operations)
    if args.json:
        _dump_json({"ok": ok, "dry_run": True, "operations": [asdict(item) for item in operations]}, stdout)
    else:
        _print_deploy_plan(operations, stdout)
    return 0 if ok else 1


def _cmd_undeploy(config, args: argparse.Namespace, stdout: TextIO) -> int:
    errors = target_filter_errors(config, (args.target,))
    errors.extend(f"Unsafe skill ID: {skill}" for skill in args.skill if not safe_skill_id(skill))
    known = known_skill_ids(config)
    errors.extend(f"Unknown skill: {skill}" for skill in args.skill if safe_skill_id(skill) and skill not in known)
    if errors:
        _dump_json({"ok": False, "errors": errors, "operations": []}, stdout) if args.json else print("error: " + "; ".join(errors), file=stdout)
        return 3
    if not args.dry_run and not args.reason.strip():
        message = "undeploy apply requires --reason"
        _dump_json({"ok": False, "error": message, "operations": []}, stdout) if args.json else print(f"error: {message}", file=stdout)
        return 2
    try:
        if args.dry_run:
            operations = plan_undeploy(config, target_name=args.target, skill_ids=tuple(args.skill))
        else:
            operations = apply_undeploy(config, target_name=args.target, skill_ids=tuple(args.skill),
                                       reason=args.reason, undeployed_by=args.undeployed_by)
        ok = all(item.ok for item in operations)
        if args.json:
            _dump_json({"ok": ok, "dry_run": args.dry_run, "operations": [asdict(item) for item in operations]}, stdout)
        else:
            print("Undeploy dry-run:" if args.dry_run else "Undeploy apply:", file=stdout)
            for item in operations:
                print(f"  {item.operation} {item.skill_id} -> {item.target}: {item.reason}", file=stdout)
                print(f"    target: {item.target_path}", file=stdout)
                if item.backup_path:
                    print(f"    backup: {item.backup_path}", file=stdout)
                    print(f"    receipt: {item.receipt_path}", file=stdout)
        return 0 if ok else 1
    except (OSError, ValueError, RuntimeError) as exc:
        completed = exc.completed if isinstance(exc, UndeployError) else ()
        if args.json:
            _dump_json({"ok": False, "dry_run": args.dry_run, "error": str(exc),
                        "operations": [asdict(item) for item in completed]}, stdout)
        else:
            print(f"error: {exc}", file=stdout)
            for item in completed:
                print(f"  completed: {item.operation} {item.skill_id} -> {item.target}", file=stdout)
        return 1


def _cmd_export(config, args: argparse.Namespace, stdout: TextIO) -> int:
    if args.export_command != "hermes-bundle":
        message = "export requires a subcommand"
        if args.json:
            _dump_json({"ok": False, "error": message}, stdout)
        else:
            print(f"error: {message}", file=stdout)
        return 2

    result = export_hermes_bundle(
        config,
        skill_set_id=args.skill_set,
        target_name=args.target,
        dry_run=bool(args.dry_run),
        instruction=args.instruction,
        reason=args.reason,
        exported_by=args.exported_by,
    )
    if args.json:
        _dump_json(_hermes_bundle_export_payload(result), stdout)
    else:
        _print_hermes_bundle_export(result, stdout)
    return 0 if result.ok else 1


def _cmd_reconcile(config, args: argparse.Namespace, stdout: TextIO) -> int:
    if args.reconcile_command is None:
        print("error: reconcile requires a subcommand: plan or apply", file=stdout)
        return 2

    filter_errors = target_filter_errors(config, (args.target,))
    filter_errors.extend(_skill_filter_errors(config, (args.skill,)))
    if filter_errors:
        if args.json:
            _dump_json({"ok": False, "errors": filter_errors}, stdout)
        else:
            for error in filter_errors:
                print(f"error: {error}", file=stdout)
        return 3

    if args.reconcile_command == "plan":
        try:
            plan = plan_reconciliation(
                config,
                target_name=args.target,
                skill_id=args.skill,
                action=args.action,
                source_path=args.source_path,
            )
        except ValueError as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 1
        if args.json:
            _dump_json({"ok": True, "plan": asdict(plan)}, stdout)
        else:
            _print_reconcile_plan(plan, stdout)
        return 0

    if args.reconcile_command == "apply":
        try:
            result = apply_reconciliation(
                config,
                target_name=args.target,
                skill_id=args.skill,
                action=args.action,
                reason=args.reason,
                adopted_by=args.adopted_by,
                source_path=args.source_path,
            )
        except ValueError as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 1

        if args.json:
            _dump_json({"ok": True, "result": asdict(result)}, stdout)
        else:
            _print_reconcile_apply(result, stdout)
        return 0

    return 2


def _cmd_proposal(config, args: argparse.Namespace, stdout: TextIO) -> int:
    if args.proposal_command is None:
        print("error: proposal requires a subcommand: inspect or apply", file=stdout)
        return 2

    if args.proposal_command == "inspect":
        try:
            inspection = inspect_import_proposal(
                config,
                args.proposal,
                verify_source=args.verify_source,
            )
        except ProposalError as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 1

        ok = not inspection.verification or inspection.verification.ok
        if args.json:
            _dump_json({"ok": ok, "proposal": asdict(inspection)}, stdout)
        else:
            _print_proposal_inspection(inspection, stdout)
        return 0 if ok else 1

    if args.proposal_command == "apply":
        try:
            result = apply_import_proposal(
                config,
                args.proposal,
                approved_by=args.approved_by,
                reason=args.reason,
            )
        except ProposalError as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 1

        if args.json:
            _dump_json({"ok": True, "result": asdict(result)}, stdout)
        else:
            _print_proposal_apply(result, stdout)
        return 0

    return 2


def _cmd_guard(config, args: argparse.Namespace, stdout: TextIO) -> int:
    if args.guard_command is None:
        message = "guard requires a subcommand: check"
        if args.json:
            _dump_json({"ok": False, "error": message}, stdout)
        else:
            print(f"error: {message}", file=stdout)
        return 2

    if args.guard_command == "check":
        try:
            decision = check_guardrail(
                config,
                operation=args.operation,
                path=args.path,
                mode=args.mode,
            )
        except (ConfigError, GuardrailConfigError) as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 3
        except ValueError as exc:
            if args.json:
                _dump_json({"ok": False, "error": str(exc)}, stdout)
            else:
                print(f"error: {exc}", file=stdout)
            return 2

        if args.json:
            _dump_json(asdict(decision), stdout)
        else:
            _print_guardrail_decision(decision, stdout)
        return 5 if decision.decision == "block" else 0

    return 2


def _print_validation(result, stdout: TextIO) -> None:
    status = "ok" if result.ok else "failed"
    print(f"Registry validation: {status}", file=stdout)
    if not result.diagnostics:
        print("  no diagnostics", file=stdout)
        return
    for item in result.diagnostics:
        print(f"  [{item.severity}] {item.code}: {item.message}", file=stdout)


def _print_tool_health(results, stdout: TextIO) -> None:
    if not results:
        print("Tool health: ok", file=stdout)
        print("  no runtime dependencies selected", file=stdout)
        return

    print("Tool health:", file=stdout)
    for result in results:
        target = result.resolved_path or "-"
        print(
            f"  {result.runtime_profile} {result.kind}:{result.dependency_name} "
            f"{result.status} path={target}",
            file=stdout,
        )
        if result.version_output:
            print(f"    output: {result.version_output}", file=stdout)


def _print_doctor(validation, results, deployment_statuses, filter_errors: list[str], stdout: TextIO) -> None:
    print("Skill Registry Doctor", file=stdout)
    print(f"  validation: {'ok' if validation.ok else 'failed'}", file=stdout)
    print(f"  deployment status: {'ok' if status_ok(deployment_statuses) else 'failed'}", file=stdout)
    print(f"  tool health: {'ok' if not filter_errors and health_ok(results) else 'failed'}", file=stdout)

    if validation.diagnostics:
        print("", file=stdout)
        _print_validation(validation, stdout)
    if filter_errors:
        print("", file=stdout)
        for error in filter_errors:
            print(f"error: {error}", file=stdout)
    if results:
        print("", file=stdout)
        _print_tool_health(results, stdout)
    if deployment_statuses:
        print("", file=stdout)
        _print_status(deployment_statuses, stdout)


def _print_discovery(entries, stdout: TextIO) -> None:
    if not entries:
        print("Discovery: no skill-like target entries found", file=stdout)
        return
    print("Discovery:", file=stdout)
    for entry in entries:
        print(
            f"  {entry.target} {entry.classification} {entry.skill_id or '-'} path={entry.path}",
            file=stdout,
        )


def _print_status(results, stdout: TextIO) -> None:
    if not results:
        print("Deployment status: no registry skills selected", file=stdout)
        return
    print("Deployment status:", file=stdout)
    for result in results:
        print(
            f"  {result.target} {result.skill_id} {result.status} path={result.target_path}",
            file=stdout,
        )
        print(f"    reason: {result.reason}", file=stdout)


def _print_deploy_plan(operations, stdout: TextIO) -> None:
    if not operations:
        print("Deploy dry-run: no operations planned", file=stdout)
        return
    print("Deploy dry-run:", file=stdout)
    for operation in operations:
        print(
            f"  {operation.operation} {operation.skill_id} -> {operation.target} "
            f"mode={operation.mode}",
            file=stdout,
        )
        print(f"    source: {operation.source_path}", file=stdout)
        print(f"    target: {operation.target_path}", file=stdout)
        print(f"    reason: {operation.reason}", file=stdout)


def _print_deploy_apply(operations, stdout: TextIO) -> None:
    if not operations:
        print("Deploy apply: no operations planned", file=stdout)
        return
    print("Deploy apply:", file=stdout)
    for operation in operations:
        status = "applied" if operation.applied else "skipped"
        print(
            f"  {status} {operation.operation} {operation.skill_id} -> {operation.target} "
            f"mode={operation.mode}",
            file=stdout,
        )
        print(f"    source: {operation.source_path}", file=stdout)
        print(f"    target: {operation.target_path}", file=stdout)
        print(f"    reason: {operation.reason}", file=stdout)
        if operation.record_path:
            print(f"    record: {operation.record_path}", file=stdout)
        if operation.backup_path:
            print(f"    backup: {operation.backup_path}", file=stdout)


def _print_hermes_bundle_export(result: HermesBundleExportResult, stdout: TextIO) -> None:
    header = "Hermes bundle export:" if result.ok else "Hermes bundle export blocked:"
    print(header, file=stdout)
    print(f"  operation: {result.operation}", file=stdout)
    if result.bundle_path:
        print(f"  bundle path: {result.bundle_path}", file=stdout)
    print(f"  skill set: {result.skill_set_id}", file=stdout)
    print(f"  target: {result.target}", file=stdout)
    if result.skills:
        print(f"  skills: {', '.join(result.skills)}", file=stdout)
    if result.yaml_sha256:
        print(f"  yaml sha256: {result.yaml_sha256}", file=stdout)
    if result.exported_by:
        print(f"  exported by: {result.exported_by}", file=stdout)
    if result.reason:
        print(f"  reason: {result.reason}", file=stdout)


def _print_reconcile_plan(plan, stdout: TextIO) -> None:
    print("Reconcile plan:", file=stdout)
    print(f"  target: {plan.target}", file=stdout)
    print(f"  skill: {plan.skill_id}", file=stdout)
    print(f"  current: {plan.current_classification}", file=stdout)
    print(f"  selected action: {plan.selected_action}", file=stdout)
    print(f"  recommended action: {plan.recommended_action}", file=stdout)
    print(f"  can apply now: {'yes' if plan.can_apply else 'no'}", file=stdout)
    print(f"  mutates target files: {'yes' if plan.mutates_target_files else 'no'}", file=stdout)
    print(f"  target path: {plan.target_path}", file=stdout)
    print(f"  reason: {plan.reason}", file=stdout)
    print(f"  available actions: {', '.join(plan.available_actions)}", file=stdout)


def _print_reconcile_apply(result, stdout: TextIO) -> None:
    print("Reconcile apply:", file=stdout)
    print(f"  target: {result.target}", file=stdout)
    print(f"  skill: {result.skill_id}", file=stdout)
    print(f"  action: {result.action}", file=stdout)
    print(f"  classification: {result.classification}", file=stdout)
    if result.record_path:
        print(f"  record: {result.record_path}", file=stdout)
    if result.proposal_path:
        print(f"  proposal: {result.proposal_path}", file=stdout)
    if result.source_path:
        print(f"  source path: {result.source_path}", file=stdout)
    if result.payload_hash:
        print(f"  payload hash: {result.payload_hash}", file=stdout)
    if result.file_count:
        print(f"  files: {result.file_count}", file=stdout)
    if result.backup_path:
        print(f"  backup: {result.backup_path}", file=stdout)
    print(f"  target path: {result.target_path}", file=stdout)
    print(f"  mutates target files: {'yes' if result.mutates_target_files else 'no'}", file=stdout)
    print(f"  reason: {result.reason}", file=stdout)


def _print_proposal_inspection(inspection, stdout: TextIO) -> None:
    print("Proposal inspect:", file=stdout)
    print(f"  proposal: {inspection.id}", file=stdout)
    print(f"  status: {inspection.status}", file=stdout)
    print(f"  type: {inspection.type}", file=stdout)
    print(f"  skill: {inspection.skill_id}", file=stdout)
    print(f"  source target: {inspection.source_target}", file=stdout)
    print(f"  source path: {inspection.source_path}", file=stdout)
    print(f"  canonical path: {inspection.canonical_path}", file=stdout)
    print(f"  package hash: {inspection.package_hash}", file=stdout)
    print(f"  files: {inspection.file_count}", file=stdout)
    print(f"  mutates_target_files: {'yes' if inspection.mutates_target_files else 'no'}", file=stdout)
    if inspection.verification:
        status = "ok" if inspection.verification.ok else "failed"
        print(f"  source verification: {status}", file=stdout)
        if inspection.verification.package_hash:
            print(f"    source package hash: {inspection.verification.package_hash}", file=stdout)
        for error in inspection.verification.errors:
            print(f"    error: {error}", file=stdout)


def _print_proposal_apply(result, stdout: TextIO) -> None:
    print("Proposal apply:", file=stdout)
    print(f"  proposal: {result.proposal_id}", file=stdout)
    print(f"  skill: {result.skill_id}", file=stdout)
    print(f"  source target: {result.source_target}", file=stdout)
    print(f"  source path: {result.source_path}", file=stdout)
    print(f"  canonical path: {result.canonical_path}", file=stdout)
    print(f"  approved by: {result.approved_by}", file=stdout)
    print(f"  reason: {result.reason}", file=stdout)
    print(f"  applied at: {result.applied_at}", file=stdout)
    print(f"  package hash: {result.package_hash}", file=stdout)
    print(f"  backup: {result.backup_path}", file=stdout)
    print(f"  apply record: {result.apply_record_path}", file=stdout)
    print(f"  copied files: {len(result.copied_files)}", file=stdout)
    print(f"  skipped control files: {', '.join(result.skipped_control_files) or '-'}", file=stdout)
    print(f"  mutates_target_files: {'yes' if result.mutates_target_files else 'no'}", file=stdout)


def _print_guardrail_decision(decision, stdout: TextIO) -> None:
    label = {"allow": "Allowed", "warn": "Warning", "block": "Blocked"}.get(decision.decision, decision.decision)
    print(f"{label}: {decision.reason}", file=stdout)
    print(f"  operation: {decision.operation}", file=stdout)
    print(f"  mode: {decision.mode}", file=stdout)
    print(f"  path: {decision.path}", file=stdout)
    print(f"  resolved path: {decision.resolved_path}", file=stdout)
    if decision.target:
        print(f"  target: {decision.target}", file=stdout)
    if decision.skill_id:
        print(f"  skill: {decision.skill_id}", file=stdout)
    if decision.target_path:
        print(f"  target path: {decision.target_path}", file=stdout)
    if decision.canonical_path:
        print(f"  canonical path: {decision.canonical_path}", file=stdout)
    if decision.deployment_mode:
        print(f"  deployment mode: {decision.deployment_mode}", file=stdout)
    print(f"  recommended action: {decision.recommended_action}", file=stdout)


def _dump_json(payload: dict, stdout: TextIO) -> None:
    json.dump(payload, stdout, indent=2, sort_keys=True)
    print(file=stdout)


def _hermes_bundle_export_payload(result: HermesBundleExportResult) -> dict:
    payload = asdict(result)
    payload["skills"] = list(result.skills)
    return payload


def _print_error(message: str, as_json: bool) -> None:
    if as_json:
        json.dump({"ok": False, "error": message}, sys.stdout, indent=2, sort_keys=True)
        print()
        return
    print(f"error: {message}", file=sys.stderr)


def _deploy_target_selection(config, args: argparse.Namespace) -> tuple[tuple[str, ...], list[str]]:
    if args.all_targets:
        return (), []

    explicit_targets = tuple(args.target or ())
    if explicit_targets:
        return explicit_targets, []

    if args.skill_set:
        targets: list[str] = []
        errors: list[str] = []
        by_id = config.skill_set_by_id
        for skill_set_id in args.skill_set:
            skill_set = by_id.get(skill_set_id)
            if skill_set is None:
                continue
            targets.extend(skill_set.default_targets)
            if not skill_set.default_targets:
                errors.append(f"Skill set {skill_set_id} has no default targets; pass --target or --all-targets")
        return _dedupe(targets), errors

    return (), ["deploy requires --target, --all-targets, or a --skill-set with default_targets"]


def _expanded_skill_filters(
    config,
    skill_filters: tuple[str, ...],
    skill_set_filters: tuple[str, ...],
) -> tuple[tuple[str, ...], list[str]]:
    skill_ids = list(skill_filters)
    errors: list[str] = []
    by_id = config.skill_set_by_id
    for skill_set_id in skill_set_filters:
        skill_set = by_id.get(skill_set_id)
        if skill_set is None:
            errors.append(f"Unknown skill set: {skill_set_id}")
            continue
        skill_ids.extend(skill_set.members)
    return _dedupe(skill_ids), errors


def _dedupe(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _runtime_filter_errors(config, runtime_filters: list[str]) -> list[str]:
    known = config.runtime_by_name
    return [f"Unknown runtime profile: {name}" for name in runtime_filters if name not in known]


def _skill_filter_errors(config, skill_filters: tuple[str, ...], *, include_local: bool = False) -> list[str]:
    known = known_skill_ids(config) if include_local else {skill.id for skill in list_registry_skills(config)}
    return [f"Unknown skill: {skill_id}" for skill_id in skill_filters if skill_id not in known]
