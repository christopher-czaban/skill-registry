# Managed Undeployment And Retirement

## Contract

`undeploy` removes selected registry-owned installations on the executing
machine. It never deletes canonical sources, target roots, other installations,
or native agent configuration. `lifecycle: retired` preserves the canonical
package while prohibiting installation. No `retire` or source-deletion command
is needed: lifecycle changes and eventual archival are reviewed Git changes.

```sh
skill-registry undeploy --target demo --skill example --dry-run --json
skill-registry undeploy --target demo --skill example \
  --undeployed-by operator --reason 'Retire obsolete workflow'
```

Exactly one registered `--target` and one or more explicit `--skill` selectors
are required. Broad target defaults, force flags, and skill-set expansion are
not supported. Apply requires a nonempty reason. JSON output uses the existing
CLI conventions: 0 success, 1 blocked or failed operation, 2 usage error, and 3
invalid configuration/selector. Every operation reports its exact target path,
mode, reason, backup and receipt where applicable.

## Planning And Ownership

Dry-run writes nothing. All selected relations must pass preflight before the
first mutation. Apply repeats proof and filesystem-identity checks immediately
before each mutation. Multiple relations are not one global transaction:
completed relations must be reported if a later operation fails.

Deletion requires a valid deployment record matching the configured target,
skill ID, canonical source path, exact lexical target entry, and deployment
mode. A name match or a symlink alone does not prove ownership. Skill IDs must
be safe single path components. Source/target/state overlap is rejected.

For symlinks, require the recorded symlink mode and the expected destination;
move/unlink only the link, never its resolved destination. A matching broken
symlink is removable after the source disappears. Wrong links, replacement
directories, local-only classifications, and malformed records block removal.

For copies, compare the current deployable payload with the recorded inventory.
An unchanged older copy is removable even when canonical content is newer.
Locally changed copies block removal. The backup preserves the entire original
entry, including excluded cache/control files and internal symlinks.

An absent entry with a valid record can be finalized as an undeployment. An
already absent known skill with no record is a no-op. Unknown IDs fail clearly.
Cleanup relies on the record and must work without a canonical source or its
old target allowlist, including after another machine pulls a source deletion.

## Local State And Recovery

Each successful removal stores a receipt at:

```text
<target.state_dir>/undeployments/<skill-id>.json
```

The receipt records schema version, source and target paths, deployment mode,
timestamp, actor, reason and immutable backup location. Backups are stored under
`<target.state_dir>/backups/<operation-id>/`, containing the original deployment
record and entry. Audit and deployment state stay local, outside Git.

The active record is removed only as part of a recoverable operation. Metadata
failure restores the entry, record and previous receipt. Interrupted operations
retain a pending journal and recovery evidence; status must flag them rather
than report a healthy absence. Repeating a completed removal does not create
new backups or events.

`status` and `doctor` inventory deployment records and receipts as well as
canonical packages. `undeployed` is healthy intentional absence; a recreated
entry without a new record is blocked. A source removed while an active record
remains is reported as `orphaned`. An active record always takes precedence over
an older undeployment receipt. Explicit deployment may reinstall a canonical
skill; implicit bulk deployment skips intentionally undeployed relations.

## Lifecycle

`lifecycle` accepts `canonical` (default), `retired`, and the legacy `placeholder`
import state. Existing placeholder behavior is preserved. Unsupported values are
validation errors. Retired packages remain in inventory. Implicit default
deployment skips them; explicit deployment, skill-set deployment and reconcile
replacement reject them. Runtime health never executes a retired skill wrapper.
Active dependencies and skill-set members must not reference retired packages.

Existing installations are not removed by a lifecycle change. A still-installed
retired package reports `retired_deployed` and needs cleanup; an absent retired
package reports healthy `retired` or `undeployed`. Agent-native enabled/disabled
settings and already loaded sessions remain outside this command's scope.

## Multiple Machines And Source Deletion

1. Upgrade every participating machine to an engine supporting retirement.
2. Commit and pull the reviewed `lifecycle: retired` change.
3. On each machine, inspect local status, dry-run and apply explicit undeploy.
4. Clean skill-set/dependency references before optionally archiving the source.

Source deletion is optional. Keep packages while another machine's cleanup is
unknown. The engine must still allow record-proven cleanup if source deletion
was pulled first. No local command claims that all other machines are clean.

## Verification

Use temporary registries to cover symlink/copy removal, source preservation,
other targets, wrong/unmanaged entries, local drift, stale copies, excluded-file
backups, unsafe paths/layouts, missing sources, source-independent receipt
inventory, dry-run byte-for-byte non-mutation, repeat calls, recreation, failed
state writes, interrupted journals, retirement deploy/reconcile/export guards,
runtime probe exclusion and Python/macOS/Linux compatibility. Package and
installed-wheel checks must prove the CLI ships with the released engine.
