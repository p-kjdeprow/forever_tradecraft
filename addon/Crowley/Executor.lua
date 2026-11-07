local _, ns = ...

-- The executor is deliberately a separate consumer of the companion queue. It
-- never reads or writes scanner data, CROWLEY_DATA, or scanner state except for
-- the fail-closed AH exclusion checked immediately before an AH transaction.
local Executor = {}
ns.Executor = Executor

local MAX_ACTIONS = 50
local MAX_SELECTION_STATES = 10000
local MAX_SAFE = 9007199254740991
local MAX_U32 = 4294967295
local DURATION = { [12] = 1, [24] = 2, [48] = 3 }

local ACTION_FIELDS = {
  buy_commodity = { seq = true, kind = true, itemID = true, quantity = true, maxUnitPrice = true },
  post_commodity = { seq = true, kind = true, itemID = true, quantity = true, unitPrice = true, durationHours = true },
  cancel_auction = { seq = true, kind = true, auctionID = true },
  vendor_sell = { seq = true, kind = true, itemID = true, quantity = true },
  vendor_buy = { seq = true, kind = true, itemID = true, quantity = true },
  craft = { seq = true, kind = true, recipeID = true, times = true },
  mail_collect = { seq = true, kind = true, maxMessages = true },
}

local state
local sessionArmed = false
local button
local frameOpen = { ah = false, merchant = false, mailbox = false, profession = false }

local function validAction(action, index)
  if type(action) ~= "table" or type(action.kind) ~= "string" then return false, "action " .. index .. " has an unknown kind" end
  local fields = ACTION_FIELDS[action.kind]
  if not fields then return false, "action " .. index .. " has an unknown kind " .. tostring(action.kind) end
  if not ns.Util.ExactKeys(action, fields) then return false, "action " .. index .. " has unknown or missing fields" end
  if action.seq ~= index or not ns.Util.IsInteger(action.seq, 1, MAX_SAFE) then return false, "action " .. index .. " has a non-contiguous seq" end
  if action.kind == "buy_commodity" then
    if not ns.Util.IsInteger(action.itemID, 1, MAX_SAFE) or not ns.Util.IsInteger(action.quantity, 1, MAX_SAFE) or not ns.Util.IsInteger(action.maxUnitPrice, 1, MAX_SAFE) then
      return false, "action " .. index .. " has an invalid positive integer"
    end
  elseif action.kind == "post_commodity" then
    if not ns.Util.IsInteger(action.itemID, 1, MAX_SAFE) or not ns.Util.IsInteger(action.quantity, 1, MAX_SAFE) or not ns.Util.IsInteger(action.unitPrice, 1, MAX_SAFE) or
        not DURATION[action.durationHours] then return false, "action " .. index .. " has an invalid post field" end
  elseif action.kind == "cancel_auction" then
    if not ns.Util.IsInteger(action.auctionID, 1, MAX_SAFE) then return false, "action " .. index .. " has an invalid auctionID" end
  elseif action.kind == "vendor_sell" or action.kind == "vendor_buy" then
    if not ns.Util.IsInteger(action.itemID, 1, MAX_SAFE) or not ns.Util.IsInteger(action.quantity, 1, MAX_SAFE) then
      return false, "action " .. index .. " has an invalid positive integer"
    end
  elseif action.kind == "craft" then
    if not ns.Util.IsInteger(action.recipeID, 1, MAX_SAFE) or not ns.Util.IsInteger(action.times, 1, MAX_SAFE) then
      return false, "action " .. index .. " has an invalid positive integer"
    end
  elseif action.kind == "mail_collect" then
    if not ns.Util.IsInteger(action.maxMessages, 1, MAX_U32) then
      return false, "action " .. index .. " has an invalid maxMessages"
    end
  end
  return true
end

