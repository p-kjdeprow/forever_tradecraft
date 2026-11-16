# Local game setup

Use the authenticated desktop session and the reviewed Wine runtime. Keep account/WTF,
Interface, prefix registry and runtime DLL links when repairing an installation. Machine
paths belong in private configuration, never tracked source.

The host installer renders separate scanner.toml and automation.toml from host.toml.
Scanner configuration contains transport and account paths; automation configuration pins
the game process/window, native identity and its independent canvas crop. Automation must
observe the client with no scanner/catalog installed. The optional host application adds
scan-aware recovery and both addon installations.

For a v0.1 upgrade, review the package-owned
[host example](../apps/host/src/crowley_host/ops/package_data/config/host.example.toml).
Remove retired host fields canvas_origin, client_x/client_y, client_width/client_height,
outer_width/outer_height, model_crop and window_client_offset. Add the independent automation
canvas crop from the example. Old companion.toml is replaced by strict scanner.toml; a host
install rerender preserves valid scanner poll/refresh values while repinning identity/paths.
Malformed existing scanner configuration fails before mutation and needs explicit repair.

Disable the old crowley-scan unit at the reviewed quiet cutover boundary before starting
crowley-host. Install the complete matching wheel set and expose component executables;
do not leave old/new console scripts selected inconsistently from two tool environments.
Verify the actual installed version and unit ExecStart paths before resuming. Keep rollback
artifacts and the pre-cutover backup until the new install is qualified.

Review the installed configuration and `crowley-ops install --dry-run` output before the
coordinated cutover. The installer copies package-owned resources and records their hashes;
production units/addons must not point into a checkout. Build the pinned XTEST tool with
`crowley-native build xdotool`. Keep API/database credentials and CROWLEY_WINE only in the
private secrets.env. `crowley-ops doctor` checks the actual installation.

The launcher reuses the logged-in Battle.net instance. Client.SingleInstance must be true;
change it only while Battle.net is stopped because a running process rewrites its settings.
Do not launch a second instance: it can invalidate the login token. Direct Wow.exe startup
cannot issue Battle.net's login ticket.

The reviewed launcher invokes one Battle.net command at a time, selects the product page,
then uses `crowley-desktop battle-net-play` for a real curved pointer click on the verified
Play button. It verifies process/window identity and fullscreen geometry, waits for character
select, and uses the independent automation frame probe to observe entry into the world.
A click dispatch alone does not establish startup. Evidence remains private in XDG state.

GNOME animations must be disabled and idle blanking off; doctor refuses a host that cannot
capture a stable canvas. Mutter IdleMonitor supplies physical-presence state. Synthetic
XTEST input does not count as a human hold, so manual agent input requires explicit ownership
of the GUI lease and suspension of the host controller.

For the existing Stormwind scanner opener, SoftTargetInteract must be 2 (keyboard) or 3 (any),
Ctrl-F12 must be free or bound to INTERACTTARGET, and the scanner character must be near the
configured auctioneer. Never assume another account's character or location is the same.

The current desktop adapters require one focused display-sized game window at the display
origin and exact process ownership (UID, Windows argv0 and Wine prefix). Configured canvas
crops are validated against actual captures; stale outer-window calibration fields are gone.

No measured current-build AH/mailbox route is shipped. First inspect native position,
facing, key bindings and UI state, then record and review an immutable measured route/target
profile. Refuse changed build/instance, combat/death/mount, stale frames and human takeover.
Do not reuse old beta routes or blind mailbox coordinates.

See [REPEATING_SCANS](REPEATING_SCANS.md) for collection and
[ARCHITECTURE](ARCHITECTURE.md) for the independent ownership contracts. Read the local-game-control
skill before any live game contact. This setup guide authorizes no trades.
