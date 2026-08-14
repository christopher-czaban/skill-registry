# Skill Registry

Skill Registry is a small Python engine for maintaining a Git-backed,
user-owned collection of agent skills and deploying those skills to supported
agent runtimes. The engine validates registry metadata, discovers target
state, plans or applies safe deployments, reports drift, and provides
guardrails for canonical and deployed copies.

The engine package contains no user skills. Most users keep one repository:
their registry instance. They install or pin this package as a dependency and
do not need to clone the engine source.

## Quick start

Create a registry instance with one or more generic target presets:

```sh
uvx --from skill-registry skill-registry init ./my-agent-skills \
  --target codex \
  --target agents
cd ./my-agent-skills
uv sync
```

The first dependency resolution creates `uv.lock`; commit that lock with the
instance. On later machines, clone only the instance repository and run
`uv sync --frozen`. Target roots are machine-specific, so validate explicit
targets before deploying on a new machine.

A generated instance starts with this ownership boundary:

```text
my-agent-skills/
├── pyproject.toml
├── registry.yaml
├── runtime-dependencies.yaml
├── runtimes.yaml
├── skill-sets.yaml
├── skills/               # canonical, user-owned skill packages
├── targets/              # portable target definitions
└── targets.local/        # optional machine-local overrides, ignored by Git
```

## Everyday commands

```sh
uv run --frozen skill-registry validate
uv run --frozen skill-registry discover --target codex
uv run --frozen skill-registry deploy --dry-run --target codex
uv run --frozen skill-registry deploy --target codex --reason "approved update"
uv run --frozen skill-registry status --target codex
uv run --frozen skill-registry doctor --target codex
```

Reconciliation and proposal commands help classify or import content that
already exists at a target. Inspect every plan or proposal before applying it:

```sh
uv run --frozen skill-registry reconcile plan --target example-target --skill example-skill
uv run --frozen skill-registry proposal inspect --proposal ./review/example-proposal
```

Every command supports `--json` for automation. Pass `--root PATH` when
running outside the instance.

## Root selection

The engine selects exactly one valid registry root in this order:

1. explicit `--root`;
2. `SKILL_REGISTRY_ROOT`;
3. the nearest registry ancestor of the physical current directory;
4. the machine-local default-root pointer.

Use `skill-registry root show`, `root set-default PATH`, and
`root clear-default` to inspect or manage the pointer. A malformed or
inaccessible higher-priority source fails closed instead of silently selecting
another registry.

## Deployment model

Targets choose `symlink` or `copy` deployment. Symlinks are preferred because
the canonical skill remains visibly authoritative. Copy is an explicit
fallback for targets or host boundaries where symlinks are unsuitable, such as
some WSL-to-Windows paths. Deployment records and payload hashes distinguish
registry-owned entries from unrelated content; replacement requires positive
ownership evidence.

Supported hosts are macOS, Linux, and WSL. Native Windows is not currently in
scope.

See [Architecture](docs/ARCHITECTURE.md), [Contributing](CONTRIBUTING.md),
[Security](SECURITY.md), and [Releasing](docs/RELEASING.md) for details.
