# Fresh native operation boundary

Read overall RESTRUCTURE_PLAN/API, CLAUDE, HANDOFF and client-contracts skill. No real
game input, deployments, service actions, credentials, production DB or real trades.
Parent owns docs/git; native Luna owns ONLY packages/automation/src/crowley_automation/addon/,
packages/common/src/crowley_common/frames.py and tests/automation/test_native*.py plus
tests/automation/native_harness.py. Preserve scanner schema/bytes. Source addon/ is now
frozen historical staging; canonical automation addon must be copied into owned package.

Implement a small explicit native operation UI and observations, not old executor or queue.
No arbitrary Lua/macros, generic RPC, retries, buying/crafting/vendors or hidden effects.

## Staging wire

Chat messages each <=128 UTF-8 bytes. /cauto begin BATCH64 ORDINAL STEP initializes a
fresh staging record; /cauto kind KIND selects one of post_commodity,post_item,
cancel_auction,collect_mail,interact,move_route. /cauto arg FIELD VALUE sends one explicit
action field using exact canonical camelCase names/integers. itemKey can be split with
/cauto key INDEX CHUNK (consecutive INDEX=1..3, <=60 bytes each, max128 total). /cauto
expires EPOCH sets short expiry (max120s); /cauto seal validates all and readies exactly
one primitive; /cauto clear disarms without action. Invalid input disarms; strict exact
field sets, bounds, no permissive tonumber forms. Never execute effects from chat.
Python recomputes action digest from published actual action object (never trust echoed
hash). Publish batchId/ordinal/stepIndex/expiry/action/ready/error and fixed button rect.
An actual hardware OnClick on the visible operation button is the only native call path.
Callback consumes staging once and increments receipt sequence BEFORE protected call.
Recheck expiry, native context, costs/item location/identity/quantity and AH ownership at
the callback. Missing/ambiguous outcomes stay unknown. Never acknowledge success from
API return alone. Client boot change must invalidate all staging and receipt correlation.

## Supported effects and observations

- Strict native PostCommodity(location,durationIndex,quantity,unitPrice) and
  PostItem(location,durationIndex,quantity,totalPrice,totalPrice) after GetItemKeyFromItem,
  full item-link identity, commodity status and exact deposit check against cap. Reject
  unknown identity/variant, insufficient stock, locked/bound/unauctionable item. Select
  a deterministic exact bag slot, never key by name. One call per click.
- Exact owned auction ID CanCancelAuction/GetCancelCost with cost cap, one CancelAuction
  call. Owned state/event and specific listing change establish postcondition.
- One non-COD mail message per click, bounded messageLimit cursor, AutoLootMailItem once;
  distinguish money/attachments/return/delete state and cache refresh. Never pay/send.
- interact and move_route are Python desktop effects; stage their exact action identity
  but do not execute arbitrary native commands. Native snapshot provides movement facts:
  UnitPosition raw x,y,instance; GetPlayerFacing; IsPlayerMoving; player dead/combat/mounted,
  target GUID/name/range, mailbox/AH event-owned open states. Missing facts fail publication
  rather than guessed values.
- /cauto inspect inventory|owned|mail PAGE publishes <=4 strict rows and hasMore. Inspection
  is read-only; unsupported/missing APIs produce explicit error, never guessed empty rows.
- Fresh operation receipt publishes exact batch/ordinal/step and callback delivered,
  postcondition bool or unknown, actionComplete bool or unknown, detail; bind observations
  to actual post baseline and causal event. A delayed unrelated event cannot prove sale.

## Ownership

Optional Crowley_Host addon is owned here too and ships within automation addon resources;
it alone bridges public CrowleyScanner suspend/quiescent/resume to native market ownership.
Automation addon never imports scanner. Without host bridge standalone owns AH naturally;
with bridge, seal requests suspend, readiness waits until scanner quiescent. Release only
after conclusive receipt or explicit clear; uncertainty retains owner and rejects new work.
No polling supervisor framework; tiny explicit bridge methods/events suffice.

Extend automation frame strict schema to version2 with operation, inspection, motion,
mailOpen and ownership fields (scanner remains version1). Bound compressed bytes and
all strings/collections; preserve fail-closed chat/focus observations and independent
boot/sequence checks. Return a precise final wire field/type report for Python backend.
Coordinate changed shared frame shape with parent before finishing; host cutover worker
will adapt old automation fixtures after parent supplies exact final shape.

## Verification

Run uv run pytest tests/automation/test_native*.py -n0 -q, scoped ruff check/format and
Pyright on common frames. Tests must prove no effects during stage/inspect, one hardware
callback, exact API arity/signatures/price units/duration, variant matching, caps, expired
and stale stage, duplicate click, AH/mail close, unrelated events, missing callbacks,
scanner active-query handoff, item/owned/mail drift, strict malformed wire and frame limits.
Native harness is a model, never proof of protected-call behavior on current live build.
Report files, real test output, unresolved client assumptions and exact wire schema.
DONE only when implementation complete. Keep concrete code small; discuss >1000 Lua
runtime lines before adding more, no interface framework.
