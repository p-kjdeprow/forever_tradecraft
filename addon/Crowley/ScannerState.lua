local _, ns = ...

-- Pure scanner model.  This file intentionally has no WoW globals: callers
-- supply native observations and receive plain Lua tables back.
local State = {}
ns.ScannerState = State

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

function State.Copy(value) return copy(value) end

function State.PlanOrder(itemIDs, cursor)
  if type(itemIDs) ~= "table" then error("scanner state itemIDs must be an array") end
  if #itemIDs == 0 then error("scanner state itemIDs must not be empty") end
  local start = 1
  if cursor ~= nil then
    local found = false
    for index, itemID in ipairs(itemIDs) do
      if itemID == cursor then start, found = index + 1, true break end
    end
    if not found then error("scanner state cursor is not in the plan") end
  end
  if start > #itemIDs then start = 1 end
  local result = {}
  for offset = 0, #itemIDs - 1 do
    result[#result + 1] = itemIDs[((start + offset - 1) % #itemIDs) + 1]
  end
  return result
end

-- Items one sweep attempts, in plan order: every core/discovery item plus cold slice k, where cold
-- item i (0-based, pools.cold order) is in slice i % N and k is the persisted rotation counter.
-- Returns the ids and {index = k, count = N}, or nil for a plan without cold items.
function State.SweepItems(plan, k)
  local slices = plan.coldSlices
  if type(slices) ~= "number" or slices < 0 or slices ~= math.floor(slices) then
    error("scanner state plan coldSlices is malformed")
  end
  if slices == 0 then return plan.itemIDs, nil end
  if type(k) ~= "number" or k < 0 or k >= slices or k ~= math.floor(k) then
    error("scanner state cold slice index is out of range")
  end
  local chosen, cold = {}, {}
  for index, itemID in ipairs(plan.pools.cold) do
    cold[itemID] = true
    if (index - 1) % slices == k then chosen[itemID] = true end
  end
  local ids = {}
  for _, itemID in ipairs(plan.itemIDs) do
    if not cold[itemID] or chosen[itemID] then ids[#ids + 1] = itemID end
  end
  return ids, { index = k, count = slices }
end

function State.PriceLevels(rows)
  if type(rows) ~= "table" then error("scanner state rows must be a table") end
  local result, seen = {}, {}
  for _, row in ipairs(rows) do
    if type(row) ~= "table" or type(row.unitPrice) ~= "table" or
        type(row.unitPrice.numerator) ~= "number" or type(row.unitPrice.denominator) ~= "number" then
      error("scanner state row has no rational unit price")
    end
    local marker = row.unitPrice.numerator .. ":" .. row.unitPrice.denominator
    if not seen[marker] then
      seen[marker] = true
      result[#result + 1] = row.unitPrice
    end
  end
  return result
end

function State.Coverage(rows, depth, complete, reason)
  if type(depth) ~= "number" or depth < 1 or depth ~= math.floor(depth) then
    error("scanner state depth must be a positive integer")
  end
  if type(complete) ~= "boolean" then error("scanner state completeness must be boolean") end
  local levels = State.PriceLevels(rows)
  if reason ~= nil then return { coverage = "incomplete", sourceComplete = false, reasonCode = reason } end
  if #levels > depth then
    return { coverage = "bounded", sourceComplete = complete, reasonCode = "ok", cutoff = levels[depth], higherPriceWitness = levels[depth + 1] }
  end
  if complete then
    return { coverage = "full", sourceComplete = true, reasonCode = "ok" }
  end
  return { coverage = "incomplete", sourceComplete = false, reasonCode = "query_timeout" }
end

function State.NewSweep(plan, house, context, startedAt, generation)
  if type(plan) ~= "table" or type(plan.itemIDs) ~= "table" then error("scanner state plan is malformed") end
  if type(house) ~= "table" or type(context) ~= "table" then error("scanner state identity is malformed") end
  if type(startedAt) ~= "number" or type(generation) ~= "number" then error("scanner state timestamps are malformed") end
  return {
    generation = generation, plan = copy(plan), order = State.PlanOrder(plan.itemIDs), nextOrdinal = 1,
    observations = {}, startedAt = startedAt, house = copy(house), context = copy(context), phase = "item",
  }
end

function State.Reduce(previous, event)
  if type(previous) ~= "table" or type(event) ~= "table" or type(event.type) ~= "string" then
    error("scanner state reducer input is malformed")
  end
  local nextState = copy(previous)
  if event.type == "item_started" then
    if type(event.item) ~= "table" or type(event.cursor) ~= "number" then
      error("scanner state item_started event is malformed")
    end
    nextState.item, nextState.cursor = event.item, event.cursor
  elseif event.type == "item_completed" then
    if type(event.itemID) ~= "number" or type(event.observation) ~= "table" then
      error("scanner state item_completed event is malformed")
    end
    nextState.observations[tostring(event.itemID)] = copy(event.observation)
    if type(event.nextOrdinal) ~= "number" or type(event.cursor) ~= "number" then
      error("scanner state item_completed transition is malformed")
    end
    nextState.cursor, nextState.nextOrdinal, nextState.item = event.cursor, event.nextOrdinal, nil
  elseif event.type == "owned_started" then
    nextState.phase, nextState.ownedHandler = "owned", event.handler
  elseif event.type == "owned_completed" then
    if type(event.owned) ~= "table" then error("scanner state owned_completed event is malformed") end
    nextState.owned, nextState.ownedHandler = copy(event.owned), nil
  elseif event.type == "finished" then
    nextState.phase = "finished"
  elseif event.type == "aborted" then
    if type(event.reason) ~= "string" or event.reason == "" then error("scanner state abort reason is malformed") end
    nextState.phase, nextState.reason = "finished", event.reason
  else
    error("scanner state reducer received unknown event " .. event.type)
  end
  return nextState
end

ns.RegisterModule("ScannerState", { requires = { "Core" }, onLoad = function() end })
