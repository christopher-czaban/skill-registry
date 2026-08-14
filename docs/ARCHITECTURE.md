# Architecture

## Engine and instance

Skill Registry separates reusable execution code from user-owned content.

The `skill-registry` distribution supplies the `skill_registry` Python package,
the CLI, and minimal instance templates. A registry instance supplies
`registry.yaml`, canonical `skills/`, target and runtime catalogs, optional
skill sets, local overrides, and generated state. The engine never searches
for or imports a separate private checkout.

Ordinary users therefore maintain one Git repository: their instance. Its
`pyproject.toml` pins the engine version, and `uv.lock` makes later-machine
installation reproducible.

## Root contract

Root selection is deterministic: explicit argument, environment variable,
nearest physical-CWD ancestor, then machine-local pointer. Each selected root
must contain a regular `registry.yaml`. A malformed, inaccessible, or invalid
higher-priority source is an error; it does not fall through to a lower source.

The default pointer is machine-local configuration rather than instance
content. Pointer updates use atomic replacement and are performed only after a
candidate instance validates.

## Initialization

`skill-registry init` reads templates through installed package resources,
stages a complete instance on the destination filesystem, validates it, and
publishes without overwriting an existing entry. Rollback removes only objects
whose ownership is proven by descriptor- or native-operation-derived identity.
A default-pointer failure after publication reports the intact scaffold rather
than claiming the whole transaction disappeared.

## Targets and deployment state

Portable target definitions live in `targets/`. Machine-specific definitions
or overrides live in ignored `targets.local/` and override tracked targets by
name. Every target declares its deployment mode and whether it supports
symlinks.

Symlink deployments point back to canonical packages. Copy deployments include
payload hashes and origin records so status can distinguish current, drifted,
missing, blocked, and unrelated entries. Replacement operations require valid
registry ownership records.

Generated state is derived evidence, not canonical skill content. It must not
be hand-edited or treated as a substitute for the instance repository.

## Guardrails and reconciliation

Guard checks classify write-like operations against canonical and deployed
paths. Strict mode blocks unsafe direct edits to managed target entries.
Reconciliation records an explicit decision for pre-existing content, and
proposal import keeps source evidence separate from canonical publication
until reviewed.

Multi-source registries and native Windows hosts are intentionally outside the
current contract.
