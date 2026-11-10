# Executor telemetry — schema 6

Source contract; native acceptance remains pending launch. The schema-6 producer and consumer must be
deployed together. No schema-5 client may be admitted as a schema-6 client, and
no launch-day live operation is authorized by this document.

## Wire envelope

The existing CRS1 envelope, 128x128 cells, 2048-byte frame, CRC, boot, sequence,
wall time, and uptime rules remain unchanged. Only the validated JSON snapshot
schema changes from `5` to `6`; `schema = 6` is required exactly.

The new top-level key is `executor`. It is validated as an exact object. It is
not optional, and it is not encoded into `render`, `pending`, `loaded`, or any
other existing field.

## Exact bounded object

```json
{
  "availability": "none | invalid | valid",
  "queueId": "",
  "house": "",
  "character": "",
  "publishedArmed": false,
  "sessionArmed": false,
  "buttonVisible": false,
  "buttonName": "",
  "currentSeq": 0,
  "currentKind": "",
  "phase": "none",
  "inFlight": false,
  "clickReady": false,
  "workDone": 0,
  "workTotal": 0,
  "blocked": false,
  "latched": false,
  "ambiguous": false,
  "clickOrdinal": 0,
  "lastClickSource": "none",
  "resultCount": 0,
  "lastResult": {"seq": 0, "kind": "", "status": "none"}
}
```

The integer wire range is the existing protocol `_integer` range `0..0xffffffff`.
The string limits use existing validator limits: `queueId` is at most 192 UTF-8
bytes (the `_uid` limit), `house` and `character` are at most 256 UTF-8 bytes
(the existing `_string` limit), and `buttonName` is at most 64 UTF-8 bytes.
The producer must enforce these limits before publication; they are not claims
about a live client’s UI or character-name limits.

`currentKind` and `lastResult.kind` use the actual `Executor.lua` action set:

```text
buy_commodity, post_commodity, cancel_auction,
vendor_sell, vendor_buy, craft, mail_collect
```

`lastResult.kind = "queue"` is reserved for an invalid queue refusal;
`lastResult.kind = ""` is allowed only with `status = "none"`.

`lastResult.status` uses the actual result statuses:

```text
none, dry_run, started, blocked, succeeded, failed, refused, skipped
```

`phase` is derived from actual `Executor.lua` state transitions. The complete
wire enum is:

```text
none, ready, complete,
await_price, ready_to_confirm, must_cancel, await_purchase,
sell_stacks, sell_wait, await_vendor_buy, await_craft,
mail_items, mail_wait
```

The mapping is deterministic: `none` means no queue or invalid queue, `complete`
means a valid queue has no current action, `ready` means a valid current action
with no Lua phase yet (including in-flight post_commodity/cancel_auction, whose
actual executor sets no phase), and all other values are the literal Lua phase names.
Unknown or future Lua phases must make the frame invalid; they must not be
silently converted to `ready`.

`lastClickSource` is exactly one of `none`, `button`, or `directAPI`. The
production `CrowleyTradeExecutorButton` `OnClick` callback is the only path that
increments `clickOrdinal`; it sets `lastClickSource = "button"`. The existing
test-only `Executor.Click()` path sets `lastClickSource = "directAPI"` and does
not increment `clickOrdinal`. A driver's causal proof therefore cannot be
satisfied by a direct Lua API call.

`buttonName` is `CrowleyTradeExecutorButton` only when the actual button object
exists; otherwise it is empty. `buttonVisible` is the button’s actual
`IsShown()` result. These are addon facts, not a screen-coordinate proof.

`clickReady` is true only when the actual `click()` function can accept a click
for the current action: a visible valid button with a current action and phase
`ready`, `ready_to_confirm`, `must_cancel`, `sell_stacks`, or `mail_items`.
It additionally requires the actual unexpired validAndCurrent identity/scanner/blocked admission, the matching open frame, and the addon click-rate interval. Readiness shares the actual click admission helper. It is false during `await_price`, `await_purchase`, `sell_wait`,
`await_vendor_buy`, `await_craft`, `mail_wait`, and whenever `inFlight` forbids
another click. The phase-less in-flight post/cancel ready frame must have
clickReady=false and a matching current seq/kind started result (latched=false) or blocked result (latched=true); top-level blocked/ambiguous remain false. No other
ready/inFlight combination is admissible. For mail, `mail_items` is intentionally accepted while the
overall action remains `inFlight`; `workDone/workTotal` describes the per-mail
postcondition progress.

## State semantics

| availability | Meaning | Required identity/arm/current fields |
|---|---|---|
| `none` | `CROWLEY_QUEUE` is absent/false and `Executor.State()` is nil | all strings empty; both arms false; button false; current seq 0; phase `none`; result count 0; last result `0/""/none` |
| `invalid` | `Executor` created a rejected queue state | identity strings empty; both arms false; button false; current seq 0; phase `none`; `lastResult = 0/queue/refused` and `resultCount >= 1` |
| `valid` | queue passed `validateQueue` | queueId/house/character are the loaded queue values; `publishedArmed` is `state.queue.armed`; `sessionArmed` is `Executor.SessionArmed()`; current/phase/result fields describe the actual state |

For `valid`, the queue has valid structure; it need not be authorized for the
current character/house. Publish its actual identity even when it differs from
Agent context. A driver must refuse that operation before input; the addon retains
its ordinary wrong-house/wrong-character refusal behavior. Do not turn this
expected refusal into a corrupt collection frame. `publishedArmed` and
`sessionArmed` remain separate observations; a driver must never set either
one or type `/crowley arm`.

`resultCount` is the length of the actual in-memory `execution.results` array;
result details are deliberately excluded from the wire object to keep the frame
bounded. SavedVariables execution details remain the companion/upload audit
record and are not a live click witness.

## Causal click proof

A driver must record the pre-click CRS tuple `(boot, sequence, crc32, queueId,
currentSeq, clickOrdinal)` and a sealed fresh `MeasuredAnchor`. It may dispatch
one physical left click only through the human-pointer path (`gui/pointer.py`, `gui/click_gate.py`).
A click is accepted as delivered only after a fresh CRS frame with the same
boot, client identity and queueId proves:

```text
clickOrdinal = preClickOrdinal + 1
lastClickSource = "button"
```

Bind the action either to unchanged current seq/kind during in-flight progress,
or a new lastResult for the pre-click seq/kind with a terminal status. Terminal
clicks may advance currentSeq; do not reject successful or dry-run advancement.
A valid completed queue uses currentSeq 0/currentKind empty/phase complete;
lastResult still identifies its completed action. Before mail starts, workTotal
reports action.maxMessages so due-time admission does not assume zero work.

The result/current-seq transition is then interpreted using the exact status and
phase enums above. A missing frame, a counter jump greater than one, a directAPI
source, an identity/boot change, or an uncertain `started`/`blocked` state is
nonterminal and blocks retry.



Individual field ceilings do not promise that all maxima compose. The deflated
snapshot must independently occupy 1–2,000 bytes. Oversized publication fails
closed and retains the previous frame; no identity is truncated. Offline proofs
include a canonical queue with six native-format pending UIDs (1,375 compressed
bytes), varied maximum executor metadata (1,955 bytes), and an explicit synthetic
aggregate rejection (2,554 bytes). These are harness measurements, not live facts.

No driver consumes this contract today (the daemon click and mailbox scheduler were deleted on 2026-11-09).
Actual button geometry, UI scale, XTEST callback delivery and protected native API behavior require launch-client
observation.
