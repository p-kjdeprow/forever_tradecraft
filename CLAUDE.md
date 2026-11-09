# Crowley

Private auction house trading platform and addon for WoW Forever (launch 2026-11-04).
**Read `docs/HANDOFF.md` first.** It has the requirements, decisions, research, current
state and roadmap. `docs/ARCHITECTURE.md` is the technical reference.
Shared agent workflows live in `.agents/skills/`; see `AGENTS.md` for when to use them.

## Rules
- **No fallbacks.** Assume one way things work, encode only that, fail loudly, and fix the
  assumption if it's wrong. Record client assumptions in the assumptions table in `docs/ARCHITECTURE.md`.
- Never key items by name; use `itemKey` (`scanformat.py`). Never mix houses.
- Schema changes go in a new `src/crowley/server/migrations/NNN_*.sql`; never edit applied ones.
- Automated clicks are hard-capped at human speed: at most one per second, enforced in code as
  at least 1.0 s between consecutive automated clicks. No config, flag or caller may raise it.
- Every automated click is a full simulated human action: curved, Fitts-timed pointer movement
  with human variability (submovements, endpoint scatter, seeded and recorded), then a real
  button press and release, with target and focus verified before the press. Never warp the
  pointer or move linearly.
- Keep the scan loop small: a new state, mode or safety layer needs a concrete invariant it
  protects or a failure it fixes.
- Ask before committing, pushing, or destroying data.

## Commands
```sh
uv run pytest                     # add CROWLEY_TEST_DATABASE_URL for the DB tests
uv run ruff check . && uv run ruff format --check .
docker compose -f deploy/docker-compose.yml up -d --build
```
