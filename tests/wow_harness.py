"""Runs the real addon Lua under Lua 5.1 (WoW's version) against a fake WoW client API."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from crowley_common.game_target import GameTarget, load_game_target, target_scan_plan
from crowley_common.watchlist import Catalog
from lupa import lua51

from tools.generate_launch_target import render_generated_lua

ROOT = Path(__file__).resolve().parents[1]
SCANNER_ADDONS = ROOT / "packages" / "scanner" / "src" / "crowley_scanner" / "addon"
AUTOMATION_ADDONS = ROOT / "packages" / "automation" / "src" / "crowley_automation" / "addon"
FIXTURES = Path(__file__).parent / "fixtures"
TARGET = load_game_target(FIXTURES / "targets" / "verified-beta.json")
DEFAULT_CATALOG = Catalog.model_validate_json(
    (FIXTURES / "catalogs" / "verified-beta.json").read_text(encoding="utf-8")
)
_NEXT_AUCTION_ID = 1
_ADDON_TREES: dict[tuple[str, str, str, str], Path] = {}
_ADDON_TEMPORARIES: dict[tuple[str, str, str, str], tempfile.TemporaryDirectory[str]] = {}


def _temporary_addon_tree(target: GameTarget, catalog: Catalog) -> Path:
    plan = target_scan_plan(catalog, target)
    plan_hash = str(plan["planHash"])
    key = (target.target_id, target.full_build, target.house, plan_hash)
    existing = _ADDON_TREES.get(key)
    if existing is not None:
        return existing

    temporary = tempfile.TemporaryDirectory(prefix="crowley-addon-tests-")
    root = Path(temporary.name) / "addon"
    shutil.copytree(SCANNER_ADDONS, root)
    for addon_dir in AUTOMATION_ADDONS.iterdir():
        if addon_dir.is_dir():
            shutil.copytree(addon_dir, root / addon_dir.name)
    generated = render_generated_lua(target, plan)
    (root / "Crowley" / "GeneratedTarget.lua").write_text(generated, encoding="utf-8")
    _ADDON_TEMPORARIES[key] = temporary
    _ADDON_TREES[key] = root
    return root


STUBS = r"""
local harness = ...
_events = {}
_timers = {}
_chat = {}
_now = 1760000000
_profile_ms = 0

function time() return _now end
function GetTime() return _now - 1760000000 end
function GetBuildInfo() return harness.clientVersion, harness.clientBuild, "Oct 1 2026", harness.interface end
function UnitExists(unit)
  if unit == "mouseover" then return harness.hoverUnit ~= "" end
  if unit == "target" then return harness.targetPresent == true end
  return unit == "player"
end
function UnitName(unit)
  if unit == "mouseover" then return harness.hoverUnit end
  if unit == "target" then
    if harness.targetPresent == true then return harness.targetName end
    return nil
  end
  return harness.playerName or "Tester"
end
function UnitGUID(unit)
  if unit == "target" then return harness.targetGUID end
  if unit == "mouseover" then return harness.hoverGUID end
end
function UnitPosition(unit)
  if unit == "player" then return harness.playerX, harness.playerY, 0, harness.playerInstance end
end
function GetPlayerFacing() return harness.playerFacing end
function GetUnitSpeed() return harness.playerSpeed end
function UnitIsDeadOrGhost() return harness.playerDead end
function InCombatLockdown() return harness.inCombat end
function IsMounted() return harness.mounted end
function CheckInteractDistance(unit, index)
  assert(unit == "target", "documented trade-range predicate unit")
  harness.interactDistanceCalls[#harness.interactDistanceCalls + 1] = index
  if harness.rangeAvailable == false then return nil end
  -- Keep other interaction-distance indexes observably distinct so a melee
  -- query cannot accidentally satisfy the trade-range fixture.
  if index == 2 then return harness.withinTradeRange == true end
  return false
end
function GetBindingAction(key)
  if key == "CTRL-F12" then
    if harness.bindingAvailable == false then return nil end
    return harness.ctrlF12Action or ""
  end
  local movement = harness.movementBindings
  return movement and movement[key] or ""
end
function GetPhysicalScreenSize() return harness.presentationWidth or 1920, harness.presentationHeight or 1080 end
function GetCursorPosition() return harness.cursorX or 960, harness.cursorY or 540 end
function GetMoney() return harness.money or 0 end
function GetCVar(name)
  assert(name == "SoftTargetInteract", "documented keyboard interaction-mode lookup")
  return harness.softTargetInteract
end
function StaticPopup_IsAnyDialogShown()
  if harness.staticPopupAvailable == false then return nil end
  return harness.staticPopupShown == true
end
GameMenuFrame = {
  IsShown = function()
    if harness.gameMenuAvailable == false then return nil end
    return harness.gameMenuShown == true
  end,
}
function UnitFactionGroup() return harness.faction end
function GetRealmName() return harness.realm end
function GetZoneText() return harness.zone end
function GetSubZoneText() return harness.subzone end
function GetCurrentRegionName() return harness.regionName end
function GetCurrentRegion() return harness.region end
function GetLocale() return "enUS" end
if harness.realmInfo then
  C_RealmInfo = { GetRealmInfo = function() end }
end
if harness.gameRules then
  C_GameRules = {
    GetCurrentGameModeRecordID = function() end,
    GetActiveGameMode = function() end,
    IsHardcoreActive = function() return false end,
    IsStandard = function() return true end,
  }
end

Enum = {
  CompressionMethod = { Deflate = 0 },
  AuctionHouseSortOrder = { Price = 1, Buyout = 2 },
  AuctionStatus = { Active = 0, Sold = 1 },
  ItemClass = { Container = 1, Weapon = 2, Armor = 4, Recipe = 9, Profession = 19, Tradegoods = 7 },
}

DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) table.insert(_chat, msg) end }
SlashCmdList = {}
UIParent = {
  width = harness.rootWidth or 1365.333374,
  height = harness.rootHeight or 768,
  effectiveScale = harness.rootEffectiveScale or 1,
  name = "UIParent",
}
function UIParent:GetWidth() return self.width end
function UIParent:GetHeight() return self.height end
function UIParent.GetEffectiveScale() return harness.rootEffectiveScale or 1 end

