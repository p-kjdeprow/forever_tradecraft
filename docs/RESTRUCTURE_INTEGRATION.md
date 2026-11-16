# Package cutover implementation

Read RESTRUCTURE_PLAN, RESTRUCTURE_API, CLAUDE and HANDOFF before editing. Parent owns
git, documentation, native operation contracts and integration approval. No deployments,
services, credentials, production DB, installed game files or live game input.

## Host/workspace loop

Luna owns apps/host/, root pyproject.toml and uv.lock, deploy/, tools/, tests/ except
tests/automation/ and tests/packages/, and the original src/ tree for the final removal.
It also owns packages/automation/pyproject.toml, desktop_input.py, desktop_observe.py,
gui/, native/, presence.py, battle_net.py, and NEW config.py and desktop_cli.py there.
Do not edit automation model/runner/ledger/cli, new market/common/scanner trees, addons,
or docs. Changes in tests/packages require a message to parent, not an edit.

1. Move retained scan composition into apps/host/src/crowley_host/scan. Reuse automation
   desktop primitives; host owns scanner-aware policy/actor/observer/loop only. Remove
   unused generic planners and calibration fields with no runtime consumers. Preserve
   tested curved seeded clicks, >=1 second hard interval and input/focus guards.
2. Give automation standalone strict configuration and observation/presence/probe/play
   CLI independent of scanner/catalog/host. Use crowley-desktop for these desktop commands
   and crowley-native for the native tool. crowley-automation remains operation CLI.
3. Switch root to crowley-host 0.2.0 uv workspace using all four packages. Host depends on
   components; components never import host or each other. Set Pyright to all five source
   roots, zero errors, retain useful strict coverage. Preserve independent component CLIs.
4. Update installer/doctor/health/resources/systemd/launcher/docker for actual ownership.
   Installer uses public scanner config renderer and automation config renderer. Market
   image builds only common+market wheels. Backups record actual crowley-market identity.
   Keep applied migration bytes. No namespace aliases or path-through-checkout resources.
5. Switch existing tests/tools imports/resource paths, adapt config fixtures to the owner,
   delete old src/ only after no production consumers remain. Canonical addon resources
   live in scanner package and automation package; coordinate addon deletion with parent.
   Root addon/ can remain temporarily until parent moves final automation resources.

Run uv sync; uv run pytest --ignore=tests/automation --ignore=tests/packages; uv run ruff
check on owned paths; uv run ruff format --check on owned paths; uv run pyright. Report
exact failures, do not erase meaningful invariants to make checks green. No DB uses
without parent-provided disposable URI. No commits/reset/stash or unrelated edits.
Completion means retained runtime actually builds/imports/runs through new package paths.
Reply DONE only when complete, with changed files and real verification results.