local function validateQueue(queue)
  if type(queue) ~= "table" then return false, "queue is not a table" end
  if not ns.Util.ExactKeys(queue, { format = true, queueId = true, generatedAt = true, expiresAt = true, house = true, character = true, armed = true, actions = true }) then
    return false, "queue has unknown or missing fields"
  end
  if queue.format ~= 1 then return false, "queue format is invalid" end
  if type(queue.queueId) ~= "string" or queue.queueId == "" or #queue.queueId > 192 then return false, "queueId is invalid" end
  if not ns.Util.IsInteger(queue.generatedAt, 1, MAX_SAFE) or not ns.Util.IsInteger(queue.expiresAt, 1, MAX_SAFE) or queue.expiresAt <= queue.generatedAt then
    return false, "queue expiry is invalid"
  end
  if type(queue.house) ~= "string" or queue.house == "" or #queue.house > 256 then return false, "queue house is invalid" end
  if type(queue.character) ~= "string" or queue.character == "" or #queue.character > 256 then return false, "queue character is invalid" end
  if type(queue.armed) ~= "boolean" then return false, "queue armed is invalid" end
  if type(queue.actions) ~= "table" then return false, "queue actions is not an array" end
  local count = #queue.actions
  if count > MAX_ACTIONS then return false, "queue has more than 50 actions" end
  for key in pairs(queue.actions) do
    if type(key) ~= "number" or key < 1 or key > count or key ~= math.floor(key) then
      return false, "queue actions is not contiguous"
    end
  end
  for index = 1, count do
    local ok, reason = validAction(queue.actions[index], index)
    if not ok then return false, reason end
  end
  return true
end

local function now() return time() end
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
  if required == "profession" and C_TradeSkillUI and type(C_TradeSkillUI.IsTradeSkillReady) == "function" then
    return C_TradeSkillUI.IsTradeSkillReady() == true
  end
  if required == "mailbox" then
    -- MAIL_CLOSED is not reliable on the installed client.  The native frame
    -- visibility is the admission fact; the event flag is retained only as
    -- diagnostic state and is deliberately not consulted here.
    if not MailFrame or type(MailFrame.IsShown) ~= "function" then return false end
    local ok, shown = pcall(function() return MailFrame:IsShown() end)
    return ok and shown == true
  end
  return frameOpen[required] == true
end

local function updateButton()
  if not button or not state or not state.valid then return end
  local action = currentAction()
  if not action then button:Hide() return end
  button:SetText("Crowley: " .. currentSummary(action))
  if isFrameOpen(action.kind) then button:Show() else button:Hide() end
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

local function finishResult(status, detail)
  if not state.result then return end
  state.result.status = status
  state.result.at = now()
  state.result.detail = detail or state.result.detail or {}
end

local function printDryRun(action) ns.Print("dry run: would " .. currentSummary(action)) end

local function advance()
  state.index = state.index + 1
  state.phase, state.inFlight, state.result, state.blocked, state.latched, state.cancelReason, state.cancelAttempted =
    nil, false, nil, false, false, nil, false
  state.mailIndex, state.mailCount, state.mailProcessed, state.beforeMailCount = nil, nil, nil, nil
  updateButton()
end

local function refuse(reason, detail, stop)
  local action = currentAction()
  if action and state.inFlight then
    local blockedDetail = type(detail) == "table" and detail or { reason = reason }
    blockedDetail.reason = blockedDetail.reason or reason
    appendResult(action.seq, action.kind, "blocked", blockedDetail)
    state.latched = true
    if action.kind == "buy_commodity" then
      state.phase, state.cancelReason = "must_cancel", reason
    end
    updateButton()
    ns.Print("executor blocked: " .. tostring(reason))
    return nil
  end
  if action and state.ambiguous then
    -- An ambiguous persisted history must stay ambiguous across reloads, so a click on it is
    -- a nonterminal event, never a terminal refusal that reconciliation could treat as resolution.
    appendResult(action.seq, action.kind, "blocked", { reason = reason })
    updateButton()
    ns.Print("executor blocked: " .. tostring(reason))
    return nil
  end
  local result = appendResult(action and action.seq or 0, action and action.kind or "queue", "refused", detail or reason)
  if action and stop ~= false then state.blocked = true end
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
  if time() >= state.queue.expiresAt then return false, "expired" end
  local ok, house = pcall(ns.Context.House)
  if not ok or type(house) ~= "table" or house.key ~= state.queue.house then return false, "house_mismatch" end
  if UnitName("player") ~= state.queue.character then return false, "character_mismatch" end
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
      name == "C_AuctionHouse.CancelCommoditiesPurchase" and state.phase == "must_cancel" and not state.cancelAttempted) then
    return false, "blocked"
  end
  if type(fn) ~= "function" then return false, name .. " unavailable" end
  fn(...)
  return true
