# Crowley

Private auction house trading platform and addon for WoW Forever (launch 2026-11-04).
**Read `docs/HANDOFF.md` first.** It has the requirements, decisions, research, current
state and roadmap. `docs/ARCHITECTURE.md` is the technical reference.
Shared agent workflows live in `.agents/skills/`; see `AGENTS.md` for when to use them.

## Rules
- **No fallbacks.** Assume one way things work, encode only that, fail loudly, and fix the
  assumption if it's wrong. Record client assumptions in the assumptions table in `docs/ARCHITECTURE.md`.
- Never key items by name; use `itemKey` (`scanformat.py`). Never mix houses.
- Schema changes go in a new `packages/market/src/crowley_market/server/migrations/NNN_*.sql`; never edit applied ones.
- Automated clicks are hard-capped at human speed: at most one per second, enforced in code as
  at least 1.0 s between consecutive automated clicks. No config, flag or caller may raise it.
- When pointer travel is needed, use curved, Fitts-timed movement with bounded variability
  (submovements, endpoint scatter, seeded and recorded). If already inside the verified safe
  target, keep the pointer stationary. Every automated click still requires a real press and
  release with fresh target/focus checks and the shared rate cap. Never warp or move linearly.
- Keep the scan loop small: a new state, mode or safety layer needs a concrete invariant it
  protects or a failure it fixes.
- Follow [the bounded agent workflow](docs/AGENT_WORKFLOW.md): local waits and checks,
  compact artifacts, fresh task context, and no repeated review/testing without new evidence.
- Ask before committing, pushing, or destroying data.

## Commands
```sh
uv run pytest                     # add CROWLEY_TEST_DATABASE_URL for the DB tests
uv run ruff check . && uv run ruff format --check .
crowley-ops image build --source "$PWD"
# Review/install a coordinated package set before changing deployed services.
```
