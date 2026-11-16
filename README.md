# Forever Tradecraft / Crowley

Auction-house collection, retained market analysis and bounded local game automation for WoW
Forever. The main dashboard and recipe watch remain at `/dashboard`. Scanner and automation
are separate installable Python packages and separate addons.

| Distribution | Source | Purpose |
|---|---|---|
| crowley-common | packages/common | Identity, scan/frame contracts, target data and private filesystem helpers |
| crowley-market | packages/market | Ingestion, history, dashboard, recipe watch and recovery tools |
| crowley-scanner | packages/scanner | Crowley/Crowley_Data addons and upload/ACK companion |
| crowley-automation | packages/automation | Game observation/input/launch, operation ledger, native actions and measured routes |
| crowley-host | apps/host | Optional combined installation, diagnosis and scan-aware game maintenance |

Each component depends on common. Scanner, automation and market never import each other;
only host composes them. Market installs without desktop libraries. Automation resolves no
scanner catalog or plan. Package-owned resources work outside a checkout.

For development:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python tools/generate_launch_target.py --check
```

The default suite needs no game or database. Database checks require a fresh disposable
TimescaleDB and serial execution; see [TESTING](docs/TESTING.md). No test should point at
the deployed database or send live game input.

Build a coordinated installable set with `uv build --all-packages`. Install the wheels as
non-editable packages, supplying the matching four component wheels as local dependencies
of crowley-host. With uv tool install, use --with-executables-from for each component to
expose its scripts as well as host's. Keep rollback wheels privately before replacing an
existing installation; never link production services or addons to a checkout.

The public commands are:

| Command | Purpose |
|---|---|
| crowley-server | Market API/dashboard |
| crowley-companion | Standalone saved-scan/probe upload and ACK transport |
| crowley-watchlist | Manage the scanner catalog and canonical plan |
| crowley-desktop | Independent game observation, presence, frame probe and Battle.net Play |
| crowley-native | Build/verify the pinned XTEST tool |
| crowley-automation | Inspect/prepare/run exact local operation batches and read receipts |
| crowley-host | Scan-aware host loop and status |
| crowley-ops | Install, doctor, health, image build and market backup/restore |

Use each command's --help for its installed version. Host configuration lives in private
host.toml ([example](apps/host/src/crowley_host/ops/package_data/config/host.example.toml));
installation renders independent scanner.toml and automation.toml. Credentials
stay in secrets.env. Review `crowley-ops install --dry-run` and doctor output before the
coordinated package/addon/service cutover. This repository refactor does not deploy itself.

The scanner addon schedules targeted sweeps every 900 seconds while the AH is open. A human
can collect without automation. At six unacknowledged sweeps it pauses without deleting
evidence. SavedVariables reaches disk on reload/logout; companion uploads original bytes,
writes Data.lua ACKs, and a later client reload loads them. The host can automate that cycle.
See [REPEATING_SCANS](docs/REPEATING_SCANS.md) and [GAME_SETUP](docs/GAME_SETUP.md).

In game, `/crowley probe`, `/crowley status`, `/crowley scan` and `/crowley set` are scanner
commands. Fresh automation uses its separate `/cauto` staging surface through the supported
Python runtime. It supports exact commodity/item listing, cancellation, non-COD mail collection,
known-location interaction and measured movement. A batch requires its full immutable digest
and a separate expiring current-client session arm. Staging/dry-run cannot post. Missing causal
receipts stay uncertain and prevent automatic retry or maintenance behind the operation.

Every automated click uses seeded curved pointer motion, a real press/release and the hard
one-second global rate gate. Focus, target, identity and physical presence are checked before
input. Routes and mailbox targets require reviewed current-build measurements; none are guessed
or shipped as qualified. Current-client protected-call behavior needs specific live qualification
before arming real trades. A listing is not a sale.

Retained analysis uses exact copper, canonical itemKey and separate houses. The summary requires
at least 6 observed market units/hour under recorded-cadence and outage-gap rules. Profit assumes
sale; listing depletion is not confirmed revenue. See the [agent operation guide](docs/AUTOMATION.md), [ANALYSIS](docs/ANALYSIS.md),
[ARCHITECTURE](docs/ARCHITECTURE.md), [HANDOFF](docs/HANDOFF.md) and
[MARKET_RECOVERY](docs/MARKET_RECOVERY.md).