end

local function finishStarted(status, detail)
  finishResult(status, detail)
  state.inFlight, state.phase, state.latched = false, nil, false
  if status == "succeeded" then advance() else state.blocked = true updateButton() end
end

local function beginStarted(detail)
  state.result = appendResult(currentAction().seq, currentAction().kind, "started", detail)
  state.inFlight = true
end

local function bagSlots(itemID)
  local slots = {}
  if not C_Container or type(C_Container.GetContainerNumSlots) ~= "function" or type(C_Container.GetContainerItemInfo) ~= "function" then return slots end
  for bag = 0, 4 do
    local count = C_Container.GetContainerNumSlots(bag) or 0
    for slot = 1, count do
      local info = C_Container.GetContainerItemInfo(bag, slot)
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
  if not C_Container or type(C_Container.GetContainerItemInfo) ~= "function" then return false end
  local info = C_Container.GetContainerItemInfo(planned.bag, planned.slot)
  local quantity = info and (info.stackCount or info.quantity or info.count)
  return info and info.itemID == planned.itemID and quantity == planned.quantity
end

local function slotSold(planned)
  if not C_Container or type(C_Container.GetContainerItemInfo) ~= "function" then return false end
  local info = C_Container.GetContainerItemInfo(planned.bag, planned.slot)
  return not info or info.itemID ~= planned.itemID
end

local function observeVendorSell()
  if not state or not state.inFlight or state.phase ~= "sell_wait" then return end
  local planned = state.stacks[state.stackIndex]
  if not planned or not slotSold(planned) then return end
  if state.stackIndex >= #state.stacks then
    finishStarted("succeeded", state.result.detail)
  else
    state.stackIndex = state.stackIndex + 1
    state.phase = "sell_stacks"
    updateButton()
  end
end

local function bagCount()
  if not C_Container or type(C_Container.GetContainerNumSlots) ~= "function" or type(C_Container.GetContainerItemInfo) ~= "function" then return nil end
  local total = 0
  for bag = 0, 4 do
    for slot = 1, (C_Container.GetContainerNumSlots(bag) or 0) do
      local info = C_Container.GetContainerItemInfo(bag, slot)
      local quantity = info and (info.stackCount or info.quantity or info.count)
      if ns.Util.IsInteger(quantity, 0, MAX_SAFE) then total = total + quantity end
    end
  end
  return total
end

local function bagItemCount(itemID)
  if not C_Container or type(C_Container.GetContainerNumSlots) ~= "function" or type(C_Container.GetContainerItemInfo) ~= "function" then return nil end
  local total = 0
  for bag = 0, 4 do
    for slot = 1, (C_Container.GetContainerNumSlots(bag) or 0) do
      local info = C_Container.GetContainerItemInfo(bag, slot)
      local quantity = info and (info.stackCount or info.quantity or info.count)
      if info and info.itemID == itemID and ns.Util.IsInteger(quantity, 0, MAX_SAFE) then total = total + quantity end
    end
  end
  return total
end