local chatEdit = harness.chatEdit
function chatEdit:IsShown() return self.shown end
function chatEdit:HasFocus() return self.focused end
function chatEdit:GetText() return self.text end
ChatFrameUtil = { GetActiveWindow = function() return chatEdit.shown and chatEdit or nil end }
AuctionHouseFrame = {
  IsShown = function() return harness.auctionHouseOpen == true end,
}
MailFrame = { IsShown = function() return harness.mailOpen == true end }
GameTooltip = {
  IsShown = function() return harness.tooltipShown == true end,
  GetOwner = function() return harness.tooltipOwner end,
}
GameTooltipTextLeft1 = { GetText = function() return harness.tooltipText end }

function CreateFrame(_, name, parent, template)
  local frame = { events = {}, name = name, parent = parent or UIParent, template = template }
  if name then _G[name] = frame end
  function frame:RegisterEvent(e) self.events[e] = true end
  function frame:UnregisterEvent(e) self.events[e] = nil end
  function frame:SetScript(kind, fn)
    if kind == "OnEvent" then self.onEvent = fn
    elseif kind == "OnUpdate" then self.onUpdate = fn
    elseif kind == "OnClick" then self.onClick = fn
    else self[kind] = fn end
  end
  function frame:SetIgnoreParentScale(value) self.ignoreParentScale = value end
  function frame:GetEffectiveScale()
    if self.ignoreParentScale then return self.scale or 1 end
    local parentScale = self.parent and self.parent:GetEffectiveScale() or 1
    return parentScale * (self.scale or 1)
  end
  function frame:SetScale(value) self.scale = value end
  function frame:GetScale() return self.scale or 1 end
  function frame:SetAlpha(value) self.alpha = value end
  function frame:SetFrameStrata(value) self.strata = value end
  function frame:SetFrameLevel(value) self.frameLevel = value end
  function frame:EnableMouse(value) self.mouse = value end
  function frame:SetSize(width, height) self.width, self.height = width, height end
  function frame:SetPoint(point, relative, relativePoint, x, y)
    self.point, self.relative, self.relativePoint, self.offsetX, self.offsetY = point, relative, relativePoint, x or 0, y or 0
    if self.name == "CrowleyAutomationOperationButton" then
      self.left = ((harness.rootWidth or 1920) - (self.width or 0)) / 2
      self.bottom = y or 0
    end
  end
  function frame:GetLeft() return self.left or 0 end
  function frame:GetBottom() return self.bottom or 0 end
  function frame:GetWidth() return self.width or 0 end
  function frame:GetHeight() return self.height or 0 end
  function frame:Show() self.shown = true end
  function frame:Hide() self.shown = false end
  function frame:IsShown() return self.shown == true end
  function frame:SetText(value) self.text = value end
  function frame:GetText() return self.text end
  function frame:SetEnabled(value) self.enabled = value == true end
  function frame:IsEnabled() return self.enabled ~= false end
  function frame:SetMovable(value) self.movable = value == true end
  function frame:RegisterForDrag(button) self.dragButton = button end
  function frame:RegisterForClicks(...) self.clicks = {...} end
  function frame:StartMoving() self.moving = true end
  function frame:StopMovingOrSizing() self.moving = false end
  function frame:Click()
    if self.enabled ~= false and self.shown == true and self.onClick then self.onClick(self) end
  end
  function frame:CreateTexture()
    harness.textureCreateCount = harness.textureCreateCount + 1
    if harness.textureFailureAt > 0 and harness.textureCreateCount == harness.textureFailureAt then
      error("native texture construction failure")
    end
    local texture = { shown = false, parent = self, color = {0, 0, 0, 1} }
    function texture:SetSnapToPixelGrid(value) self.snap = value end
    function texture:SetTexelSnappingBias(value) self.bias = value end
    function texture:SetSize(width, height) self.width, self.height = width, height end
    function texture:SetPoint(...) self.point = {...} end
    function texture:SetColorTexture(r, g, b, a) self.color = {r, g, b, a} end
    function texture:SetTexture(path, wrapHorizontal, wrapVertical, filterMode)
      self.texture = path
      self.wrapHorizontal = wrapHorizontal
      self.wrapVertical = wrapVertical
      self.filterMode = filterMode
      -- Keep every argument, including omitted optional arguments, visible to
      -- the Python raster model.  The native client defaults are not inferred
      -- here: the recorded call is the contract under test.
      self.textureArgs = {
        textureAsset = path,
        wrapHorizontal = wrapHorizontal,
        wrapVertical = wrapVertical,
        filterMode = filterMode,
      }
    end
    function texture:SetTexCoord(left, right, top, bottom)
      self.texCoord = {left, right, top, bottom}
    end
    function texture:Show() self.shown = true end
    function texture:Hide() self.shown = false end
    table.insert(harness.textures, texture)
    return texture
  end
  table.insert(_events, frame)
  return frame
end

function _fire(event, ...)
  if event == "AUCTION_HOUSE_SHOW" then harness.auctionHouseOpen = true end
  if event == "AUCTION_HOUSE_CLOSED" then harness.auctionHouseOpen = false end
  for _, frame in ipairs(_events) do
    if frame.events[event] and frame.onEvent then frame.onEvent(frame, event, ...) end
  end
end

function hooksecurefunc(target, name, callback)
  local original = target[name]
  target[name] = function(...)
    local results = { original(...) }
    callback(...)
    return unpack(results)
  end
end

C_Timer = {
  After = function(delay, fn)
    local timer = { at = _now + delay, fn = fn }
    table.insert(_timers, timer)
    if delay == 30 and harness.suppressOwnedTimeout == true then
      harness.ownedTimeoutCandidate = timer
    end
  end,
}

function _nextTimerAt()
  local earliest
  for _, timer in ipairs(_timers) do
    if not earliest or timer.at < earliest then earliest = timer.at end
  end
  return earliest
end

function _runTimers()
  local guard = 0
  while #_timers > 0 do
    local readyIndex, ready
    for i, timer in ipairs(_timers) do
      if timer.at <= _now and (not ready or timer.at < ready.at) then
        readyIndex, ready = i, timer
      end
    end
    if not ready then break end
    table.remove(_timers, readyIndex)
    ready.fn()
    guard = guard + 1
    if guard > 100000 then error("timer loop") end
  end
end

local function lookupAuction(itemID, commodity)
  for i = 1, #harness.auctions do
    local auction = harness.auctions[i]
    if auction.itemID == itemID and (commodity == nil or auction.isCommodity == commodity) then return auction end
  end
end

