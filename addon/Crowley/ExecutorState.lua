local _, ns = ...

-- Pure queue validation and execution-state transitions.  This module accepts
-- plain tables and a generated schema; it never reads a WoW namespace.
local State = {}
ns.ExecutorState = State

local MAX_SAFE = 9007199254740991
local MAX_U32 = 4294967295
local MAX_ACTIONS = 50
local DURATIONS = { [12] = true, [24] = true, [48] = true }

local function copy(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for key, child in pairs(value) do result[key] = copy(child) end
  return result
end

local function exactKeys(value, allowed)
  if type(value) ~= "table" then return false end
  for key in pairs(value) do if not allowed[key] then return false end end
  for key in pairs(allowed) do if value[key] == nil then return false end end
  return true
end

local function integer(value, minimum, maximum)
  return type(value) == "number" and value >= minimum and value <= maximum and value == math.floor(value)
end

local function validAction(action, index, schemas)
  if type(action) ~= "table" or type(action.kind) ~= "string" then return false, "action " .. index .. " has an unknown kind" end
  local fields = schemas.actionFields[action.kind]
  if not fields then return false, "action " .. index .. " has an unknown kind " .. tostring(action.kind) end
  if not exactKeys(action, fields) then return false, "action " .. index .. " has unknown or missing fields" end
  if action.seq ~= index or not integer(action.seq, 1, MAX_SAFE) then return false, "action " .. index .. " has a non-contiguous seq" end
  if action.kind == "buy_commodity" then
    if not integer(action.itemID, 1, MAX_SAFE) or not integer(action.quantity, 1, MAX_SAFE) or not integer(action.maxUnitPrice, 1, MAX_SAFE) then return false, "action " .. index .. " has an invalid positive integer" end
  elseif action.kind == "post_commodity" then
    if not integer(action.itemID, 1, MAX_SAFE) or not integer(action.quantity, 1, MAX_SAFE) or not integer(action.unitPrice, 1, MAX_SAFE) or not DURATIONS[action.durationHours] then return false, "action " .. index .. " has an invalid post field" end
  elseif action.kind == "cancel_auction" then
    if not integer(action.auctionID, 1, MAX_SAFE) then return false, "action " .. index .. " has an invalid auctionID" end
  elseif action.kind == "vendor_sell" or action.kind == "vendor_buy" then
    if not integer(action.itemID, 1, MAX_SAFE) or not integer(action.quantity, 1, MAX_SAFE) then return false, "action " .. index .. " has an invalid positive integer" end
  elseif action.kind == "craft" then
    if not integer(action.recipeID, 1, MAX_SAFE) or not integer(action.times, 1, MAX_SAFE) then return false, "action " .. index .. " has an invalid positive integer" end
  elseif action.kind == "mail_collect" and not integer(action.maxMessages, 1, MAX_U32) then
    return false, "action " .. index .. " has an invalid maxMessages"
  end
  return true
end

function State.ValidateQueue(queue, schemas)
  if type(schemas) ~= "table" or type(schemas.crowleyQueue) ~= "table" or type(schemas.actionFields) ~= "table" then
    return false, "wire schemas unavailable"
  end
  if type(queue) ~= "table" then return false, "queue is not a table" end
  if not exactKeys(queue, schemas.crowleyQueue) then return false, "queue has unknown or missing fields" end
  if queue.format ~= 1 then return false, "queue format is invalid" end
  if type(queue.queueId) ~= "string" or queue.queueId == "" or #queue.queueId > 192 then return false, "queueId is invalid" end
  if not integer(queue.generatedAt, 1, MAX_SAFE) or not integer(queue.expiresAt, 1, MAX_SAFE) or queue.expiresAt <= queue.generatedAt then return false, "queue expiry is invalid" end
  if type(queue.house) ~= "string" or queue.house == "" or #queue.house > 256 then return false, "queue house is invalid" end
  if type(queue.character) ~= "string" or queue.character == "" or #queue.character > 256 then return false, "queue character is invalid" end
  if type(queue.armed) ~= "boolean" then return false, "queue armed is invalid" end
  if type(queue.actions) ~= "table" then return false, "queue actions is not an array" end
  local count = #queue.actions
  if count > MAX_ACTIONS then return false, "queue has more than 50 actions" end
  for key in pairs(queue.actions) do
    if type(key) ~= "number" or key < 1 or key > count or key ~= math.floor(key) then return false, "queue actions is not contiguous" end
  end
  for index = 1, count do
    local ok, reason = validAction(queue.actions[index], index, schemas)
    if not ok then return false, reason end
  end
  return true
end

function State.Advance(current)
  if type(current) ~= "table" or type(current.index) ~= "number" then error("executor state is malformed") end
  local nextState = {}
  for key, value in pairs(current) do nextState[key] = value end
  nextState.index = current.index + 1
  nextState.phase, nextState.inFlight, nextState.result, nextState.blocked = nil, false, nil, false
  nextState.latched, nextState.cancelReason, nextState.cancelAttempted = false, nil, false
  nextState.mailIndex, nextState.mailCount, nextState.mailProcessed, nextState.beforeMailCount = nil, nil, nil, nil
  nextState.stacks, nextState.stackIndex = nil, nil
  nextState.beforeBagCount, nextState.expectedBagCount, nextState.expectedItemID = nil, nil, nil
  return nextState
end

local function shallowCopy(current)
  local nextState = {}
  for key, value in pairs(current) do nextState[key] = value end
  return nextState
end

local function eventNumber(event, field)
  if type(event[field]) ~= "number" then error("executor state " .. field .. " event value is malformed") end
  return event[field]
end

local function eventBoolean(event, field)
  if type(event[field]) ~= "boolean" then error("executor state " .. field .. " event value is malformed") end
  return event[field]
end

function State.Reduce(current, event)
  if type(current) ~= "table" or type(event) ~= "table" or type(event.type) ~= "string" then
    error("executor state reducer input is malformed")
  end
  if event.type == "advance" then return State.Advance(current) end

  local nextState = shallowCopy(current)
  if event.type == "click" then
    local source = event.source
    if source ~= "button" and source ~= "directAPI" then error("executor state click source is malformed") end
    if type(current.clickOrdinal) ~= "number" then error("executor state click ordinal is malformed") end
    if source == "button" then
      if current.clickOrdinal >= MAX_U32 then error("executor click ordinal exhausted") end
      nextState.clickOrdinal = current.clickOrdinal + 1
    end
    nextState.lastClickSource = source
  elseif event.type == "click_at" then
    nextState.lastClickAt = eventNumber(event, "at")
  elseif event.type == "started" then
    if type(event.result) ~= "table" then error("executor state started result is malformed") end
    nextState.result, nextState.inFlight = copy(event.result), true
  elseif event.type == "phase" then
    if type(event.phase) ~= "string" then error("executor state phase is malformed") end
    nextState.phase = event.phase
  elseif event.type == "prepare_sell" then
    if type(event.stacks) ~= "table" or type(event.stackIndex) ~= "number" then error("executor state sell preparation is malformed") end
    nextState.stacks, nextState.stackIndex, nextState.phase = event.stacks, event.stackIndex, "sell_stacks"
  elseif event.type == "clear_stacks" then
    nextState.stacks = nil
  elseif event.type == "prepare_vendor_buy" or event.type == "prepare_craft" then
    if type(event.beforeBagCount) ~= "number" or type(event.expectedBagCount) ~= "number" or
        (event.type == "prepare_vendor_buy" and type(event.expectedItemID) ~= "number") then
      error("executor state bag preparation is malformed")
    end
    nextState.phase = event.type == "prepare_vendor_buy" and "await_vendor_buy" or "await_craft"
    nextState.beforeBagCount, nextState.expectedBagCount, nextState.expectedItemID =
      event.beforeBagCount, event.expectedBagCount, event.expectedItemID
  elseif event.type == "prepare_mail" then
    if type(event.mailIndex) ~= "number" or type(event.mailCount) ~= "number" or type(event.mailProcessed) ~= "number" then
      error("executor state mail preparation is malformed")
    end
    nextState.mailIndex, nextState.mailCount, nextState.mailProcessed, nextState.phase =
      event.mailIndex, event.mailCount, event.mailProcessed, "mail_items"
  elseif event.type == "mail_wait" then
    nextState.phase, nextState.beforeMailCount = "mail_wait", event.beforeMailCount
    if type(event.beforeMailCount) ~= "number" then error("executor state mail wait is malformed") end
  elseif event.type == "mail_progress" then
    if type(event.mailProcessed) ~= "number" or type(event.mailIndex) ~= "number" then
      error("executor state mail progress is malformed")
    end
    nextState.mailProcessed, nextState.mailIndex, nextState.phase = event.mailProcessed, event.mailIndex, "mail_items"
  elseif event.type == "sell_progress" then
    if type(event.stackIndex) ~= "number" then error("executor state sell progress is malformed") end
    nextState.stackIndex, nextState.phase = event.stackIndex, "sell_stacks"
  elseif event.type == "cancel_attempted" then
    nextState.cancelAttempted = true
  elseif event.type == "result_detail" then
    if type(current.result) ~= "table" or type(event.detail) ~= "table" then error("executor state result detail is malformed") end
    nextState.result = copy(current.result)
    nextState.result.detail = copy(event.detail)
  elseif event.type == "blocked" then
    nextState.latched, nextState.blocked = eventBoolean(event, "latched"), eventBoolean(event, "blocked")
    if event.cancelReason ~= nil then
      if type(event.cancelReason) ~= "string" then error("executor state cancel reason is malformed") end
      nextState.cancelReason = event.cancelReason
    end
    if event.phase ~= nil then
      if type(event.phase) ~= "string" then error("executor state blocked phase is malformed") end
      nextState.phase = event.phase
    end
  elseif event.type == "finished" then
    if type(event.result) ~= "table" then error("executor state finished result is malformed") end
    nextState.result, nextState.inFlight, nextState.phase, nextState.latched = copy(event.result), false, nil, false
    nextState.blocked = eventBoolean(event, "blocked")
  elseif event.type == "reconcile" then
    nextState.index = eventNumber(event, "index")
    nextState.blocked, nextState.ambiguous = eventBoolean(event, "blocked"), eventBoolean(event, "ambiguous")
  else
    error("executor state reducer received unknown event " .. event.type)
  end
  return nextState
end

ns.RegisterModule("ExecutorState", { requires = { "Core" }, onLoad = function() end })
