local _, ns = ...

-- The executor is deliberately a separate consumer of the companion queue. It
-- never reads or writes scanner data, CROWLEY_DATA, or scanner state except for
-- the fail-closed AH exclusion checked immediately before an AH transaction.
local Executor = {}
ns.Executor = Executor

local MAX_SELECTION_STATES = 10000
local MAX_SAFE = 9007199254740991
local MAX_U32 = 4294967295
local DURATION = { [12] = 1, [24] = 2, [48] = 3 }

local state
local sessionArmed = false
local button
local frameOpen = { ah = false, merchant = false, mailbox = false, profession = false }

local function validateQueue(queue)
  if not ns.ExecutorState then return false, "executor state module is unavailable" end
  if not ns.GeneratedWireSchemas then return false, "generated wire schemas are unavailable" end
  return ns.ExecutorState.ValidateQueue(queue, ns.GeneratedWireSchemas)
end

local function now() return ns.ExecutorNative.Time() end
local function currentAction() return state and state.queue.actions[state.index] or nil end

local function currentSummary(action)
  if not action then return "complete" end
  if action.kind == "buy_commodity" or action.kind == "post_commodity" or action.kind == "vendor_sell" or action.kind == "vendor_buy" then
    return ("%s item %d x%d"):format(action.kind, action.itemID, action.quantity)
  elseif action.kind == "cancel_auction" then
    return ("cancel_auction %d"):format(action.auctionID)
  elseif action.kind == "craft" then
    return ("craft recipe %d x%d"):format(action.recipeID, action.times)
  end
  return ("mail_collect up to %d"):format(action.maxMessages)
end

local function actionFrame(kind)
  if kind == "buy_commodity" or kind == "post_commodity" or kind == "cancel_auction" then return "ah" end
  if kind == "vendor_sell" or kind == "vendor_buy" then return "merchant" end
  if kind == "mail_collect" then return "mailbox" end
  return "profession"
end

local function isFrameOpen(kind)
  local required = actionFrame(kind)
  if required == "ah" then return frameOpen.ah or ns.ahOpen == true end
  if required == "profession" then return ns.ExecutorNative.TradeSkillReady() end
  if required == "mailbox" then
    -- MAIL_CLOSED is not reliable on the installed client.  The native frame
    -- visibility is the admission fact; the event flag is retained only as
    -- diagnostic state and is deliberately not consulted here.
    return ns.ExecutorNative.MailFrameShown()
  end
  return frameOpen[required] == true
end

local function updateButton()
  if not button or not state or not state.valid then return end
  local action = currentAction()
  if not action then ns.ExecutorNative.SetButtonShown(button, false) return end
  ns.ExecutorNative.SetButtonText(button, "Crowley: " .. currentSummary(action))
  ns.ExecutorNative.SetButtonShown(button, isFrameOpen(action.kind))
end

local function ensureExecution()
  if not ns.db then return nil end
  ns.db.executions = ns.db.executions or {}
  local id = state.queue.queueId
  local execution = ns.db.executions[id]
  if type(execution) ~= "table" then
    execution = { queueId = id, house = state.queue.house, character = state.queue.character, results = {} }
    ns.db.executions[id] = execution
  else
    execution.queueId, execution.house, execution.character = id, state.queue.house, state.queue.character
    execution.results = execution.results or {}
  end
  state.execution = execution
  return execution
end

