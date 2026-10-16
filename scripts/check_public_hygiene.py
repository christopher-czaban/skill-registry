#!/usr/bin/env python3
"""Fail closed when public source or distributions contain unsafe content."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


EXPECTED_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        ".gitignore",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docs/ARCHITECTURE.md",
        "docs/RELEASING.md",
        "docs/UNDEPLOY-AND-RETIREMENT.md",
        "pyproject.toml",
        "scripts/check_public_hygiene.py",
        "src/skill_registry/__init__.py",
        "src/skill_registry/__main__.py",
        "src/skill_registry/bootstrap.py",
        "src/skill_registry/cli.py",
        "src/skill_registry/config.py",
        "src/skill_registry/deploy.py",
        "src/skill_registry/discovery.py",
        "src/skill_registry/guardrail.py",
        "src/skill_registry/hashing.py",
        "src/skill_registry/health.py",
        "src/skill_registry/hermes_bundle_export.py",
        "src/skill_registry/proposals.py",
        "src/skill_registry/reconcile.py",
        "src/skill_registry/root.py",
        "src/skill_registry/skills.py",
        "src/skill_registry/state.py",
        "src/skill_registry/status.py",
        "src/skill_registry/targets.py",
        "src/skill_registry/undeploy.py",
        "src/skill_registry/templates/registry-instance/.gitignore",
        "src/skill_registry/templates/registry-instance/pyproject.toml",
        "src/skill_registry/templates/registry-instance/registry.yaml",
        "src/skill_registry/templates/registry-instance/runtime-dependencies.yaml",
        "src/skill_registry/templates/registry-instance/runtimes.yaml",
        "src/skill_registry/templates/registry-instance/skill-sets.yaml",
        "src/skill_registry/validation.py",
        "tests/test_hermes_bundle_export.py",
        "tests/test_instance_bootstrap.py",
        "tests/test_local_target_overrides.py",
        "tests/test_origin_metadata.py",
        "tests/test_package_artifacts.py",
        "tests/test_public_hygiene.py",
        "tests/test_registry_root.py",
        "tests/test_runtime_health.py",
        "tests/test_undeploy.py",
        "uv.lock",
    }
)

EXPECTED_DIRECTORIES = frozenset(
    parent.as_posix()
    for relative in EXPECTED_FILES
    for parent in PurePosixPath(relative).parents
    if parent != PurePosixPath(".")
)
EXPECTED_ENTRIES = EXPECTED_FILES | EXPECTED_DIRECTORIES

FORBIDDEN_TOP_LEVEL = frozenset(
    {
        ".serena",
        ".skill-registry",
        "archive",
        "proposals",
        "skills",
        "targets",
        "targets.local",
        "vendor-intake",
    }
)

INVALID_RESOURCE_NAME = "skillsets" + ".yaml"
PRIVATE_CHECKOUT_TERMS = (
    "Local" + "_Project_Files",
    "skill-registry-public-" + "core-split",
    "skill-registry-" + "engine",
)

TEXT_RULES = (
    (
        "absolute-posix-home",
        re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\s\"'<>]+/"),
        "replace the absolute user home with a temporary path or portable placeholder",
    ),
    (
        "absolute-windows-profile",
        re.compile(r"(?i)(?:[A-Z]:[\\/]|/mnt/[a-z]/)Users[\\/][^\\/\s\"'<>]+[\\/]"),
        "replace the Windows profile with a temporary path or documentation placeholder",
    ),
    (
        "private-checkout-root",
        re.compile("(?:" + "|".join(re.escape(term) for term in PRIVATE_CHECKOUT_TERMS) + ")"),
        "remove local checkout provenance from public content",
    ),
    (
        "private-key-marker",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        "remove the key material and rotate the exposed credential",
    ),
    (
        "credential-token",
        re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})"),
        "remove and rotate the credential",
    ),
    (
        "email-address",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        "remove personal contact data or use a repository-native reporting channel",
    ),
    (
        "forbidden-private-skill",
        re.compile(r"\bskill-registry-(?:migration|deployment)\b"),
        "keep instance-owned authoring skills outside the public engine",
    ),
    (
        "invalid-skill-set-resource",
        re.compile(r"\b" + re.escape(INVALID_RESOURCE_NAME) + r"\b"),
        "use the exact packaged resource name skill-sets.yaml",
    ),
)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    remediation: str
    line: int | None = None

    def render(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"ERROR [{self.rule}] {location}: unsafe public content; remediation: {self.remediation}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Public repository root to inspect.")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="Wheel or sdist to inspect. Repeatable.",
    )
    parser.add_argument(
        "--require-no-git",
        action="store_true",
        help="Fail if the staging root contains any .git entry.",
    )
    return parser.parse_args(argv)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _filesystem_entries(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        directory_names[:] = sorted(directory_names)
        entries.extend(current_path / name for name in directory_names)
        entries.extend(current_path / name for name in sorted(file_names))
    return tuple(entries)


def _git_tracked_files(root: Path) -> tuple[tuple[Path, ...], list[Finding]]:
    command = ["git", "-C", str(root), "ls-files", "-z", "--"]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError:
        return (), [Finding("git-inventory-failed", ".git", "install Git and rerun the tracked-file hygiene scan")]
    if result.returncode != 0:
        return (), [
            Finding(
                "git-inventory-failed",
                ".git",
                "repair the repository so git ls-files can produce the reviewed inventory",
            )
        ]

    files: list[Path] = []
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            return (), [
                Finding(
                    "invalid-tracked-path",
                    ".git",
                    "rename tracked paths to valid UTF-8 before public review",
                )
            ]
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or pure == PurePosixPath("."):
            return (), [
                Finding(
                    "invalid-tracked-path",
                    ".git",
                    "repair the tracked inventory so every path is repository-relative",
                )
            ]
        files.append(root.joinpath(*pure.parts))
    return tuple(files), []


def collect_public_files(root: Path) -> tuple[Path, ...]:
    if os.path.lexists(root / ".git"):
        files, findings = _git_tracked_files(root)
        if findings:
            return ()
        return files

    files: list[Path] = []
    for path in _filesystem_entries(root):
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            files.append(path)
    return tuple(files)


def scan_text(display_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    control_lines: set[int] = set()
    line_number = 1
    for character in text:
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r":
            control_lines.add(line_number)
        if character == "\n":
            line_number += 1
    for control_line in sorted(control_lines):
        findings.append(
            Finding(
                "disallowed-control-byte",
                display_path,
                "replace the binary/control payload with reviewed UTF-8 text",
                control_line,
            )
        )
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, remediation in TEXT_RULES:
            if pattern.search(line):
                findings.append(Finding(rule, display_path, remediation, line_number))
    return findings


def scan_source(root: Path, *, require_no_git: bool) -> tuple[list[Finding], int, int]:
    findings: list[Finding] = []
    if not root.is_dir():
        return [Finding("missing-root", str(root), "provide the public repository directory")], 0, 0

    git_entry = root / ".git"
    git_backed = os.path.lexists(git_entry)
    if require_no_git and git_backed:
        findings.append(Finding("git-history", ".git", "remove Git metadata from the pre-promotion staging tree"))
        return findings, 0, 0

    if git_backed:
        files, inventory_findings = _git_tracked_files(root)
        findings.extend(inventory_findings)
        if inventory_findings:
            return findings, 0, 0
        relative_files = {path.relative_to(root).as_posix() for path in files}
    else:
        try:
            entries = _filesystem_entries(root)
        except OSError:
            return [Finding("unreadable-root", str(root), "fix access before public review")], 0, 0
        relative_entries: set[str] = set()
        files_list: list[Path] = []
        for path in entries:
            relative = path.relative_to(root).as_posix()
            relative_entries.add(relative)
            try:
                metadata = path.lstat()
            except OSError:
                findings.append(Finding("unreadable-entry", relative, "fix access or remove the entry"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                findings.append(Finding("symlink", relative, "replace the link with a reviewed filesystem entry"))
            if not stat.S_ISDIR(metadata.st_mode):
                files_list.append(path)
        files = tuple(files_list)
        relative_files = {path.relative_to(root).as_posix() for path in files}
        for missing in sorted(EXPECTED_DIRECTORIES - relative_entries):
            findings.append(Finding("missing-allowlisted-entry", missing, "restore the reviewed public entry"))
        for unexpected in sorted(relative_entries - EXPECTED_ENTRIES):
            path = root / unexpected
            try:
                is_directory = stat.S_ISDIR(path.lstat().st_mode)
            except OSError:
                is_directory = False
            rule = "unexpected-directory" if is_directory else "unexpected-file"
            findings.append(Finding(rule, unexpected, "remove it or review and add it to the explicit allowlist"))

    for missing in sorted(EXPECTED_FILES - relative_files):
        findings.append(Finding("missing-allowlisted-file", missing, "restore the reviewed public file"))
    if git_backed:
        for unexpected in sorted(relative_files - EXPECTED_FILES):
            findings.append(
                Finding("unexpected-file", unexpected, "remove it or review and add it to the explicit allowlist")
            )

    text_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError:
            findings.append(Finding("unreadable-entry", relative, "fix access or remove the unreviewable entry"))
            continue
        if stat.S_ISLNK(metadata.st_mode):
            findings.append(Finding("symlink", relative, "replace the link with a reviewed regular file"))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            findings.append(Finding("non-regular-file", relative, "keep only reviewed regular files"))
            continue
        if metadata.st_nlink != 1:
            findings.append(Finding("hardlink", relative, "replace the hardlink with an independent file copy"))
        try:
            payload = path.read_bytes()
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("unexpected-binary", relative, "remove or explicitly redesign the public artifact policy"))
            continue
        except OSError:
            findings.append(Finding("unreadable-file", relative, "fix access or remove the unreviewable file"))
            continue
        text_bytes += len(payload)
        findings.extend(scan_text(relative, text))
    return findings, len(files), text_bytes


def _safe_member_name(archive: Path, name: str) -> list[Finding]:
    display = f"{archive.name}!{name}"
    pure = PurePosixPath(name)
    findings: list[Finding] = []
    if pure.is_absolute() or ".." in pure.parts:
        findings.append(Finding("archive-path-escape", display, "rebuild with normalized relative member names"))
    if ".git" in pure.parts:
        findings.append(Finding("git-history", display, "remove Git metadata from the distribution"))
    if any(part in FORBIDDEN_TOP_LEVEL for part in pure.parts):
        findings.append(Finding("forbidden-archive-path", display, "remove instance-owned content from the distribution"))
    if INVALID_RESOURCE_NAME in pure.parts:
        findings.append(Finding("invalid-skill-set-resource", display, "use exact skill-sets.yaml"))
    return findings


def _scan_archive_payload(archive: Path, name: str, payload: bytes) -> list[Finding]:
    display = f"{archive.name}!{name}"
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding("unexpected-binary", display, "publish only the reviewed pure-Python distribution")]
    return scan_text(display, text)


def scan_zip(archive: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    members = 0
    try:
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                if info.is_dir():
                    continue
                members += 1
                findings.extend(_safe_member_name(archive, info.filename))
                member_mode = (info.external_attr >> 16) & 0o170000
                if member_mode == stat.S_IFLNK:
                    findings.append(
                        Finding("archive-symlink", f"{archive.name}!{info.filename}", "rebuild using regular files")
                    )
                    continue
                findings.extend(_scan_archive_payload(archive, info.filename, package.read(info)))
    except (OSError, zipfile.BadZipFile):
        findings.append(Finding("invalid-wheel", str(archive), "rebuild a readable wheel"))
    return findings, members


def scan_tar(archive: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    members = 0
    try:
        with tarfile.open(archive, mode="r:gz") as package:
            for member in package.getmembers():
                if member.isdir():
                    continue
                members += 1
                findings.extend(_safe_member_name(archive, member.name))
                if member.issym() or member.islnk():
                    findings.append(
                        Finding("archive-link", f"{archive.name}!{member.name}", "rebuild using regular files")
                    )
                    continue
                if not member.isfile():
                    findings.append(
                        Finding("archive-special-file", f"{archive.name}!{member.name}", "remove the special entry")
                    )
                    continue
                extracted = package.extractfile(member)
                if extracted is None:
                    findings.append(
                        Finding("unreadable-archive-member", f"{archive.name}!{member.name}", "rebuild the sdist")
                    )
                    continue
                findings.extend(_scan_archive_payload(archive, member.name, extracted.read()))
    except (OSError, tarfile.TarError):
        findings.append(Finding("invalid-sdist", str(archive), "rebuild a readable gzip-compressed sdist"))
    return findings, members


def scan_artifacts(artifacts: Iterable[Path]) -> tuple[list[Finding], int, int]:
    findings: list[Finding] = []
    artifact_count = 0
    member_count = 0
    for artifact in artifacts:
        artifact_count += 1
        if artifact.name.endswith(".whl"):
            artifact_findings, members = scan_zip(artifact)
        elif artifact.name.endswith(".tar.gz"):
            artifact_findings, members = scan_tar(artifact)
        else:
            artifact_findings = [
                Finding("unsupported-artifact", str(artifact), "provide one wheel or gzip-compressed sdist")
            ]
            members = 0
        findings.extend(artifact_findings)
        member_count += members
    return findings, artifact_count, member_count


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    findings, source_files, text_bytes = scan_source(root, require_no_git=args.require_no_git)
    artifact_findings, artifact_count, member_count = scan_artifacts(args.artifact)
    findings.extend(artifact_findings)
    if findings:
        for finding in sorted(findings, key=lambda item: (item.path, item.line or 0, item.rule)):
            print(finding.render(), file=sys.stderr)
        return 1
    print(
        "Public hygiene passed: "
        f"{source_files} source files, {text_bytes} UTF-8 bytes, "
        f"{artifact_count} artifacts, {member_count} archive members."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
