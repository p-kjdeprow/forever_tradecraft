# Forever Tradecraft

Project repository: [p-kjdeprow/forever_tradecraft](https://github.com/p-kjdeprow/forever_tradecraft).
The addon, Python package, configuration paths and `/crowley` commands currently retain
the Crowley name.

Auction house data platform and addon for **WoW Forever**. Scan the AH in game, store the
retained observation history per house on a server, and compute gold-making strategies.

```
addon/Crowley        in-game scanner, /crowley commands (Lua)
addon/Crowley_Data   data file the companion writes; loaded at login or /reload
src/crowley/core     shared: SavedVariables parser, scan format, stats, Lua writer
src/crowley/companion  watches the game folder, uploads scans, writes Crowley_Data
src/crowley/server   FastAPI + TimescaleDB ingest and query API
deploy/              docker compose (db + server), companion systemd unit
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for requirements, history and roadmap, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical design.

The [first-run watchlist](docs/WATCHLIST.md) contains the 544-item catalog, its management
commands and delivery path; 428 active item IDs are in the current scan plan. The addon
consumes the immutable format-2 plan and
starts targeted sweeps every 900 seconds while the AH is open, with bounded retained depth.

## Quickstart (server and game on one Linux machine)

```sh
# 1. Server + database
cp deploy/.env.example deploy/.env      # set CROWLEY_API_TOKEN and CROWLEY_DB_PASSWORD
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
curl localhost:8080/healthz

# 2. Companion install and config
# Replace /absolute/checkout with the absolute path of this checkout.
uv tool install --editable /absolute/checkout
mkdir -p ~/.config/crowley && cat > ~/.config/crowley/companion.toml <<TOML
wow_dir    = "/home/you/.wine/drive_c/Program Files (x86)/World of Warcraft/_forever_"
server_url = "http://127.0.0.1:8080"
api_token  = "<CROWLEY_API_TOKEN from deploy/.env>"
TOML
crowley-companion install-addon  # symlinks Crowley, copies Crowley_Data
crowley-companion doctor         # checks paths, addons and the server
crowley-companion run            # or install deploy/crowley-companion.service

# The user service reads the authenticated config above and invokes the installed
# command directly; it does not require this checkout as its working directory.
mkdir -p ~/.config/systemd/user
cp deploy/crowley-companion.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crowley-companion
```

In game:

| Command | What it does |
|---|---|
| `/crowley probe` | Records the client's AH API surface. **Run this first on the beta.** |
| `/crowley status` | Current house, next due time, loaded plan, cursor, stored sweeps and failures |
| `/crowley scan` | Targeted watchlist sweep (AH must be open; starts are 15 minutes apart) |
| `/crowley set faction neutral` | Override house detection (`region`/`ruleset`/`faction`, or `auto`) |

Sweeps reach the server after `/reload` or logout, because that is when WoW writes SavedVariables.
At six pending sweeps the addon pauses and never evicts unacknowledged evidence. The first
reload lets the companion upload and write acknowledgements; a second reload loads those
acknowledgements and resumes collection.

Format 2 is the only accepted upload format. Its compressed JSON payload carries the unchanged
canonical `planJSON` and `planHash`, one object observation per planned item, and a separately
gated owned snapshot. Exact reduced rational unit prices, raw item variants, tagged unknowns,
coverage cutoffs and witnesses prevent fractional copper rounding or missing data being treated
as zero supply. Public-market churn is an uncalibrated proxy; an observed disappearance is not a
confirmed sale. Confirmed own sales are recorded separately only after a full owned query.

## Game and server on different machines (Tailscale)

1. On the server, set `CROWLEY_BIND` in `deploy/.env` to `tailscale ip -4` and restart compose with `--env-file deploy/.env`.
2. On the game machine, set `server_url = "http://<server-tailnet-name>:8080"` and run the companion there.

## Historical local deployment (2026-10-15)

The game, companion, API and database run on Linux host `3080servermach`; the repository is
at `/home/dev/repos/pegasus`. WoW 1.60.1 build 70374/interface 16001 runs locally through
Lutris/Wine. Crowley is a repo
symlink and generated `Crowley_Data` has the 428-active-ID, 900-second, 20-level plan. The
verified house is `beta-normal-alliance`; Moonlight/Sunshine and the fullscreen watcher are
disabled. The companion is active. The database has six accepted sweeps, 2,568 observations
and 5,726 captured price rows.

The full native verification sweep `beta-normal-alliance-1792040351-Arthur-6643` ran from
2026-10-15 04:59:11 to 05:07:15 UTC (484 seconds). It queried all 428 active IDs with zero
incomplete or unqueried items: 306 full commodity observations (1,328 rows), 23 bounded
commodity observations (460 rows), and 99 full item observations (1,101 rows). It captured
2,889 rows across 271 row-bearing IDs and 125,395 units, all under `beta-normal-alliance`.
Native item examples include equipment 873, recipe 2555, and containers 4238, 4240, 4241,
4245 and 5765.

A later scheduled sweep, `beta-normal-alliance-1792041251-Arthur-0169`, ran 05:14:11–05:19:36
UTC and stored 1,599 rows. It attempted 293 items (292 successful, one active item
interrupted when the AH closed) and left 135 unqueried when the AH closed. This later scan is
newer by scan time; the preceding 428-item run is the full verification result. After the
second reload, the addon reported two uploaded scans cleared and loaded the 428-active-ID
plan. A further normal reload persisted the cleared queue, independently verified empty on
disk. Arthur is parked at Buckler with the AH closed. The earlier
malformed 961-row sweep remains intact in private quarantine and was never ingested. The earlier three
rows for item 2555 came from a cold level-zero SellSearch query and are not proof of current
item dispatch. The class-aware correction is now verified natively for recipes, containers
and equipment. Sol approved it; 118 addon/core tests and the normal suite (149 passed, 15 DB
tests skipped) passed. The earlier isolated full database run preceded those later addon
checks. See the [live validation record](.archive/live-client/real-validation.md) and
[handoff](docs/HANDOFF.md) for chronology, remaining limits and preserved payload hashes.
Repository guidance is in [AGENTS.md](AGENTS.md); reusable game/client workflows are in
`.agents/skills/`.

## Tests

```sh
export CARGO=/home/dev/.cache/crowley/rust-toolchain-1.85.1/toolchain/bin/cargo
flock /home/dev/.cache/r-pg.lock uv run pytest  # bounded xdist default: 16 workers
```

Run DB coverage serially against a fresh disposable test database using the
[documented command](docs/TESTING.md). Its fixtures reset one schema; the default suite refuses a shared database URL with xdist workers.
Final launch-preparation verification (2026-10-24): **2,455 passed, 25 DB-gated skips
in 153.78s** externally measured; the separate serial DB matrix passed 178 tests
with zero skips.
See [TESTING](docs/TESTING.md) for the full launch-readiness test contract.
