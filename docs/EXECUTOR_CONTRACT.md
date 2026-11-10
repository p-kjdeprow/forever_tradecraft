# Trade executor contract (queue → addon → results)

Parent-authored contract shared by the server/companion queue pipeline and the addon executor. Neither side
changes it alone. Money is integer copper; items are numeric item IDs (never names).

## Safety model
- Nothing trades unless **both** arms are set: the published queue has `armed = true`, **and** someone typed
  `/crowley arm <queueId>` in the current game session (session-local; cleared by any reload or logout). Otherwise
  every click is a **dry run**: the executor resolves and validates the action, shows "dry run: would …", records a
  `dry_run` result and makes no protected call.
- One protected API call per hardware click on the executor button. The button never chains protected calls.
- Automated clicking (when it exists) is capped at one click per second (CLAUDE.md, enforced in `gui/click_gate.py`); the addon
  additionally refuses a click that arrives less than 1.0 s after the previous executor click (`too_fast`).
- Any mismatch fails closed: the action is refused with a reason code and the executor stops at it until skipped or
  the queue is replaced. A `too_fast` click is a refusal like any other (it stops the action).
- A refusal while the action is **in flight** (after its first protected call, before a terminal result) records a
  nonterminal `blocked` event and **latches** the action: no further protected call for it is allowed, except the single
  `CancelCommoditiesPurchase` click of a started commodity purchase (phase `must_cancel`). A latched action ends only by a
  terminal result (e.g. the cancel completing → `refused`) or by replacing the queue.
- Both arms are re-checked immediately before **every** protected call, including later clicks of a multi-click action
  (confirmation, further vendor stacks, further mail messages). Disarming mid-action stops it.
- Calls whose protection status is unknown are treated as protected: they get their own click.
- AH actions are refused (`scanner_active`) while a Crowley sweep is in progress; if the scanner's state cannot be read,
  they are refused as well.
- `/crowley skip` is refused while an action is in flight (between a started call and its terminal result).
- After a reload, results already persisted for the same `queueId` are reconciled **across all seqs** before anything runs:
  a seq that has any `started` result with no later terminal result for that seq (`succeeded`, `failed`, `refused`,
  `skipped`; `blocked` events are not terminal) is ambiguous, and any ambiguous seq anywhere in the queue blocks the whole
  queue (`ambiguous_history`) until it is replaced; a seq with a terminal `succeeded` is not repeated; `dry_run` results do
  not count as completed.

## Queue file
The companion writes `Interface/AddOns/Crowley_Data/Queue.lua`, loaded by `Crowley_Data.toc` after `Data.lua`,
assigning one global (never a key inside `CROWLEY_DATA`, whose keys the decoder and companion read strictly):

```lua
CROWLEY_QUEUE = {
  format = 1,
  queueId = "<house>-<character>-<generatedAt>-<4 hex>",  -- unique
  generatedAt = <epoch seconds>,
  expiresAt = <epoch seconds>,          -- executor refuses after this time (`expired`)
  house = "<house key>",                 -- must equal ns.Context.House().key (`house_mismatch`)
  character = "<name>",                  -- must equal UnitName("player") (`character_mismatch`)
  armed = false,                         -- server-side arm (see safety model)
  actions = {                            -- array, executed in order
    { seq = 1, kind = "buy_commodity",  itemID = 2589, quantity = 20, maxUnitPrice = 25 },
    { seq = 2, kind = "post_commodity", itemID = 2589, quantity = 20, unitPrice = 30, durationHours = 12 },
    { seq = 3, kind = "cancel_auction", auctionID = 123456789 },
    { seq = 4, kind = "vendor_sell",    itemID = 2589, quantity = 20 },
    { seq = 5, kind = "vendor_buy",     itemID = 3371, quantity = 5 },     -- whole vendor bundles
    { seq = 6, kind = "craft",          recipeID = 2963, times = 1 },      -- recipeID = spell ID
    { seq = 7, kind = "mail_collect",   maxMessages = 10 },
  },
}
```
`Queue.lua` with no queue assigns `CROWLEY_QUEUE = false`. Field rules: `seq` 1..n contiguous; positive integers
everywhere; `durationHours` ∈ {12, 24, 48}; at most 50 actions; `expiresAt > generatedAt`.