local function nativeClassID(itemID)
  if harness.missingClass and harness.missingClass[itemID] then return nil end
  local auction = lookupAuction(itemID)
  if auction then return auction.classID end
  return Enum.ItemClass.Tradegoods
end

local function nativeLevel(itemID)
  if not harness.keyWarm or not harness.keyWarm[itemID] then return 0 end
  local auction = lookupAuction(itemID)
  return auction and auction.itemLevel or 0
end

local function equipmentAuction(auction)
  return auction and not auction.isCommodity and
    (auction.classID == Enum.ItemClass.Weapon or auction.classID == Enum.ItemClass.Armor or auction.classID == Enum.ItemClass.Profession)
end

C_Item = {
  DoesItemExistByID = function(id) return id ~= 999999 end,
  GetItemInfoInstant = function(item)
    local itemID = type(item) == "number" and item or (type(item) == "table" and item.itemID)
    if type(itemID) ~= "number" then return nil end
    return itemID, "Item", "", "", 1, nativeClassID(itemID)
  end,
  GetItemInfo = function(itemID)
    if harness.itemInfoPending and harness.itemInfoPending[itemID] then return nil end
    local override = harness.itemInfo and harness.itemInfo[itemID]
    local auction = lookupAuction(itemID)
    if override then
      return override.name, override.link, override.quality, override.itemLevel, override.minLevel,
        override.itemType, override.itemSubType, override.stackCount, override.equipLoc, override.texture,
        override.sellPrice, override.classID, override.subclassID, override.bindType, override.expansionID,
        override.setID, override.isCraftingReagent
    end
    local classID = auction and auction.classID
    if not auction then classID = Enum.ItemClass.Tradegoods end
    return auction and auction.name or ("Item " .. itemID), auction and auction.link or "",
      auction and auction.quality or 1, auction and auction.itemLevel or 0, auction and auction.minLevel or 0,
      "Trade Goods", "", auction and (auction.stackCount or auction.count) or 1, "", 1,
      auction and auction.sellPrice or 0, classID,
      auction and auction.subclassID or 0, auction and auction.bindType or 0,
      auction and auction.expansionID or 1, auction and auction.setID or 0,
      auction and auction.isCraftingReagent == true or false
  end,
  RequestLoadItemDataByID = function(id)
    harness.requested[id] = true
    harness.itemInfoPending = harness.itemInfoPending or {}
    harness.itemInfoPending[id] = true
    if harness.itemInfoNever and harness.itemInfoNever[id] then return end
    C_Timer.After(harness.itemInfoDelay or harness.metadataDelay or 0, function()
      if harness.metadataPending then harness.metadataPending[id] = nil end
      if harness.itemInfoPending then harness.itemInfoPending[id] = nil end
      harness.keyWarm = harness.keyWarm or {}
      harness.keyWarm[id] = true
      _fire("ITEM_KEY_ITEM_INFO_RECEIVED", id)
      if harness.itemInfoFailures and harness.itemInfoFailures[id] then
        _fire("ITEM_DATA_LOAD_RESULT", id, false)
      else
        _fire("ITEM_DATA_LOAD_RESULT", id, true)
      end
    end)
  end,
}

local function keyValues(key)
  if type(key) ~= "table" then return nil end
  return key.itemID, key.itemLevel or 0, key.itemSuffix or 0, key.battlePetSpeciesID or 0
end

local function makeKey(itemID, itemLevel, itemSuffix, pet)
  return { itemID = itemID, itemLevel = itemLevel, itemSuffix = itemSuffix, battlePetSpeciesID = pet }
end

local function findAuction(itemID, commodity)
  return lookupAuction(itemID, commodity)
end