local function mailMessageEmpty(index)
  if type(GetInboxHeaderInfo) ~= "function" then return false end
  local ok, _, _, _, _, money, _, _, hasItem = pcall(GetInboxHeaderInfo, index)
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
    if type(GetInboxNumItems) == "function" then
      local after = GetInboxNumItems()
      local removed = ns.Util.IsInteger(after, 0, MAX_SAFE) and after < state.beforeMailCount
      local emptied = mailMessageEmpty(state.mailIndex)
      if removed or emptied then
        state.mailProcessed = state.mailProcessed + 1
        if state.mailProcessed >= state.mailCount then
          finishStarted("succeeded", state.result.detail)
        else
          -- A removed message shifts the next message into this same index.
          -- The action remains in flight across the next hardware click.
          if not removed then state.mailIndex = state.mailIndex + 1 end
          state.phase = "mail_items"
          updateButton()
        end
      end
    end
  end
end

local function merchantIndex(itemID)
  if type(GetMerchantNumItems) ~= "function" or type(GetMerchantItemID) ~= "function" then return nil end
  for index = 1, GetMerchantNumItems() do if GetMerchantItemID(index) == itemID then return index end end
end

local function recipeReady(action)
  if not C_TradeSkillUI or type(C_TradeSkillUI.IsTradeSkillReady) ~= "function" or C_TradeSkillUI.IsTradeSkillReady() ~= true then return false, "wrong_frame" end
  if type(C_TradeSkillUI.GetRecipeInfo) ~= "function" or type(C_TradeSkillUI.GetCraftableCount) ~= "function" or type(C_TradeSkillUI.CraftRecipe) ~= "function" then
    return false, "profession_api_unavailable"
  end
  local info = C_TradeSkillUI.GetRecipeInfo(action.recipeID)
  if type(info) ~= "table" or info.learned ~= true then return false, "recipe_not_learned" end
  local count = C_TradeSkillUI.GetCraftableCount(action.recipeID)
  if not ns.Util.IsInteger(count, 0, MAX_SAFE) or count < action.times then return false, "craftable_count_short" end
  return true
end

local function buyAPIsAvailable()
  return C_AuctionHouse and type(C_AuctionHouse.StartCommoditiesPurchase) == "function" and
    type(C_AuctionHouse.ConfirmCommoditiesPurchase) == "function" and type(C_AuctionHouse.CancelCommoditiesPurchase) == "function"
end

