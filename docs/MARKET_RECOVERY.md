# Crowley market snapshot and restore drill

This offline tool creates a private PostgreSQL custom dump plus an inventory of
the immutable raw archive volume, then checks that the dump can be restored into
a fresh Compose project with no host-published ports. It is a one-time operator
tool, not a scheduled backup service or a whole-machine clone.

Run it from the repository root, passing the checkout explicitly:

```sh
crowley-ops snapshot --repo "$PWD"
crowley-ops drill --repo "$PWD" \
  ~/.local/state/crowley/market-backups/<printed-backup-id>
```

The default state directory and each snapshot/receipt directory are mode `0700`;
their files are mode `0600`. `snapshot` prints only the generated basename. A
snapshot is accepted by `drill` only when it has exactly `database.dump`,
`raw.tar`, `manifest.json`, and `COMPLETE`, all canonical hashes validate, and
the `INCOMPLETE` sentinel is absent. Interrupted snapshots remain invalid
evidence. The manifest binds the exact dump and raw-tar bytes and inventories
every regular raw member by path, size, and SHA-256.

The source PostgreSQL database is read only through `pg_dump` and version
metadata queries. The raw archive is copied from a read-only volume mount after
the dump; temporary dotfiles from the ingester's atomic archive writes are
excluded, and symlinks or special files stop the snapshot. The tool does not
query live row counts. All row counts and market-reference results in the
private receipt are derived from the restored disposable database.

The source commit and dirty-tree digest in the manifest record where the
snapshot was made; unrelated later source or artifact edits do not invalidate
it. Restore compatibility still requires the recorded Compose and migration
digests, pinned database/server image identities, PostgreSQL and TimescaleDB
versions, and the migration files embedded in the pinned server image to match.
Container, volume, and network IDs from the snapshot remain provenance and a
collision deny set. The drill separately freezes the current source identities
and rechecks them while it runs; recreated source IDs do not invalidate an
otherwise compatible snapshot.

The drill validates the entire snapshot before creating resources. It generates
a new project name and credentials, resolves Compose privately, removes both
services' host-port mappings, disables restart policies, and creates fresh
project-scoped volumes on an internal network. It checks IDs, labels, mounts,
images, and the absence of port bindings before every restore write and cleanup.
The actual target must use the recorded images, exactly one expected mount per
service, and two distinct project volumes assigned to the database and raw
archive roles. Any extra mount or collision with a current or historical source
resource blocks the drill.
The dump restore sequence is database creation, TimescaleDB extension creation,
`timescaledb_pre_restore()`, single-job `pg_restore --no-owner`,
`timescaledb_post_restore()`, and `ANALYZE`. This follows the [Timescale logical
backup and restore guide](https://docs.timescale.com/self-hosted/latest/backup-and-restore/logical-backup/)
and the [whole-database migration guide](https://docs.timescale.com/self-hosted/latest/migration/entire-database/);
the drill further requires exact recorded PostgreSQL and TimescaleDB version
equality for its offline image pin.

After restore, it checks table counts, build/interface/house/plan groupings,
house keys, plan hashes, and migration names. Every format-2 sweep reference must
resolve to its exact `/data/`-mapped raw member and match its logical digest,
envelope identity, house, client metadata, and restored plan. Unreferenced raw
members are retained and classified as orphans. A legacy `scans.raw_path` is
not interpreted as format 2: if any legacy scan row exists, the drill fails
closed as `unsupported_legacy_reference`. That legacy format needs a separate
decoder/identity decision before such a restore can qualify.

Only after those checks does the drill start the recorded server image and make
private-network `GET /healthz` and authenticated read-only `GET /v1/houses`
probes. The temporary generated credentials are removed on exit. Cleanup uses
only recorded container IDs, project labels, mounts, volume names, and network
IDs. It verifies each resource is absent after removal and records only the
confirmed removed subset. If Compose creation fails before the complete target
inventory can be proven, possible partial resources are left untouched and the
receipt marks cleanup blocked. Ambiguous ownership stops cleanup instead of
broadening it. The private receipt is bound to the snapshot manifest digest and
records exact target identities and restored evidence. A receipt says
`restore_verified` only when
all 15 operations pass, restored-dump counts and migrations agree, both API
probes succeed, typed reference and target inventories validate, and cleanup
verifies absence for every target resource.
If database setup fails, an adjacent owner-only `failure-stage.json` may bind
one fixed stage label and safe reason code to the strict receipt digest. It may
also record a categorical `pg_isready` result and read-only database setup flags
from the isolated target after rechecking its identity. A failed `pg_restore`
may add one fixed error category and a few read-only catalog flags from the
disposable database; raw command text is inspected only in memory and is never
retained. The CLI reports only the fixed stage label and never includes SQL or
command output.

The preserved manual snapshot at
`/home/dev/.local/state/crowley/market-backups/beta-20261017T061327Z` predates
this layout and has no v1 completion contract. The canonical validator refuses
it; its recovery remains a separate manual procedure. No receipt, environment
value, connection string, credential, raw payload, or unsanitized operational
metadata belongs in the repository.
