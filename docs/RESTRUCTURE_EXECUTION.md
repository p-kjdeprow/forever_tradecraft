# Refactor completion record

Updated 2026-11-15. The user authorized parallel Luna implementation / independent Sol review
loops, landing changes on main, and cleanup of task worktrees and temporary artifacts. No live
trade, deployment, production database operation or installed game-file change was performed.
Read RESTRUCTURE_PLAN.md for the approved deletion inventory and acceptance requirements.

## Delivered architecture

- crowley-common owns pure identity/scan/frame contracts, selected target data and shared private
  filesystem helpers.
- crowley-market owns retained ingestion, history, main dashboard, recipe watch and backup/restore.
- crowley-scanner owns scanner configuration, companion/upload/ACK state and Crowley/Crowley_Data.
- crowley-automation owns independent desktop/native observation/input, exact operation batches,
  durable ledger, concrete execution, measured movement and its separate addon.
- crowley-host composes the components for installation, diagnosis and scan-aware maintenance.

Scanner, market and automation never import each other's implementation. Each component builds
and installs independently. Only host composes scanner and automation; the optional in-game host
bridge coordinates exclusive AH use. Automation never resolves a scanner catalog or scan plan.
The old namespace, duplicate resource trees and deprecated command aliases are removed.

Deleted inactive products include advanced dashboard/advice/forecast views, paper/backtests,
old executor and remote queues, Queue.lua, account/reference capture and dormant lifecycle stacks.
Historical SQL tables remain; all 12 applied migration files preserve their exact original bytes.
Existing exact six-units/hour dashboard behavior and retained scan/ACK semantics are preserved.

Fresh automation implements commodity/item listing, cancellation, bounded non-COD mail,
known-location interaction and measured routes. It uses exact immutable batch authorization,
expiring current-boot arms, causal per-primitive receipts, persisted progress and an uncertainty
interlock. No buying, vendors, crafting, arbitrary Lua, retry framework, plugin bus or second
supervisor was added. Host maintenance checks the existing runner/ledger and shared GUI lease.
Every click retains curved seeded pointer movement, real press/release and the global 1-second gate.

## Implementation and review

All implementation/fix rounds used gpt-6-luna at xhigh; independent read-only reviews used
gpt-6.1-sol at xhigh. Parent owned integration, verification, docs and git. The three cleanup
loops, package extraction, strict operation foundation, measured movement, concrete backend,
native actions and final host coordination all reached Sol STATUS: READY with no outstanding
Must-Fix or Should-Fix findings. Reviews did not claim actual protected-call qualification.

Concrete review fixes included native SavedVariables restore ordering, exact staging/readback,
item-link identity and cursor scale; sequential batch ownership and fresh motion samples;
stop/expiry/focus checks after blocking calls; causal interaction completion; durable dispatch
recovery; and launcher-child admission under the GUI lease. Regression tests cover these failures.
No active worker owns further source changes.

## Final parent checks

- Default full suite: 1203 passed, 31 skipped. DB-dependent checks skip without an explicit URL.
- Disposable tmpfs Timescale: 37 migration/server/item-loader/house/client-data checks passed,
  plus 1 real packaged-migration backup inventory check passed, all serially.
- Ruff check and format: pass. Pyright: zero errors. Generated selected target: check passed.
- Package qualification is included in the full suite: wheel/SDIST builds, an isolated host SDIST
  rebuild without workspace sources, exact package resources, all five installed wheels, independent
  imports outside the repository and all eight command help checks.
- All 12 applied SQL migrations retain exact bytes from before the move.
- The stale test worktree was removed; main is the only worktree. Task-owned build/test resources
  are removed at completion. Pre-existing private archives, wheels and unrelated temp data stay intact.

The implementation scopes in RESTRUCTURE_API/INTEGRATION/NATIVE/MOVEMENT/BACKEND/COORDINATION
are retained as design/review specifications; their temporary worker ownership sections are historical.
README, ARCHITECTURE, HANDOFF, TESTING and AUTOMATION describe the final supported interfaces.

## Qualification boundary

Source implementation and artifact verification are complete. Nothing was deployed and no live
operation was sent. Current-client protected posting/cancellation/mail behavior and current-build
route/target measurements still require specific live qualification. No measured profile is shipped
as qualified. An agent can inspect/prepare through the documented CLI; real effects require the
concrete unchanged batch authorization, current-session arm and qualified native assumptions.
