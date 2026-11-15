# Refactor execution

Read RESTRUCTURE_PLAN.md in full. User authorizes Luna implementation and Sol review loops,
commits on main, and cleanup of task worktrees and temporary artifacts. No live trading is authorized.
Do not deploy or touch installed game files, credentials, live services, databases, or private evidence.
All implementation workers use gpt-6-luna xhigh; independent reviewers use gpt-6.1-sol xhigh.
Do not commit, reset, stash, or revert another worker's changes. Parent owns git and integration.
Use the current checkout; installed addons are copies, not links into this checkout.

## Wave one ownership

Three disjoint implementation loops run in parallel. No frameworks, compatibility layers, speculative
actions, generalized routing, or unrelated improvements. Prefer deleting entire retired consumers.
Ask parent to resolve cross-scope dependencies. Exact verification commands are in each assignment.

### Market cleanup

Own src/crowley/analysis/, src/crowley/trading/, src/crowley/advisor/, src/crowley/forecast/,
and new src/crowley/market/. Own tests/test_analysis*.py, tests/test_advisor*.py,
tests/test_forecast*.py, tests/test_trading*.py, tests/perf/bench_paper.py.
Do not change server, companion, addon, scan, config, ops, pyproject, tools, or docs.
Extract only the calculations and data access required by trading-summary and recipe-watch.
Delete inactive analysis/rank CLI workflows except helpers actually required by retained outputs.
Remove all advanced advice, simulation, strategy runner, forecasting, paper and backtest code/tests.
Preserve active dashboard semantics, including exact turnover floor and gap rules.

### Addon and observation cleanup

Own addon/Crowley/, addon/Crowley_Data/, addon/Crowley_Automation/ (new), addon/Crowley_Host/ (new),
src/crowley/gui/frames.py, src/crowley/scan/, tools/generate_launch_target.py,
tests/test_addon.py, tests/test_executor.py, tests/wow_harness.py,
tests/test_gui_frames.py, tests/test_scan*.py, tests/test_cold_tier.py,
tests/test_launch_target.py, tests/support/scan_frames.py.
Do not change companion/server/config/ops/pyproject/market modules or other tests/docs.
Read .agents/skills/crowley-client-contracts/SKILL.md.
Delete dormant executor, capture modules, construction/recovery/measurement workflows, and unused
telemetry. Separate scanner and game observations with independent addon lifecycle and schemas;
scanner must work without automation addon and automation observation without scanner/plan.
Preserve active AH loop behavior through explicit integration/public observation.
Remove queue schema generation. Keep generated scan/data schemas and native scan serialization.
Use simple bounded publications and explicit types; do not recreate the retired session stack.

### Transport and server cleanup

Own src/crowley/companion/, src/crowley/server/ except migrations and static/trading-dashboard.html,
src/crowley/store/accounts.py, references.py, queues.py, paper.py;
src/crowley/config/; src/crowley/core/companion_state.py;
tests/test_companion.py, tests/test_server*.py, tests/test_trade_queue.py,
tests/test_account.py, tests/test_reference.py, tests/test_dashboard.py,
tests/support/companion_state.py, tests/support/config.py, tests/config/.
Do not change migrations, analysis/trading/advisor/forecast, addon, scan, gui, tools, ops, pyproject, docs.
Remove advanced dashboard/advice/paper/queue routes and background advice warming. Remove dormant
account/reference/execution uploads and queue fetching/writing from companion. Retain scanner/probe
transport, strict original payload acceptance, ACK bytes, retries/rejections, main dashboard/recipe watch.
Preserve historical tables; remove store readers only if no retained consumers, coordinate with parent.
Remove retired config fields and update affected owned fixtures. Never edit applied migrations.

## Parent integration

Parent owns project metadata, dependency lock, CLI smoke checks, ops/deploy cleanup, documentation,
remaining orphan tests/core modules, package extraction, final verification and git operations.
Before spending Sol review, parent inspects scope and independently reproduces relevant passing tests.
Reviewers are read-only and must report Must-Fix / Should-Fix / Nits and STATUS.
Loop until substantive findings are resolved; do not invent extra features during fixes.

## Gates

1. Baseline preserved; current main-dashboard modifications recorded separately.
2. Wave one retained behavior passes and all three loops have READY Sol reviews.
3. Independent package builds/import boundaries/install checks pass.
4. Fresh scoped automation and its crash/admission/coordination cases pass review and checks.
5. Full retained tests, lint, typing and artifact checks pass on main; temporary artifacts are removed.

Live qualification requiring specific real-gold operations remains separate from code completion.

## Implementation checkpoint

- Main contains the approved planning commit. Application cleanup is still uncommitted until the
  coordinated producer/consumer batch is complete. No changes have been deployed or pushed.
- Original uncommitted turnover code/UI/test changes are preserved in the working cleanup.
- market_cleanup is Luna. Its cleanup passed independent Sol market_review after restoring retained
  item-book and real loader DB tests. It now owns the fresh automation foundation per RESTRUCTURE_API.
- transport_cleanup is Luna. Its cleanup passed independent Sol transport_review. It now prepares
  new common/market/scanner packages per RESTRUCTURE_API without changing original source paths.
- addon_cleanup is Luna and still owns the original addon/frames/scan/harness rewrite. It must finish
  and pass independent Sol review before the package/resource handoff and old import switch.
- market_review and transport_review are read-only Sol workers available for subsequent rounds.
- Parent removed orphan core account/reference/queue modules, volume API-only reporting/projection
  helpers and store readers, unused domain/typed-clock/procfs scaffolding, paper deployment/CLI
  metadata, and abandoned screenshot tooling. Preserved active volume comparison behavior/tests.
- Parent ops integration handles the second canvas, separate addon resource roots, no Queue.lua,
  and removal of paper units. Existing backup type errors were corrected without behavior changes.
- Parent independent checks: market retained suite 83 passed; restored item-book suite 58 passed;
  actual item-book loader DB 1 passed; volume/summary/watch 69 passed; transport suite 65 passed and
  14 DB skips; ops backup 109 passed and 1 skipped; install/doctor 46 passed; migrations on disposable
  Timescale 13 passed. Server DB suite 13 passed, with one cross-addon test blocked on the harness
  rewrite at that point. Re-run after addon work completes.
- The pre-existing old test worktree and its test cache were removed; main is the only worktree.
  A parent-owned disposable tmpfs Timescale test container is active for DB qualification; discover
  its published port with docker rather than using the production database, and remove it afterward.
- Root package metadata, old-to-new import switch, source removal, docs refresh, baseline ratchets,
  final artifact tests, native automation adapters, movement, and live dry-run qualification remain.

Scanner standalone configuration will be its own strict scanner.toml [scanner] with game_root,
wtf_account, character, server_url, poll_seconds, and refresh_minutes. Character preserves the
existing state directory layout. Host installation renders this from its master host contract.
