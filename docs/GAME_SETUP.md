# Local WoW setup

Keep the selected client's account data, WTF, Interface and registry files when
repairing the runtime. Record the configured game root and product directory in
the host configuration; do not put machine paths in tracked files.

## Start the game

Use the desktop entry for the selected client or run the rendered launcher:

```sh
~/.local/bin/crowley-world-of-warcraft
```

This starts the cached Battle.net installation with the restored Wine runtime.
The entry is `~/.local/share/applications/net.lutris.world-of-warcraft-1.desktop`;
it invokes the wrapper directly rather than depending on the deleted Lutris game
registration. It checks exact process identity, owner and prefix to avoid a
second client. Startup still requires the authenticated desktop session.

Before starting the launcher, provide the required host-session environment:

```sh
export CROWLEY_WINE="/path/to/reviewed/wine/bin/wine"
export XAUTHORITY="/path/to/reviewed/xauthority"
export XDG_STATE_HOME="/path/to/crowley-state"
```

The launcher takes `DISPLAY` from `display` in the selected `host.toml`; do not
override it through the shell environment. Missing `CROWLEY_WINE`,
`XAUTHORITY`, or `XDG_STATE_HOME` causes the launcher to fail loudly, and an
unreadable Xauthority file is also refused.

The launcher uses the verified Battle.net handoff: it ensures one logged-in
Battle.net is running, invokes one `--exec` at a time (`launch_uid wowf`, then
`focus play`, which selects the product page). It selects the one visible
`battle.net.exe`-class window named `Battle.net` (failing unless exactly one
matches), un-maximizes it, activates it and runs `crowley-ops battle-net-play`.
That command captures the screen with `gnome-screenshot`, finds the large solid
blue Play button (exactly one blue region of plausible size in the window's
bottom-left quarter whose label is the four glyphs of "Play", roughly centred;
it fails loudly otherwise) and clicks inside its inner 60% as a full simulated human click: a
seeded, curved, Fitts-timed pointer path through the pinned XTEST emitter, a real
press and release, held under the canonical click-rate gate (at least 1 s between
automated clicks). Motion, an endpoint and target-window check, and the press are
separate emissions, as in the session driver. The seed and plan are recorded under
`$XDG_STATE_HOME/crowley/executor-evidence/`. Keyboard activation is not used:
after a fresh Battle.net start the embedded Chromium view has no keyboard focus,
`focus play` does not move it, Return does nothing, and Tab reaches the logo menu
(which contains Log Out). If the game has not started within 20 s the click is
retried (fresh capture) up to 2 more times, then the launcher fails. The launcher
resolves `crowley-ops` from `PATH` and requires `CROWLEY_WINE`. Battle.net must have `Client.SingleInstance` set to
`"true"`. Set that value only while Battle.net is stopped: a running Battle.net
rewrites its configuration from memory, and starting a second instance deletes
the saved login token. Battle.net is the only component that can issue the game
login ticket; starting `_forever_/Wow.exe` directly with
`-launcherlogin -uid wowf` fails with `WOW51900308`.

Disable GNOME animations before using native screenshots or the takeover hover:

```sh
gsettings set org.gnome.desktop.interface enable-animations false
crowley-ops doctor
```

The doctor refuses the host unless `gsettings get
org.gnome.desktop.interface enable-animations` prints `false`. The screenshot
flash otherwise generates XI2 crossing and motion events on the fullscreen game
window, which the Guardian correctly treats as unattributed input.

Required client settings (stored by the client per account):

- `SoftTargetInteract` must be `2` (keyboard) or `3` (any). The WoW Forever
  default is `1` (gamepad only), and the session refuses to open the auction
  house with "native keyboard interaction is disabled (mode='gamepad')". Set it
  once in game with `/console SoftTargetInteract 2`.
- The display is 1920x1080 and the client runs windowed-fullscreen
  (`gxMaximize 1` in `_forever_/WTF/Config.wtf`); the launcher then makes the
  window fullscreen at the origin. Wine maps the window with fixed-size
  `WM_NORMAL_HINTS` and `_MOTIF_WM_HINTS` limited to move|close, which makes the
  window manager ignore a fullscreen request, so the launcher removes
  `WM_NORMAL_HINTS` and resets `_MOTIF_WM_HINTS` with `xprop` first, then
  verifies `_NET_WM_STATE_FULLSCREEN`, display-sized outer geometry and a single
  display-sized child, failing loudly after 30 s. Once verified, it waits a
  fixed 45 s for character select (the shell has no detector for it) and sends
  one `Return` (Enter World). The launcher's only automated click is the Battle.net Play click above; this step is keyboard-only. The telemetry canvas is pinned to a
  1920x1080 client.

The launcher template is [crowley-world-of-warcraft.sh.in](../deploy/launcher/crowley-world-of-warcraft.sh.in).
Render it from the selected target and preserve executable mode 0700 on the
installed wrapper. Keep recovery evidence private under the XDG state directory.

## Required runtime files

The Wine runtime is selected by the host configuration:

```text
`$XDG_DATA_HOME` or the configured host runtime directory.
```

Its matching [maintainer release](https://github.com/Kron4ek/Wine-Builds/releases/tag/11.16)
provides `wine-11.16-staging-amd64.tar.xz`, SHA256:

```text
4b6bae7ba94652c49bb9527abb6428828fa05d6c16bc3aad5c46f10311276366
```

The prefix also references these directories under `~/.local/share/lutris/runtime/`:

- `dxvk/v2.6.2/`
- `vkd3d/v2.14/`
- `dxvk-nvapi/v0.9.0/`

## Install the addon and units

Install a non-editable package. The installer renders `companion.toml`,
`session.toml`, and `secrets.env` into the XDG config directory; edit the
account and secrets before starting services:

```sh
uv tool install .
crowley-ops install --game-root "/path/to/World of Warcraft" --dry-run
crowley-ops install --game-root "/path/to/World of Warcraft"
# Set wtf_account in companion.toml and secrets in secrets.env.
crowley-ops doctor
```

The game root is explicit; the installer appends the packaged target's
`productDir` and copies both addon directories into that flavor. Use
`--dev-addon-from` only when deliberately testing a checkout symlink.

Those are active game dependencies even though startup now uses Wine directly.
Deleting a launcher/runtime data directory can leave dangling DLL links in the
existing prefix. Check both `windows/system32` and `windows/syswow64` after a repair.

## Verified recovery

Keep runtime checksums, resolved DLL links, duplicate-process receipts, registry,
addon and SavedVariables backups private under Crowley's executor-evidence
directory in the XDG state directory.

A new game process invalidates Crowley's old native/input calibration bindings.
Keep scans paused until fresh native and deliberate human input calibration pass.
