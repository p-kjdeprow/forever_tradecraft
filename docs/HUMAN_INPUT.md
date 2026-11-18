# Input delivery and qualification

This source change does not install packages, change the running scanner, create
input devices or authorize a trade. Host/scanner startup stays on its pinned XTEST
transport. The standalone automation configuration defaults to `input_backend =
"xtest"`; selecting `"uinput"` requires an absolute `input_calibration` path.
Selection never substitutes another transport when requirements fail.

Both typing callers use one validated immutable keyboard timeline per command.
Seeded correlated cadence, key dwell and boundary pauses are bounded; unsupported
characters reject the entire command before input. Modifiers balance around their
letters. There is no typo simulation, per-character child, image capture or model
call. The native helper admits only balanced numeric key transitions and bounded
sleeps. Timeout includes declared duration plus startup overhead and never renews
a batch/policy/session arm. Cheap stop/session/process/focus/presence checks run
during emission; fresh complete exact-text native readback precedes separate Return.
A 123-character plan stays below eight seconds across the tested seed set. This is
planned timing, not a measurement of compositor delivery or live startup latency.

Pointer travel retains curved Fitts-style timing, endpoint variation and recorded
seed. If already inside the verified safe target, the plan has no trajectory and
freezes the current pixel. Every click still verifies the exact endpoint, target,
window/focus, staged native action and shared one-second press spacing. No warp or
fake out-and-back movement is introduced.

## Selected Linux device transport

The uinput helper is an ordinary unprivileged process using python-evdev, with one
owned virtual keyboard/relative mouse for a foreground GUI session. It receives
only bounded input jobs, not trading policy, ledger state or secrets. It is still
a virtual device; this implementation makes no hardware identity or detection claim.
Grant only the device access required by a separately approved deployment; do not
run trading as root or change global device permissions to bypass an error.

The helper inherits the actual held GUI lock descriptor. Parent disconnect,
watchdog/authorization failure, physical takeover, event loss, device replacement
or hotplug releases controls and destroys the owned device. Its lease descriptor
survives parent loss until teardown. A surviving parent closes/reaps the helper
before releasing the lock. Runtime and CLI cleanup execute inside the lease.
XTEST also retains a per-job supervisor: parent loss stops/reaps its sealed native
emitter and sends only possible-control releases under the inherited lease. Failed
release cleanup keeps the lease blocked; standalone context unwinding cannot unlock
while a tracked child or possible held control remains. Keyboard jobs and pointer samples use monotonic scheduling; a missed scheduling
bound rejects the job instead of replaying a burst of overdue events.

All physical event nodes must be readable; enumeration never hides inaccessible
nodes. Exclusion uses the exact kernel object returned by device creation, not a
name/prefix. Activity includes keyboard, mouse, controller and switch events, and
held buttons keep the idle window active. Private key contents are not logged.

A new raw reader has no historical coverage. Two explicitly configured cases exist:

- An empty `idle_coverage` requires a full 300-second idle observation before device
  creation. This is preparation inside a foreground session. Stop and expiry can
  cancel it. A short already-issued manual arm fails immediately when that history
  cannot fit, instead of waiting for inevitable expiry.
- Nonempty `idle_coverage` is an empirical per-device qualification manifest for the
  exact bound GNOME seat0 keyboard/mouse topology. Every event node, kernel identity,
  capability hash and seat must match. Controller, touch/digitizer, unknown relative
  axes, switches and unknown event classes cannot use this bootstrap. Readers open
  before pre-owned-event Mutter idleness is sampled; queued events and held controls
  override that baseline. After owned input begins, only raw activity history is used.

A mismatched manifest is an error. It does not switch to another history source.
Read-only inspection prepares before starting its bounded command deadline. Existing
manual batch arms and seller policy/session deadlines never extend.

## Calibration contract

A private user-owned JSON file (schema 1) contains exactly these fields:

| Field | Required evidence |
|---|---|
| `schema` | Integer `1` |
| `display` | Exact configured X11 display |
| `authority_sha256` | SHA256 of this session's authenticated Xauthority bytes; never copy the cookies into the profile |
| `x_keymap_sha256` | SHA256 of exact `xmodmap -pke` stdout |
| `mouse_settings_sha256` | SHA256 of concatenated `gsettings get org.gnome.desktop.peripherals.mouse` stdout for `speed`, `accel-profile`, `left-handed`, in that order |
| `x_to_evdev` | Measured mapping from canonical decimal X keycode strings to Linux evdev integer codes; no inferred offset or aliases |
| `gain_x`, `gain_y` | Positive measured initial relative-count/pixel estimates, each 0.01–100 |
| `display_size` | Exact `[width, height]` |
| `idle_coverage` | `{}` for full observation, or the exact qualified device manifest below |

Manifest keys are literal `/dev/input/eventN` paths. Each record contains exactly
`identity_sha256`, `capabilities_sha256`, `seat` (`"seat0"`). The identity hash is
SHA256 of Python `json.dumps(device_identity(path))`; the capability hash is SHA256
of `json.dumps(device.capabilities(absinfo=False), sort_keys=True)`. Qualification
must establish that activity on **each** recorded device resets Mutter idleness in
this authenticated GNOME session. Merely copying hashes or labeling a device as a
keyboard does not establish that evidence. Permission or identity changes invalidate
coverage. Use the implementation's hash serialization exactly; it includes spaces.

Only right-handed logical button mapping is supported; left-handed mode is rejected
before device input. Relative counts are not assumed equal to pixels or to have constant acceleration.
A persistent read-only X11 connection checks each curved waypoint, emits calibrated
relative counts and permits only bounded corrections. An unreached endpoint, changed
display/settings or absent mapping refuses further input. Final native target/window
checks remain mandatory before a click. Calibration must be repeated after relevant
session/layout/pointer changes; no calibration profile ships as qualified.

## Movement boundary

XTEST retains bounded 250ms movement chunks. Selected uinput supports continuous
forward movement **inside one admitted waypoint**: unchanged keys emit no repeated
press, while independent feedback-age (at most two seconds), travel, authorization
and parent-watchdog (1.5 seconds) budgets bound exposure. The controller carries an
absolute travel deadline through keymap and guard latency; delivery cannot rebase it. A blocked capture cannot
renew those budgets. Travel during capture counts in the measured controller.

Turning uses the existing stationary calibration, with forward released first.
Forward-plus-turn steering and continuity across waypoint/chat/receipt boundaries
are not implemented. Every exit releases controls before final position verification
and receipt. Build/instance/character/house, bindings, combat/death/mount, stale or
implausible motion and no progress remain gates. No route becomes live-qualified
from passing simulated tests.

## Verification and rollout

Tests use fake devices/descriptors, clocks and subprocess adapters. They do not
open real event/uinput devices, capture the desktop, contact WoW or trade. They
cover whole-text validation/timing, constant job count, parser allowlists, stationary
clicks, exact coverage/bootstrap, takeover and permission failures, independent
hold expiry, helper disconnect/watchdog teardown, lease ordering and continuous
waypoint behavior. Package-boundary checks build and inspect component wheels/SDISTs.

Deployment remains a separate coordinated package cutover and bounded live
qualification: keyboard readback, idle coverage/takeover, calibrated relative paths,
click spacing, failure cleanup and measured routes. Preserve the working installed
scanner until that cutover is explicitly performed. Actual posting/cancel/mail
qualification and immutable trade authorization remain separate requirements.

Primary transport references: [python-evdev API](https://python-evdev.readthedocs.io/en/latest/apidoc.html),
[systemd seat assignment](https://www.freedesktop.org/software/systemd/man/latest/sd-login.html).