local function resolve(action)
  local kind = action.kind
  if kind == "buy_commodity" then
    if not buyAPIsAvailable() then return false, "buy_api_unavailable" end
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", { itemID = action.itemID, quantity = action.quantity }) advance() return true end
    local ok, reason = protected("C_AuctionHouse.StartCommoditiesPurchase", C_AuctionHouse.StartCommoditiesPurchase, action.itemID, action.quantity)
    if not ok then return false, reason end
    state.phase = "await_price"
    beginStarted({ itemID = action.itemID, quantity = action.quantity })
    return true
  elseif kind == "post_commodity" then
    local slots = bagSlots(action.itemID)
    local chosen
    for _, slot in ipairs(slots) do if slot.quantity >= action.quantity then chosen = slot break end end
    if not chosen then return false, "missing_bag_stack" end
    if not ItemLocation or type(ItemLocation.CreateFromBagAndSlot) ~= "function" then return false, "item_location_unavailable" end
    if not C_AuctionHouse or type(C_AuctionHouse.IsSellItemValid) ~= "function" or type(C_AuctionHouse.CalculateCommodityDeposit) ~= "function" or type(C_AuctionHouse.PostCommodity) ~= "function" then
      return false, "auction_api_unavailable"
    end
    local location = ItemLocation:CreateFromBagAndSlot(chosen.bag, chosen.slot)
    if C_AuctionHouse.IsSellItemValid(location) ~= true then return false, "sell_item_invalid" end
    local ok, deposit = pcall(C_AuctionHouse.CalculateCommodityDeposit, action.itemID, DURATION[action.durationHours], action.quantity)
    if not ok or type(deposit) ~= "number" or deposit < 0 or deposit ~= math.floor(deposit) then return false, "deposit_invalid" end
    local detail = { bag = chosen.bag, slot = chosen.slot, deposit = deposit }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local called, reason = protected("C_AuctionHouse.PostCommodity", C_AuctionHouse.PostCommodity, location, DURATION[action.durationHours], action.quantity, action.unitPrice)
    if not called then return false, reason end
    beginStarted(detail)
    return true
  elseif kind == "cancel_auction" then
    if not C_AuctionHouse or type(C_AuctionHouse.CanCancelAuction) ~= "function" or C_AuctionHouse.CanCancelAuction(action.auctionID) ~= true then return false, "cannot_cancel" end
    if type(C_AuctionHouse.GetCancelCost) ~= "function" then return false, "cancel_cost_unavailable" end
    local costOK, cost = pcall(C_AuctionHouse.GetCancelCost, action.auctionID)
    if not costOK or not ns.Util.IsInteger(cost, 0, MAX_SAFE) then return false, "cancel_cost_invalid" end
    local detail = { auctionID = action.auctionID, cost = cost }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local called, reason = protected("C_AuctionHouse.CancelAuction", C_AuctionHouse.CancelAuction, action.auctionID)
    if not called then return false, reason end
    beginStarted(detail)
    return true
  elseif kind == "vendor_sell" then
    local selected, reason = exactBagSelection(action.itemID, action.quantity)
    if not selected then return false, reason end
    local detail = { stacks = selected }
    if not C_Container or type(C_Container.UseContainerItem) ~= "function" then return false, "container_api_unavailable" end
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    state.stacks, state.stackIndex, state.phase = selected, 1, "sell_stacks"
    local slot = state.stacks[1]
    if not slotMatches(slot) then state.stacks = nil return false, "slot_changed" end
    local called, callReason = protected("C_Container.UseContainerItem", C_Container.UseContainerItem, slot.bag, slot.slot)
    if not called then return false, callReason end
    beginStarted(detail)
    state.phase = "sell_wait"
    observeVendorSell()
    return true
  elseif kind == "vendor_buy" then
    local index = merchantIndex(action.itemID)
    if not index then return false, "merchant_item_missing" end
    if type(BuyMerchantItem) ~= "function" then return false, "BuyMerchantItem unavailable" end
    local detail = { merchantIndex = index }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local before = bagItemCount(action.itemID)
    if not before then return false, "bag_api_unavailable" end
    local called, reason = protected("BuyMerchantItem", BuyMerchantItem, index, action.quantity)
    if not called then return false, reason end
    beginStarted(detail)
    state.phase, state.beforeBagCount, state.expectedBagCount, state.expectedItemID = "await_vendor_buy", before, action.quantity, action.itemID
    observePostcondition()
    return true
  elseif kind == "craft" then
    local ready, reason = recipeReady(action)
    if not ready then return false, reason end
    local info = C_TradeSkillUI.GetRecipeInfo(action.recipeID)
    local detail = { recipeID = action.recipeID, times = action.times }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    local expectedItemID = info and (info.itemID or info.resultItemID) or nil
    local before = expectedItemID and bagItemCount(expectedItemID) or bagCount()
    if not before then return false, "bag_api_unavailable" end
    local called, callReason = protected("C_TradeSkillUI.CraftRecipe", C_TradeSkillUI.CraftRecipe, action.recipeID, action.times)
    if not called then return false, callReason end
    beginStarted(detail)
    state.phase, state.beforeBagCount, state.expectedBagCount, state.expectedItemID = "await_craft", before, action.times, expectedItemID
    observePostcondition()
    return true
  elseif kind == "mail_collect" then
    if type(GetInboxNumItems) ~= "function" or type(AutoLootMailItem) ~= "function" then return false, "mail_api_unavailable" end
    local available = GetInboxNumItems()
    if not ns.Util.IsInteger(available, 0, MAX_SAFE) or available < 1 then return false, "mailbox_empty" end
    local count = math.min(action.maxMessages, available)
    local detail = { messages = count }
    if not isArmed() then printDryRun(action) appendResult(action.seq, kind, "dry_run", detail) advance() return true end
    state.mailIndex, state.mailCount, state.mailProcessed, state.phase = 1, count, 0, "mail_items"
    local before = GetInboxNumItems()
    local called, callReason = protected("AutoLootMailItem", AutoLootMailItem, 1)
    if not called then return false, callReason end
    beginStarted(detail)
    state.phase, state.beforeMailCount = "mail_wait", before
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
    if source == "button" then
      if (state.clickOrdinal or 0) >= MAX_U32 then error("executor click ordinal exhausted") end
      state.clickOrdinal = (state.clickOrdinal or 0) + 1
      state.lastClickSource = "button"
    elseif source == "directAPI" then
      state.lastClickSource = "directAPI"
    end
  end
  if not state or not state.valid then return end
  local action = currentAction()
  if not action then return end
  local at = GetTime()
  local accepted, branch = clickAdmission(action, at)
  if not accepted then
    state.lastClickAt = at
    refuse(branch, branch)
    return
  end
  state.lastClickAt = at
  if branch == "ready_to_confirm" then
    local called, callReason = protected("C_AuctionHouse.ConfirmCommoditiesPurchase", C_AuctionHouse and C_AuctionHouse.ConfirmCommoditiesPurchase, action.itemID, action.quantity)
    if not called then refuse(callReason, callReason) return end
    state.phase = "await_purchase"
    return
  end
  if branch == "must_cancel" then
    local called, callReason = protected("C_AuctionHouse.CancelCommoditiesPurchase", C_AuctionHouse and C_AuctionHouse.CancelCommoditiesPurchase, action.itemID)
    if not called then refuse(callReason, callReason) return end
    state.cancelAttempted = true
    finishStarted("refused", { unitPrice = state.result.detail.unitPrice, totalPrice = state.result.detail.totalPrice, reason = state.cancelReason or "price_above_max" })
    return
  end
  if branch == "sell_stacks" then
    local slot = state.stacks[state.stackIndex]
    if not slot or not slotMatches(slot) then refuse("slot_changed", "slot_changed") return end
    local called, callReason = protected("C_Container.UseContainerItem", C_Container and C_Container.UseContainerItem, slot.bag, slot.slot)
    if not called then refuse(callReason, callReason) return end
    state.phase = "sell_wait"
    state.inFlight = true
    observeVendorSell()
    return
  end
  if branch == "mail_items" then
    local before = GetInboxNumItems()
    local called, callReason = protected("AutoLootMailItem", AutoLootMailItem, state.mailIndex)
    if not called then refuse(callReason, callReason) return end
    state.phase, state.beforeMailCount, state.inFlight = "mail_wait", before, true
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
  if not valid then
    state.result.detail.unitPrice, state.result.detail.totalPrice = unitPrice, totalPrice
    refuse("price_invalid", { unitPrice = unitPrice, totalPrice = totalPrice })
    return
  end
  state.result.detail.unitPrice, state.result.detail.totalPrice = unitPrice, totalPrice
  if unitPrice > action.maxUnitPrice then
    refuse("price_above_max", { unitPrice = unitPrice, totalPrice = totalPrice })
  else
    state.phase = "ready_to_confirm"
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
  elseif event == "BAG_UPDATE" or event == "BAG_UPDATE_DELAYED" or event == "MAIL_INBOX_UPDATE" or event == "MAIL_UPDATE" or event == "MERCHANT_UPDATE" or event == "TRADE_SKILL_UPDATE" then
    observeVendorSell()
    observePostcondition()
  end
