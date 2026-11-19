# Current handoff

Updated 2026-11-19. Read only linked documents needed for the current task.

## Operator hold

The user explicitly stopped the scanner for Codex quota diagnosis. Host, companion,
health service and health timer are stopped. **Do not resume without a new explicit
user instruction.** WoW was left open. No game input or screenshots were used in
the quota audit. Private evidence: `~/.local/state/crowley/quota-audit-20261119/`.

## Authorization and scope

The user authorized Crowley refactoring, main/origin/main commits and task-artifact
cleanup. No real trade is authorized. Posting/cancellation/mail/movement still need
an exact immutable batch and a separate current-session arm. Buying, vendors and
crafting automation are outside scope. Steam Deck deployment was cancelled.

## Deployed desktop

- Forever build `1.60.1.70959`, target `forever-us-camwow7`, character Dandune,
  Alliance. The installed host owns launch, AH opening and save/ACK reloads.
- Compatible runtime is the 8b755b9 baseline plus the startup observation-retry
  hotfix. Natural-input/uinput and two-hour listing source changes are **not deployed**.
- Catalog/target revision 11: 509 core + 344 cold IDs, six slices; 566/567 queries
  per sweep, 900-second cadence, 750-second cap. Existing IDs were preserved.
- Server image: `sha256:eabc6a0298481938bfcc885706001e84dae29b55ad80c36cc64d692026e61356`.
  Database and mounts were preserved. Source edits alone do not deploy packages.
- Last qualification: sweeps 1011/1012 each queried all 567 items, including all
  176 route dependencies; durations 729/651s, starts 900s apart. These are historical
  results, not evidence of current collection. Read installed state before a cutover.

## Retained functionality

- Main dashboard: Alchemy/Tailoring, up to 25 results per profession, saved skill
  and profit filters; positive margins are displayed without an activity gate.
- Recipe watch remains. User-managed alert work was paused; do not resume it here.
- Merchant's Favor tab: all 28 recipes, positive-margin priority, finite shared-stock
  optimization, daily-writ completion and ownership controls. User progress defaults
  Neutral 2,150, Favor 30, character level 35. No gold or Alchemy-skill cap. See
  [Merchant's Favor](MERCHANT_FAVOR.md).
- Scanner, market and automation are separate packages/addons, composed by host.
  Seller source matches price, uses per-item batches and recycles only managed
  listings. No current-build movement route or live trading session is qualified.

## Efficient operation

The compatible host install now adds only ops CLI, `watch.py` and `usage.py`;
the remaining files in the 27-file package stay on the installed baseline. `doctor`
passes and the stopped-services watch returns exit 3 without restarting anything.
Project Codex settings retain 2K tool-output tokens, compact growing context at 64K,
and cap workers at 2, depth 1; selected models and reasoning levels are unchanged.

Use [the bounded agent workflow](AGENT_WORKFLOW.md). `uv run crowley-ops usage`
reports local token increments; `uv run crowley-ops watch --after-sweep ID --count 2`
is a local read-only wait. Do not keep a model polling unchanged status. Keep full
logs in private artifacts and return compact outcomes.

## Read only when relevant

[Scan operation](REPEATING_SCANS.md) · [Architecture](ARCHITECTURE.md) ·
[Testing](TESTING.md) · [Seller](SELLER.md) · [Input qualification](HUMAN_INPUT.md) ·
[Listing defaults](LISTING_DEFAULTS.md) · [Recovery](SCANNER_RECOVERY_20261118.md).
The complete previous handoff, receipts and decisions are preserved in
[the dated history](history/HANDOFF_20261119.md). Historical state is not current authority.