local function appendResult(seq, kind, status, detail)
  local execution = ensureExecution()
  if not execution then return nil end
  local result = { seq = seq, kind = kind, status = status, at = now(), detail = detail or {} }
  execution.results[#execution.results + 1] = result
  return result
end

local function printDryRun(action) ns.Print("dry run: would " .. currentSummary(action)) end

local function advance()
  state = ns.ExecutorState.Reduce(state, { type = "advance" })
  updateButton()
end

local function reduce(event)
  state = ns.ExecutorState.Reduce(state, event)
end

local function persistFinishedResult(status, detail)
  local results = state.execution and state.execution.results
  if not results or not state.result then return end
  for index = #results, 1, -1 do
    local result = results[index]
    if result.status == "started" and result.seq == state.result.seq and result.kind == state.result.kind then
      result.status, result.at, result.detail = status, now(), detail or result.detail or {}
      return
    end
  end
end

local function refuse(reason, detail, stop)
  local action = currentAction()
  if action and state.inFlight then
    local blockedDetail = type(detail) == "table" and detail or { reason = reason }
    blockedDetail.reason = blockedDetail.reason or reason
    appendResult(action.seq, action.kind, "blocked", blockedDetail)
    reduce({
      type = "blocked", latched = true, blocked = state.blocked,
      cancelReason = action.kind == "buy_commodity" and reason or nil,
      phase = action.kind == "buy_commodity" and "must_cancel" or nil,
    })
    updateButton()
    ns.Print("executor blocked: " .. tostring(reason))
    return nil
  end
  if action and state.ambiguous then
    -- An ambiguous persisted history must stay ambiguous across reloads, so a click on it is
    -- a nonterminal event, never a terminal refusal that reconciliation could treat as resolution.
    appendResult(action.seq, action.kind, "blocked", { reason = reason })
    reduce({ type = "blocked", latched = state.latched, blocked = state.blocked })
    updateButton()
    ns.Print("executor blocked: " .. tostring(reason))
    return nil
  end
  local result = appendResult(action and action.seq or 0, action and action.kind or "queue", "refused", detail or reason)
  reduce({ type = "blocked", latched = state.latched, blocked = action ~= nil and stop ~= false or state.blocked })
  updateButton()
  ns.Print("executor refused: " .. tostring(reason))
  return result
end

local function scannerAdmission(action)
  if not action or actionFrame(action.kind) ~= "ah" then return true end
  if not ns.Scanner or type(ns.Scanner.InProgress) ~= "function" then return false, "scanner_state_unavailable" end
  local ok, active = pcall(ns.Scanner.InProgress)
  if not ok then return false, "scanner_state_unavailable" end
  if active == true then return false, "scanner_active" end
  return true
end

local function validAndCurrent()
  if not state or not state.valid then return false, "queue_invalid" end
  if not currentAction() then return false, "complete" end
  if ns.ExecutorNative.Time() >= state.queue.expiresAt then return false, "expired" end
  local ok, house = pcall(ns.Context.House)
  if not ok or type(house) ~= "table" or house.key ~= state.queue.house then return false, "house_mismatch" end
  if ns.ExecutorNative.UnitName("player") ~= state.queue.character then return false, "character_mismatch" end
  local scannerOK, scannerReason = scannerAdmission(currentAction())
  if not scannerOK then return false, scannerReason end
  if state.latched and not (state.inFlight and state.phase == "must_cancel" and not state.cancelAttempted) then
    return false, "blocked"
  end
  if state.blocked then return false, state.ambiguous and "ambiguous_history" or "blocked" end
  return true
end

local function isArmed() return state and state.queue.armed == true and sessionArmed == true end

-- This is intentionally the only path to a protected API. The arm and AH
-- exclusion checks are adjacent to the call, so later clicks cannot inherit
-- a stale admission from an earlier click.
local function protected(name, fn, ...)
  local action = currentAction()
  if not isArmed() then return false, "not_armed" end
  local scannerOK, scannerReason = scannerAdmission(action)
  if not scannerOK then return false, scannerReason end
  if state.latched and not (state.inFlight and action and action.kind == "buy_commodity" and
      name == ns.ExecutorNative.AuctionAction("CancelCommoditiesPurchase") and state.phase == "must_cancel" and not state.cancelAttempted) then
    return false, "blocked"
  end
  if type(fn) ~= "function" then return false, name .. " unavailable" end
  fn(...)
  return true
end

local function finishStarted(status, detail)
  local result = ns.Util.Copy(state.result)
  result.status, result.at, result.detail = status, now(), detail or result.detail or {}
  persistFinishedResult(status, result.detail)
  reduce({ type = "finished", result = result, blocked = status ~= "succeeded" })
  if status == "succeeded" then advance() else updateButton() end
end

local function beginStarted(detail)
  local result = appendResult(currentAction().seq, currentAction().kind, "started", detail)
  reduce({ type = "started", result = result })
end

local function bagSlots(itemID)
  local slots = {}
  local countSlots, itemInfo = ns.ExecutorNative.ContainerFunction("GetContainerNumSlots"), ns.ExecutorNative.ContainerFunction("GetContainerItemInfo")
  if not countSlots or not itemInfo then return slots end
  for bag = 0, 4 do
    local count = countSlots(bag) or 0
    for slot = 1, count do
      local info = itemInfo(bag, slot)
      local id = info and info.itemID
      local quantity = info and (info.stackCount or info.quantity or info.count)
      if id == itemID and ns.Util.IsInteger(quantity, 1, MAX_SAFE) then
        slots[#slots + 1] = { bag = bag, slot = slot, quantity = quantity, itemID = itemID }
      end
    end
  end
  return slots
end

local function exactBagSelection(itemID, wanted)
  local slots = bagSlots(itemID)
  local total = 0
  for _, slot in ipairs(slots) do total = total + slot.quantity end
  if total < wanted then return nil, "missing_bag_stack" end
  local selected, memo, visited = {}, {}, 0
  local function search(index, remaining)
    if remaining == 0 then return true end
    if index > #slots then return false end
    local key = index .. ":" .. remaining
    if memo[key] ~= nil then return memo[key] end
    visited = visited + 1
    if visited > MAX_SELECTION_STATES then memo[key] = false return false end
    for i = index, #slots do
      if slots[i].quantity <= remaining then
        selected[#selected + 1] = slots[i]
        if search(i + 1, remaining - slots[i].quantity) then memo[key] = true return true end
        selected[#selected] = nil
      end
    end
    memo[key] = false
    return false
  end
  if not search(1, wanted) then return nil, "stack_mismatch" end
  return selected
end

local function slotMatches(planned)
  local itemInfo = ns.ExecutorNative.ContainerFunction("GetContainerItemInfo")
  if not itemInfo then return false end
  local info = itemInfo(planned.bag, planned.slot)
  local quantity = info and (info.stackCount or info.quantity or info.count)
  return info and info.itemID == planned.itemID and quantity == planned.quantity
end

local function slotSold(planned)
  local itemInfo = ns.ExecutorNative.ContainerFunction("GetContainerItemInfo")
  if not itemInfo then return false end
  local info = itemInfo(planned.bag, planned.slot)
  return not info or info.itemID ~= planned.itemID
end

local function observeVendorSell()
  if not state or not state.inFlight or state.phase ~= "sell_wait" then return end
  local planned = state.stacks[state.stackIndex]
  if not planned or not slotSold(planned) then return end
  if state.stackIndex >= #state.stacks then
    finishStarted("succeeded", state.result.detail)
  else
    reduce({ type = "sell_progress", stackIndex = state.stackIndex + 1 })
    updateButton()
  end
end

local function bagCount()
  local countSlots, itemInfo = ns.ExecutorNative.ContainerFunction("GetContainerNumSlots"), ns.ExecutorNative.ContainerFunction("GetContainerItemInfo")
  if not countSlots or not itemInfo then return nil end
  local total = 0
  for bag = 0, 4 do
    for slot = 1, (countSlots(bag) or 0) do
      local info = itemInfo(bag, slot)
      local quantity = info and (info.stackCount or info.quantity or info.count)
      if ns.Util.IsInteger(quantity, 0, MAX_SAFE) then total = total + quantity end
    end
  end
  return total
end

local function bagItemCount(itemID)
  local countSlots, itemInfo = ns.ExecutorNative.ContainerFunction("GetContainerNumSlots"), ns.ExecutorNative.ContainerFunction("GetContainerItemInfo")
  if not countSlots or not itemInfo then return nil end
  local total = 0
  for bag = 0, 4 do
    for slot = 1, (countSlots(bag) or 0) do
      local info = itemInfo(bag, slot)
      local quantity = info and (info.stackCount or info.quantity or info.count)
      if info and info.itemID == itemID and ns.Util.IsInteger(quantity, 0, MAX_SAFE) then total = total + quantity end
    end
  end
  return total
end

local function mailMessageEmpty(index)
  local header = ns.ExecutorNative.GlobalFunction("GetInboxHeaderInfo")
  if not header then return false end
  local ok, _, _, _, _, money, _, _, hasItem = pcall(header, index)
  if not ok then return false end
  if money == nil and hasItem == nil then return false end
  return ns.Util.IsInteger(money or 0, 0, MAX_SAFE) and (money or 0) == 0 and hasItem ~= true
end

local function observePostcondition()
  if not state or not state.inFlight or not state.result then return end
  local phase = state.phase
  if phase == "await_vendor_buy" or phase == "await_craft" then
    local after = state.expectedItemID and bagItemCount(state.expectedItemID) or bagCount()
    if after and after >= state.beforeBagCount + state.expectedBagCount then finishStarted("succeeded", state.result.detail) end
  elseif phase == "mail_wait" then
    local inboxCount = ns.ExecutorNative.GlobalFunction("GetInboxNumItems")
    if inboxCount then
      local after = inboxCount()
      local removed = ns.Util.IsInteger(after, 0, MAX_SAFE) and after < state.beforeMailCount
      local emptied = mailMessageEmpty(state.mailIndex)
      if removed or emptied then
        local processed = state.mailProcessed + 1
        if processed >= state.mailCount then
          finishStarted("succeeded", state.result.detail)
        else
          -- A removed message shifts the next message into this same index.
          -- The action remains in flight across the next hardware click.
          reduce({
            type = "mail_progress", mailProcessed = processed,
            mailIndex = removed and state.mailIndex or state.mailIndex + 1,
          })
          updateButton()
        end
      end
    end
  end
end

local function merchantIndex(itemID)
  local countItems, itemIDAt = ns.ExecutorNative.GlobalFunction("GetMerchantNumItems"), ns.ExecutorNative.GlobalFunction("GetMerchantItemID")
  if not countItems or not itemIDAt then return nil end
  for index = 1, countItems() do if itemIDAt(index) == itemID then return index end end
end

local function recipeReady(action)
  if not ns.ExecutorNative.TradeSkillReady() then return false, "wrong_frame" end
  local recipeInfo, craftableCount, craftRecipe = ns.ExecutorNative.TradeSkillFunction("GetRecipeInfo"), ns.ExecutorNative.TradeSkillFunction("GetCraftableCount"), ns.ExecutorNative.TradeSkillFunction("CraftRecipe")
  if not recipeInfo or not craftableCount or not craftRecipe then
    return false, "profession_api_unavailable"
  end
  local info = recipeInfo(action.recipeID)
  if type(info) ~= "table" or info.learned ~= true then return false, "recipe_not_learned" end
  local count = craftableCount(action.recipeID)
  if not ns.Util.IsInteger(count, 0, MAX_SAFE) or count < action.times then return false, "craftable_count_short" end
  return true
end

local function buyAPIsAvailable()
  return ns.ExecutorNative.AuctionFunction("StartCommoditiesPurchase") ~= nil and
    ns.ExecutorNative.AuctionFunction("ConfirmCommoditiesPurchase") ~= nil and
    ns.ExecutorNative.AuctionFunction("CancelCommoditiesPurchase") ~= nil
end

local function resolve(action)
  local kind = action.kind
  if kind == "buy_commodity" then
    if not buyAPIsAvailable() then return false, "buy_api_unavailable" end
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", { itemID = action.itemID, quantity = action.quantity }) advance() return true end
    local ok, reason = protected(ns.ExecutorNative.AuctionAction("StartCommoditiesPurchase"), ns.ExecutorNative.AuctionFunction("StartCommoditiesPurchase"), action.itemID, action.quantity)
    if not ok then return false, reason end
    beginStarted({ itemID = action.itemID, quantity = action.quantity })
    reduce({ type = "phase", phase = "await_price" })
    return true
  elseif kind == "post_commodity" then
    local slots = bagSlots(action.itemID)
    local chosen
    for _, slot in ipairs(slots) do if slot.quantity >= action.quantity then chosen = slot break end end
    if not chosen then return false, "missing_bag_stack" end
    if not ns.ExecutorNative.CreateItemLocation then return false, "item_location_unavailable" end
    local sellValid, calculateDeposit, postCommodity = ns.ExecutorNative.AuctionFunction("IsSellItemValid"), ns.ExecutorNative.AuctionFunction("CalculateCommodityDeposit"), ns.ExecutorNative.AuctionFunction("PostCommodity")
    if not sellValid or not calculateDeposit or not postCommodity then
      return false, "auction_api_unavailable"
    end
    local location = ns.ExecutorNative.CreateItemLocation(chosen.bag, chosen.slot)
    if not location then return false, "item_location_unavailable" end
    if sellValid(location) ~= true then return false, "sell_item_invalid" end
    local ok, deposit = pcall(calculateDeposit, action.itemID, DURATION[action.durationHours], action.quantity)
    if not ok or type(deposit) ~= "number" or deposit < 0 or deposit ~= math.floor(deposit) then return false, "deposit_invalid" end
    local detail = { bag = chosen.bag, slot = chosen.slot, deposit = deposit }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local called, reason = protected(ns.ExecutorNative.AuctionAction("PostCommodity"), postCommodity, location, DURATION[action.durationHours], action.quantity, action.unitPrice)
    if not called then return false, reason end
    beginStarted(detail)
    return true
  elseif kind == "cancel_auction" then
    local canCancel, cancelCost, cancelAuction = ns.ExecutorNative.AuctionFunction("CanCancelAuction"), ns.ExecutorNative.AuctionFunction("GetCancelCost"), ns.ExecutorNative.AuctionFunction("CancelAuction")
    if not canCancel or canCancel(action.auctionID) ~= true then return false, "cannot_cancel" end
    if not cancelCost then return false, "cancel_cost_unavailable" end
    local costOK, cost = pcall(cancelCost, action.auctionID)
    if not costOK or not ns.Util.IsInteger(cost, 0, MAX_SAFE) then return false, "cancel_cost_invalid" end
    local detail = { auctionID = action.auctionID, cost = cost }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local called, reason = protected(ns.ExecutorNative.AuctionAction("CancelAuction"), cancelAuction, action.auctionID)
    if not called then return false, reason end
    beginStarted(detail)
    return true
  elseif kind == "vendor_sell" then
    local selected, reason = exactBagSelection(action.itemID, action.quantity)
    if not selected then return false, reason end
    local detail = { stacks = selected }
    local useContainerItem = ns.ExecutorNative.ContainerFunction("UseContainerItem")
    if not useContainerItem then return false, "container_api_unavailable" end
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    reduce({ type = "prepare_sell", stacks = selected, stackIndex = 1 })
    local slot = state.stacks[1]
    if not slotMatches(slot) then reduce({ type = "clear_stacks" }) return false, "slot_changed" end
    local called, callReason = protected(ns.ExecutorNative.ContainerAction("UseContainerItem"), useContainerItem, slot.bag, slot.slot)
    if not called then return false, callReason end
    beginStarted(detail)
    reduce({ type = "phase", phase = "sell_wait" })
    observeVendorSell()
    return true
  elseif kind == "vendor_buy" then
    local index = merchantIndex(action.itemID)
    if not index then return false, "merchant_item_missing" end
    local buyMerchantItem = ns.ExecutorNative.GlobalFunction("BuyMerchantItem")
    if not buyMerchantItem then return false, "BuyMerchantItem unavailable" end
    local detail = { merchantIndex = index }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local before = bagItemCount(action.itemID)
    if not before then return false, "bag_api_unavailable" end
    local called, reason = protected(ns.ExecutorNative.GlobalAction("BuyMerchantItem"), buyMerchantItem, index, action.quantity)
    if not called then return false, reason end
    beginStarted(detail)
    reduce({ type = "prepare_vendor_buy", beforeBagCount = before, expectedBagCount = action.quantity, expectedItemID = action.itemID })
    observePostcondition()
    return true
  elseif kind == "craft" then
    local ready, reason = recipeReady(action)
    if not ready then return false, reason end
    local info = ns.ExecutorNative.TradeSkillFunction("GetRecipeInfo")(action.recipeID)
    local detail = { recipeID = action.recipeID, times = action.times }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local expectedItemID = info and (info.itemID or info.resultItemID) or nil
    local before = expectedItemID and bagItemCount(expectedItemID) or bagCount()
    if not before then return false, "bag_api_unavailable" end
    local called, callReason = protected(ns.ExecutorNative.TradeSkillAction("CraftRecipe"), ns.ExecutorNative.TradeSkillFunction("CraftRecipe"), action.recipeID, action.times)
    if not called then return false, callReason end
    beginStarted(detail)
    reduce({ type = "prepare_craft", beforeBagCount = before, expectedBagCount = action.times, expectedItemID = expectedItemID })
    observePostcondition()
    return true
  elseif kind == "mail_collect" then
    local inboxCount, autoLoot = ns.ExecutorNative.GlobalFunction("GetInboxNumItems"), ns.ExecutorNative.GlobalFunction("AutoLootMailItem")
    if not inboxCount or not autoLoot then return false, "mail_api_unavailable" end
    local available = inboxCount()
    if not ns.Util.IsInteger(available, 0, MAX_SAFE) or available < 1 then return false, "mailbox_empty" end
    local count = math.min(action.maxMessages, available)
    local detail = { messages = count }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    reduce({ type = "prepare_mail", mailIndex = 1, mailCount = count, mailProcessed = 0 })
    local before = inboxCount()
    local called, callReason = protected(ns.ExecutorNative.GlobalAction("AutoLootMailItem"), autoLoot, 1)
    if not called then return false, callReason end
    beginStarted(detail)
    reduce({ type = "mail_wait", beforeMailCount = before })
    observePostcondition()
    return true
  end
  return false, "unknown_action"
end

local CLICKABLE_PHASES = {
  ready_to_confirm = true, must_cancel = true, sell_stacks = true, mail_items = true,
}

-- This is the single acceptance policy used by both the physical callback and
-- telemetry.  It intentionally excludes wait phases that would only produce
-- a refusal (or an in-flight refusal) from clickReady.
local function clickBranch()
  if not state or not state.valid or not currentAction() then return false, "none" end
  if state.phase == nil then return not state.inFlight, "resolve" end
  if CLICKABLE_PHASES[state.phase] then return true, state.phase end
  return false, state.phase
end

local function clickRateAllowed(at)
  return state.lastClickAt == nil or at - state.lastClickAt >= 1.0
end

-- The callback and the telemetry projection both use this complete admission
-- helper.  It includes rate, identity/scanner/blocked, native frame visibility,
-- and phase acceptance, so clickReady cannot drift into a softer predicate.
local function clickAdmission(action, at)
  if not clickRateAllowed(at) then return false, "too_fast" end
  local admitted, reason = validAndCurrent()
  if not admitted then return false, reason end
  if not isFrameOpen(action.kind) then return false, "wrong_frame" end
  local accepted, branch = clickBranch()
  if accepted then return true, branch end
  if branch == "await_price" then return false, "price_pending" end
  if state.inFlight then return false, "in_flight" end
  return false, branch
end

local function click(source)
  if state then
    reduce({ type = "click", source = source, at = ns.ExecutorNative.Uptime() })
  end
  if not state or not state.valid then return end
  local action = currentAction()
  if not action then return end
  local at = ns.ExecutorNative.Uptime()
  local accepted, branch = clickAdmission(action, at)
  if not accepted then
    reduce({ type = "click_at", at = at })
    refuse(branch, branch)
    return
  end
  reduce({ type = "click_at", at = at })
  if branch == "ready_to_confirm" then
    local called, callReason = protected(ns.ExecutorNative.AuctionAction("ConfirmCommoditiesPurchase"), ns.ExecutorNative.AuctionFunction("ConfirmCommoditiesPurchase"), action.itemID, action.quantity)
    if not called then refuse(callReason, callReason) return end
    reduce({ type = "phase", phase = "await_purchase" })
    return
  end
  if branch == "must_cancel" then
    local called, callReason = protected(ns.ExecutorNative.AuctionAction("CancelCommoditiesPurchase"), ns.ExecutorNative.AuctionFunction("CancelCommoditiesPurchase"), action.itemID)
    if not called then refuse(callReason, callReason) return end
    reduce({ type = "cancel_attempted" })
    finishStarted("refused", { unitPrice = state.result.detail.unitPrice, totalPrice = state.result.detail.totalPrice, reason = state.cancelReason or "price_above_max" })
    return
  end
  if branch == "sell_stacks" then
    local slot = state.stacks[state.stackIndex]
    if not slot or not slotMatches(slot) then refuse("slot_changed", "slot_changed") return end
    local called, callReason = protected(ns.ExecutorNative.ContainerAction("UseContainerItem"), ns.ExecutorNative.ContainerFunction("UseContainerItem"), slot.bag, slot.slot)
    if not called then refuse(callReason, callReason) return end
    reduce({ type = "phase", phase = "sell_wait" })
    observeVendorSell()
    return
  end
  if branch == "mail_items" then
    local inboxCount, autoLoot = ns.ExecutorNative.GlobalFunction("GetInboxNumItems"), ns.ExecutorNative.GlobalFunction("AutoLootMailItem")
    local before = inboxCount()
    local called, callReason = protected(ns.ExecutorNative.GlobalAction("AutoLootMailItem"), autoLoot, state.mailIndex)
    if not called then refuse(callReason, callReason) return end
    reduce({ type = "mail_wait", beforeMailCount = before })
    observePostcondition()
    return
  end
  if branch ~= "resolve" then return end
  local ok, callReason = resolve(action)
  if not ok then refuse(callReason, callReason) end
end

local function commodityPrice(_, first, second, third)
  if not state or not state.inFlight or state.phase ~= "await_price" then return end
  local action = currentAction()
  if not action or action.kind ~= "buy_commodity" then return end
  local unitPrice, totalPrice
  if third == nil then
    unitPrice, totalPrice = first, second
  else
    if first ~= action.itemID then return end
    unitPrice, totalPrice = second, third
  end
  local valid = ns.Util.IsInteger(unitPrice, 0, MAX_SAFE) and ns.Util.IsInteger(totalPrice, 0, MAX_SAFE)
  if valid then valid = totalPrice >= unitPrice * action.quantity end
  local detail = ns.Util.Copy(state.result.detail)
  detail.unitPrice, detail.totalPrice = unitPrice, totalPrice
  reduce({ type = "result_detail", detail = detail })
  if not valid then
    refuse("price_invalid", { unitPrice = unitPrice, totalPrice = totalPrice })
    return
  end
  if unitPrice > action.maxUnitPrice then
    refuse("price_above_max", { unitPrice = unitPrice, totalPrice = totalPrice })
  else
    reduce({ type = "phase", phase = "ready_to_confirm" })
  end
  updateButton()
end

local function eventFailure(event, ...)
  if not state then return end
  local action = currentAction()
  if event == "COMMODITY_PURCHASE_SUCCEEDED" and state.inFlight and state.phase == "await_purchase" and action and action.kind == "buy_commodity" then
    finishStarted("succeeded", state.result.detail)
  elseif event == "COMMODITY_PURCHASE_FAILED" and state.inFlight and action and action.kind == "buy_commodity" then
    local first, second = ...
    local detail = { event = event }
    if first ~= nil or second ~= nil then detail.error = tostring(second or first) end
    finishStarted("failed", detail)
  elseif event == "AUCTION_HOUSE_AUCTION_CREATED" and state.inFlight and action and action.kind == "post_commodity" then
    finishStarted("succeeded", state.result.detail)
  elseif event == "AUCTION_CANCELED" and state.inFlight and action and action.kind == "cancel_auction" then
    local auctionID = ...
    if auctionID == action.auctionID then finishStarted("succeeded", state.result.detail) end
  elseif event == "UI_ERROR_MESSAGE" and state.inFlight then
    local first, second = ...
    finishStarted("failed", { error = tostring(second or first) })
  elseif event == "BAG_UPDATE" or event == "BAG_UPDATE_DELAYED" or event == "MAIL_INBOX_UPDATE" or event == "MERCHANT_UPDATE" then
    observeVendorSell()
    observePostcondition()
  end
end

local function eventFrame(event)
  if event == "AUCTION_HOUSE_SHOW" then frameOpen.ah = true
  elseif event == "AUCTION_HOUSE_CLOSED" then frameOpen.ah = false
  elseif event == "MERCHANT_SHOW" then frameOpen.merchant = true
  elseif event == "MERCHANT_CLOSED" then frameOpen.merchant = false
  elseif event == "MAIL_SHOW" then frameOpen.mailbox = true
  elseif event == "MAIL_CLOSED" then frameOpen.mailbox = false
  elseif event == "TRADE_SKILL_SHOW" then frameOpen.profession = true
  elseif event == "TRADE_SKILL_CLOSE" then frameOpen.profession = false end
  updateButton()
end

local function makeButton()
  local parent = ns.ExecutorNative.UIParent()
  button = ns.ExecutorNative.CreateButton("CrowleyTradeExecutorButton", parent, "UIPanelButtonTemplate")
  ns.ExecutorNative.ConfigureButton(button, parent, function() click("button") end)
end

local function reconcile()
  local results = state.execution and state.execution.results or {}
  local terminal = {}
  local unresolved = {}
  for _, result in ipairs(results) do
    local seq = result.seq
    if seq and seq > 0 then
      if result.status == "started" then
        unresolved[seq] = true
      elseif result.status == "succeeded" or result.status == "failed" or result.status == "skipped" or
          result.status == "refused" then
        -- Contract: these are terminal. In-flight refusals are recorded as nonterminal
        -- "blocked" events, so a "refused" after "started" means the operation ended
        -- (e.g. a cancelled commodity purchase).
        unresolved[seq] = nil
        terminal[seq] = result.status
      end
      -- A blocked event records an interruption but never resolves the
      -- protected operation that preceded it.
    end
  end
  local index, blocked, ambiguous = 1, false, false
  for seq = 1, #state.queue.actions do
    if unresolved[seq] then
      index, blocked, ambiguous = seq, true, true
      ns.Print("queue blocked: ambiguous_history")
      return reduce({ type = "reconcile", index = index, blocked = blocked, ambiguous = ambiguous })
    end
  end
  for seq = 1, #state.queue.actions do
    if not terminal[seq] or (terminal[seq] ~= "succeeded" and terminal[seq] ~= "skipped") then
      index = seq
      blocked = terminal[seq] == "refused" or terminal[seq] == "failed"
      return reduce({ type = "reconcile", index = index, blocked = blocked, ambiguous = false })
    end
    index = seq + 1
  end
  reduce({ type = "reconcile", index = index, blocked = false, ambiguous = false })
end

local function rejectQueue(reason)
  local queue = CROWLEY_QUEUE
  local queueId = type(queue) == "table" and type(queue.queueId) == "string" and queue.queueId or "<invalid>"
  local house = type(queue) == "table" and type(queue.house) == "string" and queue.house or ""
  local character = type(queue) == "table" and type(queue.character) == "string" and queue.character or ""
  state = { queue = { queueId = queueId, house = house, character = character, actions = {}, armed = false }, valid = false, index = 1 }
  ensureExecution()
  appendResult(0, "queue", "refused", reason)
  ns.Print("queue refused: " .. tostring(reason))
end

local function loadQueue()
  if CROWLEY_QUEUE == nil or CROWLEY_QUEUE == false then return end
  if type(CROWLEY_QUEUE) ~= "table" then rejectQueue("queue is not a table") return end
  local ok, reason = validateQueue(CROWLEY_QUEUE)
  if not ok then rejectQueue(reason) return end
  state = {
    queue = CROWLEY_QUEUE, valid = true, index = 1, inFlight = false, blocked = false,
    latched = false, cancelAttempted = false, clickOrdinal = 0, lastClickSource = "none",
  }
  ensureExecution()
  reconcile()
  makeButton()
  updateButton()
end

local function wirePhase(action)
  if not action then return "complete" end
  if state.phase == nil then return "ready" end
  if state.phase == "await_price" or state.phase == "ready_to_confirm" or state.phase == "must_cancel" or
      state.phase == "await_purchase" or state.phase == "sell_stacks" or state.phase == "sell_wait" or
      state.phase == "await_vendor_buy" or state.phase == "await_craft" or state.phase == "mail_items" or
      state.phase == "mail_wait" then
    return state.phase
  end
  error("executor telemetry has an unknown phase")
end

local function buttonIsShown()
  return button ~= nil and ns.ExecutorNative.ButtonIsShown(button)
end

local function clickReady()
  if not state or not state.valid or not currentAction() or not buttonIsShown() then return false end
  local accepted = clickAdmission(currentAction(), ns.ExecutorNative.Uptime())
  return accepted == true
end

local function emptyTelemetry(availability)
  return {
    availability = availability, queueId = "", house = "", character = "",
    publishedArmed = false, sessionArmed = false, buttonVisible = false, buttonName = "",
    currentSeq = 0, currentKind = "", phase = "none", inFlight = false, clickReady = false,
    workDone = 0, workTotal = 0, blocked = false, latched = false, ambiguous = false,
    clickOrdinal = 0, lastClickSource = "none", resultCount = 0,
    lastResult = { seq = 0, kind = "", status = "none" },
  }
end

function Executor.Telemetry()
  if not state then return emptyTelemetry("none") end
  local results = state.execution and state.execution.results or {}
  if not state.valid then
    local value = emptyTelemetry("invalid")
    value.resultCount = #results
    value.lastResult = { seq = 0, kind = "queue", status = "refused" }
    return value
  end
  local action = currentAction()
  local latest = results[#results]
  local value = {
    availability = "valid", queueId = state.queue.queueId, house = state.queue.house,
    character = state.queue.character, publishedArmed = state.queue.armed == true,
    sessionArmed = sessionArmed == true, buttonVisible = buttonIsShown(),
    buttonName = button and "CrowleyTradeExecutorButton" or "", currentSeq = action and action.seq or 0,
    currentKind = action and action.kind or "", phase = wirePhase(action), inFlight = state.inFlight == true,
    clickReady = clickReady(), workDone = 0, workTotal = 0, blocked = state.blocked == true,
    latched = state.latched == true, ambiguous = state.ambiguous == true,
    clickOrdinal = state.clickOrdinal or 0, lastClickSource = state.lastClickSource or "none",
    resultCount = #results, lastResult = { seq = 0, kind = "", status = "none" },
  }
  if action and action.kind == "mail_collect" then
    value.workDone = state.mailProcessed or 0
    value.workTotal = state.mailCount or action.maxMessages
  end
  if latest then
    value.lastResult = { seq = latest.seq or 0, kind = latest.kind or "", status = latest.status or "none" }
  end
  return value
end

function Executor.ValidateQueue(queue) return validateQueue(queue) end
function Executor.Button() return button end
function Executor.Valid() return state ~= nil and state.valid == true end
function Executor.Current() return currentAction() end
function Executor.State() return state end
function Executor.Click() click("directAPI") end
function Executor.SessionArmed() return sessionArmed == true end
function Executor.Results() return state and state.execution and state.execution.results or nil end

function Executor.QueueCommand()
  if not state then return ns.Print("queue: none") end
  local action = currentAction()
  local results = state.execution and state.execution.results or {}
  ns.Print(("queue: %s, published armed=%s, session armed=%s, current=%s, results=%d"):format(
    state.queue.queueId or "<invalid>", tostring(state.queue.armed == true), tostring(sessionArmed == true), action and currentSummary(action) or "complete", #results))
  for _, result in ipairs(results) do
    ns.Print(("result: seq=%s kind=%s status=%s"):format(tostring(result.seq), tostring(result.kind), tostring(result.status)))
  end
end

function Executor.Arm(queueId)
  if not state or not state.valid then ns.Print("arm refused: queue_invalid") return false end
  if queueId ~= state.queue.queueId then ns.Print("arm refused: queue_id_mismatch") return false end
  if ns.ExecutorNative.Time() >= state.queue.expiresAt then ns.Print("arm refused: expired") return false end
  if state.queue.armed ~= true then ns.Print("arm refused: published armed=false") return false end
  sessionArmed = true
  ns.Print(("armed: queueId=%s, published=true, session=true"):format(queueId))
  return true
end

function Executor.Disarm()
  sessionArmed = false
  ns.Print("disarmed: session=false")
end

function Executor.Skip()
  if not state or not state.valid or not currentAction() then ns.Print("skip refused: no current action") return false end
  if state.inFlight then ns.Print("skip refused: in_flight") return false end
  if state.ambiguous then ns.Print("skip refused: ambiguous_history") return false end
  local action = currentAction()
  appendResult(action.seq, action.kind, "skipped", {})
  advance()
  ns.Print("skipped: " .. currentSummary(action))
  return true
end

ns.RegisterModule("Executor", {
  requires = { "Core", "ExecutorState", "GeneratedWireSchemas", "ExecutorNative" },
  onLoad = function()
    if CROWLEY_QUEUE ~= nil and CROWLEY_QUEUE ~= false then loadQueue() end
  end,
})

local REGISTERED_EVENTS = {
  { name = "AUCTION_HOUSE_SHOW", handler = eventFrame },
  { name = "AUCTION_HOUSE_CLOSED", handler = eventFrame },
  { name = "MERCHANT_SHOW", handler = eventFrame },
  { name = "MERCHANT_CLOSED", handler = eventFrame },
  { name = "MAIL_SHOW", handler = eventFrame },
  { name = "MAIL_CLOSED", handler = eventFrame },
  { name = "TRADE_SKILL_SHOW", handler = eventFrame },
  { name = "TRADE_SKILL_CLOSE", handler = eventFrame },
  { name = "COMMODITY_PRICE_UPDATED", handler = commodityPrice },
  { name = "COMMODITY_PURCHASE_SUCCEEDED", handler = eventFailure },
  { name = "COMMODITY_PURCHASE_FAILED", handler = eventFailure },
  { name = "AUCTION_HOUSE_AUCTION_CREATED", handler = eventFailure },
  { name = "AUCTION_CANCELED", handler = eventFailure },
  { name = "UI_ERROR_MESSAGE", handler = eventFailure },
  { name = "BAG_UPDATE", handler = eventFailure },
  { name = "BAG_UPDATE_DELAYED", handler = eventFailure },
  { name = "MAIL_INBOX_UPDATE", handler = eventFailure },
  { name = "MERCHANT_UPDATE", handler = eventFailure },
}

function Executor.RegisteredEvents()
  local events = {}
  for index, registration in ipairs(REGISTERED_EVENTS) do events[index] = registration.name end
  return events
end

-- With Queue.lua's false placeholder, the executor creates no frame and
-- registers no events, leaving the scanner's live surface untouched.
if CROWLEY_QUEUE ~= nil and CROWLEY_QUEUE ~= false then
  for _, registration in ipairs(REGISTERED_EVENTS) do ns.ExecutorNative.On(registration.name, registration.handler) end
end