end

local function eventFrame(event)
  if event == "AUCTION_HOUSE_SHOW" then frameOpen.ah = true
  elseif event == "AUCTION_HOUSE_CLOSED" then frameOpen.ah = false
  elseif event == "MERCHANT_SHOW" then frameOpen.merchant = true
  elseif event == "MERCHANT_CLOSED" or event == "MERCHANT_CLOSE" then frameOpen.merchant = false
  elseif event == "MAIL_SHOW" then frameOpen.mailbox = true
  elseif event == "MAIL_CLOSED" then frameOpen.mailbox = false
  elseif event == "TRADE_SKILL_SHOW" or event == "PROFESSION_SHOW" then frameOpen.profession = true
  elseif event == "TRADE_SKILL_CLOSE" or event == "PROFESSION_CLOSE" then frameOpen.profession = false end
  updateButton()
end

local function makeButton()
  button = CreateFrame("Button", "CrowleyTradeExecutorButton", UIParent, "UIPanelButtonTemplate")
  button:SetSize(260, 34)
  button:SetPoint("CENTER", UIParent, "CENTER", 0, 0)
  if button.SetMovable then button:SetMovable(true) end
  if button.EnableMouse then button:EnableMouse(true) end
  if button.RegisterForDrag then button:RegisterForDrag("LeftButton") end
  if button.SetScript then
    button:SetScript("OnDragStart", function(self) if self.StartMoving then self:StartMoving() end end)
    button:SetScript("OnDragStop", function(self) if self.StopMovingOrSizing then self:StopMovingOrSizing() end end)
    button:SetScript("OnClick", function() click("button") end)
  end
  button:Hide()
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
  state.index = 1
  for seq = 1, #state.queue.actions do
    if unresolved[seq] then
      state.index, state.blocked, state.ambiguous = seq, true, true
      ns.Print("queue blocked: ambiguous_history")
      return
    end
  end
  for seq = 1, #state.queue.actions do
    if not terminal[seq] or (terminal[seq] ~= "succeeded" and terminal[seq] ~= "skipped") then
      state.index = seq
      if terminal[seq] == "refused" or terminal[seq] == "failed" then state.blocked = true end
      return
    end
    state.index = seq + 1
  end
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
  if not button or type(button.IsShown) ~= "function" then return false end
  local ok, shown = pcall(function() return button:IsShown() end)
  return ok and shown == true
