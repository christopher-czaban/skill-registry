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
uvx --from agent-skill-registry==0.2.0 skill-registry init ./my-agent-skills \
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

## Removing and retiring skills

Remove one machine's registry-managed installation with explicit selectors:

```sh
uv run --frozen skill-registry undeploy --target codex --skill example-skill --dry-run
uv run --frozen skill-registry undeploy --target codex --skill example-skill \
  --reason "No longer needed on this target" --undeployed-by operator
```

Removal requires a valid deployment record. Unmanaged entries and locally
changed copies are blocked. Backups and a local receipt under the target's
`state_dir/undeployments/` preserve the entry, reason, actor and recovery evidence.
Canonical files and other targets stay available. Repeated removal is a no-op.

Set `lifecycle: retired` in `skills/<id>/skill.yaml` through a reviewed Git change
to prohibit future installation while keeping the package. Retirement does not
remove existing deployments; run `undeploy` on each participating machine.
Reactivate through a separate reviewed `lifecycle: canonical` change. Engines
older than 0.2.0 do not enforce retirement and must be upgraded first.

See [Managed Undeployment And Retirement](docs/UNDEPLOY-AND-RETIREMENT.md) for
ownership checks, status, backups, failure recovery and source-deletion ordering.

See [Architecture](docs/ARCHITECTURE.md), [Contributing](CONTRIBUTING.md),
[Security](SECURITY.md), and [Releasing](docs/RELEASING.md) for details.
