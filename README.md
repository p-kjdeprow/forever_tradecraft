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
src/crowley/scan     crowley-scan: keeps the game in the world with the AH open (observe, decide, act)
src/crowley/gui      pointer, click gate, XTEST tool, keyboard, capture, frame decode
src/crowley/companion  watches the game folder, uploads scans, writes Crowley_Data
src/crowley/server   FastAPI + TimescaleDB ingest and query API
deploy/              systemd, launcher and image-build templates
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for requirements, history and roadmap, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical design.

The [first-run watchlist](docs/WATCHLIST.md) contains the 544-item catalog, its management
commands and delivery path; 428 active item IDs are in the current scan plan. The addon
consumes the immutable format-2 plan and
starts targeted sweeps every 900 seconds while the AH is open, with bounded retained depth.

## Quickstart (server and game on one Linux machine)

```sh
# 1. Create the untracked host contract, then install the package.
uv tool install --link-mode copy .
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
mkdir -p "$XDG_CONFIG_HOME/crowley"
cp deploy/templates/host.example.toml "$XDG_CONFIG_HOME/crowley/host.toml"
$EDITOR "$XDG_CONFIG_HOME/crowley/host.toml"
crowley-ops install

# 2. Review the tunable templates. Identity and paths stay in host.toml.
$EDITOR "$XDG_CONFIG_HOME/crowley/companion.toml"
$EDITOR "$XDG_CONFIG_HOME/crowley/secrets.env"
# Put CROWLEY_API_TOKEN, CROWLEY_DB_PASSWORD, CROWLEY_IMAGE_VERSION,
# CROWLEY_DATABASE_URL, CROWLEY_BIND and CROWLEY_PAPER_RUN only in secrets.env.
chmod 600 "$XDG_CONFIG_HOME/crowley/secrets.env"

# 3. Build the server image from this checkout (or pass an explicit wheel instead).
crowley-ops image build --source "$PWD"

# 4. Server + database. Compose uses the values in secrets.env for its internal DSN;
# CROWLEY_DATABASE_URL is also the URL used by doctor and paper trading.
docker compose --env-file "$XDG_CONFIG_HOME/crowley/secrets.env" \
  -f "$(crowley-ops compose-file)" up -d
# Verify the health endpoint at the server_url configured in host.toml.

# 5. Copy units, timers, launcher and packaged addon files, then inspect every check.
crowley-ops doctor
crowley-native build xdotool        # the pinned XTEST input tool (once)

# 6. Start the companion and the scan loop only after reviewing doctor output.
systemctl --user enable --now crowley-companion crowley-scan
crowley-scan status
```

`crowley-scan` relaunches the game when needed (the launcher needs `CROWLEY_WINE` in `secrets.env`), enters the world,
opens the AH and reloads at the right moments, and holds while you use the keyboard or mouse. See
[docs/REPEATING_SCANS.md](docs/REPEATING_SCANS.md) and [docs/GAME_SETUP.md](docs/GAME_SETUP.md).

In game:

| Command | What it does |
|---|---|
| `/crowley probe` | Records the client's AH API surface. **Run this first on a new client build.** |
| `/crowley status` | Current house, next due time, loaded plan, cursor, stored sweeps and failures |
| `/crowley scan` | Manual targeted watchlist sweep (AH must be open); the addon also sweeps every 15 minutes on its own |
| `/crowley set faction neutral` | Override house detection (`region`/`ruleset`/`faction`, or `auto`) |

Sweeps reach the server after `/reload` or logout, because that is when WoW writes SavedVariables
(`crowley-scan` types the `/reload`).
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

1. On the server, set `CROWLEY_BIND` in `$XDG_CONFIG_HOME/crowley/secrets.env` to the selected Tailscale address and restart Compose with that env file.
2. On the game machine, set `server_url` in `host.toml` to the server endpoint and run the companion there.

## Deployment notes

The installer copies units, timers and packaged addon files into user-owned destinations and
records a sha256 manifest under the XDG state directory. It never links a production unit or
addon to a checkout. Use `--dev-addon-from` only for an explicit development checkout, and
use `--dry-run` to review the diff first.

## Tests

```sh
uv run pytest  # bounded xdist default: 16 workers
```

Run DB coverage serially against a fresh disposable test database using the
[documented command](docs/TESTING.md). Its fixtures reset one schema; the default suite refuses a shared database URL with xdist workers.
See [TESTING](docs/TESTING.md).