end

local function clickReady()
  if not state or not state.valid or not currentAction() or not buttonIsShown() then return false end
  local accepted = clickAdmission(currentAction(), GetTime())
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
  if time() >= state.queue.expiresAt then ns.Print("arm refused: expired") return false end
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
  requires = { "Core" },
  onLoad = function()
    if CROWLEY_QUEUE ~= nil and CROWLEY_QUEUE ~= false then loadQueue() end
  end,
})

-- With Queue.lua's false placeholder, the executor creates no frame and
-- registers no events, leaving the scanner's live surface untouched.
if CROWLEY_QUEUE ~= nil and CROWLEY_QUEUE ~= false then
  ns.On("AUCTION_HOUSE_SHOW", eventFrame)
  ns.On("AUCTION_HOUSE_CLOSED", eventFrame)
  ns.On("MERCHANT_SHOW", eventFrame)
  ns.On("MERCHANT_CLOSED", eventFrame)
  ns.On("MERCHANT_CLOSE", eventFrame)
  ns.On("MAIL_SHOW", eventFrame)
  ns.On("MAIL_CLOSED", eventFrame)
  ns.On("TRADE_SKILL_SHOW", eventFrame)
  ns.On("TRADE_SKILL_CLOSE", eventFrame)
  ns.On("PROFESSION_SHOW", eventFrame)
  ns.On("PROFESSION_CLOSE", eventFrame)
  ns.On("COMMODITY_PRICE_UPDATED", commodityPrice)
  ns.On("COMMODITY_PURCHASE_SUCCEEDED", eventFailure)
  ns.On("COMMODITY_PURCHASE_FAILED", eventFailure)
  ns.On("AUCTION_HOUSE_AUCTION_CREATED", eventFailure)
  ns.On("AUCTION_CANCELED", eventFailure)
  ns.On("UI_ERROR_MESSAGE", eventFailure)
  ns.On("BAG_UPDATE", eventFailure)
  ns.On("BAG_UPDATE_DELAYED", eventFailure)
  ns.On("MAIL_INBOX_UPDATE", eventFailure)
  ns.On("MAIL_UPDATE", eventFailure)
  ns.On("MERCHANT_UPDATE", eventFailure)
  ns.On("TRADE_SKILL_UPDATE", eventFailure)
end
