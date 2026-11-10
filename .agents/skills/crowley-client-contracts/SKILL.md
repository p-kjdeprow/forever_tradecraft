---
name: crowley-client-contracts
description: Diagnose or change Crowley's native WoW API, serialized scan format, and Lua test harness when validating addon-to-server behavior. Use for client contract fixes and live scan evidence, not ordinary watchlist edits.
---


# Crowley client contracts

Read `docs/HANDOFF.md` for current state and the assumptions table in `docs/ARCHITECTURE.md` for verified client
facts. Preserve the project's no-fallback rule: encode the observed native contract and reject alternatives instead
of hiding a bad assumption behind a second decoder or guessed default.

Exercise captured native file and event fixtures before requesting live input. Freeze a manifest only after the
final edit and checks. Map each assigned race or deadline requirement to its actual regression; a broad green
total does not cover an omitted case. Validate scalar types before sentinel comparisons: Python's `False == 0`
and falsey `None` can bypass malformed-input rejection. Per-field byte ceilings and the aggregate compressed-frame
limit are separate; measure varied real compositions and assert oversize publication fails closed.

Lua API stubs must forward varargs to argument counters; fixed signatures discard
unexpected arguments before the counter can detect them. Assert exact failure
reasons and use distinct simultaneous failures when testing precedence.
Decide SavedVariables-dependent startup in `ADDON_LOADED`, with initialization
order covered by the harness: load files, inject saved values, then emit the event.
File execution alone does not establish that saved values have been restored.
When fence release also rearms a scheduler, test the real lifecycle through its
publication and commit, with the triggering event present. A setter-only test in
an already-ordinary load misses a release refused before lifecycle commit. Keep
failure cases free of live timers; label simulated event order separately from
client-reachable behavior.

- Treat the Lua harness as a model to verify, not proof of native behavior. Check
  real payloads before declaring serialization correct. Native empty Lua tables
  become JSON arrays; nested empty collections matter as much as top-level ones.
  Fixture builders must reproduce that behavior. Keep negative wire fixtures
  malformed so an encoder helper cannot silently make a rejection test pass.
  Model search and sell-search separately; aliasing distinct native methods in a
  mock hid ignored commodity requests. Test API selection with mixed item kinds.
  Inspect returned query keys: zero arguments do not guarantee zero fields, and
  cache warming can change native item levels. Equipment comparison keys and
  ordinary recipe/container search keys follow different native contracts.
- Preserve original failed payload bytes. Validate any internal normalization
  separately from the wire contract and ingestion identity. Never edit a game's
  SavedVariables file to make an upload pass.
- Record actual query event order before changing freshness barriers. Installed
  Auctionator source can establish its call signatures and readiness checks;
  its implementation alone does not establish which events our live query saw.
  Use bounded diagnostics with sequence, item/attempt identity, dispatch, result
  and throttle events, readiness, quantity, and completion state. Diagnostics
  must not send extra queries or change scanner outcomes.
- An observation record does not prove an item was queried. Report attempted,
  unqueried, incomplete, and priced counts distinctly. Count attempts from query
  provenance, not reason codes alone: a sweep stop can mark both the active queried
  item and the unqueried tail with the same reason. Failure counters may include
  that tail and exceed attempted counts; validate them against planned counts.
  A timeout with zero rows
  does not establish an empty market. Inspect diagnostics before repeating a
  long sweep that has already produced only timeouts.
- Scanner completion can transition directly to `scheduled`, not only `ready`.
  Detect a fresh saved UID for the observed run, its pending ownership and terminal
  scanner state; preserve partial results too. Read actual start/finish timestamps
  from the published or saved run; a planned deadline or reused watcher default
  is not an observed start. Use actual finish time for upload latency.
- Verify stages independently: addon loaded, house/plan correct, scan saved,
  original payload accepted, real retained prices stored, acknowledgement loaded.
  Data.lua producer fields also affect the scan loop's reader (`scan/observe.py`).
  Update that reader and its fixtures together, then validate preserved actual producer
  bytes; addon acceptance alone does not prove ACK compatibility.
  A successful probe or zero-row ingestion proves only the stages it exercised.
  A typed `/reload` and logged Enter dispatch do not establish a reload: require
  its actual boot transition and stable SavedVariables containing the exact UID.
  Preserve the new payload after that save flush; a copy made before it contains
  the previous scan. Bind the copy's own hash rather than a later file's hash.
  The frame decoder accepts a new boot only after our own `/reload` (armed
  explicitly); arbitrary resets can hide boot drift.
  Reload writes the previous session before the new session consumes acknowledgements;
  a cleared in-memory queue can still appear in the saved file. Verify the written
  queue after a subsequent normal flush rather than claiming disk cleanup from chat.
  Separate when an acknowledgement was written from when an operator noticed it.
  Check UID and file timestamps; a later periodic refresh is not publication latency.
  Parse queued scan/sweep keys rather than searching for a UID substring in metadata.
  The companion requests ACKs only for currently queued IDs; a later empty ACK list
  after pruning does not prove upload failure or retrospectively prove the loaded ACK.
- Test depth limits at N-1, exactly N with ties, and N+1 distinct prices. A complete
  market with exactly N levels is full/unbounded; bounded coverage requires a
  strictly higher witness. Keep the validator strict when producer metadata is wrong.
- Keep regression tests runnable without the deployed database. Use a disposable
  database for schema-changing tests; never point them at persistent Crowley data.
  Restore any companion service deliberately paused during a failed-upload loop
  after deploying the reviewed correction.

Update the relevant existing skill when live evidence disproves guidance; keep incident dumps and transient client
facts in the handoff.
