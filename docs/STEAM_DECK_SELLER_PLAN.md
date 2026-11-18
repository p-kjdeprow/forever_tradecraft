# Steam Deck seller preparation

The user wants the listing system on the Steam Deck, with the current machine
retaining the scanner and website. Remote access and deployment preparation are
authorized; no trading policy, protected trial or game login is armed by this plan.

## Verified preparation

- SSH works using the existing dedicated key and an already-trusted host identity.
- The Deck runs SteamOS 3.9.5 in a Gamescope Gaming Mode session, with Xwayland.
- WoW Forever is installed and its active build matches 1.60.1.70959.
- WoW was not running when installation was admitted. No game was launched.
- The standalone `Crowley_Automation` addon 0.3.0/schema 3 is installed, with all
  eight installed files verified against the reviewed package resources.
- Matching common/automation 0.2.0 wheels and a source manifest are staged on the
  Deck. No desktop runtime, background service, scanner addon or host addon was
  installed or started. No policy or execution session was armed.
- Existing scanner/website operation on the current machine is unchanged.

The bundle contains source revision `0da6cbd`. Machine paths, hashes, remote
inventory and the exact deployment receipt remain private under
`~/.local/state/crowley/steamdeck-prep-20261117/`. The remote preparation receipt
identifies the installed addon and staged bundle. No screenshots or GUI input
were used. No keys, account files or database/API credentials were copied.

## Why the current controller cannot simply be started

The current observation path uses GNOME Screenshot, and physical-presence checks
use Mutter IdleMonitor. Neither GNOME service is present on this Gaming Mode
session. User-manager environment does not publish the DISPLAY/XAUTHORITY pair
that the existing session provider requires. System xdotool is present, but it
does not replace Crowley's separately built and verified private input tool.
The Deck also has no Crowley CLI, uv, or wmctrl in its current command path.
Python 3.14.6 is present; dependencies and input-tool build still need qualification.

Do not replace these failed requirements with guessed display variables, false
idleness, disabled focus checks, system xdotool or a fake GNOME service. SSH access
is management access; it does not establish safe interaction with the game.

## Bounded next steps

1. Establish account/session ownership before logging into WoW. If the Deck uses
   the scanner's account, plan a deliberate handoff rather than displacing the
   live scanner. The user has been asked which account arrangement applies.
2. Implement and qualify the actual Gamescope capture, keyboard-focus, session
   and physical-presence path using existing desktop interfaces. Controller,
   touchscreen and other physical takeover must stop new input. Keep all
   classification/decoding local; no model or remote-image loop is needed.
3. Install an isolated user-space automation runtime and build/verify its private
   input helper. Keep services and trading inactive during setup. Do not port
   Battle.net cold-start or change the Deck OS as a prerequisite for the first
   AH test: begin with a manually opened, positively verified game and AH.
4. Verify the loaded addon, exact character/realm/house/build, inventory, owned
   listings and native button geometry. Use supported observation and dry-run
   commands; installation alone is not native-call qualification.
5. Prepare one exact small commodity listing with explicit stock, price, duration
   and cost limits. Inspect the immutable digest, obtain the required specific
   authorization and current-session arm, then qualify the posting receipt.
   Qualify cancellation and the exact returned quantity separately.
6. Measure current Deck AH/mailbox targets and routes before any automated travel.
   No qualified current-build profiles ship. Then exercise a bounded full recycle
   before enabling the continuous seller with explicit stock/expiry/spending caps.

The recurring seller currently supports commodities; single exact non-commodity
posts exist as a primitive, but continuous equipment/variant selling is additional
work. Preserve matched pricing, one fresh batch per item per minute during tied
competition, and cancellation only of managed listings of the exact item.

## Making the AH workflow a user-facing addon

The native AH execution layer already is an addon. `Crowley_Automation` implements
inventory/market/owned/mail observations, staged post/cancel/collect actions,
visible action buttons and native receipts. The Python seller owns repeat-selling
decisions and durable session/stock/spending state. See [SELLER](SELLER.md) and
[AUTOMATION](AUTOMATION.md).

A user-facing addon needs item selection and settings, batch/price/duration caps,
run/pause controls, a pending-action view, and clear listing/return/error status.
Periodic market monitoring and preparation can be driven by addon events/timers;
the current protected execution path deliberately runs from a real button click.
An attended addon can expose those actions directly. Do not promise autonomous
timer-driven posting, cancellation or movement before checking this custom
client's actual permitted execution contexts.

For the first unattended Deck deployment, retain the existing Python controller
and ledger as the single authority, with the addon executing native actions and
an explicit Gamescope backend supplying qualified input. This reuses implemented
business rules rather than creating a second competing controller. If a later
addon-only design is feasible, move policy/session ownership and recovery tests
together, including uncertain-effect handling; do not duplicate live authorities.
SSH is for deployment and narrow status/receipt retrieval, not a shell call or
model interpretation for every character, frame or market decision.

Valve documents SSH-backed deployment in its
[SteamOS development guide](https://partner.steamgames.com/doc/steamhardware/loadgames?language=english)
and the standard KDE Desktop Mode in its
[Desktop FAQ](https://help.steampowered.com/en/faqs/view/671A-4453-E8D2-323C/).
The actual Deck inventory above establishes that this target currently uses Gaming Mode.
