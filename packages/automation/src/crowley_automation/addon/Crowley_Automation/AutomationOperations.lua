local _, ns = ...

local Operations = {}
ns.AutomationOperations = Operations

local MAX_SAFE, MAX_ITEM_KEY_BYTES, MAX_ACTION_BYTES = 9007199254740991, 128, 128
local MAX_MARKET_RESULTS, MAX_MARKET_MORE_REQUESTS, MAX_MANAGED_IDS = 100000, 8, 1024
local MARKET_QUERY_TIMEOUT_SECONDS, MARKET_ROWS_PER_UPDATE = 20, 256
local DURATIONS = { [12] = 1, [24] = 2, [48] = 3 }
local NATIVE_KINDS = {
  post_commodity = true, post_item = true, cancel_auction = true, collect_mail = true, collect_return = true,
}
local FIELD_SETS = {
  post_commodity = { itemKey = true, quantity = true, unitPriceCopper = true, durationHours = true, maxDepositCopper = true },
  post_item = { itemKey = true, quantity = true, unitPriceCopper = true, durationHours = true, maxDepositCopper = true },
  cancel_auction = { auctionId = true, maxCancelCostCopper = true },
  collect_mail = { messageLimit = true },
  collect_return = { itemKey = true, quantity = true, mailFingerprint = true },
  interact = { targetId = true },
  move_route = { routeId = true },
}

local function copy(source)
  local result = {}
  for key, value in pairs(source) do result[key] = value end
  return result
end

local function exactInteger(value, minimum, maximum)
  return type(value) == "number" and value == math.floor(value) and value >= minimum and value <= maximum
end

local function addSafeInteger(total, value, label)
  if not exactInteger(total, 0, MAX_SAFE) or not exactInteger(value, 0, MAX_SAFE) or total > MAX_SAFE - value then
    error(label .. " exceeds exact integer range", 0)
  end
  return total + value
end

local function finite(value)
  return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

local function integerText(text, minimum, maximum)
  if text == "0" and minimum == 0 then return 0 end
  if type(text) ~= "string" or not text:match("^[1-9][0-9]*$") then return nil end
  local value = tonumber(text)
  if not exactInteger(value, minimum, maximum) or string.format("%.0f", value) ~= text then return nil end
  return value
end