## Execution per kind (one hardware click each unless stated)
| kind | requires open | click behaviour |
|---|---|---|
| buy_commodity | AH | preflight: Start, Confirm and Cancel functions all exist. Click 1: `C_AuctionHouse.StartCommoditiesPurchase(itemID, quantity)` → phase `await_price` (no click accepted). On `COMMODITY_PRICE_UPDATED(unitPrice, totalPrice)` validate both as non-negative integers with `totalPrice == unitPrice × quantity` or ≥ it (record both): if `unitPrice > maxUnitPrice` → phase `must_cancel`, next click: `CancelCommoditiesPurchase(itemID)`, result `refused` (`price_above_max`); else phase `ready_to_confirm`, next click: `ConfirmCommoditiesPurchase(itemID, quantity)` → phase `await_purchase`; success only on `COMMODITY_PURCHASE_SUCCEEDED` in `await_purchase`, failure on `COMMODITY_PURCHASE_FAILED` |
| post_commodity | AH | find a bag stack of `itemID` with ≥ quantity (`C_Container`), `ItemLocation:CreateFromBagAndSlot`, check `C_AuctionHouse.IsSellItemValid`, record `CalculateCommodityDeposit`; click: `C_AuctionHouse.PostCommodity(location, duration, quantity, unitPrice)` with duration 1/2/3 for 12/24/48 h; success on `AUCTION_HOUSE_AUCTION_CREATED` |
| cancel_auction | AH | `C_AuctionHouse.CanCancelAuction(auctionID)` must be true and `GetCancelCost(auctionID)` must exist and return a non-negative integer (recorded); click: `CancelAuction(auctionID)`; success on `AUCTION_CANCELED` for that auctionID |
| vendor_sell | merchant | choose bag stacks of `itemID` summing exactly to quantity (no splitting; bounded search; refuse `stack_mismatch` otherwise); one click per stack: re-read the slot immediately before `C_Container.UseContainerItem(bag, slot)` and refuse (`slot_changed`) unless it still holds `itemID` with the planned count; a stack is sold when the slot no longer holds it |
| vendor_buy | merchant | find merchant index with `GetMerchantItemID(i) == itemID`; click: `BuyMerchantItem(index, quantity)` |
| craft | profession window (`C_TradeSkillUI.IsTradeSkillReady()`) | recipe learned (`C_TradeSkillUI.GetRecipeInfo(recipeID).learned`) and `GetCraftableCount(recipeID) >= times`; click: `C_TradeSkillUI.CraftRecipe(recipeID, times)` |
| mail_collect | mailbox | one click per message: `AutoLootMailItem(index)` (money and items), up to `maxMessages` |

Vendor buy, craft and mail actions stay in flight after their call until an observable postcondition (bag count
increased by the bought/crafted quantity, message removed or emptied) or a `UI_ERROR_MESSAGE`; they never succeed
synchronously.

Assumption: these calls behave as their names suggest in the Forever client (presence verified by probe 1792639338;
behaviour is not). Every assumption is confirmed live in dry run first, then one armed action at a time.

## Results
The addon appends to `CrowleyDB.executions[queueId] = { queueId, house, character, results = { ... } }`, one result per
attempted action: `{ seq, kind, status, at, detail }` with `status` ∈ {`dry_run`, `started`, `blocked`, `succeeded`,
`failed`, `refused`, `skipped`} (`blocked` is a nonterminal event with a `reason`) and `detail` holding observed values (unitPrice/totalPrice for buys, deposit for posts, cancel cost,
bag/slot used, merchant index, error event text). The companion uploads each queue's execution record once it changes;
the server stores it and reconciles it against the queue.

## Live telemetry

The addon and the frame decoder (`gui/frames.py`) use strict [telemetry schema 6](EXECUTOR_TELEMETRY.md).
The executor publishes actual queue identity, separate published/session arm bits,
button visibility/readiness, per-message progress, latest result and a button-only
click ordinal. A direct Lua API call cannot establish hardware callback delivery.
Wrong queue identity stays visible as a refusal fact; operation admission rejects
it before input. Schema 5 frames are refused by the schema 6 consumer.

No program drives the executor button today (the session daemon's guarded click and mail loop were deleted on
2026-11-09; see [HANDOFF.md](HANDOFF.md)). No telemetry projection alone authorizes arming, a protected call, or
retry after an uncertain effect. A future driver must follow the causal click proof in
[EXECUTOR_TELEMETRY.md](EXECUTOR_TELEMETRY.md) and the click rules in `CLAUDE.md`.