local function queryRows(itemID, commodity)
  local rows = {}
  for i = 1, #harness.auctions do
    local a = harness.auctions[i]
    if a.itemID == itemID and a.isCommodity == commodity then rows[#rows + 1] = a end
  end
  table.sort(rows, function(x, y)
    local xp = x.buyout
    local yp = y.buyout
    return xp < yp
  end)
  return rows
end

local function ownerList(a)
  local owners = a.owners or { a.owner or "Seller" }
  local result = {}
  for i, owner in ipairs(owners) do result[i] = owner end
  return result
end

local function appendSend(kind, itemID)
  harness.sendLog = harness.sendLog or {}
  table.insert(harness.sendLog, { at = _now, kind = kind, itemID = itemID })
end

local function recordQueryMethod(method, itemID, key)
  harness.queryMethods = harness.queryMethods or {}
  table.insert(harness.queryMethods, { method = method, itemID = itemID,
    itemLevel = key.itemLevel, itemSuffix = key.itemSuffix, battlePetSpeciesID = key.battlePetSpeciesID })
end

local function keysEqual(left, right)
  local li, ll, ls, lp = keyValues(left)
  local ri, rl, rs, rp = keyValues(right)
  return li and ri and li == ri and ll == rl and ls == rs and lp == rp
end

local function fireResults(kind, key, itemID)
  local event = harness.eventKind or "UPDATED"
  if harness.emitStale then
    _fire(kind == "commodity" and "COMMODITY_SEARCH_RESULTS_UPDATED" or "ITEM_SEARCH_RESULTS_UPDATED", kind == "commodity" and (itemID + 999999) or makeKey(itemID + 999999, 0, 0, 0))
  end
  if event ~= "NONE" then
    local name = (kind == "commodity" and "COMMODITY_SEARCH_RESULTS_" or "ITEM_SEARCH_RESULTS_") .. event
    if kind == "commodity" then _fire(name, itemID) else _fire(name, makeKey(key.itemID, key.itemLevel, key.itemSuffix, key.battlePetSpeciesID)) end
  end
end

local function emitResults(kind, key, itemID)
  local delay = harness.eventDelay or 0
  C_Timer.After(delay, function() fireResults(kind, key, itemID) end)
end

local function runSearchQuery(key, method)
  local itemID = keyValues(key)
  local commodity = findAuction(itemID, true) ~= nil
  local auction = findAuction(itemID, false)
  local valid = commodity and method == "SendSearchQuery" or
    not commodity and (not auction or not equipmentAuction(auction) and method == "SendSearchQuery" or
      equipmentAuction(auction) and method == "SendSellSearchQuery" and key.itemLevel == 0 and key.itemSuffix == 0 and key.battlePetSpeciesID == 0)
  harness.queryCalls = harness.queryCalls + 1
  harness.lastQuery = { key = key, itemID = itemID, commodity = commodity, valid = valid, loaded = 0 }
  local rows = queryRows(itemID, commodity)
  harness.lastQuery.loaded = valid and math.min(harness.pageSize or 50, #rows) or 0
  harness.lastQuery.full = not harness.forceIncomplete and harness.lastQuery.loaded >= #rows
  recordQueryMethod(method, itemID, key)
  appendSend("search", itemID)
  if valid and harness.eventBeforeBarriers then fireResults(commodity and "commodity" or "item", key, itemID) end
  _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT")
  _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED")
  if valid and not harness.eventBeforeBarriers then emitResults(commodity and "commodity" or "item", key, itemID) end
end

C_AuctionHouse = {
  IsThrottledMessageSystemReady = function()
    if harness.forceThrottle then return false end
    local count = 0
    for _, sent in ipairs(harness.sendLog or {}) do if _now - sent.at < 60 then count = count + 1 end end
    return count < 100
  end,
  MakeItemKey = function(itemID, itemLevel, itemSuffix, battlePetSpeciesID)
    local level = nativeLevel(itemID)
    if itemLevel ~= nil and itemLevel ~= 0 then level = itemLevel end
    return makeKey(itemID, level, itemSuffix or 0, battlePetSpeciesID or 0)
  end,
  GetItemKeyInfo = function(key, restrictQualityToFilter)
    local itemID = keyValues(key)
    if not itemID or (harness.metadataPending and harness.metadataPending[itemID]) then return nil end
    local a = findAuction(itemID, true) or findAuction(itemID, false)
    return { itemID = itemID, battlePetSpeciesID = key.battlePetSpeciesID or 0, itemName = a and a.name or ("Item " .. itemID),
      quality = a and a.quality or 1, iconFileID = 1, isPet = (key.battlePetSpeciesID or 0) > 0,
      isCommodity = a and a.isCommodity == true or false, isEquipment = equipmentAuction(a) == true }
  end,
  SendSellSearchQuery = function(key, sorts, separateOwnerItems)
    assert(type(key) == "table" and type(sorts) == "table" and type(separateOwnerItems) == "boolean", "documented SendSellSearchQuery signature")
    local itemID = keyValues(key)
    if findAuction(itemID, true) then
      recordQueryMethod("SendSellSearchQuery", itemID, key)
      harness.ignoredCommoditySellQueries = (harness.ignoredCommoditySellQueries or 0) + 1
      return
    end
    runSearchQuery(key, "SendSellSearchQuery")
  end,
  SendSearchQuery = function(key, sorts, separateOwnerItems, minLevelFilter, maxLevelFilter)
    assert(type(key) == "table" and type(sorts) == "table" and type(separateOwnerItems) == "boolean", "documented SendSearchQuery signature")
    runSearchQuery(key, "SendSearchQuery")
  end,
  RequestMoreCommoditySearchResults = function(itemID)
    assert(harness.lastQuery and harness.lastQuery.commodity and harness.lastQuery.itemID == itemID)
    local rows = queryRows(itemID, true)
    harness.lastQuery.loaded = math.min(harness.lastQuery.loaded + (harness.pageSize or 50), #rows)
    harness.lastQuery.full = not harness.forceIncomplete and harness.lastQuery.loaded >= #rows
    harness.queryCalls = harness.queryCalls + 1
    appendSend("commodity_page", itemID)
    if harness.eventBeforeBarriers then fireResults("commodity", harness.lastQuery.key, itemID) end
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT")
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED")
    if not harness.eventBeforeBarriers then emitResults("commodity", harness.lastQuery.key, itemID) end
    return harness.lastQuery.full
  end,
  RequestMoreItemSearchResults = function(key)
    local itemID = keyValues(key)
    assert(harness.lastQuery and not harness.lastQuery.commodity and harness.lastQuery.itemID == itemID)
    local rows = queryRows(itemID, false)
    harness.lastQuery.loaded = math.min(harness.lastQuery.loaded + (harness.pageSize or 50), #rows)
    harness.lastQuery.full = not harness.forceIncomplete and harness.lastQuery.loaded >= #rows
    harness.queryCalls = harness.queryCalls + 1
    appendSend("item_page", itemID)
    if harness.eventBeforeBarriers then fireResults("item", key, itemID) end
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT")
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED")
    if not harness.eventBeforeBarriers then emitResults("item", key, itemID) end
    return harness.lastQuery.full
  end,
  HasFullCommoditySearchResults = function(itemID)
    return harness.lastQuery and harness.lastQuery.commodity and harness.lastQuery.itemID == itemID and harness.lastQuery.full or false
  end,
  HasFullItemSearchResults = function(key)
    local itemID = keyValues(key)
    return harness.lastQuery and not harness.lastQuery.commodity and harness.lastQuery.itemID == itemID and harness.lastQuery.full or false
  end,
  HasSearchResults = function(key)
    local itemID, itemLevel, itemSuffix, pet = keyValues(key)
    local current = harness.lastQuery
    return current and current.itemID == itemID and current.key.itemLevel == itemLevel and
      current.key.itemSuffix == itemSuffix and current.key.battlePetSpeciesID == pet or false
  end,
  GetCommoditySearchResultsQuantity = function(itemID)
    if not harness.lastQuery or not harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID then return 0 end
    local total = 0
    for index, auction in ipairs(queryRows(itemID, true)) do
      if index > harness.lastQuery.loaded then break end
      total = total + auction.count
    end
    return total
  end,
  GetItemSearchResultsQuantity = function(key)
    local itemID = keyValues(key)
    if not harness.lastQuery or harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID then return 0 end
    local total = 0
    for index, auction in ipairs(queryRows(itemID, false)) do
      if index > harness.lastQuery.loaded then break end
      total = total + auction.count
    end
    return total
  end,
  GetNumItemSearchResults = function(key)
    local itemID = keyValues(key); if not harness.lastQuery or harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID then return 0 end
    return math.min(harness.lastQuery.loaded, #queryRows(itemID, false))
  end,
  GetItemSearchResultInfo = function(key, index)
    local itemID = keyValues(key); local rows = queryRows(itemID, false)
    if not harness.lastQuery or not harness.lastQuery.valid or harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID or
        not keysEqual(key, harness.lastQuery.key) or index > harness.lastQuery.loaded or
        (harness.missingResults and harness.missingResults[index]) then return nil end
    local a = rows[index]; if not a then return nil end
    local itemKey = makeKey(a.itemID, a.itemLevel or 0, a.itemSuffix or 0, a.battlePetSpeciesID or 0)
    return { itemKey = itemKey, owners = ownerList(a), totalNumberOfOwners = a.totalOwners or #(a.owners or { a.owner or "Seller" }),
      timeLeft = a.timeLeft, auctionID = a.auctionID, quantity = a.count, itemLink = a.link,
      containsOwnerItem = a.containsOwnerItem == true, containsAccountItem = a.containsAccountItem == true,
      containsSocketedItem = false, minBid = a.minBid > 0 and a.minBid or nil, bidAmount = a.bid > 0 and a.bid or nil,
      buyoutAmount = a.buyout > 0 and a.buyout or nil, timeLeftSeconds = a.timeLeftSeconds }
  end,
  GetNumCommoditySearchResults = function(itemID)
    if not harness.lastQuery or not harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID then return 0 end
    return math.min(harness.lastQuery.loaded, #queryRows(itemID, true))
  end,
  GetCommoditySearchResultInfo = function(itemID, index)
    if not harness.lastQuery or not harness.lastQuery.commodity or harness.lastQuery.itemID ~= itemID or index > harness.lastQuery.loaded or
        (harness.missingResults and harness.missingResults[index]) then return nil end
    local a = queryRows(itemID, true)[index]; if not a then return nil end
    return { itemID = a.itemID, quantity = a.count, unitPrice = a.buyout, auctionID = a.auctionID,
      owners = ownerList(a), totalNumberOfOwners = a.totalOwners or #(a.owners or { a.owner or "Seller" }),
      timeLeftSeconds = a.timeLeftSeconds, numOwnerItems = a.numOwnerItems or 0,
      containsOwnerItem = a.containsOwnerItem == true, containsAccountItem = a.containsAccountItem == true }
  end,
  QueryOwnedAuctions = function(sorts)
    assert(type(sorts) == "table" and type(sorts[1]) == "table" and
      type(sorts[1].sortOrder) == "number" and type(sorts[1].reverseSort) == "boolean",
      "documented QueryOwnedAuctions signature")
    harness.ownedQueries = harness.ownedQueries + 1
    if harness.suppressOwnedTimeout == true and harness.ownedTimeoutCandidate then
      for index, timer in ipairs(_timers) do
        if timer == harness.ownedTimeoutCandidate then
          table.remove(_timers, index)
          harness.ownedTimeoutSkipped = true
          break
        end
      end
      harness.ownedTimeoutCandidate = nil
    end
    appendSend("owned", 0)
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT")
    _fire("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED")
    C_Timer.After(harness.ownedDelay or 0, function()
      if harness.emitOwnedEvent ~= false then _fire("OWNED_AUCTIONS_UPDATED") end
    end)
  end,
  HasFullOwnedAuctionResults = function() return harness.ownedFull ~= false end,
  GetNumOwnedAuctions = function() return #(harness.owned or {}) end,
  GetOwnedAuctionInfo = function(index)
    local a = (harness.owned or {})[index]; if not a then return nil end
    return { auctionID = a.auctionID, itemKey = makeKey(a.itemID, a.itemLevel or 0, a.itemSuffix or 0, a.battlePetSpeciesID or 0),
      itemLink = a.link, status = a.status == "sold" and Enum.AuctionStatus.Sold or Enum.AuctionStatus.Active,
      quantity = a.count, timeLeftSeconds = a.timeLeftSeconds, timeLeft = a.timeLeft,
      bidAmount = a.bid > 0 and a.bid or nil, buyoutAmount = a.buyout > 0 and a.buyout or nil }
  end,
  GetTimeLeftBandInfo = function(band) return band * 3600, band * 3600 + 3600 end,

}

function _serialize(name, value)
  local out = {}
  local function emit(v, indent)
    local t = type(v)
    if t == "table" then
      table.insert(out, "{\n")
      for k, child in pairs(v) do
        table.insert(out, indent .. "\t[" .. (type(k) == "string" and string.format("%q", k) or tostring(k)) .. "] = ")
        emit(child, indent .. "\t")
        table.insert(out, ",\n")
      end
      table.insert(out, indent .. "}")
    elseif t == "string" then
      table.insert(out, string.format("%q", v))
    else
      table.insert(out, tostring(v))
    end
  end
  table.insert(out, name .. " = ")
  emit(value, "")
  table.insert(out, "\n")
  return table.concat(out)
end

"""


@dataclass
class Auction:
    itemID: int
    name: str
    count: int
    buyout: int
    link: str = ""
    minBid: int = 0
    bid: int = 0
    owner: str = "Seller"
    timeLeft: int = 3
    quality: int = 1
    level: int = 1
    hasAllInfo: bool = True
    isCommodity: bool = False
    itemLevel: int = 0
    itemSuffix: int = 0
    classID: int | None = 7
    subclassID: int = 0
    minLevel: int = 0
    stackCount: int | None = None
    sellPrice: int = 1
    bindType: int = 0
    expansionID: int = 1
    setID: int = 0
    isCraftingReagent: bool = False
    battlePetSpeciesID: int = 0
    bonusIDs: list[int] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    hiddenOwners: bool = False
    totalOwners: int = 0
    containsOwnerItem: bool = False
    containsAccountItem: bool = False
    numOwnerItems: int = 0
    timeLeftSeconds: int | None = None
    auctionID: int | None = -1
    status: str = "active"

    def __post_init__(self) -> None:
        global _NEXT_AUCTION_ID
        if self.auctionID == -1:
            self.auctionID = _NEXT_AUCTION_ID
            _NEXT_AUCTION_ID += 1
        if not self.link:
            bonuses = ":".join(str(value) for value in self.bonusIDs)
            count = len(self.bonusIDs)
            fields = [
                str(self.itemID),
                "0",
                "0",
                "0",
                "0",
                "0",
                str(self.itemSuffix),
                "0",
                str(self.level),
                "0",
                "0",
                "0",
                str(count),
            ]
            if bonuses:
                fields.append(bonuses)
            self.link = "|Hitem:" + ":".join(fields) + f"|h[{self.name}]|h"
        if not self.owners and not self.hiddenOwners:
            self.owners = [self.owner]
        if not self.totalOwners and not self.hiddenOwners:
            self.totalOwners = len(self.owners)


@dataclass
class WowClient:
    faction: str = "Horde"
    realm: str | None = None
    zone: str = "Orgrimmar"
    subzone: str = ""
    auctions: list[Auction] = field(default_factory=list)
    current_region: int | None = None
    current_region_name: str | None = "US"
    realm_info: bool = False
    game_rules: bool = True
    suppress_owned_timeout: bool = False
    presentation_width: int = 1920
    presentation_height: int = 1080
    root_ui_scale: float = 1.0
    root_width: float | None = None
    root_height: float | None = None
    root_effective_scale: float | None = None
    player_name: str = "Tester"
    target_present: bool = False
    target_name: str = ""
    range_available: bool = True
    within_trade_range: bool = False
    binding_available: bool = True
    ctrl_f12_action: str = ""
    soft_target_interact: str | None = "3"
    static_popup_available: bool = True
    static_popup_shown: bool = False
    game_menu_available: bool = True
    game_menu_shown: bool = False
    initial_savedvars: dict | None = None
    target: GameTarget = TARGET
    catalog: Catalog = field(default_factory=lambda: DEFAULT_CATALOG)
    load_scanner: bool = True
    load_automation: bool = True
    combined_install: bool = False

    def __post_init__(self) -> None:
        if self.realm is None:
            self.realm = self.target.realm
        if self.current_region is None:
            self.current_region = self.target.native_region_id
        if self.presentation_width != 1920 or self.presentation_height != 1080:
            raise ValueError("the harness models only the pinned 1920x1080 presentation")
        if self.root_ui_scale <= 0:
            raise ValueError("root_ui_scale must be positive")
        if self.root_effective_scale is None:
            self.root_effective_scale = self.root_ui_scale
        if self.root_height is None:
            self.root_height = 768 / self.root_ui_scale
        if self.root_width is None:
            self.root_width = (1920 / 1080) * self.root_height
        if self.combined_install and not (self.load_scanner and self.load_automation):
            raise ValueError("the combined host bridge requires scanner and automation addons")
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True, encoding="latin-1")
        self.state = self.lua.table_from(
            {
                "faction": self.faction,
                "realm": self.realm,
                "clientVersion": self.target.client_version,
                "clientBuild": self.target.native_build,
                "interface": self.target.interface,
                "zone": self.zone,
                "subzone": self.subzone,
                "region": self.current_region,
                "regionName": self.current_region_name,
                "presentationWidth": self.presentation_width,
                "presentationHeight": self.presentation_height,
                "realmInfo": self.realm_info,
                "gameRules": self.game_rules,
                "rootWidth": self.root_width,
                "rootHeight": self.root_height,
                "rootEffectiveScale": self.root_effective_scale,
                "targetPresent": self.target_present,
                "targetName": self.target_name,
                "targetGUID": "Creature-0-0-0-0-1-1",
                "rangeAvailable": self.range_available,
                "withinTradeRange": self.within_trade_range,
                "interactDistanceCalls": self.lua.table(),
                "bindingAvailable": self.binding_available,
                "ctrlF12Action": self.ctrl_f12_action,
                "softTargetInteract": self.soft_target_interact,
                "staticPopupAvailable": self.static_popup_available,
                "staticPopupShown": self.static_popup_shown,
                "gameMenuAvailable": self.game_menu_available,
                "gameMenuShown": self.game_menu_shown,
                "auctionHouseOpen": False,
                "playerName": self.player_name,
                "playerX": 0.0,
                "playerY": 0.0,
                "playerInstance": 0,
                "playerFacing": 0.0,
                "playerSpeed": 0.0,
                "playerDead": False,
                "inCombat": False,
                "mounted": False,
                "movementBindings": self.lua.table_from(
                    {"UP": "MOVEFORWARD", "LEFT": "TURNLEFT", "RIGHT": "TURNRIGHT"}
                ),
                "cursorX": 960.0,
                "cursorY": 540.0,
                "hoverUnit": "",
                "hoverGUID": "",
                "tooltipShown": False,
                "tooltipOwner": None,
                "tooltipText": "",
                "money": 0,
                "mailOpen": False,
                "requested": self.lua.table(),
                "itemInfo": self.lua.table(),
                "itemInfoPending": self.lua.table(),
                "itemInfoNever": self.lua.table(),
                "itemInfoFailures": self.lua.table(),
                "queryCalls": 0,
                "queryMethods": self.lua.table(),
                "ignoredCommoditySellQueries": 0,
                "ownedQueries": 0,
                "suppressOwnedTimeout": self.suppress_owned_timeout,
                "ownedTimeoutSkipped": False,
                "auctions": self.lua.table(),
                "owned": self.lua.table(),
                "ownedFull": True,
                "emitOwnedEvent": True,
                "sendLog": self.lua.table(),
                "missingResults": self.lua.table(),
                "keyWarm": self.lua.table(),
                "missingClass": self.lua.table(),
                "textures": self.lua.table(),
                "chatEdit": self.lua.table_from({"shown": False, "focused": False, "text": ""}),
                "textureCreateCount": 0,
                "textureFailureAt": 0,
            }
        )
        self.lua.execute("return function(...) " + STUBS + " end")(self.state)
        self._install_codec()
        self.ns = self.lua.table()
        self.automation_ns = self.lua.table()
        if self.initial_savedvars is not None:
            self.lua.globals().CrowleyDB = self._to_lua(self.initial_savedvars)
        self._load_addon()

    def _install_codec(self) -> None:
        # The runtime maps Lua byte strings to latin-1 str, so convert at the boundary.
        def raw(s):
            return s.encode("latin-1") if isinstance(s, str) else s

        def compress(s, method):
            assert method == 0  # Enum.CompressionMethod.Deflate: raw deflate
            compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            return compressor.compress(raw(s)) + compressor.flush()

        def to_lua(value):
            if isinstance(value, dict):
                table = self.lua.table()
                for key, child in value.items():
                    table[key] = to_lua(child)
                return table
            if isinstance(value, list):
                table = self.lua.table()
                for index, child in enumerate(value, start=1):
                    table[index] = to_lua(child)
                return table
            return value

        def from_lua(value):
            if hasattr(value, "items"):
                pairs = list(value.items())
                if not pairs:
                    # C_EncodingUtil.SerializeJSON follows the native WoW
                    # table encoder: an empty Lua table is an array, while a
                    # non-empty keyed table is an object.
                    return []
                if all(isinstance(key, (int, float)) for key, _ in pairs):
                    return [from_lua(child) for _, child in sorted(pairs)]
                return {str(key): from_lua(child) for key, child in pairs}
            if isinstance(value, str):
                try:
                    return value.encode("latin-1").decode("utf-8")
                except UnicodeDecodeError:
                    pass
            return value

        g = self.lua.globals()
        g.C_EncodingUtil = self.lua.table_from(
            {
                "CompressString": compress,
                "EncodeBase64": lambda b: base64.b64encode(raw(b)).decode(),
                "DeserializeJSON": lambda source: to_lua(json.loads(source)),
                "SerializeJSON": lambda value: json.dumps(
                    from_lua(value), sort_keys=True, separators=(",", ":")
                ),
            }
        )

    def _load_addon(self) -> None:
        # Load order mirrors the client: scanner data, scanner, then independent observations.
        addon_root = _temporary_addon_tree(self.target, self.catalog)
        if self.load_scanner:
            self.run_file(addon_root / "Crowley_Data" / "Data.lua")
            for line in (addon_root / "Crowley" / "Crowley.toc").read_text().splitlines():
                if line.endswith(".lua"):
                    self.run_file(addon_root / "Crowley" / line)
        if self.load_automation:
            for line in (
                (addon_root / "Crowley_Automation" / "Crowley_Automation.toc").read_text().splitlines()
            ):
                if line.endswith(".lua"):
                    path = addon_root / "Crowley_Automation" / line
                    source = path.read_text()
                    if self.combined_install and line == "Mode.lua":
                        source = source.replace("ns.Coordinated = false", "ns.Coordinated = true")
                    self.run_source(source, path.name, self.automation_ns, addon_name="Crowley_Automation")
        if self.combined_install:
            self.run_file(addon_root / "Crowley_Host" / "Host.lua", addon_name="Crowley_Host")

    def inject_saved_variables(self, value: dict) -> None:
        """Inject SavedVariables after file execution and before ADDON_LOADED."""

        self.lua.globals().CrowleyDB = self._to_lua(value)

    def run_file(self, path: Path, namespace=None, *, addon_name: str = "Crowley") -> None:
        self.run_source(path.read_text(), path.name, namespace, addon_name=addon_name)

    def run_source(self, source: str, name: str, namespace=None, *, addon_name: str = "Crowley") -> None:
        chunk = self.lua.globals().loadstring(source, "@" + name)
        if chunk is None:
            raise SyntaxError(f"failed to load {name}")
        chunk(addon_name, namespace if namespace is not None else self.ns)

    def set_auctions(self, auctions: list[Auction]) -> None:
        table = self.lua.table()
        for i, auction in enumerate(auctions, start=1):
            table[i] = self._to_lua(auction.__dict__)
        self.state.auctions = table

    def set_source_incomplete(self, incomplete: bool = True) -> None:
        self.state.forceIncomplete = incomplete

    def set_missing_results(self, indices: list[int]) -> None:
        self.state.missingResults = self._to_lua({index: True for index in indices})

    def set_house(self, *, faction: str | None = None, realm: str | None = None) -> None:
        if faction is not None:
            self.state.faction = faction
        if realm is not None:
            self.state.realm = realm

    def set_region(self, *, region: int | None = None, name: str | None = None) -> None:
        self.state.region = region
        self.state.regionName = name

    def set_owned(self, auctions: list[Auction], *, complete: bool = True) -> None:
        table = self.lua.table()
        for i, auction in enumerate(auctions, start=1):
            table[i] = self._to_lua(auction.__dict__)
        self.state.owned = table
        self.state.ownedFull = complete

    def set_metadata_pending(self, item_ids: list[int]) -> None:
        self.state.metadataPending = self._to_lua({item_id: True for item_id in item_ids})
        if self.state.metadataDelay:
            self.state.fastCanonical = False

    def set_key_warm(self, item_ids: list[int]) -> None:
        self.state.keyWarm = self._to_lua({item_id: True for item_id in item_ids})

    def set_class_unavailable(self, item_ids: list[int]) -> None:
        self.state.missingClass = self._to_lua({item_id: True for item_id in item_ids})

    def set_event_behavior(
        self, *, kind: str = "UPDATED", delay: int = 0, stale: bool = False, before_barriers: bool = False
    ) -> None:
        self.state.eventKind = kind
        self.state.eventDelay = delay
        self.state.emitStale = stale
        self.state.eventBeforeBarriers = before_barriers
        if delay or kind != "UPDATED" or stale or before_barriers:
            self.state.fastCanonical = False

    def external_search(self, item_id: int) -> None:
        key = self.lua.globals().C_AuctionHouse.MakeItemKey(item_id, 0, 0, 0)
        sorts = self.lua.table()
        sorts[1] = self._to_lua({"sortOrder": 1, "reverseSort": False})
        self.lua.globals().C_AuctionHouse.SendSearchQuery(key, sorts, False, 0, 0)

    def set_owned_behavior(self, *, complete: bool = True, emit: bool = True, delay: int = 0) -> None:
        self.state.ownedFull = complete
        self.state.emitOwnedEvent = emit
        self.state.ownedDelay = delay
        if not complete or not emit or delay:
            self.state.fastCanonical = False

    def set_throttled(self, throttled: bool = True) -> None:
        self.state.forceThrottle = throttled
        if throttled:
            self.state.fastCanonical = False

    def set_auctioneer_state(
        self,
        *,
        target_name: str | None = None,
        range_available: bool = True,
        within_trade_range: bool = False,
        binding_available: bool = True,
        ctrl_f12_action: str = "",
    ) -> None:
        self.state.targetPresent = target_name is not None
        self.state.targetName = target_name or ""
        self.state.rangeAvailable = range_available
        self.state.withinTradeRange = within_trade_range
        self.state.bindingAvailable = binding_available
        self.state.ctrlF12Action = ctrl_f12_action

    def set_soft_target_interact(self, value: str | None) -> None:
        """Set the native interaction-mode CVar exposed to automation observations."""

        self.state.softTargetInteract = value

    def set_blocking_ui_state(
        self,
        *,
        static_popup_available: bool = True,
        static_popup_shown: bool = False,
        game_menu_available: bool = True,
        game_menu_shown: bool = False,
    ) -> None:
        self.state.staticPopupAvailable = static_popup_available
        self.state.staticPopupShown = static_popup_shown
        self.state.gameMenuAvailable = game_menu_available
        self.state.gameMenuShown = game_menu_shown

    def probe(self) -> dict:
        self.ns.Probe()
        return self._json_table(self.lua.globals().CrowleyDB.probe)

    @property
    def send_log(self) -> list[dict]:
        return [dict(entry.items()) for entry in self.state.sendLog.values()]

    @property
    def query_methods(self) -> list[dict]:
        return [dict(entry.items()) for entry in self.state.queryMethods.values()]

    @property
    def ignored_commodity_sell_queries(self) -> int:
        return self.state.ignoredCommoditySellQueries

    def _json_table(self, value) -> dict | None:
        if value is None:
            return None
        return json.loads(self.lua.globals().C_EncodingUtil.SerializeJSON(value))

    @property
    def query_trace(self) -> dict | None:
        return self._json_table(self.lua.globals().CrowleyDB.lastQueryTrace)

    @property
    def active_query_trace(self) -> dict | None:
        return self._json_table(self.lua.globals().CrowleyDB.activeQueryTrace)

    def set_readiness_api(self, name: str, mode: str) -> None:
        if mode == "absent":
            self.lua.globals().C_AuctionHouse[name] = None
        elif mode == "throwing":
            self.lua.globals().C_AuctionHouse[name] = self.lua.eval(
                "function() error('diagnostic test error') end"
            )
        else:
            raise ValueError(f"unknown readiness API mode: {mode}")

    def set_plan(self, item_ids: list[int]) -> None:
        body = {
            "schemaVersion": 2,
            "id": "first-run",
            "revision": 1,
            "catalogHash": "a" * 64,
            # Keep the fixture on the selected packaged client.  Deliberately
            # short plans remain negative fixtures for the production
            # canonical-plan gate; the old false build hid that gate entirely.
            "targetClientBuild": self.target.full_build,
            "cadenceSeconds": self.target.cadence_seconds,
            "initialPriceLevels": self.target.initial_price_levels,
            "itemIDs": sorted(item_ids),
            "pools": {"core": sorted(item_ids), "discovery": []},
        }
        plan_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
        plan = {**body, "planHash": hashlib.sha256(plan_json.encode()).hexdigest(), "planJSON": plan_json}
        self.lua.globals().CROWLEY_DATA.scanPlan = self._to_lua(plan)

    def set_canonical_plan(self) -> None:
        """Load the exact packaged plan into the native fixture."""

        self.lua.globals().CROWLEY_DATA.scanPlan = self._to_lua(target_scan_plan(self.catalog, self.target))
        # This is a harness clock convenience only.  It advances to the next
        # real timer deadline after the current callbacks drain, while the
        # production scanner still enforces its 100/60s send window.  Tests
        # that exercise late or out-of-order events disable it through one of
        # the behavior setters above.
        self.state.fastCanonical = True

    def focus_item(self, item_id: int) -> None:
        """Rotate the canonical order so an adversarial fixture starts here."""

        plan = target_scan_plan(self.catalog, self.target)
        ids = plan["itemIDs"]
        index = ids.index(item_id)
        previous = ids[index - 1] if index else ids[-1]
        db = self.lua.globals().CrowleyDB
        db.cursorItemID = previous
        db.cursorPlanHash = plan["planHash"]

    def _to_lua(self, value):
        if isinstance(value, dict):
            table = self.lua.table()
            for key, child in value.items():
                table[key] = self._to_lua(child)
            return table
        if isinstance(value, list):
            table = self.lua.table()
            for index, child in enumerate(value, start=1):
                table[index] = self._to_lua(child)
            return table
        return value

    def fire(self, event: str, *args) -> None:
        self.lua.globals()._fire(event, *args)

    def set_chat_edit(self, *, active: bool, focused: bool, text: str = "") -> None:
        edit = self.state.chatEdit
        edit.shown, edit.focused, edit.text = active, focused, text

    def set_chat_edit_bytes(self, *, active: bool, focused: bool, text: bytes = b"") -> None:
        """Set the edit box with the exact byte sequence Lua's ``#`` observes."""

        self.set_chat_edit(active=active, focused=focused, text=text.decode("latin-1"))

    def run_timers(self) -> None:
        g = self.lua.globals()
        if not self.state.fastCanonical:
            g._runTimers()
            return
        before = len(list(g.CrowleyDB.scans.items())) if g.CrowleyDB else 0
        for _ in range(2000):
            g._runTimers()
            after = len(list(g.CrowleyDB.scans.items())) if g.CrowleyDB else 0
            if after > before:
                return
            deadline = g._nextTimerAt()
            if deadline is None:
                return
            g._now = deadline

    def advance(self, seconds: int) -> None:
        g = self.lua.globals()
        g._now = g._now + seconds

    def advance_with_timers(self, seconds: int) -> None:
        """Advance the fake clock while dispatching timers at their deadlines.

        ``advance`` intentionally leaves due callbacks queued so tests can model a
        late event. Timer-order tests that need real deadline ordering should use
        this helper instead of jumping straight to the target timestamp.
        """
        g = self.lua.globals()
        target = g._now + seconds
        while True:
            deadline = g._nextTimerAt()
            if deadline is None or deadline > target:
                break
            g._now = deadline
            g._runTimers()
        g._now = target
        g._runTimers()

    def slash(self, msg: str) -> None:
        self.lua.globals().SlashCmdList.CROWLEY(msg)

    @property
    def chat(self) -> list[str]:
        return list(self.lua.globals()._chat.values())

    def saved_variables(self) -> bytes:
        g = self.lua.globals()
        return g._serialize("CrowleyDB", g.CrowleyDB).encode("latin-1")

    def login(self) -> None:
        if self.load_scanner:
            self.fire("ADDON_LOADED", "Crowley")
        if self.load_automation:
            self.fire("ADDON_LOADED", "Crowley_Automation")
        self.fire("PLAYER_LOGIN")
        self.fire("PLAYER_ENTERING_WORLD")

    def scanner_snapshot(self) -> dict:
        return self._json_table(self.ns.ScanStatusSnapshot())

    def automation_snapshot(self) -> dict:
        return self._json_table(self.lua.globals().CrowleyAutomation.snapshot())

    @property
    def scanner_frame(self) -> bytes:
        value = self.ns.ScanStatusFrameBytes()
        return value.encode("latin-1") if isinstance(value, str) else bytes(value)

    @property
    def automation_frame(self) -> bytes:
        value = self.lua.globals().CrowleyAutomation.frameBytes()
        return value.encode("latin-1") if isinstance(value, str) else bytes(value)
