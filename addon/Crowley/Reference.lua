local _, ns = ...

-- Item data is captured independently of Scanner.lua.  In particular, this
-- module never changes the scanner's cursor, schedule, or saved sweep state.
local Reference = {}
ns.Reference = Reference

local MAX_SAFE = 9007199254740991
local SMALLINT_MAX = 32767
local INTEGER_MAX = 2147483647
local LOAD_TIMEOUT = 30
local MAX_OUTSTANDING = 50
local INFO_FIELDS = {
  "quality", "itemLevel", "minLevel", "classID", "subclassID", "maxStack",
  "sellPrice", "bindType", "expansionID", "isCraftingReagent",
}
local running

local function validItemIDs(value)
  if type(value) ~= "table" then return false end
  local count = #value
  if count == 0 then return false end
  for key in pairs(value) do
    if type(key) ~= "number" or key < 1 or key > count or key ~= math.floor(key) then return false end
  end
  local seen = {}
  for index = 1, count do
    local itemID = value[index]
    if not ns.Util.IsInteger(itemID, 1, MAX_SAFE) or seen[itemID] then return false end
    seen[itemID] = true
  end
  return true
end

local function recordInvalid(item, field)
  item.status = "invalid_native"
  item.field = field
end

local function readInfo(itemID)
  local name, link, quality, itemLevel, minLevel, itemType, itemSubType, stackCount,
    equipLoc, texture, sellPrice, classID, subclassID, bindType, expansionID, setID,
    isCraftingReagent = C_Item.GetItemInfo(itemID)
  local values = {
    quality = quality, itemLevel = itemLevel, minLevel = minLevel, classID = classID,
    subclassID = subclassID, maxStack = stackCount, sellPrice = sellPrice,
    bindType = bindType, expansionID = expansionID, isCraftingReagent = isCraftingReagent,
  }
  for _, field in ipairs(INFO_FIELDS) do
    local value = values[field]
    if field == "isCraftingReagent" then
      if type(value) ~= "boolean" then return nil, field end
    else
      local maximum = MAX_SAFE
      if field == "quality" or field == "bindType" then
        maximum = SMALLINT_MAX
      elseif field ~= "sellPrice" then
        maximum = INTEGER_MAX
      end
      if not ns.Util.IsInteger(value, field == "maxStack" and 1 or 0, maximum) then return nil, field end
    end
  end
  return values, nil
end

local function deposit(capture, itemID, maxStack)
  if not capture.ahOpen then return { kind = "unqueried", reason = "ah_closed" } end
  local quantities = { 1 }
  if maxStack ~= 1 then quantities[2] = maxStack end
  local values, sawNumber, sawNil, badType = {}, false, false, nil
  for duration = 1, 3 do
    for _, quantity in ipairs(quantities) do
      local ok, result = pcall(C_AuctionHouse.CalculateCommodityDeposit, itemID, duration, quantity)
      if not ok then
        badType = badType or "error"
      elseif result == nil then
        sawNil = true
      elseif type(result) ~= "number" then
        badType = badType or type(result)
      elseif not ns.Util.IsInteger(result, 0, MAX_SAFE) then
        badType = badType or "number"
      else
        sawNumber = true
        values[#values + 1] = { duration = duration, quantity = quantity, copper = result }
      end
    end
  end
  if badType or (sawNumber and sawNil) then return { kind = "error", detail = badType or "nil" } end
  if not sawNumber then return { kind = "not_commodity" } end
  return { kind = "commodity", values = values }
end

local function finish(capture)
  if running ~= capture then return end
  running = nil
  ns.Off("ITEM_DATA_LOAD_RESULT", capture.handler)
  local finishedAt = time()
  ns.db.reference = {
    format = 1,
    id = capture.id,
    house = capture.house,
    context = capture.context,
    startedAt = capture.startedAt,
    finishedAt = finishedAt,
    ahOpen = capture.ahOpen,
    items = capture.items,
  }
  local counts = { ok = 0, not_found = 0, load_timeout = 0, invalid_native = 0 }
  for _, item in pairs(capture.items) do counts[item.status] = (counts[item.status] or 0) + 1 end
  ns.Print(("reference saved: ok=%d not_found=%d load_timeout=%d invalid=%d - /reload to upload")
    :format(counts.ok, counts.not_found, counts.load_timeout, counts.invalid_native))
end

local function pump(capture)
  if running ~= capture then return end
  while capture.outstandingCount < MAX_OUTSTANDING and capture.nextIndex <= #capture.ids do
    local itemID = capture.ids[capture.nextIndex]
    capture.nextIndex = capture.nextIndex + 1
    capture.outstanding[itemID] = true
    capture.outstandingCount = capture.outstandingCount + 1
    C_Item.RequestLoadItemDataByID(itemID)
  end
  if capture.outstandingCount == 0 and capture.nextIndex > #capture.ids then finish(capture) end
end

local function loaded(capture, _, itemID, success)
  if running ~= capture or not capture.outstanding[itemID] then return end
  capture.outstanding[itemID] = nil
  capture.outstandingCount = capture.outstandingCount - 1
  local item = { itemID = itemID }
  if success == false then
    item.status = "not_found"
  elseif success == true then
    local info, field = readInfo(itemID)
    if not info then
      recordInvalid(item, field)
    else
      item.status = "ok"
      item.info = info
      item.deposit = deposit(capture, itemID, info.maxStack)
    end
  else
    error("Crowley received a malformed ITEM_DATA_LOAD_RESULT success value")
  end
  capture.items[tostring(itemID)] = item
  pump(capture)
end

local function deadline(capture)
  if running ~= capture then return end
  for itemID in pairs(capture.outstanding) do
    capture.outstanding[itemID] = nil
    capture.outstandingCount = capture.outstandingCount - 1
    capture.items[tostring(itemID)] = { itemID = itemID, status = "load_timeout" }
  end
  for index = capture.nextIndex, #capture.ids do
    local itemID = capture.ids[index]
    capture.items[tostring(itemID)] = { itemID = itemID, status = "load_timeout" }
  end
  finish(capture)
end

function Reference.InProgress() return running ~= nil end

function Reference.Start()
  if ns.Scanner.InProgress() then return false, "sweep_active" end
  if running then return false, "capture_running" end
  local ids = CROWLEY_DATA and CROWLEY_DATA.referenceItemIDs
  if type(ids) == "table" and next(ids) == nil then return false, "reference_item_ids_empty" end
  if not validItemIDs(ids) then return false, "reference_item_ids_unavailable" end
  local house = ns.Context.House()
  local context = ns.Context.Snapshot()
  local startedAt = time()
  local capture = {
    ids = {}, nextIndex = 1, outstanding = {}, outstandingCount = 0, items = {},
    house = house, context = context, startedAt = startedAt, ahOpen = ns.ahOpen == true,
  }
  for index = 1, #ids do capture.ids[index] = ids[index] end
  capture.id = ns.Util.NewID({ house.key, startedAt, context.character }, 0, 9999)
  capture.handler = function(event, itemID, success) loaded(capture, event, itemID, success) end
  capture.deadline = function() deadline(capture) end
  running = capture
  ns.On("ITEM_DATA_LOAD_RESULT", capture.handler)
  C_Timer.After(LOAD_TIMEOUT, capture.deadline)
  pump(capture)
  return true
end

ns.RegisterModule("Reference", { requires = { "Core" }, onLoad = function() ns.Reference = Reference end })
