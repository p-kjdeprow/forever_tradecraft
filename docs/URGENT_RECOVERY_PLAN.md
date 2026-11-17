# Typing and dashboard recovery

Completed 2026-11-17. The user subsequently requested resumption and verification
of the full website. Host and companion are now active. Fresh sweep 906 completed,
uploaded and appeared through the website's automatic refresh.
The user closed WoW normally before the offline repair. No screenshots or game input
were used for that repair; resumed collection uses normal local observation/input.
User-managed alerts remain paused; their unfinished changes are preserved privately.

## Causes and changes

The cold-start refactor captured native and paired scanner screenshots before every
character and launched a guarded Python input child for each character. This made
a 31-character target take about 98 seconds and the 123-character binding about
397 seconds. Text entry now sends the same measured key press/release transitions
through one control child. Full screen validation runs at command admission;
bounded child monitoring checks stop, human presence, maintenance, session, bound
process identity and window ownership/focus without capturing images. A separate
Return action still requires fresh screen checks and exact complete command text.
Cancellation reaps the child and releases only this operation's possibly held keys
before releasing the GUI lease. The standalone trading backend retains its own
per-character guards. There is no new observer thread, service or model runtime.

The latest scanner data was build 1.60.1.70959, but the server had no matching client
export, so trading-summary returned 404. Normal client shutdown finalized the correct
cache. The supported exporter produced 2,282 recipes and 4,556 items, and the normal
transactional loader imported export 5. Historical 70938 facts remain unchanged.
The recipe-watch browser validator also assumed build 70938; it now validates the
build format and requires the response to match the current sweep when available.

## Verification and deployment

- Focused scanner, child-control, dashboard, route and package-boundary suite:
  134 passed. Ruff check/format passed; Pyright reported zero errors.
- Regression checks cover one command child, constant capture count during text
  entry, fresh Return admission, window ownership/focus and cancellation cleanup.
- An isolated no-input benchmark substituted a sealed `/usr/bin/true`: 123 child
  launches became one, with about 0.10 seconds helper overhead. This is not a live
  game typing measurement. On resumed startup, the actual binding action completed
  approximately 10 seconds after the preceding chat action, including observation
  and admission, versus approximately 397 seconds before the repair. This does not
  isolate keystroke-only duration.
- The installed host/scanner/startup path uses local Python, pixel decoding and
  OCR, with no model client or agent launch. Local screen capture does not itself
  incur model tokens. Agent diagnostics and development quota are separate.
- All five installed package namespaces byte-match the coordinated repair wheels;
  `crowley-ops doctor` passes. Host and companion are now active.
- The repaired server image is
  `sha256:e6230b4abd20bd383ff51e2e03abc07eb1548ef4a1375f1c5562253d7f8b5994`.
  Database container and DB/raw mounts are unchanged. Served dashboard bytes match
  source, and trading-summary returns HTTP 200 for build 70959 and sweep 906
  (470 queried, 470 successful). There are zero qualifying opportunities under
  the existing filters; this is an empty result, not a data-load failure.
- Live resumption completed 470/470 queries successfully with 10,825 priced rows.
  A real headless browser automatically advanced from sweep 905 to 906 with matching
  summary/watch identities, fresh scan status, no JavaScript errors and HTTP 200 on
  every API response. Profit preferences persist; invalid house input is rejected.
  No browser screenshots were taken. The host and companion remain running.

Private artifacts and receipts are under
`~/.local/state/crowley/urgent-recovery-20261117/`; the genuine export is under
`~/.local/state/crowley/dashboard-recovery-20261117/`. The previous server image
and coordinated rollout evidence are retained for rollback. Luna workers hit their
usage limit; the parent finished and checked the simpler final batching design.
Do not describe that final design as independently reviewed by Sol.
Resumption and browser receipts are under `~/.local/state/crowley/resume-20261117/`.
