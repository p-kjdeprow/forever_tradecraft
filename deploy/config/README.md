# Deployed configuration

These files are reviewable configuration templates. `crowley-ops install` renders
the target-specific paths into the user's XDG config directory; it does not create
links into this repository.

| File | Used by | Linked from |
|---|---|---|
| `session.toml` | `crowley-session` (daemon and CLI) | `$XDG_CONFIG_HOME/crowley/session.toml` |
| `companion.toml` | `crowley-companion` | `$XDG_CONFIG_HOME/crowley/companion.toml` |
| `secrets.env` | all installed units and Compose | `$XDG_CONFIG_HOME/crowley/secrets.env` |
| `routes/stormwind.json` | route walker (`routes_file` in `session.toml`) | — |

Secrets never live in the TOML templates. Put `CROWLEY_API_TOKEN`,
`CROWLEY_DB_PASSWORD`, `CROWLEY_IMAGE_VERSION`, `CROWLEY_DATABASE_URL`, `CROWLEY_BIND` and
`CROWLEY_PAPER_RUN` in
`$XDG_CONFIG_HOME/crowley/secrets.env` with mode 0600. Installed units load it
with `EnvironmentFile=%E/crowley/secrets.env`.

`routes/stormwind.json` is built from private hand-walked recordings. Keep source
evidence under the XDG state directory and review route provenance before use.

```sh
uv tool install .
crowley-ops install --game-root "/path/to/World of Warcraft"
crowley-ops doctor
```
