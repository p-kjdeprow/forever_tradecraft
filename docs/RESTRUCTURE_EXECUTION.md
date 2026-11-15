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

Own addon/Crowley/, addon/Crowley_Automation/ (new), addon/Crowley_Host/ (new),
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
