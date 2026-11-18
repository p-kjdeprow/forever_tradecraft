# Desktop seller preparation

The user selected this desktop as the only seller host and cancelled the Deck
deployment. Its addon and staged bundle have been removed and absence verified.
WoW was running on the Deck at the final check; an in-memory copy requires the
user's normal reload or exit to unload. No remote game input was sent.
The scanner, companion and website remain running on the desktop.

## Completed checks

- `crowley-ops doctor`: OK; installed seller inspect/preview/start interfaces exist.
- 283 automation tests pass. These cover source/harness behavior, not live trades.
- The coordinated addon's eight files match the installed resource contract,
  including the host-rendered coordination mode. The pinned input tool verifies.
- Supported native observation confirms Dandune, the expected Alliance realm/house,
  build 70959/interface 16001, an open auction house, idle movement, no blocking UI
  and no pending automation action. Ledger status is empty; nothing is armed.
- A draft AH target pins the observed auctioneer GUID/name, character, house,
  build and instance. It round-trips through the public profile validator and is
  retained privately until the seller character is confirmed.

Private native observations, file hashes, target draft, input-helper identity and
Deck uninstall proof: `~/.local/state/crowley/desktop-seller-prep-20261117/`.
No protected action, GUI input, movement or model image was used for this preflight.

## Invoke the CLI from an agent/SSH shell

The desktop unit already resolves its authenticated session. An unrelated shell
can lack XAUTHORITY; do not guess a display/cookie path or weaken the checks.
Use the existing session provider before invoking the installed CLI. For example,
this is a read-only observation through the supported interface:

```sh
uv run python - observe <<'PY'
import os
import shutil
import sys
from crowley_automation.config import load_config
from crowley_automation.desktop_session import DesktopSessionProvider

command = shutil.which("crowley-automation")
assert command is not None, "installed automation CLI is missing"
config = load_config()
session = DesktopSessionProvider(config.display).current()
environment = dict(os.environ)
environment.update(session.environment)
os.execve(command, [command, *sys.argv[1:]], environment)
PY
```

The session provider validates user-manager authority and the live X connection.
Actual operation inputs remain behind the normal GUI lease, presence/focus,
exact identity and immutable-batch/session checks.

## First trial and remaining qualification

The environment is ready to prepare a small listing trial. The user has been asked
for the character, items, available quantity and posting batch size. Do not invent
stock, price floors, deposit/spending caps, duration, expiry or an authorization.
If the selected character differs from the current configured character, qualify
its exact native identity and targets before reusing any preparation.

Use fresh bag and market observations to prepare a concrete immutable batch, inspect
its digest and dry-run it through the supported CLI. Authorization for real effects
and a current-session arm are separate from preparation. Verify an actual posting
receipt before attempting cancellation, and verify the exact returned stock before
reposting. Listed is not sold.

The full unattended seller is not yet qualified: no active target/route profiles
are installed, the current client has not completed protected-operation trials,
and AH/mailbox travel must be measured on the selected character/build. Do not
fabricate profiles or call the complete recycle loop ready. Its initial scope is
commodities; exact one-off item posting is supported separately.
