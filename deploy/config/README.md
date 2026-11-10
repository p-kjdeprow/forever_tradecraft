# Deployed configuration

These files are tracked tunable templates. `crowley-ops install` copies them
to `$XDG_CONFIG_HOME/crowley/` after validating the untracked host contract at
`$XDG_CONFIG_HOME/crowley/host.toml`.

| File | Contents |
|---|---|
| `companion.toml` | companion polling and refresh intervals |
| `paper.env` | paper-trading tunables |

The host contract is documented in `deploy/templates/host.example.toml`.
It owns the absolute Wine/game roots, selected account and character, server
URL, display, and measured window geometry. The game flavor, SavedVariables,
addon files, state directory and runtime directory are
derived by `crowley.config`.

Secrets remain only in `$XDG_CONFIG_HOME/crowley/secrets.env` with mode 0600.