local function tokens(message)
  if type(message) ~= "string" or #message + 7 > MAX_ACTION_BYTES or message:find("[^\032-\126]") then
    return nil, "chat_message_invalid"
  end
  local result = {}
  for value in message:gmatch("%S+") do result[#result + 1] = value end
  if #result == 0 or table.concat(result, " ") ~= message then return nil, "chat_spacing_invalid" end
  return result
end

local function truthyBoolean(value)
  return type(value) == "boolean"
end

local function requireFunction(owner, name, label)
  if type(owner) ~= "table" or type(owner[name]) ~= "function" then error(label .. " unavailable", 0) end
  return owner[name]
end

local function playerIdentity()
  if type(GetBuildInfo) ~= "function" or type(GetCurrentRegion) ~= "function" or
      type(UnitName) ~= "function" or type(GetRealmName) ~= "function" or type(UnitFactionGroup) ~= "function" then
    error("native player identity APIs unavailable", 0)
  end
  local _, build, _, interface = GetBuildInfo()
  local region = GetCurrentRegion()
  local character, realm = UnitName("player"), GetRealmName()
  local faction = UnitFactionGroup("player")
  if type(build) ~= "string" or build == "" or not exactInteger(interface, 1, 100000) or
      not exactInteger(region, 1, 4294967295) or type(character) ~= "string" or character == "" or
      type(realm) ~= "string" or realm == "" or type(faction) ~= "string" or faction == "" then
    error("native player identity facts malformed", 0)
  end
  return { build = build, interface = interface, region = region, character = character, realm = realm, faction = faction }
end

local function identitiesEqual(left, right)
  return left and right and left.build == right.build and left.interface == right.interface and
    left.region == right.region and left.character == right.character and left.realm == right.realm and left.faction == right.faction
end

local function isAuctionHouseOpen()
  if _G.AuctionHouseFrame == nil or type(_G.AuctionHouseFrame.IsShown) ~= "function" then
    error("auction house frame visibility unavailable", 0)
  end
  return AuctionHouseFrame:IsShown() == true
end

local function isMailOpen()
  if _G.MailFrame == nil or type(_G.MailFrame.IsShown) ~= "function" then error("mail frame visibility unavailable", 0) end
  return MailFrame:IsShown() == true
end

local function money()
  if type(GetMoney) ~= "function" then error("player money unavailable", 0) end
  local value = GetMoney()
  if not exactInteger(value, 0, MAX_SAFE) then error("player money malformed", 0) end
  return value
end

local function containerSlots(bag)
  local count = requireFunction(C_Container, "GetContainerNumSlots", "bag slot query")(bag)
  if not exactInteger(count, 0, 256) then error("bag slot count malformed", 0) end
  return count
end

local function captureReturns(...)
  local result = { n = select("#", ...) }
  for index = 1, result.n do result[index] = select(index, ...) end
  return result
end

local function generalFreeBagSlots()
  local query = requireFunction(C_Container, "GetContainerNumFreeSlots", "free bag slot query")
  local total = 0
  for bag = 0, 4 do
    local values = captureReturns(query(bag))
    local freeSlots, family = values[1], values[2]
    if values.n ~= 2 or not exactInteger(freeSlots, 0, 256) or not exactInteger(family, 0, MAX_SAFE) then
      error("free bag slot facts malformed", 0)
    end
    if family == 0 then total = total + freeSlots end
  end

  return total
end

local function itemAt(bag, slot)
  local info = requireFunction(C_Container, "GetContainerItemInfo", "bag item query")(bag, slot)
  if info == nil then return nil end
  if type(info) ~= "table" or not exactInteger(info.stackCount, 1, MAX_SAFE) or
      type(info.isLocked) ~= "boolean" or type(info.hyperlink) ~= "string" or info.hyperlink == "" then
    error("bag item facts malformed", 0)
  end
  local locationFactory = requireFunction(ItemLocation, "CreateFromBagAndSlot", "bag location")
  local location = locationFactory(ItemLocation, bag, slot)
  local key = requireFunction(C_AuctionHouse, "GetItemKeyFromItem", "native item identity")(location)
  local itemKey = ns.AutomationIdentity.Canonical(key, info.hyperlink)
  if not itemKey then error("bag item variant cannot be identified exactly", 0) end
  local bound = requireFunction(C_Item, "IsBound", "bound item query")(location)
  local commodityStatus = requireFunction(C_AuctionHouse, "GetItemCommodityStatus", "commodity status query")(location)
  local status = type(Enum) == "table" and Enum.ItemCommodityStatus or nil
  if type(status) ~= "table" or status.Unknown == nil or status.Item == nil or status.Commodity == nil or
      (commodityStatus ~= status.Item and commodityStatus ~= status.Commodity) then
    error("item commodity status is unavailable", 0)
  end
  local valid = requireFunction(C_AuctionHouse, "IsSellItemValid", "auctionable item query")(location, false)
  if not truthyBoolean(bound) or not truthyBoolean(valid) then error("bag item safety facts malformed", 0) end
  local itemID = key.itemID
  if not exactInteger(itemID, 1, MAX_SAFE) then error("native item ID malformed", 0) end
  return {
    bag = bag,
    slot = slot,
    location = location,
    itemKey = itemKey,
    itemLink = info.hyperlink,
    count = info.stackCount,
    locked = info.isLocked,
    bound = bound,
    commodity = commodityStatus == status.Commodity,
    itemID = itemID,
  }
end

local function allBagItems()
  local rows = {}
  for bag = 0, 4 do
    for slot = 1, containerSlots(bag) do
      local row = itemAt(bag, slot)
      if row then rows[#rows + 1] = row end
    end
  end
  return rows
end

local function ownedAuctions()
  local auction = C_AuctionHouse
  if requireFunction(auction, "HasFullOwnedAuctionResults", "owned auction completeness query")() ~= true then
    error("owned auction results are incomplete", 0)
  end
  local count = requireFunction(auction, "GetNumOwnedAuctions", "owned auction count")()
  if not exactInteger(count, 0, 100000) then error("owned auction count malformed", 0) end
  local enum = type(Enum) == "table" and Enum.AuctionStatus or nil
  if type(enum) ~= "table" or enum.Active == nil or enum.Sold == nil then error("auction status enum unavailable", 0) end
  local rows = {}
  for index = 1, count do
    local info = requireFunction(auction, "GetOwnedAuctionInfo", "owned auction query")(index)
    if type(info) ~= "table" or not exactInteger(info.auctionID, 1, MAX_SAFE) or
        not exactInteger(info.quantity, 1, MAX_SAFE) or type(info.itemLink) ~= "string" or info.itemLink == "" then
      error("owned auction facts malformed", 0)
    end
    local itemKey = ns.AutomationIdentity.Canonical(info.itemKey, info.itemLink)
    if not itemKey or (info.status ~= enum.Active and info.status ~= enum.Sold) then
      error("owned auction identity or status is malformed", 0)
    end
    local canCancel = requireFunction(auction, "CanCancelAuction", "auction cancellation permission")(info.auctionID)
    if type(canCancel) ~= "boolean" then error("auction cancellation permission malformed", 0) end
    local costKnown, cost = false, 0
    if canCancel then
      cost = requireFunction(auction, "GetCancelCost", "auction cancellation cost")(info.auctionID)
      if not exactInteger(cost, 0, MAX_SAFE) then error("auction cancellation cost malformed", 0) end
      costKnown = true
    end
    local buyoutKnown = info.buyoutAmount ~= nil
    local buyout = buyoutKnown and info.buyoutAmount or 0
    if not exactInteger(buyout, 0, MAX_SAFE) then error("owned auction buyout malformed", 0) end
    rows[#rows + 1] = {
      auctionId = info.auctionID,
      itemKey = itemKey,
      itemLink = info.itemLink,
      quantity = info.quantity,
      buyoutKnown = buyoutKnown,
      buyoutCopper = buyout,
      status = info.status == enum.Active and "active" or "sold",
      cancelAllowed = canCancel,
      cancelCostKnown = costKnown,
      cancelCostCopper = cost,
    }
  end
  return rows
end

local function inboxMessage(index)
  local getHeader = requireFunction(_G, "GetInboxHeaderInfo", "mail header query")
  local header = captureReturns(getHeader(index))
  if header.n ~= 15 then error("mail header arity malformed", 0) end
  local packageIcon, stationeryIcon, sender, subject, mailMoney, cod, daysLeft, nativeItemCount,
    wasRead, wasReturned, textCreated, canReply, isGM, firstItemQuantity, firstItemLink =
    header[1], header[2], header[3], header[4], header[5], header[6], header[7], header[8], header[9],
    header[10], header[11], header[12], header[13], header[14], header[15]
  if type(sender) ~= "string" or type(subject) ~= "string" or not exactInteger(mailMoney, 0, MAX_SAFE) or
      not exactInteger(cod, 0, MAX_SAFE) or
      (nativeItemCount ~= nil and not exactInteger(nativeItemCount, 1, 16)) or
      not finite(daysLeft) or daysLeft < 0 or daysLeft > 365 or
      type(wasReturned) ~= "boolean" or type(isGM) ~= "boolean" or
      (nativeItemCount ~= nil and (not exactInteger(firstItemQuantity, 1, MAX_SAFE) or
        type(firstItemLink) ~= "string" or firstItemLink == "")) or
      (nativeItemCount == nil and (firstItemQuantity ~= nil or firstItemLink ~= nil)) then
    error("mail header facts malformed", 0)
  end
  local attachmentCount = nativeItemCount or 0
  local ids, quantities, attachments = {}, {}, {}
  local hasItem = requireFunction(_G, "HasInboxItem", "mail attachment presence query")
  local getItem = requireFunction(_G, "GetInboxItem", "mail attachment query")
  for attachment = 1, 16 do
    local presence = captureReturns(hasItem(index, attachment))
    if presence.n ~= 1 then error("mail attachment presence arity malformed", 0) end
    local present = presence[1]
    if type(present) ~= "boolean" then error("mail attachment presence malformed", 0) end
    if present then
      local values = captureReturns(getItem(index, attachment))
      local itemName, itemID, itemCount = values[1], values[2], values[4]
      if values.n ~= 7 or type(itemName) ~= "string" or itemName == "" or
          not exactInteger(itemID, 1, MAX_SAFE) or not exactInteger(itemCount, 1, MAX_SAFE) then
        error("mail attachment identity unavailable", 0)
      end
      ids[#ids + 1], quantities[#quantities + 1] = itemID, itemCount
      local linkValues = captureReturns(requireFunction(_G, "GetInboxItemLink", "mail attachment link query")(
        index, attachment
      ))
      if linkValues.n ~= 1 or type(linkValues[1]) ~= "string" or linkValues[1] == "" then
        error("mail_attachment_link_unavailable", 0)
      end
      local itemKey = ns.AutomationIdentity.CommodityKeyFromLink(linkValues[1], itemID) or false
      attachments[#attachments + 1] = {
        attachment = attachment,
        itemID = itemID,
        itemKey = itemKey,
        quantity = itemCount,
      }
    end
  end
  if #ids ~= attachmentCount then error("mail header and attachment slots disagree", 0) end
  local deleteResult = captureReturns(requireFunction(_G, "InboxItemCanDelete", "mail return/delete state query")(index))
  local canDelete = deleteResult[1]
  if deleteResult.n ~= 1 or type(canDelete) ~= "boolean" then error("mail return/delete state malformed", 0) end
  return {
    index = index,
    moneyCopper = mailMoney,
    codCopper = cod,
    attachmentCount = attachmentCount,
    attachmentItemIDs = ids,
    attachmentQuantities = quantities,
    attachments = attachments,
    daysLeft = daysLeft,
    returned = wasReturned,
    canDelete = canDelete,
    isGM = isGM,
    sender = sender,
    subject = subject,
  }
end

local function inboxMessages()
  local counts = captureReturns(requireFunction(_G, "GetInboxNumItems", "mail count query")())
  local count, total = counts[1], counts[2]
  if counts.n ~= 2 then error("mail count arity malformed", 0) end
  if not exactInteger(count, 0, 100) or not exactInteger(total, count, 100) then error("mail count malformed", 0) end
  if count ~= total then error("mail inbox is incomplete", 0) end
  local rows = {}
  for index = 1, count do rows[#rows + 1] = inboxMessage(index) end
  return rows
end

local function nativeCommodityItem(itemKey)
  if type(itemKey) ~= "string" or not itemKey:match("^[1-9][0-9]*$") then return false end
  local itemID = integerText(itemKey, 1, MAX_SAFE)
  if not itemID then return false end
  local makeKey = requireFunction(C_AuctionHouse, "MakeItemKey", "commodity item identity API")
  local key = makeKey(itemID)
  if type(key) ~= "table" or key.itemID ~= itemID or key.itemLevel ~= 0 or
      key.itemSuffix ~= 0 or key.battlePetSpeciesID ~= 0 then
    error("commodity item key is not the exact base identity", 0)
  end
  local info = requireFunction(C_AuctionHouse, "GetItemKeyInfo", "commodity item-kind API")(key, false)
  if type(info) ~= "table" or info.itemID ~= itemID or type(info.isCommodity) ~= "boolean" then
    error("commodity item kind is unavailable", 0)
  end
  return info.isCommodity == true
end

local function bagCountsByID()
  local totals = {}
  local getItemInfo = requireFunction(C_Container, "GetContainerItemInfo", "raw bag count query")
  for bag = 0, 4 do
    for slot = 1, containerSlots(bag) do
      local info = getItemInfo(bag, slot)
      if info ~= nil then
        if type(info) ~= "table" or not exactInteger(info.itemID, 1, MAX_SAFE) or
            not exactInteger(info.stackCount, 1, MAX_SAFE) then
          error("raw bag count facts malformed", 0)
        end
        local total = (totals[info.itemID] or 0) + info.stackCount
        if total > MAX_SAFE then error("raw bag count total exceeds exact integer range", 0) end
        totals[info.itemID] = total
      end
    end
  end
  return totals
end

local function inspectionPageSize(kind)
  return kind == "mail" and 1 or 4
end

local function tablePage(rows, page, pageSize)
  local result = {}
  local first = page * pageSize + 1
  for index = first, math.min(first + pageSize - 1, #rows) do result[#result + 1] = rows[index] end
  return result, first + #result <= #rows
end

local inspectionCounter = 0
local inspectionCache = {}
local returnSelections = {}

local function inspectionToken()
  inspectionCounter = (inspectionCounter + 1) % 4294967296
  local uptime = math.floor(GetTime() * 1000) % 4294967296
  local randomA, randomB = math.random(0, 65535), math.random(0, 65535)
  local random = randomA * 65536 + randomB
  return string.format("%08x%08x%08x%08x", time() % 4294967296, uptime, inspectionCounter, random)
end

local function mintReturnSelection(itemKey, quantity)
  local token
  repeat token = inspectionToken() .. inspectionToken() until not returnSelections[token]
  returnSelections[token] = {
    itemKey = itemKey,
    quantity = quantity,
    expiresAt = time() + 60,
    used = false,
  }
  return token
end

local function attachReturnSelections(rows)
  returnSelections = {}
  for _, row in ipairs(rows) do
    for _, attachment in ipairs(row.attachments) do
      attachment.selectionToken = false
      if row.codCopper == 0 and not row.isGM and
          type(attachment.itemKey) == "string" and exactInteger(attachment.quantity, 1, MAX_SAFE) then
        local ok, commodity = pcall(nativeCommodityItem, attachment.itemKey)
        if ok and commodity then
          attachment.selectionToken = mintReturnSelection(attachment.itemKey, attachment.quantity)
        end
      end
    end
  end
end

local function publishInspection(kind, page, rows)
  local token = inspectionToken()
  local cache = { rows = rows, requestId = token, capturedAt = time(), sourceCount = #rows }
  inspectionCache[kind] = cache
  local pageRows, hasMore = tablePage(rows, page, inspectionPageSize(kind))
  return {
    kind = kind, page = page, rows = pageRows, hasMore = hasMore, error = "",
    requestId = token, capturedAt = cache.capturedAt, sourceCount = cache.sourceCount,
  }
end

local function inspect(kind, page, mailOpen, ownedRows)
  local rows
  if page == 0 then
    inspectionCache[kind] = nil
    if kind == "mail" then returnSelections = {} end
    if kind == "inventory" then
      rows = {}
      for _, row in ipairs(allBagItems()) do
        rows[#rows + 1] = {
          bag = row.bag, slot = row.slot, itemKey = row.itemKey, itemLink = row.itemLink,
          count = row.count, locked = row.locked, bound = row.bound, commodity = row.commodity,
        }
      end
    elseif kind == "owned" then
      if not isAuctionHouseOpen() then error("auction_house_closed", 0) end
      if type(ownedRows) ~= "table" then error("owned_query_pending", 0) end
      rows = ownedRows
    elseif kind == "mail" then
      if not mailOpen or not isMailOpen() then error("mail_closed", 0) end
      rows = {}
      for _, row in ipairs(inboxMessages()) do
        rows[#rows + 1] = {
          index = row.index, moneyCopper = row.moneyCopper, codCopper = row.codCopper,
          attachmentCount = row.attachmentCount, attachmentItemIDs = row.attachmentItemIDs,
          returned = row.returned, canDelete = row.canDelete, daysLeft = row.daysLeft,
          isGM = row.isGM, attachments = row.attachments,
        }
      end
      attachReturnSelections(rows)
    else
      error("inspection_kind_invalid", 0)
    end
    return publishInspection(kind, page, rows)
  end

  local cache = inspectionCache[kind]
  if not cache or time() - cache.capturedAt > 60 then error("inspection_snapshot_unavailable", 0) end
  rows = cache.rows
  local pageSize = inspectionPageSize(kind)
  local pageRows, hasMore = tablePage(rows, page, pageSize)
  if page * pageSize > #rows then error("inspection_page_out_of_range", 0) end
  return {
    kind = kind, page = page, rows = pageRows, hasMore = hasMore, error = "",
    requestId = cache.requestId, capturedAt = cache.capturedAt, sourceCount = cache.sourceCount,
  }
end

local function inspectError(kind, page, reason)
  return {
    kind = kind, page = page, rows = {}, hasMore = false, error = tostring(reason):sub(1, 128),
    requestId = "", capturedAt = 0, sourceCount = 0,
  }
end

local function actionFields(kind, fields, chunks)
  local expected = FIELD_SETS[kind]
  if type(expected) ~= "table" then return nil, "operation_kind_invalid" end
  for name in pairs(expected) do
    if name == "itemKey" then
      if not chunks[1] then return nil, "item_key_missing" end
      for index = 1, 3 do if chunks[index] == nil and chunks[index + 1] ~= nil then return nil, "item_key_chunks_not_consecutive" end end
      local itemKey = table.concat(chunks)
      if #itemKey > MAX_ITEM_KEY_BYTES or not ns.AutomationIdentity.Validate(itemKey) then
        return nil, "item_key_invalid"
      end
      fields.itemKey = itemKey
    elseif fields[name] == nil then
      return nil, "action_field_missing"
    end
  end
  for name in pairs(fields) do if not expected[name] then return nil, "action_field_unknown" end end
  local action = { kind = kind }
  for name, value in pairs(fields) do action[name] = value end
  if kind == "post_commodity" or kind == "post_item" then
    if action.quantity < 1 or action.quantity > 200 or action.unitPriceCopper < 1 or
        not DURATIONS[action.durationHours] or action.maxDepositCopper < 0 or
        action.unitPriceCopper * action.quantity > MAX_SAFE then return nil, "posting_bounds_invalid" end
  elseif kind == "cancel_auction" then
    if action.auctionId < 1 or action.maxCancelCostCopper < 0 then return nil, "cancel_bounds_invalid" end
  elseif kind == "collect_mail" then
    if action.messageLimit < 1 or action.messageLimit > 50 then return nil, "mail_limit_invalid" end
  elseif kind == "collect_return" then
    if action.quantity < 1 or action.quantity > MAX_SAFE or
        #action.mailFingerprint ~= 64 or not action.mailFingerprint:match("^[0-9a-f]+$") then
      return nil, "return_selection_invalid"
    end
  elseif kind == "interact" then
    if not action.targetId:match("^[a-z][a-z0-9_-]*$") then return nil, "target_id_invalid" end
  elseif kind == "move_route" then
    if not action.routeId:match("^[a-z][a-z0-9_-]*$") then return nil, "route_id_invalid" end
  end
  return action
end

local function actionError(reason)
  if type(reason) ~= "string" then return "operation_invalid" end
  return reason:sub(1, 128)
end

function Operations.New()
  if type(ns.Coordinated) ~= "boolean" then error("automation mode must be an explicit boolean", 0) end
  local saved = type(_G.CrowleyAutomationDB) == "table" and _G.CrowleyAutomationDB or {}
  _G.CrowleyAutomationDB = saved
  local pendingSaved = saved.pending
  local unresolved = pendingSaved ~= nil
  local uncertainKind = type(pendingSaved) == "table" and pendingSaved.kind or ""
  local uncertainMarket = uncertainKind ~= "collect_mail" and uncertainKind ~= "collect_return"
  local self = {
    stage = nil,
    receipt = { sequence = 0, batchId = "", ordinal = 0, stepIndex = 0,
      callbackDelivered = "unknown", postcondition = "unknown", actionComplete = "unknown", detail = "", result = false },
    inspection = {
      kind = "none", page = 0, rows = {}, hasMore = false, error = "",
      requestId = "", capturedAt = 0, sourceCount = 0,
    },
    sellerMarket = {
      requestId = "", itemKey = "", complete = false, coverage = "none", resultCount = 0,
      bestCompetitorUnitPriceCopper = false, bestCompetitorQuantity = 0, bestCompetitorResultCount = 0,
      ambiguousOwnerUnitPriceCopper = false, managedUnitPriceCopper = false, managedAuctionCount = 0,
      managedQuantity = 0, error = "", capturedAt = 0,
    },
    sellerQuery = nil,
    marketSetup = nil,
    bridge = nil,
    grantHeld = unresolved and uncertainMarket,
    uncertain = unresolved,
    uncertainMarket = unresolved and uncertainMarket,
    pending = nil,
    ownedInspection = nil,
    heldBatchId = "",
    heldExpiresAt = 0,
    lastError = unresolved and "unresolved_prior_effect" or "",
    ahOpen = false,
    mailOpen = false,
    scannerState = ns.Coordinated and "unavailable" or "not_installed",
    ahOwner = unresolved and uncertainMarket and "automation" or "none",
    identity = nil,
  }

  local function release()
    if not self.grantHeld then
      self.ahOwner = "none"
      self.heldBatchId, self.heldExpiresAt = "", 0
      self.scannerState = ns.Coordinated and (self.bridge and "available" or "unavailable") or "not_installed"
      return true
    end
    if ns.Coordinated then
      if not self.bridge or type(self.bridge.resume) ~= "function" then
        self.lastError, self.scannerState = "coordinator_unavailable", "unavailable"
        return false
      end
      local ok, reason = self.bridge.resume("crowley-automation")
      if ok ~= true then
        self.lastError = actionError(reason or "scanner_resume_refused")
        return false
      end
    end
    self.grantHeld, self.ahOwner = false, "none"
    self.heldBatchId, self.heldExpiresAt = "", 0
    self.scannerState = ns.Coordinated and "available" or "not_installed"
    return true
  end

  local function clearStage(reason)
    self.stage = nil
    if reason then self.lastError = actionError(reason) else self.lastError = "" end
    self.heldBatchId, self.heldExpiresAt = "", 0
    if not self.uncertain and not self.sellerQuery then release() end
  end

  local function failInput(reason)
    clearStage(reason)
    if self.uncertain then self.ahOwner = self.uncertainMarket and "automation" or "none" end
  end

  local function hasMarket(action)
    return action.kind == "post_commodity" or action.kind == "post_item" or action.kind == "cancel_auction"
  end

  local function acquireAuctionOwnership()
    if not self.ahOpen or not isAuctionHouseOpen() then return false, "auction_house_closed" end
    if ns.Coordinated then
      if not self.bridge then return false, "coordinator_unavailable" end
      local ok, reason = self.bridge.suspend("crowley-automation")
      if ok ~= true and reason ~= "scan_active" then
        self.scannerState = "unavailable"
        return false, actionError(reason or "scanner_suspend_refused")
      end
      self.grantHeld = true
      local quiet = self.bridge.quiescent("crowley-automation")
      if quiet ~= true then
        self.scannerState, self.ahOwner = "active", "scanner"
        return false, ""
      end
      self.scannerState, self.ahOwner = "quiescent", "automation"
    else
      self.ahOwner, self.scannerState = "automation", "not_installed"
    end
    return true, ""
  end

  local function verifyHeldAuctionOwnership()
    if not self.ahOpen or not isAuctionHouseOpen() then return false, "auction_house_closed" end
    if self.ahOwner ~= "automation" then return false, "auction_house_ownership_lost" end
    if ns.Coordinated then
      if not self.grantHeld or not self.bridge or type(self.bridge.quiescent) ~= "function" then
        return false, "auction_house_ownership_lost"
      end
      local ok, quiet = pcall(self.bridge.quiescent, "crowley-automation")
      if not ok or quiet ~= true then return false, "auction_house_ownership_lost" end
    end
    return true, ""
  end

  local function searchItemID(value)
    if exactInteger(value, 1, MAX_SAFE) then return value end
    if type(value) == "table" and exactInteger(value.itemID, 1, MAX_SAFE) then return value.itemID end
    return nil
  end

  local function noteNativeSearch(kind, value)
    local itemID = searchItemID(value)
    local request = self.sellerQuery
    if request then
      if request.issuing == kind and itemID == request.itemID then
        request.dispatchSeen = true
      else
        request.foreignQuery = true
      end
    end
  end

  local function installSearchHooks()
    if type(hooksecurefunc) ~= "function" or type(C_AuctionHouse) ~= "table" then
      self.marketHookError = "market_search_hooks_unavailable"
      return false
    end
    local hooks = {
      { "SendSearchQuery", "send" },
      { "SendSellSearchQuery", "sell" },
      { "RequestMoreCommoditySearchResults", "more" },
      { "RequestMoreItemSearchResults", "more_item" },
      { "RefreshCommoditySearchResults", "refresh" },
      { "RefreshItemSearchResults", "refresh_item" },
    }
    for _, hook in ipairs(hooks) do
      if type(C_AuctionHouse[hook[1]]) ~= "function" then
        self.marketHookError = "market_search_hook_missing_" .. hook[1]
        return false
      end
      local apiName, kind = hook[1], hook[2]
      local ok = pcall(hooksecurefunc, C_AuctionHouse, apiName, function(first, ...)
        noteNativeSearch(kind, first)
      end)
      if not ok then
        self.marketHookError = "market_search_hook_install_failed_" .. apiName
        return false
      end
    end
    return true
  end

  installSearchHooks()

  local function failedMarket(request, reason)
    if not request or self.sellerQuery ~= request then return end
    self.sellerMarket = {
      requestId = request.requestId,
      itemKey = request.itemKey,
      complete = false,
      coverage = "none",
      resultCount = 0,
      bestCompetitorUnitPriceCopper = false,
      bestCompetitorQuantity = 0,
      bestCompetitorResultCount = 0,
      ambiguousOwnerUnitPriceCopper = false,
      managedUnitPriceCopper = false,
      managedAuctionCount = 0,
      managedQuantity = 0,
      error = actionError(reason),
      capturedAt = time(),
    }
    self.sellerQuery = nil
    if not self.uncertain then release() end
  end

  local function completeMarket(request)
    if not request or self.sellerQuery ~= request then return end
    local ownershipHeld, ownershipError = verifyHeldAuctionOwnership()
    if not ownershipHeld then
      failedMarket(request, ownershipError)
      return
    end
    local bestPrice = false
    local bestQuantity, bestCount = 0, 0
    local ambiguousPrice = false
    local managedPrice = false
    local managedCount, managedQuantity = 0, 0
    for _, price in ipairs(request.prices) do
      local level = request.byPrice[price]
      if level.competitorCount > 0 and (bestPrice == false or price < bestPrice) then
        bestPrice, bestQuantity, bestCount = price, level.competitorQuantity, level.competitorCount
      end
      if level.ambiguousOwner and (ambiguousPrice == false or price < ambiguousPrice) then
        ambiguousPrice = price
      end
      if level.managedCount > 0 and (managedPrice == false or price < managedPrice) then
        managedPrice = price
      end
      managedCount = addSafeInteger(managedCount, level.managedCount, "managed auction count")
      managedQuantity = addSafeInteger(managedQuantity, level.managedQuantity, "managed auction quantity")
    end
    self.sellerMarket = {
      requestId = request.requestId,
      itemKey = request.itemKey,
      complete = true,
      coverage = "full",
      resultCount = request.resultCount,
      bestCompetitorUnitPriceCopper = bestPrice,
      bestCompetitorQuantity = bestQuantity,
      bestCompetitorResultCount = bestCount,
      ambiguousOwnerUnitPriceCopper = ambiguousPrice,
      managedUnitPriceCopper = managedPrice,
      managedAuctionCount = managedCount,
      managedQuantity = managedQuantity,
      error = "",
      capturedAt = time(),
    }
    if not self.uncertain and not release() then
      failedMarket(request, "scanner_resume_refused")
      return
    end
    self.sellerQuery = nil
  end

  local function marketQueryKey(request)
    local auction = requireFunction(C_AuctionHouse, "MakeItemKey", "native commodity ItemKey constructor")
    local key = auction(request.itemID)
    if type(key) ~= "table" or key.itemID ~= request.itemID or key.itemLevel ~= 0 or
        key.itemSuffix ~= 0 or key.battlePetSpeciesID ~= 0 then
      error("commodity_item_key_unavailable", 0)
    end
    local info = requireFunction(C_AuctionHouse, "GetItemKeyInfo", "native ItemKey information")(key, false)
    if type(info) ~= "table" or info.itemID ~= request.itemID or type(info.isCommodity) ~= "boolean" then
      error("commodity_item_kind_unavailable", 0)
    end
    if not info.isCommodity then error("seller_commodity_required", 0) end
    return key
  end

  local function throttleReady()
    local ok, ready = pcall(function()
      return requireFunction(C_AuctionHouse, "IsThrottledMessageSystemReady", "auction search throttle")()
    end)
    if not ok or type(ready) ~= "boolean" then error("auction_search_throttle_unavailable", 0) end
    return ready
  end

  local function dispatchMarketQuery(request, kind)
    local auction = C_AuctionHouse
    if type(Enum) ~= "table" or type(Enum.AuctionHouseSortOrder) ~= "table" or
        Enum.AuctionHouseSortOrder.Price == nil then
      error("auction_house_price_sort_unavailable", 0)
    end
    local sort = { { sortOrder = Enum.AuctionHouseSortOrder.Price, reverseSort = false } }
    request.issuing, request.dispatchSeen = kind, false
    request.dispatchReturned = false
    request.messagePending = true
    request.sentBarrier, request.responseBarrier, request.queryEvent = false, false, false
    local ok, result
    if kind == "send" then
      ok, result = pcall(requireFunction(auction, "SendSearchQuery", "commodity search API"),
        marketQueryKey(request), sort, true, 0, 0)
    else
      local more = requireFunction(auction, "RequestMoreCommoditySearchResults", "commodity page API")
      ok, result = pcall(more, request.itemID)
      request.moreRequests = request.moreRequests + 1
    end
    request.issuing = false
    if not ok then error(kind == "send" and "commodity_search_call_failed" or "commodity_page_call_failed", 0) end
    if kind == "more" and type(result) ~= "boolean" then error("commodity_page_result_malformed", 0) end
    if not request.dispatchSeen or request.foreignQuery then error("market_query_attribution_ambiguous", 0) end
    request.started, request.dispatchReturned = true, true
    request.awaitingEvent = true
    request.messageDeadline = time() + MARKET_QUERY_TIMEOUT_SECONDS
    if kind == "more" and result then request.nativeFullHint = true end
  end

  local function beginMarketRows(request)
    local auction = C_AuctionHouse
    local count = requireFunction(auction, "GetNumCommoditySearchResults", "commodity result count")(
      request.itemID
    )
    if not exactInteger(count, 0, MAX_MARKET_RESULTS) then error("commodity_result_count_malformed", 0) end
    local totalQuantity = requireFunction(auction, "GetCommoditySearchResultsQuantity", "commodity result quantity")(
      request.itemID
    )
    if not exactInteger(totalQuantity, 0, MAX_SAFE) then error("commodity_result_quantity_malformed", 0) end
    request.resultCount, request.nativeTotalQuantity = count, totalQuantity
    request.scanIndex, request.scannedQuantity = 1, 0
    request.byPrice, request.prices, request.auctionIDsSeen = {}, {}, {}
    request.phase = "rows"
  end

  local function scanMarketRows(request)
    local finish = math.min(request.resultCount, request.scanIndex + MARKET_ROWS_PER_UPDATE - 1)
    for index = request.scanIndex, finish do
      local row = requireFunction(C_AuctionHouse, "GetCommoditySearchResultInfo", "commodity result query")(
        request.itemID, index
      )
      if type(row) ~= "table" or row.itemID ~= request.itemID or
          not exactInteger(row.quantity, 1, MAX_SAFE) or not exactInteger(row.unitPrice, 1, MAX_SAFE) or
          not exactInteger(row.auctionID, 1, MAX_SAFE) or type(row.owners) ~= "table" or
          not exactInteger(row.totalNumberOfOwners, 1, 100000) or not exactInteger(row.numOwnerItems, 0, 100000) or
          type(row.containsOwnerItem) ~= "boolean" or type(row.containsAccountItem) ~= "boolean" then
        error("commodity_result_malformed", 0)
      end
      if request.auctionIDsSeen[row.auctionID] then error("commodity_result_repeats_auction_id", 0) end
      request.auctionIDsSeen[row.auctionID] = true
      local ownerCount = #row.owners
      if ownerCount > row.totalNumberOfOwners then error("commodity_result_owners_malformed", 0) end
      for _, owner in ipairs(row.owners) do
        if type(owner) ~= "string" or owner == "" then error("commodity_result_owners_malformed", 0) end
      end
      local price = row.unitPrice
      local level = request.byPrice[price]
      if not level then
        level = { competitorCount = 0, competitorQuantity = 0, ambiguousOwner = false, managedCount = 0, managedQuantity = 0 }
        request.byPrice[price], request.prices[#request.prices + 1] = level, price
      end
      request.scannedQuantity = addSafeInteger(request.scannedQuantity, row.quantity, "commodity result quantity")
      if request.managedIDs[row.auctionID] and row.containsOwnerItem and not row.containsAccountItem and
          row.numOwnerItems == 1 and row.totalNumberOfOwners == 1 and ownerCount == 1 then
        if not request.managedSeen[row.auctionID] then
          request.managedSeen[row.auctionID] = true
          level.managedCount = addSafeInteger(level.managedCount, 1, "managed auction count")
          level.managedQuantity = addSafeInteger(level.managedQuantity, row.quantity, "managed auction quantity")
        else
          error("commodity_managed_auction_repeated", 0)
        end
      elseif row.containsOwnerItem or row.containsAccountItem or row.numOwnerItems > 0 then
        level.ambiguousOwner = true
        if request.managedIDs[row.auctionID] then request.managedAmbiguous = true end
      else
        level.competitorCount = addSafeInteger(level.competitorCount, 1, "competitor result count")
        level.competitorQuantity = addSafeInteger(level.competitorQuantity, row.quantity, "competitor quantity")
      end
    end
    request.scanIndex = finish + 1
    if request.scanIndex > request.resultCount then
      if request.scannedQuantity ~= request.nativeTotalQuantity then error("commodity_result_quantity_mismatch", 0) end
      local managedSeenCount = 0
      for _ in pairs(request.managedSeen) do managedSeenCount = managedSeenCount + 1 end
      if request.managedAmbiguous then error("commodity_managed_ownership_ambiguous", 0) end
      if managedSeenCount ~= request.managedCount then error("commodity_managed_set_mismatch", 0) end
      table.sort(request.prices)
      completeMarket(request)
    end
  end

  local function requestOwnedResults(request)
    if request.queryError then return false, request.queryError end
    if not request.queryRequested then
      local query = C_AuctionHouse and C_AuctionHouse.QueryOwnedAuctions
      if type(query) ~= "function" then
        request.queryError = "owned_auction_query_unavailable"
        return false, request.queryError
      end
      local result = captureReturns(pcall(query, {}))
      if result[1] ~= true or result.n ~= 1 then
        request.queryError = "owned_auction_query_rejected"
        return false, request.queryError
      end
      request.queryRequested, request.queryStartedAt = true, time()
      return false, ""
    end
    if not request.queryEvent then
      if time() - request.queryStartedAt >= 15 then
        request.queryError = "owned_auction_query_timeout"
        return false, request.queryError
      end
      return false, ""
    end
    local ok, full = pcall(function()
      return requireFunction(C_AuctionHouse, "HasFullOwnedAuctionResults", "owned auction completeness query")()
    end)
    if not ok then return false, "owned_auction_completeness_unavailable" end
    if type(full) ~= "boolean" then return false, "owned_auction_completeness_malformed" end
    if not full then return false, "owned_auction_results_incomplete" end
    return true, ""
  end

  local function ownedIDSet(rows)
    local result = {}
    for _, row in ipairs(rows) do result[row.auctionId] = true end
    return result
  end

  local function findPostLocation(action)
    local items = allBagItems()
    local chosen, total, sameItem = nil, 0, false
    for _, row in ipairs(items) do
      if row.itemID == tonumber(action.itemKey:match("^[1-9][0-9]*")) then sameItem = true end
      if row.itemKey == action.itemKey then
        if not row.locked and not row.bound then
          local canSell = requireFunction(C_AuctionHouse, "IsSellItemValid", "auctionable item query")(row.location, false)
          if type(canSell) ~= "boolean" then error("auctionable item facts malformed", 0) end
          if canSell then
            if row.commodity ~= (action.kind == "post_commodity") then error("item commodity kind mismatch", 0) end
            if row.commodity then
              chosen = chosen or row
              total = total + row.count
            elseif row.count >= action.quantity and not chosen then
              chosen = row
            end
          end
        end
      end
    end
    if not chosen then
      if sameItem then error("item_variant_or_location_changed", 0) end
      error("item_stock_missing", 0)
    end
    local available = requireFunction(C_AuctionHouse, "GetAvailablePostCount", "available post count")(chosen.location)
    if not exactInteger(available, 0, MAX_SAFE) then error("available post count malformed", 0) end
    if action.kind == "post_commodity" and total < action.quantity then error("insufficient_commodity_stock", 0) end
    if action.quantity > available then error("insufficient_post_count", 0) end
    local duration = DURATIONS[action.durationHours]
    local deposit
    if action.kind == "post_commodity" then
      local itemID = tonumber(action.itemKey:match("^[1-9][0-9]*"))
      deposit = requireFunction(C_AuctionHouse, "CalculateCommodityDeposit", "commodity deposit calculation")(
        itemID, duration, action.quantity
      )
    else
      deposit = requireFunction(C_AuctionHouse, "CalculateItemDeposit", "item deposit calculation")(
        chosen.location, duration, action.quantity
      )
    end
    if not exactInteger(deposit, 0, MAX_SAFE) then error("deposit calculation unavailable", 0) end
    if deposit > action.maxDepositCopper then error("deposit_limit_exceeded", 0) end
    if money() < deposit then error("insufficient_gold_for_deposit", 0) end
    return chosen, deposit
  end

  local function cancelPrecondition(action)
    local rows = ownedAuctions()
    local found
    for _, row in ipairs(rows) do if row.auctionId == action.auctionId then found = row break end end
    if not found or found.status ~= "active" then error("owned_auction_missing", 0) end
    if not found.cancelAllowed or not found.cancelCostKnown then error("auction_cancellation_unavailable", 0) end
    if found.cancelCostCopper > action.maxCancelCostCopper then error("cancel_cost_limit_exceeded", 0) end
    if money() < found.cancelCostCopper then error("insufficient_gold_for_cancel", 0) end
    return rows, found.cancelCostCopper, found
  end

  local function mailFingerprint(row)
    return row.sender .. "\0" .. row.subject .. "\0" .. tostring(row.returned) .. "\0" .. tostring(row.isGM)
  end

  local function mailPrecondition(action, stepIndex)
    if not self.mailOpen or not isMailOpen() then error("mail_closed", 0) end
    if stepIndex >= action.messageLimit then error("mail_limit_complete", 0) end
    local rows = inboxMessages()
    local chosen
    for _, row in ipairs(rows) do
      if not row.isGM and row.codCopper == 0 and (row.moneyCopper > 0 or row.attachmentCount > 0) then
        chosen = row
        break
      end
    end
    if not chosen then error("no_eligible_non_cod_mail", 0) end
    local identity = mailFingerprint(chosen)
    local matches = 0
    for _, row in ipairs(rows) do if mailFingerprint(row) == identity then matches = matches + 1 end end
    if matches ~= 1 then error("mail_identity_ambiguous", 0) end
    if chosen.attachmentCount > 0 then
      if generalFreeBagSlots() < chosen.attachmentCount then error("insufficient_mail_bag_slots", 0) end
    end
    local bags = bagCountsByID()
    local attachmentCounts = {}
    for index, itemID in ipairs(chosen.attachmentItemIDs) do
      attachmentCounts[itemID] = (attachmentCounts[itemID] or 0) + chosen.attachmentQuantities[index]
    end
    return {
      message = chosen,
      identity = identity,
      gold = money(),
      bagCounts = bags,
      attachmentCounts = attachmentCounts,
      rows = rows,
    }
  end

  local function returnPrecondition(action)
    if not self.mailOpen or not isMailOpen() then error("mail_closed", 0) end
    local mailCommandPending = requireFunction(C_Mail, "IsCommandPending", "mail command state query")()
    if type(mailCommandPending) ~= "boolean" then error("mail command state malformed", 0) end
    if mailCommandPending then error("mail_command_pending", 0) end
    local selection = returnSelections[action.mailFingerprint]
    if not selection then error("return_selection_unknown", 0) end
    if selection.used then error("return_selection_used", 0) end
    if time() >= selection.expiresAt then error("return_selection_expired", 0) end
    if selection.itemKey ~= action.itemKey or selection.quantity ~= action.quantity then
      error("return_selection_mismatch", 0)
    end
    if not nativeCommodityItem(action.itemKey) then error("return_item_not_commodity", 0) end

    local chosen, chosenAttachment
    for _, row in ipairs(inboxMessages()) do
      if not row.isGM and row.codCopper == 0 then
        for _, attachment in ipairs(row.attachments) do
          if attachment.itemKey == selection.itemKey and attachment.quantity == selection.quantity then
            chosen, chosenAttachment = row, attachment
            break
          end
        end
      end
      if chosen then break end
    end
    if not chosen or not chosenAttachment then error("return_attachment_not_found", 0) end
    if generalFreeBagSlots() < 1 then error("insufficient_return_bag_slots", 0) end
    return {
      selection = selection,
      message = chosen,
      attachment = chosenAttachment,
      itemID = tonumber(action.itemKey),
      bagCounts = bagCountsByID(),
    }
  end

  local function operationReady(action)
    if action.kind == "post_commodity" or action.kind == "post_item" or action.kind == "cancel_auction" then
      local acquired, acquireError = acquireAuctionOwnership()
      if not acquired then return false, acquireError end
      local queried, queryError = requestOwnedResults(self.stage)
      if not queried then return false, queryError end
      local ok, reason = pcall(function()
        if action.kind == "post_commodity" or action.kind == "post_item" then
          findPostLocation(action)
        else
          cancelPrecondition(action)
        end
      end)
      return ok, ok and "" or actionError(reason)
    elseif action.kind == "collect_mail" then
      if not self.mailOpen or not isMailOpen() then return false, "mail_closed" end
      local ok, reason = pcall(mailPrecondition, action, self.stage.stepIndex)
      return ok, ok and "" or actionError(reason)
    elseif action.kind == "collect_return" then
      local ok, reason = pcall(returnPrecondition, action)
      return ok, ok and "" or actionError(reason)
    end
    return true, ""
  end

  local function capturePostBaseline(action)
    local rows = ownedAuctions()
    return { rows = rows, ids = ownedIDSet(rows), moneyBefore = money() }
  end

  local function savePending(action, stage, baseline)
    saved.pending = {
      batchId = stage.batchId,
      ordinal = stage.ordinal,
      stepIndex = stage.stepIndex,
      kind = action.kind,
    }
    self.pending = { action = copy(action), stage = copy(stage), baseline = baseline }
    if action.kind == "collect_mail" then self.pending.mailBaseline = baseline end
    self.uncertain, self.uncertainMarket = true, hasMarket(action)
    self.ahOwner = self.uncertainMarket and "automation" or "none"
  end

  local function finish(postcondition, actionComplete, detail, result)
    self.receipt.postcondition = postcondition
    self.receipt.actionComplete = actionComplete
    self.receipt.detail = actionError(detail or "")
    self.receipt.result = result or false
    if postcondition ~= "unknown" and actionComplete ~= "unknown" then
      local completedStage = self.stage
      local completedAction = self.pending and self.pending.action or completedStage and completedStage.action
      saved.pending, self.pending, self.uncertain, self.uncertainMarket = nil, nil, false, false
      self.stage = nil
      if postcondition == true and actionComplete == true and completedStage and completedAction and
          hasMarket(completedAction) then
        self.heldBatchId, self.heldExpiresAt = completedStage.batchId, completedStage.expiresAt
        self.ahOwner = "automation"
      else
        release()
      end
    else
      self.uncertain = true
      self.uncertainMarket = self.pending and hasMarket(self.pending.action) or false
      self.ahOwner = self.uncertainMarket and "automation" or "none"
    end
  end

  local function currentOwnedByID(auctionID)
    for _, row in ipairs(ownedAuctions()) do if row.auctionId == auctionID then return row end end
    return nil
  end

  local function createdListingMatches(row, action, eventAuctionID)
    local totalPrice = action.unitPriceCopper * action.quantity
    return row and row.auctionId == eventAuctionID and row.status == "active" and row.itemKey == action.itemKey and
      row.quantity == action.quantity and row.buyoutKnown and row.buyoutCopper == totalPrice
  end

  local function exactPostReceipt(pending, row)
    local baseline = pending.baseline
    local charged = baseline.moneyBefore - money()
    if charged ~= baseline.depositCopper then return nil end
    return {
      kind = pending.action.kind,
      auctionId = row.auctionId,
      itemKey = row.itemKey,
      quantity = row.quantity,
      unitPriceCopper = pending.action.unitPriceCopper,
      depositCopper = charged,
    }
  end

  local function exactCancelReceipt(pending)
    local baseline, row = pending.baseline, pending.baseline.cancelRow
    local charged = baseline.moneyBefore - money()
    if charged ~= baseline.cost then return nil end
    return {
      kind = "cancel_auction",
      auctionId = row.auctionId,
      itemKey = row.itemKey,
      remainingQuantity = row.quantity,
      cancelCostCopper = charged,
    }
  end

  local function verifyPendingMarketEffect(pending)
    if pending.action.kind == "cancel_auction" and pending.eventSeen then
      local row = currentOwnedByID(pending.action.auctionId)
      if row ~= nil then return end
      local result = exactCancelReceipt(pending)
      if result then finish(true, true, "owned_auction_removed_after_cancel_event", result) end
    elseif pending.eventAuctionID then
      local row = currentOwnedByID(pending.eventAuctionID)
      if createdListingMatches(row, pending.action, pending.eventAuctionID) then
        local result = exactPostReceipt(pending, row)
        if result then finish(true, true, "created_listing_matches_owned_auction", result) end
      end
    end
  end

  local function verifyMailChange(baseline)
    local after = inboxMessages()
    local target
    for _, row in ipairs(after) do if mailFingerprint(row) == baseline.identity then target = row break end end
    local mailChanged = target == nil or target.moneyCopper < baseline.message.moneyCopper or
      target.attachmentCount < baseline.message.attachmentCount
    if not mailChanged then return false end
    if baseline.message.moneyCopper > 0 and money() < baseline.gold + baseline.message.moneyCopper then return false end
    if baseline.message.attachmentCount > 0 then
      local bags = bagCountsByID()
      for itemID, count in pairs(baseline.attachmentCounts) do
        if (bags[itemID] or 0) < (baseline.bagCounts[itemID] or 0) + count then return false end
      end
    end
    return true
  end

  local function verifyReturnEffect(pending)
    if not pending.takeSuccess then return end
    local baseline = pending.baseline
    local bags = bagCountsByID()
    local before = baseline.bagCounts[baseline.itemID] or 0
    local after = bags[baseline.itemID] or 0
    local gained = after - before
    if gained ~= baseline.attachment.quantity then return end
    finish(true, true, "native_mail_success_and_exact_bag_increment", {
      kind = "collect_return",
      itemKey = pending.action.itemKey,
      quantity = baseline.attachment.quantity,
      mailFingerprint = pending.action.mailFingerprint,
    })
  end

  function self:SetHostBridge(value)
    if type(value) ~= "table" or type(value.suspend) ~= "function" or
        type(value.quiescent) ~= "function" or type(value.resume) ~= "function" then
      self.bridge, self.scannerState = nil, "unavailable"
      return false
    end
    self.bridge = value
    if ns.Coordinated then
      self.scannerState = "available"
      if self.uncertain and self.uncertainMarket then
        self.grantHeld, self.ahOwner = true, "automation"
        local ok, reason = value.suspend("crowley-automation")
        if ok ~= true and reason ~= "scan_active" then
          self.scannerState = "unavailable"
          return false
        end
        self.scannerState = value.quiescent("crowley-automation") == true and "quiescent" or "active"
      end
    end
    return true
  end

  function self:Snapshot(buttonRect, buttonAvailable)
    local stage = self.stage
    if not stage or not stage.action then
      return {
        batchId = "",
        ordinal = 0,
        stepIndex = 0,
        expiresAt = 0,
        action = false,
        ready = false,
        error = self.lastError,
        buttonRect = buttonRect,
        receipt = copy(self.receipt),
      }
    end
    local action = stage and stage.action or false
    local ready = stage and stage.ready == true or false
    local operationError = stage and stage.error or self.lastError
    if action and NATIVE_KINDS[action.kind] and buttonAvailable ~= true then
      ready = false
      if operationError == "" then operationError = "operation_button_unavailable" end
    end
    return {
      batchId = stage and stage.batchId or "",
      ordinal = stage and stage.ordinal or 0,
      stepIndex = stage and stage.stepIndex or 0,
      expiresAt = stage and stage.expiresAt or 0,
      action = action,
      ready = ready,
      error = operationError,
      buttonRect = buttonRect,
      receipt = copy(self.receipt),
    }
  end

  function self:Inspection()
    return self.inspection
  end

  function self:SellerMarket()
    return copy(self.sellerMarket)
  end

  function self:Ownership()
    local mode = ns.Coordinated and "coordinated" or "standalone"
    local owner = self.ahOwner
    if self.uncertain and self.uncertainMarket then owner = "automation" end
    local scanner = ns.Coordinated and self.scannerState or "not_installed"
    if ns.Coordinated and not self.bridge and not self.uncertain then scanner = "unavailable" end
    return { mode = mode, ahOwner = owner, scannerState = scanner }
  end

  function self:ActionKind()
    return self.stage and self.stage.action and self.stage.action.kind or ""
  end

  function self:Interval()
    return self:ActionKind() == "move_route" and 0.1 or 1.0
  end

  function self:Update()
    local marketRequest = self.sellerQuery
    if marketRequest and not marketRequest.issuing then
      if time() >= marketRequest.deadline then
        failedMarket(marketRequest, "commodity_search_timeout")
      elseif marketRequest.foreignQuery then
        failedMarket(marketRequest, "market_query_attribution_ambiguous")
      elseif not marketRequest.started then
        if time() >= marketRequest.startDeadline then
          failedMarket(marketRequest, "auction_house_ownership_timeout")
        else
          local acquired, acquireError = acquireAuctionOwnership()
          if not acquired and acquireError ~= "" then
            failedMarket(marketRequest, acquireError)
          elseif acquired then
            local throttleOk, ready = pcall(throttleReady)
            if not throttleOk then
              failedMarket(marketRequest, tostring(ready))
            elseif ready then
              local dispatched, reason = pcall(dispatchMarketQuery, marketRequest, "send")
              if not dispatched then failedMarket(marketRequest, tostring(reason)) end
            end
          end
        end
      else
        local held, ownershipError = verifyHeldAuctionOwnership()
        if not held then
          failedMarket(marketRequest, ownershipError)
        elseif marketRequest.messagePending and time() >= marketRequest.messageDeadline then
          failedMarket(marketRequest, "commodity_search_response_timeout")
        elseif marketRequest.queryEvent then
          marketRequest.queryEvent, marketRequest.awaitingEvent = false, false
          marketRequest.messagePending = false
          local fullOk, full = pcall(function()
            return requireFunction(C_AuctionHouse, "HasFullCommoditySearchResults", "commodity completeness query")(
              marketRequest.itemID
            )
          end)
          if not fullOk or type(full) ~= "boolean" then
            failedMarket(marketRequest, "commodity_completeness_unavailable")
          elseif full then
            marketRequest.needMore = false
            local begun, reason = pcall(beginMarketRows, marketRequest)
            if not begun then failedMarket(marketRequest, tostring(reason)) end
          elseif marketRequest.moreRequests >= MAX_MARKET_MORE_REQUESTS then
            failedMarket(marketRequest, "commodity_results_truncated")
          else
            marketRequest.needMore = true
          end
        elseif marketRequest.needMore then
          if marketRequest.moreRequests >= MAX_MARKET_MORE_REQUESTS then
            failedMarket(marketRequest, "commodity_results_truncated")
          else
            local throttleOk, ready = pcall(throttleReady)
            if not throttleOk then
              failedMarket(marketRequest, tostring(ready))
            elseif ready then
              marketRequest.needMore = false
              local dispatched, reason = pcall(dispatchMarketQuery, marketRequest, "more")
              if not dispatched then failedMarket(marketRequest, tostring(reason)) end
            end
          end
        elseif marketRequest.phase == "rows" then
          local scanned, reason = pcall(scanMarketRows, marketRequest)
          if not scanned then failedMarket(marketRequest, tostring(reason)) end
        end
      end
    end

    if self.marketSetup and time() - self.marketSetup.createdAt >= 120 then
      self.marketSetup = nil
      self.lastError = "partial_market_setup_expired"
    end

    local stage = self.stage
    if stage and not stage.action and time() - stage.createdAt >= 120 then
      clearStage("partial_stage_expired")
    elseif stage and stage.action and not self.uncertain then
      if time() >= stage.expiresAt then
        clearStage("operation_expired")
      else
        local ok, reason = operationReady(stage.action)
        if ok then stage.ready, stage.error = true, ""
        elseif reason ~= "" then stage.ready, stage.error = false, reason end
      end
    end
    if self.ownedInspection then
      local request = self.ownedInspection
      if time() >= request.expiresAt then
        self.inspection = inspectError("owned", request.page, "owned_auction_query_timeout")
        self.ownedInspection = nil
        if not self.uncertain then release() end
      else
        local acquired, acquireError = acquireAuctionOwnership()
        if not acquired and acquireError ~= "" then
          self.inspection = inspectError("owned", request.page, acquireError)
          self.ownedInspection = nil
          if not self.uncertain then release() end
        elseif acquired then
          local ready, queryError = requestOwnedResults(request)
          if ready then
            local ok, value = pcall(function()
              return inspect("owned", request.page, self.mailOpen, ownedAuctions())
            end)
            self.inspection = ok and value or inspectError("owned", request.page, value)
            self.ownedInspection = nil
            if not self.uncertain then release() end
          elseif queryError ~= "" then
            self.inspection = inspectError("owned", request.page, queryError)
            self.ownedInspection = nil
            if not self.uncertain then release() end
          end
        end
      end
    end
    if self.heldBatchId ~= "" and time() >= self.heldExpiresAt and not self.uncertain then release() end
    if self.uncertain and self.uncertainMarket and ns.Coordinated and self.bridge then
      self.grantHeld, self.ahOwner = true, "automation"
      local ok, reason = self.bridge.suspend("crowley-automation")
      if ok == true or reason == "scan_active" then
        self.scannerState = self.bridge.quiescent("crowley-automation") == true and "quiescent" or "active"
      else
        self.scannerState = "unavailable"
      end
    end
    if self.grantHeld and not self.uncertain and not self.stage and not self.sellerQuery and
        not self.ownedInspection and self.heldBatchId == "" then
      release()
    end
  end

  function self:OnEvent(event, ...)
    if event == "AUCTION_HOUSE_SHOW" then self.ahOpen = true
    elseif event == "AUCTION_HOUSE_CLOSED" then
      self.ahOpen = false
      if self.sellerQuery then failedMarket(self.sellerQuery, "auction_house_closed") end
      if self.stage and self.stage.action and hasMarket(self.stage.action) and not self.pending then
        clearStage("auction_house_closed")
      end
      if self.pending then self.receipt.detail = "auction_house_closed_while_effect_unresolved" end
    elseif event == "MAIL_SHOW" then self.mailOpen = true
    elseif event == "MAIL_CLOSED" then
      self.mailOpen = false
      if self.stage and self.stage.action and
          (self.stage.action.kind == "collect_mail" or self.stage.action.kind == "collect_return") and not self.pending then
        clearStage("mail_closed")
      end
      if self.pending and (self.pending.action.kind == "collect_mail" or self.pending.action.kind == "collect_return") then
        self.receipt.detail = "mail_closed_while_effect_unresolved"
      end
    end

    local request = self.sellerQuery
    if request and request.messagePending then
      if event == "AUCTION_HOUSE_THROTTLED_MESSAGE_SENT" then
        request.sentBarrier = true
      elseif event == "AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED" and request.sentBarrier then
        request.responseBarrier = true
      end
    end

    if event == "OWNED_AUCTIONS_UPDATED" then
      if self.stage and self.stage.action and hasMarket(self.stage.action) and self.stage.queryRequested then
        self.stage.queryEvent = true
      end
      if self.ownedInspection and self.ownedInspection.queryRequested then self.ownedInspection.queryEvent = true end
    end
    if event == "COMMODITY_SEARCH_RESULTS_UPDATED" or event == "COMMODITY_SEARCH_RESULTS_ADDED" then
      local itemID = ...
      if request and request.started and itemID == request.itemID then
        if request.phase == "rows" then
          failedMarket(request, "commodity_results_changed_during_scan")
        elseif request.dispatchReturned and request.messagePending and request.sentBarrier and
            request.responseBarrier then
          if request.foreignQuery then failedMarket(request, "market_query_attribution_ambiguous")
          else
            request.queryEvent = true
            self:Update()
          end
        end
      end
    end
    if not self.pending then return end
    local pending = self.pending
    if pending.action.kind == "cancel_auction" and event == "AUCTION_CANCELED" then
      local auctionID = ...
      if auctionID == pending.action.auctionId then
        pending.eventSeen = true
        local ok = pcall(verifyPendingMarketEffect, pending)
        if not ok then self.receipt.detail = "native_effect_verification_failed" end
      end
    elseif (pending.action.kind == "post_item" or pending.action.kind == "post_commodity") and
        event == "AUCTION_HOUSE_AUCTION_CREATED" then
      local auctionID = ...
      if exactInteger(auctionID, 1, MAX_SAFE) and not pending.baseline.ids[auctionID] then
        pending.eventAuctionID = auctionID
        local ok = pcall(verifyPendingMarketEffect, pending)
        if not ok then self.receipt.detail = "native_effect_verification_failed" end
      end
    elseif event == "MAIL_INBOX_UPDATE" and pending.action.kind == "collect_mail" and pending.mailBaseline then
      local ok, changed = pcall(verifyMailChange, pending.mailBaseline)
      if ok and changed then finish(true, false, "mail_state_and_settlement_changed") end
    elseif event == "MAIL_SUCCESS" and pending.action.kind == "collect_return" then
      local itemID = ...
      if itemID == pending.baseline.itemID then
        pending.takeSuccess = true
        local ok = pcall(verifyReturnEffect, pending)
        if not ok then self.receipt.detail = "native_effect_verification_failed" end
      end
    elseif event == "MAIL_FAILED" and pending.action.kind == "collect_return" then
      local itemID = ...
      if itemID == pending.baseline.itemID then finish(false, false, "native_mail_take_failed") end
    elseif (event == "BAG_UPDATE_DELAYED" or event == "MAIL_INBOX_UPDATE") and
        pending.action.kind == "collect_return" and pending.takeSuccess then
      local ok = pcall(verifyReturnEffect, pending)
      if not ok then self.receipt.detail = "native_effect_verification_failed" end
    elseif event == "OWNED_AUCTIONS_UPDATED" then
      local ok = pcall(verifyPendingMarketEffect, pending)
      if not ok then self.receipt.detail = "native_effect_verification_failed" end
    elseif event == "PLAYER_MONEY" and
        (pending.action.kind == "post_item" or pending.action.kind == "post_commodity" or
          pending.action.kind == "cancel_auction") then
      local ok = pcall(verifyPendingMarketEffect, pending)
      if not ok then self.receipt.detail = "native_effect_verification_failed" end
    end
  end

  function self:OnClick(button, mouseButton, down)
    if button == nil or mouseButton ~= "LeftButton" or down == true then return false end
    local stage = self.stage
    if not stage or not stage.ready or not stage.action or not NATIVE_KINDS[stage.action.kind] or self.uncertain then
      return false
    end
    if type(button.IsShown) ~= "function" or type(button.IsEnabled) ~= "function" or
        button:IsShown() ~= true or button:IsEnabled() ~= true then
      return false
    end
    stage.ready = false
    self.receipt.sequence = self.receipt.sequence + 1
    self.receipt.batchId, self.receipt.ordinal, self.receipt.stepIndex = stage.batchId, stage.ordinal, stage.stepIndex
    self.receipt.callbackDelivered = true
    self.receipt.postcondition, self.receipt.actionComplete = "unknown", "unknown"
    self.receipt.detail = "dispatching"
    self.receipt.result = false

    local action = stage.action
    local baseline
    local ok, reason = pcall(function()
      if time() >= stage.expiresAt then error("operation_expired", 0) end
      if not identitiesEqual(self.identity, playerIdentity()) then error("client_identity_changed", 0) end
      if hasMarket(action) then
        if not self.ahOpen or not isAuctionHouseOpen() or self.ahOwner ~= "automation" then
          error("auction_house_ownership_not_held", 0)
        end
        if ns.Coordinated and (not self.grantHeld or not self.bridge or self.bridge.quiescent("crowley-automation") ~= true) then
          error("scanner_not_quiescent", 0)
        end
        baseline = capturePostBaseline(action)
      elseif action.kind == "collect_mail" then
        baseline = mailPrecondition(action, stage.stepIndex)
      elseif action.kind == "collect_return" then
        baseline = returnPrecondition(action)
      end
      if action.kind == "post_commodity" or action.kind == "post_item" then
        local location, deposit = findPostLocation(action)
        baseline.itemKey = action.itemKey
        baseline.quantity = action.quantity
        baseline.unitPriceCopper = action.unitPriceCopper
        baseline.durationIndex = DURATIONS[action.durationHours]
        baseline.location = location.location
        baseline.depositCopper = deposit
      elseif action.kind == "cancel_auction" then
        local rows, cost, row = cancelPrecondition(action)
        baseline.rows = rows
        baseline.cost = cost
        baseline.cancelRow = row
      end
    end)
    if not ok then
      if action.kind == "collect_mail" and
          (reason == "no_eligible_non_cod_mail" or reason == "mail_limit_complete") then
        finish(true, true, "mail_collection_complete_without_effect")
        return true
      end
      finish(false, false, tostring(reason))
      return false
    end

    savePending(action, stage, baseline)
    local called, nativeError = pcall(function()
      if action.kind == "post_commodity" then
        requireFunction(C_AuctionHouse, "PostCommodity", "commodity post API")(
          baseline.location, baseline.durationIndex, action.quantity, action.unitPriceCopper
        )
      elseif action.kind == "post_item" then
        local total = action.unitPriceCopper * action.quantity
        requireFunction(C_AuctionHouse, "PostItem", "item post API")(
          baseline.location, baseline.durationIndex, action.quantity, total, total
        )
      elseif action.kind == "cancel_auction" then
        requireFunction(C_AuctionHouse, "CancelAuction", "auction cancel API")(action.auctionId)
      elseif action.kind == "collect_mail" then
        local message = baseline.message
        if message.codCopper ~= 0 or message.isGM then error("mail_payment_or_gm_mail_forbidden", 0) end
        requireFunction(_G, "AutoLootMailItem", "mail collection API")(message.index)
        self.pending.mailBaseline = baseline
      elseif action.kind == "collect_return" then
        baseline.selection.used = true
        requireFunction(_G, "TakeInboxItem", "selective mail attachment API")(
          baseline.message.index, baseline.attachment.attachment
        )
      end
    end)
    if not called then
      self.receipt.detail = actionError("native_call_raised:" .. tostring(nativeError))
      if self.receipt.postcondition == "unknown" then
        self.uncertain, self.uncertainMarket = true, hasMarket(action)
        self.ahOwner = self.uncertainMarket and "automation" or "none"
      end
      return false
    end
    self.receipt.detail = "awaiting_causal_native_event"
    return true
  end

  function self:Command(message)
    local args, parseError = tokens(message)
    if not args then failInput(parseError) return false end
    local command = table.remove(args, 1)
    if command == "clear" and #args == 0 then
      if self.sellerQuery then
        self.lastError = "market_query_active"
        return false
      end
      self.stage = nil
      self.marketSetup = nil
      self.ownedInspection = nil
      if self.uncertain then
        self.lastError = "uncertain_effect_requires_safe_reconciliation"
        self.ahOwner = self.uncertainMarket and "automation" or "none"
        return false
      end
      self.lastError = ""
      release()
      self.inspection = {
        kind = "none", page = 0, rows = {}, hasMore = false, error = "",
        requestId = "", capturedAt = 0, sourceCount = 0,
      }
      return true
    elseif command == "begin" and #args == 3 then
      if self.uncertain or self.pending then failInput("uncertain_effect_blocks_new_work") return false end
      if self.sellerQuery or self.marketSetup then failInput("market_query_active") return false end
      local batchID = args[1]
      local ordinal = integerText(args[2], 0, MAX_SAFE)
      local step = integerText(args[3], 0, MAX_SAFE)
      if not batchID:match("^[0-9a-f]+$") or #batchID ~= 64 or not ordinal or not step then
        failInput("begin_identity_invalid") return false
      end
      if self.stage then
        local previous = self.stage.action
        local replaceable = previous and (previous.kind == "interact" or previous.kind == "move_route")
        if not replaceable then failInput("clear_existing_stage_first") return false end
        if self.stage.batchId ~= batchID then failInput("held_batch_requires_clear") return false end
      end
      if self.heldBatchId ~= "" and time() >= self.heldExpiresAt then release() end
      if self.heldBatchId ~= "" and self.heldBatchId ~= batchID then
        self.lastError = "held_batch_requires_clear"
        return false
      end
      self.stage = { batchId = batchID, ordinal = ordinal, stepIndex = step, expiresAt = 0,
        createdAt = time(), action = nil, ready = false, error = "", fields = {}, keyChunks = {} }
      self.lastError = ""
      return true
    elseif command == "kind" and #args == 1 then
      local stage = self.stage
      if not stage or stage.actionKind or not FIELD_SETS[args[1]] then failInput("kind_invalid") return false end
      stage.actionKind = args[1]
      return true
    elseif command == "arg" and #args == 2 then
      local stage = self.stage
      if not stage or not stage.actionKind then failInput("argument_without_kind") return false end
      local field, value = args[1], args[2]
      local expected = FIELD_SETS[stage.actionKind]
      if not expected[field] or field == "itemKey" or stage.fields[field] ~= nil then failInput("argument_field_invalid") return false end
      local parsed = value
      if field == "quantity" then
        parsed = integerText(value, 1, stage.actionKind == "collect_return" and MAX_SAFE or 200)
      elseif field == "unitPriceCopper" then parsed = integerText(value, 1, MAX_SAFE)
      elseif field == "durationHours" then parsed = integerText(value, 12, 48)
      elseif field == "maxDepositCopper" or field == "maxCancelCostCopper" then parsed = integerText(value, 0, MAX_SAFE)
      elseif field == "auctionId" then parsed = integerText(value, 1, MAX_SAFE)
      elseif field == "messageLimit" then parsed = integerText(value, 1, 50)
      elseif field == "mailFingerprint" then
        if #value ~= 64 or not value:match("^[0-9a-f]+$") then parsed = nil end
      elseif field == "targetId" or field == "routeId" then
        if #value > 64 or not value:match("^[a-z][a-z0-9_-]*$") then parsed = nil end
      end
      if parsed == nil then failInput("argument_value_invalid") return false end
      stage.fields[field] = parsed
      return true
    elseif command == "key" and #args == 2 then
      local stage = self.stage
      local index = integerText(args[1], 1, 3)
      local chunk = args[2]
      if not stage or stage.actionKind ~= "post_commodity" and stage.actionKind ~= "post_item" and
          stage.actionKind ~= "collect_return" or
          not index or #chunk < 1 or #chunk > 60 or stage.keyChunks[index] ~= nil then
        failInput("item_key_chunk_invalid") return false
      end
      for i = 1, index - 1 do if stage.keyChunks[i] == nil then failInput("item_key_chunks_not_consecutive") return false end end
      stage.keyChunks[index] = chunk
      local total = 0
      for _, value in pairs(stage.keyChunks) do total = total + #value end
      if total > MAX_ITEM_KEY_BYTES then failInput("item_key_too_long") return false end
      return true
    elseif command == "expires" and #args == 1 then
      local stage = self.stage
      local expires = integerText(args[1], 1, MAX_SAFE)
      if not stage or not expires or expires <= time() or expires > time() + 120 then
        failInput("expiry_invalid") return false
      end
      stage.expiresAt = expires
      return true
    elseif command == "seal" and #args == 0 then
      local stage = self.stage
      if not stage or not stage.actionKind or stage.expiresAt == 0 or stage.expiresAt <= time() or
          stage.expiresAt > time() + 120 then
        failInput("seal_without_valid_stage") return false
      end
      local action, reason = actionFields(stage.actionKind, stage.fields, stage.keyChunks)
      if not action then failInput(reason) return false end
      local ok, identity = pcall(playerIdentity)
      if not ok then failInput("native_context_unavailable") return false end
      stage.action, self.identity = action, identity
      if NATIVE_KINDS[action.kind] then
        local ready, readinessError = operationReady(action)
        stage.ready, stage.error = ready, readinessError
        if readinessError ~= "" then self.lastError = readinessError end
      else
        stage.ready, stage.error = true, ""
      end
      self.lastError = stage.error
      return true
    elseif command == "market" and #args == 2 then
      local requestID, itemIDText = args[1], args[2]
      local itemID = integerText(itemIDText, 1, MAX_SAFE)
      if not requestID:match("^[0-9a-f]+$") or #requestID ~= 32 or not itemID or
          tostring(itemID) ~= itemIDText or self.marketSetup or self.sellerQuery then
        failInput("market_request_invalid") return false
      end
      if self.stage or self.uncertain or self.ownedInspection or self.heldBatchId ~= "" or self.grantHeld then
        failInput("market_ownership_busy") return false
      end
      self.marketSetup = {
        requestId = requestID, itemKey = itemIDText, itemID = itemID,
        managedIDs = {}, nextChunk = 1, managedCount = 0, lastManagedID = 0, createdAt = time(),
      }
      return true
    elseif command == "market_ids" and #args == 3 then
      local setup = self.marketSetup
      local requestID, chunkIndex, encodedIDs = args[1], integerText(args[2], 1, MAX_MANAGED_IDS), args[3]
      if not setup or requestID ~= setup.requestId or not chunkIndex or chunkIndex ~= setup.nextChunk or
          #encodedIDs > 72 then
        failInput("market_managed_ids_invalid") return false
      end
      local parsed, previous = {}, 0
      for encoded in encodedIDs:gmatch("[^,]+") do
        local auctionID = integerText(encoded, 1, MAX_SAFE)
        if not auctionID or tostring(auctionID) ~= encoded or auctionID <= previous or
            auctionID <= setup.lastManagedID then
          failInput("market_managed_ids_invalid") return false
        end
        parsed[#parsed + 1], previous = auctionID, auctionID
      end
      if #parsed == 0 or table.concat(parsed, ",") ~= encodedIDs or
          setup.managedCount + #parsed > MAX_MANAGED_IDS then
        failInput("market_managed_ids_invalid") return false
      end
      for _, auctionID in ipairs(parsed) do
        if setup.managedIDs[auctionID] then failInput("market_managed_ids_duplicate") return false end
        setup.managedIDs[auctionID] = true
      end
      setup.managedCount = setup.managedCount + #parsed
      setup.lastManagedID = previous
      setup.nextChunk = setup.nextChunk + 1
      return true
    elseif command == "market_start" and #args == 2 then
      local setup = self.marketSetup
      local expectedCount = integerText(args[2], 0, MAX_MANAGED_IDS)
      if not setup or args[1] ~= setup.requestId or not expectedCount or expectedCount ~= setup.managedCount then
        failInput("market_start_invalid") return false
      end
      if self.stage or self.uncertain or self.ownedInspection or self.heldBatchId ~= "" or self.grantHeld or self.sellerQuery then
        failInput("market_ownership_busy") return false
      end
      if self.marketHookError then
        self.sellerQuery = {
          requestId = setup.requestId, itemKey = setup.itemKey, itemID = setup.itemID,
        }
        self.marketSetup = nil
        failedMarket(self.sellerQuery, self.marketHookError)
        return true
      end
      if not self.ahOpen or not isAuctionHouseOpen() then
        self.sellerQuery = {
          requestId = setup.requestId, itemKey = setup.itemKey, itemID = setup.itemID,
          managedIDs = setup.managedIDs, started = false, startDeadline = time() + MARKET_QUERY_TIMEOUT_SECONDS,
        }
        self.marketSetup = nil
        failedMarket(self.sellerQuery, "auction_house_closed")
        return true
      end
      self.sellerQuery = {
        requestId = setup.requestId, itemKey = setup.itemKey, itemID = setup.itemID,
        managedIDs = setup.managedIDs, started = false, startDeadline = time() + MARKET_QUERY_TIMEOUT_SECONDS,
        moreRequests = 0, foreignQuery = false, managedSeen = {}, managedCount = setup.managedCount,
        deadline = time() + MARKET_QUERY_TIMEOUT_SECONDS * (MAX_MARKET_MORE_REQUESTS + 1),
      }
      self.marketSetup = nil
      self.sellerMarket = {
        requestId = setup.requestId, itemKey = setup.itemKey, complete = false, coverage = "none",
        resultCount = 0, bestCompetitorUnitPriceCopper = false, bestCompetitorQuantity = 0,
        bestCompetitorResultCount = 0, ambiguousOwnerUnitPriceCopper = false,
        managedUnitPriceCopper = false, managedAuctionCount = 0, managedQuantity = 0,
        error = "", capturedAt = 0,
      }
      self:Update()
      return true
    elseif command == "inspect" and #args == 2 then
      local kind, page = args[1], integerText(args[2], 0, 4095)
      if not page or (kind ~= "inventory" and kind ~= "owned" and kind ~= "mail") then
        failInput("inspection_request_invalid") return false
      end
      if kind == "owned" and page > 0 and inspectionCache.owned then
        local ok, value = pcall(inspect, kind, page, self.mailOpen)
        self.inspection = ok and value or inspectError(kind, page, value)
        return ok
      elseif kind == "owned" then
        if self.stage or self.uncertain or self.ownedInspection or self.heldBatchId ~= "" or
            self.sellerQuery or self.marketSetup or self.grantHeld then
          self.inspection = inspectError(kind, page, "owned_inspection_busy")
          return false
        end
        if page == 0 then inspectionCache.owned = nil end
        self.ownedInspection = { page = page, expiresAt = time() + 15, queryRequested = false, queryEvent = false }
        self.inspection = inspectError(kind, page, "owned_query_pending")
        self:Update()
        return true
      end
      local ok, value = pcall(inspect, kind, page, self.mailOpen)
      self.inspection = ok and value or inspectError(kind, page, value)
      return ok
    end
    failInput("command_grammar_invalid")
    return false
  end

  function self:Receipt() return copy(self.receipt) end

  function self:ClickReady()
    return self.stage ~= nil and self.stage.ready == true and self.stage.action ~= nil and
      NATIVE_KINDS[self.stage.action.kind] == true and not self.uncertain
  end

  function self:ButtonLabel()
    if self.uncertain then return "Crowley: unresolved effect" end
    if not self.stage or not self.stage.action then return "Crowley: no operation" end
    return self.stage.ready and "Crowley: confirm one action" or "Crowley: waiting"
  end

  return self
end
