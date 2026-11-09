"""Runs the real addon Lua under Lua 5.1 (WoW's version) against a fake WoW client API."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import shutil
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from lupa import lua51
from PIL import Image

from crowley.core.game_target import GameTarget, load_game_target, target_scan_plan
from crowley.core.watchlist import Catalog, load_catalog
from tools.generate_launch_target import render_generated_lua

ADDONS = Path(__file__).resolve().parents[1] / "addon"
TARGET = load_game_target(Path(__file__).parent / "fixtures" / "targets" / "verified-beta.json")
_NEXT_AUCTION_ID = 1
_BYTE_ATLAS_PATH = ADDONS / "Crowley" / "Textures" / "ByteAtlas.tga"
_BYTE_ATLAS: Image.Image | None = None
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
    shutil.copytree(ADDONS, root)
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
function debugprofilestop() _profile_ms = _profile_ms + 0.01 return _profile_ms end
function collectgarbage(option)
  harness.gcCalls = (harness.gcCalls or 0) + 1
  if option ~= "count" then
    harness.gcMutations = (harness.gcMutations or 0) + 1
    error("harness only exposes the read-only count diagnostic")
  end
  if harness.luaHeapAvailable == false then return nil end
  local values = harness.luaHeapValues
  if values and #values > 0 then
    harness.luaHeapIndex = (harness.luaHeapIndex or 0) + 1
    return values[math.min(harness.luaHeapIndex, #values)]
  end
  return harness.luaHeapValue or 512
end
function UnitExists(unit)
  if unit == "mouseover" then return harness.mouseoverExists == true end
  if unit == "target" then return harness.targetPresent == true end
  return unit == "player"
end
function UnitName(unit)
  if unit == "mouseover" then return harness.mouseoverName end
  if unit == "target" then
    if harness.targetPresent == true then return harness.targetName end
    return nil
  end
  return harness.playerName or "Tester"
end
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
  assert(key == "CTRL-F12", "documented interaction binding lookup")
  if harness.bindingAvailable == false then return nil end
  return harness.ctrlF12Action or ""
end
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
if harness.accountFakes then
  function GetMoney() return harness.money end
  function GetProfessions()
    local values = harness.professionIndices or {}
    return values[1], values[2], values[3], values[4], values[5]
  end
  function GetProfessionInfo(index)
    local info = harness.professions and harness.professions[index]
    if not info then return nil end
    return info.name, nil, info.level, info.maxLevel, nil, nil, info.skillLineID
  end
end

-- Native movement telemetry APIs.  These return the same value shapes the
-- installed client exposes; Agent.lua is responsible for rejecting absent or
-- malformed values rather than filling in safety defaults.
function UnitPosition(unit)
  if unit ~= "player" then return nil end
  return harness.positionX, harness.positionY, harness.positionZ, harness.positionInstance
end
function GetPlayerFacing() return harness.facing end
function GetUnitSpeed(unit)
  if unit ~= "player" then return nil end
  harness.speedCalls = (harness.speedCalls or 0) + 1
  local tuple = harness.speedTuple
  if tuple then return tuple[1], tuple[2], tuple[3], tuple[4] end
  return harness.speed, harness.speedRun or 0, harness.speedFlight or 0, harness.speedSwim or 0
end
function UnitAffectingCombat(unit)
  if unit ~= "player" then return nil end
  return harness.combat
end
function UnitIsDeadOrGhost(unit)
  if unit ~= "player" then return nil end
  return harness.deadOrGhost
end
function GetBindingKey(action)
  local bindings = harness.bindings
  return bindings and bindings[action]
end

if not harness.ctmMissing then
  C_CVar = {
    GetCVarBool = function(name)
      assert(name == "autointeract", "documented CVar accessor signature")
      harness.ctmCalls = (harness.ctmCalls or 0) + 1
      local mode = harness.ctmMode or "false"
      if mode == "error" then error("CVar accessor error") end
      if mode == "nil" then return nil end
      if mode == "wrong_type" then return "false" end
      return mode == "true"
    end,
  }
end

local function nativePredicate(field, mode, ...)
  harness.groundCalls = harness.groundCalls or {}
  harness.groundArgCounts = harness.groundArgCounts or {}
  harness.groundCalls[field] = (harness.groundCalls[field] or 0) + 1
  harness.groundArgCounts[field] = (harness.groundArgCounts[field] or 0) + select("#", ...)
  mode = mode or "false"
  if mode == "error" then error("native predicate error") end
  if mode == "nil" then return nil end
  if mode == "wrong_type" then return "false" end
  return mode == "true"
end
if not (harness.groundModes and harness.groundModes.flying == "missing") then
  function IsFlying(...) return nativePredicate("flying", harness.groundModes and harness.groundModes.flying, ...) end
end
if not (harness.groundModes and harness.groundModes.falling == "missing") then
  function IsFalling(...) return nativePredicate("falling", harness.groundModes and harness.groundModes.falling, ...) end
end
if not (harness.groundModes and harness.groundModes.swimming == "missing") then
  function IsSwimming(...) return nativePredicate("swimming", harness.groundModes and harness.groundModes.swimming, ...) end
end
if not (harness.groundModes and harness.groundModes.submerged == "missing") then
  function IsSubmerged(...) return nativePredicate("submerged", harness.groundModes and harness.groundModes.submerged, ...) end
end
if not (harness.groundModes and harness.groundModes.mounted == "missing") then
  function IsMounted(...) return nativePredicate("mounted", harness.groundModes and harness.groundModes.mounted, ...) end
end
if not (harness.groundModes and harness.groundModes.taxi == "missing") then
  function UnitOnTaxi(unit, ...)
    assert(unit == "player", "documented UnitOnTaxi signature")
    return nativePredicate("taxi", harness.groundModes and harness.groundModes.taxi, unit, ...)
  end
end

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
function UIParent:GetEffectiveScale() return self.effectiveScale end

GameTooltip = {}
function GameTooltip:IsShown() return harness.tooltipShown == true end
function GameTooltip:GetOwner()
  if harness.tooltipOwnerUIParent == true then return UIParent end
  return harness.otherTooltipOwner
end
GameTooltipTextLeft1 = {}
function GameTooltipTextLeft1:GetText() return harness.tooltipName end

local chatEdit = harness.chatEdit
function chatEdit:IsShown() return self.shown end
function chatEdit:HasFocus() return self.focused end
function chatEdit:GetText() return self.text end
ChatFrameUtil = { GetActiveWindow = function() return chatEdit.shown and chatEdit or nil end }

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
  function frame:EnableMouse(value) self.mouse = value end
  function frame:SetSize(width, height) self.width, self.height = width, height end
  function frame:SetPoint(...) self.point = {...} end
  function frame:Show() self.shown = true end
  function frame:Hide() self.shown = false end
  function frame:IsShown() return self.shown == true end
  function frame:SetText(value) self.text = value end
  function frame:GetText() return self.text end
  function frame:SetEnabled(value) self.enabled = value == true end
  function frame:IsEnabled() return self.enabled ~= false end
  function frame:SetMovable(value) self.movable = value == true end
  function frame:RegisterForDrag(button) self.dragButton = button end
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

local function callArgs(...)
  local args = {}
  for index = 1, select("#", ...) do args[index] = select(index, ...) end
  return args, select("#", ...)
end

local function recordTradeCall(name, ...)
  harness.tradeCalls = harness.tradeCalls or {}
  table.insert(harness.tradeCalls, name)
  harness.tradeCallRecords = harness.tradeCallRecords or {}
  local args, argc = callArgs(...)
  table.insert(harness.tradeCallRecords, { name = name, argc = argc, args = args })
end

local function recordProtectedCall(name, ...)
  recordTradeCall(name, ...)
  harness.protectedCalls = harness.protectedCalls or {}
  table.insert(harness.protectedCalls, name)
  harness.protectedCallRecords = harness.protectedCallRecords or {}
  local args, argc = callArgs(...)
  table.insert(harness.protectedCallRecords, { name = name, argc = argc, args = args })
end

local function isProtectedCall(name)
  return name == "C_AuctionHouse.StartCommoditiesPurchase" or
    name == "C_AuctionHouse.ConfirmCommoditiesPurchase" or
    name == "C_AuctionHouse.CancelCommoditiesPurchase" or
    name == "C_AuctionHouse.PostCommodity" or name == "C_AuctionHouse.CancelAuction" or
    name == "C_Container.UseContainerItem" or name == "BuyMerchantItem" or
    name == "AutoLootMailItem" or name == "C_TradeSkillUI.CraftRecipe"
end

local function installTradeNamespace(name, functions)
  _G[name] = {}
  for _, functionName in ipairs(functions) do
    _G[name][functionName] = function(...)
      local fullName = name .. "." .. functionName
      if isProtectedCall(fullName) then recordProtectedCall(fullName, ...) else recordTradeCall(fullName, ...) end
    end
  end
end

local tradeNamespaces = harness.tradeNamespaces or {}
local tradeGlobals = harness.tradeGlobals or {}
if tradeNamespaces.C_Timer == false then
  C_Timer = nil
elseif tradeNamespaces.C_Timer ~= nil then
  installTradeNamespace("C_Timer", tradeNamespaces.C_Timer)
else
  C_Timer = {
    After = function(delay, fn)
      local timer = { at = _now + delay, fn = fn }
      table.insert(_timers, timer)
      if delay == 30 and harness.suppressOwnedTimeout == true then
        harness.ownedTimeoutCandidate = timer
      end
    end,
  }
end
if harness.accountFakes and tradeNamespaces.C_Spell == nil then
  C_Spell = {
    GetSpellCooldown = function(spellID)
      return harness.tradeSkill and harness.tradeSkill.cooldowns and harness.tradeSkill.cooldowns[spellID]
    end,
  }
end
if harness.accountFakes or (harness.tradeFrames and harness.tradeFrames.MailFrame) then
  MailFrame = {
    IsShown = function() return harness.mailOpen == true end,
  }
end
for name, functions in pairs(tradeNamespaces) do
  if name ~= "C_Timer" and functions ~= false then installTradeNamespace(name, functions) end
end

for name, present in pairs(harness.tradeGlobals or {}) do
  if present then
    _G[name] = function(...)
      if isProtectedCall(name) then recordProtectedCall(name, ...) else recordTradeCall(name, ...) end
    end
  end
end

for name, present in pairs(harness.tradeFrames or {}) do
  if present and name ~= "MailFrame" then _G[name] = {} end
end

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
    if harness.referenceInfoPending and harness.referenceInfoPending[itemID] then return nil end
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
    harness.referenceInfoPending = harness.referenceInfoPending or {}
    harness.referenceInfoPending[id] = true
    if harness.referenceNever and harness.referenceNever[id] then return end
    C_Timer.After(harness.referenceDelay or harness.metadataDelay or 0, function()
      if harness.metadataPending then harness.metadataPending[id] = nil end
      if harness.referenceInfoPending then harness.referenceInfoPending[id] = nil end
      harness.keyWarm = harness.keyWarm or {}
      harness.keyWarm[id] = true
      _fire("ITEM_KEY_ITEM_INFO_RECEIVED", id)
      if harness.referenceFailures and harness.referenceFailures[id] then
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
  StartCommoditiesPurchase = function(...)
    local itemID, quantity = ...
    recordProtectedCall("C_AuctionHouse.StartCommoditiesPurchase", ...)
    harness.commodityPurchase = { itemID = itemID, quantity = quantity }
  end,
  ConfirmCommoditiesPurchase = function(...)
    local itemID, quantity = ...
    recordProtectedCall("C_AuctionHouse.ConfirmCommoditiesPurchase", ...)
    harness.commodityConfirmation = { itemID = itemID, quantity = quantity }
  end,
  CancelCommoditiesPurchase = function(...)
    local itemID = ...
    recordProtectedCall("C_AuctionHouse.CancelCommoditiesPurchase", ...)
    harness.commodityCancellation = { itemID = itemID }
  end,
  IsSellItemValid = function(location)
    return harness.sellItemValid ~= false and type(location) == "table"
  end,
  PostCommodity = function(...)
    local location, duration, quantity, unitPrice = ...
    recordProtectedCall("C_AuctionHouse.PostCommodity", ...)
    harness.postedCommodity = { location = location, duration = duration, quantity = quantity, unitPrice = unitPrice }
  end,
  CanCancelAuction = function(auctionID)
    return not (harness.uncancellable and harness.uncancellable[auctionID])
  end,
  GetCancelCost = function(auctionID)
    harness.cancelCostCalls = harness.cancelCostCalls or {}
    table.insert(harness.cancelCostCalls, auctionID)
    return harness.cancelCost or 0
  end,
  CancelAuction = function(...)
    local auctionID = ...
    recordProtectedCall("C_AuctionHouse.CancelAuction", ...)
    harness.cancelledAuction = auctionID
  end,
  CalculateCommodityDeposit = function(itemID, duration, quantity)
    harness.depositCalls = harness.depositCalls or {}
    table.insert(harness.depositCalls, { itemID = itemID, duration = duration, quantity = quantity })
    local overrides = harness.depositReturns and harness.depositReturns[itemID]
    local durationValues = overrides and overrides[duration]
    if durationValues and durationValues[quantity] ~= nil then return durationValues[quantity] end
    if harness.depositRaises and harness.depositRaises[itemID] ~= nil then
      error(harness.depositRaises[itemID])
    end
    if harness.depositErrors and harness.depositErrors[itemID] ~= nil then return harness.depositErrors[itemID] end
    local auction = lookupAuction(itemID, true)
    if not auction then return nil end
    return (auction.sellPrice or 1) * duration * quantity
  end,
}

ItemLocation = {}
function ItemLocation:CreateFromBagAndSlot(bag, slot) return { bag = bag, slot = slot } end

local function addBagItems(itemID, quantity)
  if harness.autoPostconditions == false then return end
  harness.bags = harness.bags or {}
  local bag = harness.bags[0]
  if not bag then
    bag = { numSlots = 0, freeSlots = 0, slots = {} }
    harness.bags[0] = bag
  end
  bag.slots = bag.slots or {}
  local emptySlot
  for slot = 1, (bag.numSlots or 0) do
    local info = bag.slots[slot]
    if info and info.itemID == itemID then
      info.stackCount = (info.stackCount or info.quantity or info.count or 0) + quantity
      return
    end
    if not info and not emptySlot then emptySlot = slot end
  end
  local slot = emptySlot
  if not slot then
    slot = (bag.numSlots or 0) + 1
    bag.numSlots = slot
  end
  bag.slots[slot] = { itemID = itemID, stackCount = quantity }
  if bag.freeSlots and bag.freeSlots > 0 then bag.freeSlots = bag.freeSlots - 1 end
end

if tradeNamespaces.C_Container == nil then
  C_Container = {
    GetContainerNumSlots = function(bag)
      local value = harness.bags and harness.bags[bag]
      return value and value.numSlots or 0
    end,
    GetContainerNumFreeSlots = function(bag)
      local value = harness.bags and harness.bags[bag]
      return value and value.freeSlots or 0
    end,
    GetContainerItemInfo = function(bag, slot)
      local value = harness.bags and harness.bags[bag]
      return value and value.slots and value.slots[slot] or nil
    end,
    UseContainerItem = function(...)
      local bag, slot = ...
      recordProtectedCall("C_Container.UseContainerItem", ...)
      harness.usedContainerItems = harness.usedContainerItems or {}
      table.insert(harness.usedContainerItems, { bag = bag, slot = slot })
      if harness.autoPostconditions ~= false and harness.bags and harness.bags[bag] then
        local value = harness.bags[bag]
        local previous = value.slots and value.slots[slot]
        if previous and (value.freeSlots or 0) < (value.numSlots or 0) then
          value.freeSlots = (value.freeSlots or 0) + 1
        end
        value.slots[slot] = {}
      end
    end,
  }
end

if tradeGlobals.GetMerchantNumItems == nil then
  function GetMerchantNumItems() return harness.merchantItems and #harness.merchantItems or 0 end
end
if tradeGlobals.GetMerchantItemID == nil then
  function GetMerchantItemID(index)
    local item = harness.merchantItems and harness.merchantItems[index]
    return item and item.itemID or nil
  end
end
if tradeGlobals.BuyMerchantItem == nil then
  function BuyMerchantItem(...)
    local index, quantity = ...
    recordProtectedCall("BuyMerchantItem", ...)
    harness.merchantPurchase = { index = index, quantity = quantity }
    local item = harness.merchantItems and harness.merchantItems[index]
    if item and item.itemID then addBagItems(item.itemID, quantity) end
  end
end

if tradeGlobals.GetInboxNumItems == nil then
  function GetInboxNumItems() return harness.mail and #harness.mail or 0 end
end
if tradeGlobals.GetInboxHeaderInfo == nil then
  function GetInboxHeaderInfo(index)
    local message = harness.mail and harness.mail[index]
    if not message then return nil end
    if message.headerAvailable == false then return nil end
    local items = message.items or {}
    local hasItem = message.hasItem
    if hasItem == nil then hasItem = #items > 0 end
    return nil, nil, message.sender, message.subject, message.money or 0, message.codAmount or 0,
      message.daysLeft or 0, hasItem == true
  end
end
if tradeGlobals.GetInboxItem == nil then
  function GetInboxItem(index, attachment)
    local message = harness.mail and harness.mail[index]
    local item = message and message.items and message.items[attachment]
    if not item then return nil end
    return nil, item.itemID, nil, item.count or item.stackCount or item.itemCount, item.quality or 1, true
  end
end
if tradeGlobals.AutoLootMailItem == nil then
  function AutoLootMailItem(...)
    local index = ...
    recordProtectedCall("AutoLootMailItem", ...)
    harness.lootedMail = harness.lootedMail or {}
    table.insert(harness.lootedMail, index)
    if harness.autoPostconditions ~= false then
      local message = harness.mail and harness.mail[index]
      if message and message.removeOnLoot then
        table.remove(harness.mail, index)
      elseif message then
        message.money, message.hasItem, message.items, message.empty = 0, false, {}, true
      else
        -- A count-only fixture is represented by synthetic messages, so this
        -- branch is only reachable when a native implementation mutates the
        -- inbox behind the harness.
      end
    end
  end
end

if tradeNamespaces.C_TradeSkillUI == nil then
  C_TradeSkillUI = {
    IsTradeSkillReady = function() return harness.tradeSkill and harness.tradeSkill.ready == true end,
    GetBaseProfessionInfo = function()
      return harness.tradeSkill and { skillLineID = harness.tradeSkill.skillLineID } or nil
    end,
    GetAllRecipeIDs = function() return harness.tradeSkill and harness.tradeSkill.recipeIDs or {} end,
    GetRecipeInfo = function(recipeID)
      return harness.tradeSkill and harness.tradeSkill.recipeInfos and harness.tradeSkill.recipeInfos[recipeID] or nil
    end,
    GetCraftableCount = function(recipeID)
      return harness.tradeSkill and harness.tradeSkill.craftableCounts and
        harness.tradeSkill.craftableCounts[recipeID] or 0
    end,
    CraftRecipe = function(...)
      local recipeID, times = ...
      recordProtectedCall("C_TradeSkillUI.CraftRecipe", ...)
      harness.craftedRecipe = { recipeID = recipeID, times = times }
      local recipe = harness.tradeSkill and harness.tradeSkill.recipeInfos and harness.tradeSkill.recipeInfos[recipeID]
      addBagItems(recipe and (recipe.resultItemID or recipe.itemID) or recipeID, times)
    end,
  }
end

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

function ReloadUI()
  harness.reloadCalls = (harness.reloadCalls or 0) + 1
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
    trade_namespaces: dict[str, list[str] | bool] = field(default_factory=dict)
    trade_globals: dict[str, bool] = field(default_factory=dict)
    trade_frames: dict[str, bool] = field(default_factory=dict)
    queue: dict | bool | None = None
    suppress_owned_timeout: bool = False
    bags: dict[int, dict] = field(default_factory=dict)
    merchant_items: list[dict] = field(default_factory=list)
    sell_item_valid: bool = True
    cancel_cost: int = 0
    auto_postconditions: bool = True
    presentation_width: int = 1920
    presentation_height: int = 1080
    root_ui_scale: float = 1.0
    root_width: float | None = None
    root_height: float | None = None
    root_effective_scale: float | None = None
    position: tuple[float, float, float, int] = (100.25, 200.5, 30.75, 1)
    facing: float = 1.25
    speed: float = 0.0
    speed_run: float = 7.0
    speed_flight: float = 7.0
    speed_swim: float = 4.7222218513489
    ctm_mode: str = "false"
    ctm_missing: bool = False
    ground_modes: dict[str, str] = field(
        default_factory=lambda: {
            "flying": "false",
            "falling": "false",
            "swimming": "false",
            "submerged": "false",
            "mounted": "false",
            "taxi": "false",
        }
    )
    combat: bool = False
    dead_or_ghost: bool = False
    bindings: dict[str, str] = field(
        default_factory=lambda: {
            "MOVEFORWARD": "W",
            "MOVEBACKWARD": "S",
            "STRAFELEFT": "A",
            "STRAFERIGHT": "D",
            "TURNLEFT": "Q",
            "JUMP": "SPACE",
            "TURNRIGHT": "E",
        }
    )
    money: int = 12345
    professions: list[dict] = field(default_factory=list)
    trade_ready: bool = False
    trade_skill_line_id: int = 171
    recipe_ids: list[int] = field(default_factory=list)
    recipe_infos: dict[int, dict] = field(default_factory=dict)
    cooldowns: dict[int, dict] = field(default_factory=dict)
    mail_open: bool = False
    player_name: str = "Tester"
    mouseover_exists: bool = False
    mouseover_name: str | None = None
    tooltip_shown: bool = False
    tooltip_name: str = ""
    tooltip_owner_ui_parent: bool = True
    mail: list[dict] = field(default_factory=list)
    account_fakes: bool | None = None
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
    catalog: Catalog | None = None

    def __post_init__(self) -> None:
        if self.realm is None:
            self.realm = self.target.realm
        if self.current_region is None:
            self.current_region = self.target.native_region_id
        if self.presentation_width != 1920 or self.presentation_height != 1080:
            raise ValueError("the harness models only the pinned 1920x1080 presentation")
        if self.root_ui_scale <= 0:
            raise ValueError("root_ui_scale must be positive")
        if self.account_fakes is None:
            self.account_fakes = not self.trade_globals and not self.trade_namespaces
        if self.root_effective_scale is None:
            self.root_effective_scale = self.root_ui_scale
        if self.root_height is None:
            self.root_height = 768 / self.root_ui_scale
        if self.root_width is None:
            self.root_width = (1920 / 1080) * self.root_height
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
                "realmInfo": self.realm_info,
                "gameRules": self.game_rules,
                "tradeNamespaces": self._to_lua(self.trade_namespaces),
                "tradeGlobals": self._to_lua(self.trade_globals),
                "tradeFrames": self._to_lua(self.trade_frames),
                "tradeCalls": self.lua.table(),
                "protectedCalls": self.lua.table(),
                "tradeCallRecords": self.lua.table(),
                "protectedCallRecords": self.lua.table(),
                "bags": self._to_lua(self.bags),
                "merchantItems": self._to_lua(self.merchant_items),
                "sellItemValid": self.sell_item_valid,
                "cancelCost": self.cancel_cost,
                "autoPostconditions": self.auto_postconditions,
                "rootWidth": self.root_width,
                "rootHeight": self.root_height,
                "rootEffectiveScale": self.root_effective_scale,
                "positionX": self.position[0],
                "positionY": self.position[1],
                "positionZ": self.position[2],
                "positionInstance": self.position[3],
                "facing": self.facing,
                "speed": self.speed,
                "speedRun": self.speed_run,
                "speedFlight": self.speed_flight,
                "speedSwim": self.speed_swim,
                "speedTuple": self.lua.table_from(
                    {1: self.speed, 2: self.speed_run, 3: self.speed_flight, 4: self.speed_swim}
                ),
                "ctmMode": self.ctm_mode,
                "ctmMissing": self.ctm_missing,
                "groundModes": self._to_lua(self.ground_modes),
                "groundArgCounts": self.lua.table(),
                "combat": self.combat,
                "deadOrGhost": self.dead_or_ghost,
                "targetPresent": self.target_present,
                "targetName": self.target_name,
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
                "bindings": self.lua.table(),
                "money": self.money,
                "professionIndices": self._to_lua(
                    {index: index for index in range(1, len(self.professions) + 1)}
                ),
                "professions": self._to_lua(
                    {index: profession for index, profession in enumerate(self.professions, start=1)}
                ),
                "tradeSkill": self._to_lua(
                    {
                        "ready": self.trade_ready,
                        "skillLineID": self.trade_skill_line_id,
                        "recipeIDs": self.recipe_ids,
                        "recipeInfos": self.recipe_infos,
                        "cooldowns": self.cooldowns,
                        "craftableCounts": {},
                    }
                ),
                "mailOpen": self.mail_open,
                "playerName": self.player_name,
                "mouseoverExists": self.mouseover_exists,
                "mouseoverName": self.mouseover_name,
                "tooltipShown": self.tooltip_shown,
                "tooltipName": self.tooltip_name,
                "tooltipOwnerUIParent": self.tooltip_owner_ui_parent,
                "otherTooltipOwner": self.lua.table(),
                "mail": self._to_lua(self.mail),
                "accountFakes": self.account_fakes,
                "requested": self.lua.table(),
                "itemInfo": self.lua.table(),
                "referenceInfoPending": self.lua.table(),
                "referenceNever": self.lua.table(),
                "referenceFailures": self.lua.table(),
                "depositReturns": self.lua.table(),
                "depositRaises": self.lua.table(),
                "depositErrors": self.lua.table(),
                "depositCalls": self.lua.table(),
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
                "luaHeapAvailable": True,
                "luaHeapValue": 512,
                "luaHeapValues": self.lua.table(),
                "luaHeapIndex": 0,
                "gcCalls": 0,
                "gcMutations": 0,
                "reloadCalls": 0,
                "textureCreateCount": 0,
                "textureFailureAt": 0,
            }
        )
        for action, key in self.bindings.items():
            self.state.bindings[action] = key
        self.lua.execute("return function(...) " + STUBS + " end")(self.state)
        self._install_codec()
        self.ns = self.lua.table()
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
        # Load order mirrors the client: dependency Crowley_Data first, then Crowley's TOC.
        catalog = self.catalog if self.catalog is not None else load_catalog()
        addon_root = _temporary_addon_tree(self.target, catalog)
        self.run_file(addon_root / "Crowley_Data" / "Data.lua")
        self.run_file(addon_root / "Crowley_Data" / "Queue.lua")
        if self.queue is not None:
            self.lua.globals().CROWLEY_QUEUE = self._to_lua(self.queue)
        for line in (addon_root / "Crowley" / "Crowley.toc").read_text().splitlines():
            if line.endswith(".lua"):
                self.run_file(addon_root / "Crowley" / line)

    def inject_saved_variables(self, value: dict) -> None:
        """Inject SavedVariables after file execution and before ADDON_LOADED."""

        self.lua.globals().CrowleyDB = self._to_lua(value)

    def run_file(self, path: Path) -> None:
        chunk = self.lua.globals().loadstring(path.read_text(), "@" + path.name)
        if chunk is None:
            raise SyntaxError(f"failed to load {path}")
        chunk("Crowley", self.ns)

    def set_auctions(self, auctions: list[Auction]) -> None:
        table = self.lua.table()
        for i, auction in enumerate(auctions, start=1):
            table[i] = self._to_lua(auction.__dict__)
        self.state.auctions = table

    def set_money(self, copper: int) -> None:
        self.state.money = copper

    def set_bags(self, bags: dict[int, dict]) -> None:
        self.state.bags = self._to_lua(bags)

    def set_professions(self, professions: list[dict]) -> None:
        self.state.professionIndices = self._to_lua(
            {index: index for index in range(1, len(professions) + 1)}
        )
        self.state.professions = self._to_lua(
            {index: profession for index, profession in enumerate(professions, start=1)}
        )

    def set_trade_skill(
        self,
        *,
        ready: bool,
        skill_line_id: int = 171,
        recipe_ids: list[int] | None = None,
        recipe_infos: dict[int, dict] | None = None,
        cooldowns: dict[int, dict] | None = None,
        craftable_counts: dict[int, int] | None = None,
    ) -> None:
        trade = self.state.tradeSkill
        trade.ready = ready
        trade.skillLineID = skill_line_id
        trade.recipeIDs = self._to_lua(recipe_ids or [])
        trade.recipeInfos = self._to_lua(recipe_infos or {})
        trade.cooldowns = self._to_lua(cooldowns or {})
        trade.craftableCounts = self._to_lua(craftable_counts or {})

    def set_mail(self, *, open: bool, messages: list[dict] | None = None) -> None:
        self.state.mailOpen = open
        self.state.mail = self._to_lua(messages or [])

    def set_player_name(self, name: str) -> None:
        self.state.playerName = name

    def set_hover_unit(self, name: str | None) -> None:
        self.state.mouseoverExists = name is not None
        self.state.mouseoverName = name

    def set_hover_unit_bytes(self, name: bytes) -> None:
        self.state.mouseoverExists = True
        self.state.mouseoverName = name.decode("latin-1")

    def set_hover_object(self, name: str, *, shown: bool = True, owner_ui_parent: bool = True) -> None:
        self.state.mouseoverExists = False
        self.state.mouseoverName = None
        self.state.tooltipShown = shown
        self.state.tooltipName = name
        self.state.tooltipOwnerUIParent = owner_ui_parent

    def clear_hover(self) -> None:
        self.state.mouseoverExists = False
        self.state.mouseoverName = None
        self.state.tooltipShown = False
        self.state.tooltipName = ""
        self.state.tooltipOwnerUIParent = True

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

    def set_lua_heap(
        self, *, available: bool = True, values: list[float] | None = None, value: float = 512
    ) -> None:
        self.state.luaHeapAvailable = available
        self.state.luaHeapValue = value
        self.state.luaHeapValues = self._to_lua(values or [])
        self.state.luaHeapIndex = 0

    def set_motion(
        self,
        *,
        position: tuple[float, float, float, int] | None = None,
        facing: float | None = None,
        speed: float | None = None,
        combat: bool | None = None,
        dead_or_ghost: bool | None = None,
        bindings: dict[str, str] | None = None,
    ) -> None:
        """Mutate the native movement APIs exposed by this client fixture."""

        if position is not None:
            self.state.positionX, self.state.positionY, self.state.positionZ, self.state.positionInstance = (
                position
            )
        if facing is not None:
            self.state.facing = facing
        if speed is not None:
            self.state.speed = speed
            self.state.speedTuple[1] = speed
        if combat is not None:
            self.state.combat = combat
        if dead_or_ghost is not None:
            self.state.deadOrGhost = dead_or_ghost
        if bindings is not None:
            self.state.bindings = self._to_lua(bindings)

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
        """Set the native SoftTargetInteract CVar exposed to Agent.lua."""

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
    def gc_calls(self) -> int:
        return int(self.state.gcCalls or 0)

    @property
    def gc_mutations(self) -> int:
        return int(self.state.gcMutations or 0)

    @property
    def send_log(self) -> list[dict]:
        return [dict(entry.items()) for entry in self.state.sendLog.values()]

    @property
    def query_methods(self) -> list[dict]:
        return [dict(entry.items()) for entry in self.state.queryMethods.values()]

    @property
    def ignored_commodity_sell_queries(self) -> int:
        return self.state.ignoredCommoditySellQueries

    @property
    def trade_calls(self) -> list[str]:
        return list(self.state.tradeCalls.values())

    @property
    def protected_calls(self) -> list[str]:
        return list(self.state.protectedCalls.values())

    @property
    def protected_call_records(self) -> list[dict]:
        return [self._json_table(entry) for entry in self.state.protectedCallRecords.values()]

    @property
    def executor_button(self):
        return self.lua.globals().CrowleyTradeExecutorButton

    def click_executor(self, *, force: bool = False) -> None:
        button = self.executor_button
        if button is None:
            raise AssertionError("trade executor button is unavailable")
        if force:
            self.ns.Executor.Click()
        else:
            button.Click(button)

    def set_merchant_items(self, items: list[dict]) -> None:
        self.state.merchantItems = self._to_lua(items)

    def set_mail_count(self, count: int) -> None:
        self.state.mail = self._to_lua(
            [
                {
                    "money": 0,
                    "daysLeft": 0,
                    "hasItem": False,
                    "items": [],
                    "headerAvailable": False,
                    "removeOnLoot": True,
                }
                for _ in range(count)
            ]
        )

    def set_mail_items(self, items: list[dict]) -> None:
        self.state.mail = self._to_lua(
            [
                {
                    **item,
                    "daysLeft": item.get("daysLeft", 0),
                    "hasItem": item.get("hasItem", (item.get("itemCount", 0) or 0) > 0),
                    "items": item.get("items", []),
                    "removeOnLoot": False,
                }
                for item in items
            ]
        )

    def set_auto_postconditions(self, enabled: bool) -> None:
        self.state.autoPostconditions = enabled

    def set_cancel_cost(self, cost: int) -> None:
        self.state.cancelCost = cost

    def set_frame_open(self, frame: str, opened: bool = True) -> None:
        events = {
            "ah": ("AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED"),
            "merchant": ("MERCHANT_SHOW", "MERCHANT_CLOSED"),
            "mailbox": ("MAIL_SHOW", "MAIL_CLOSED"),
            "profession": ("TRADE_SKILL_SHOW", "TRADE_SKILL_CLOSE"),
        }
        if frame not in events:
            raise ValueError(f"unknown frame: {frame}")
        if frame == "mailbox":
            # Executor admission reads the native MailFrame:IsShown() fact.
            # Keep the fixture's native visibility and event stream aligned;
            # tests may still deliberately mutate MAIL_CLOSED independently.
            self.state.mailOpen = opened
        self.fire(events[frame][0 if opened else 1])

    def execution(self, queue_id: str | None = None) -> dict | None:
        executions = self.lua.globals().CrowleyDB.executions
        if executions is None:
            return None
        if queue_id is None:
            queue_id = self.lua.globals().CROWLEY_QUEUE.queueId
        return self._json_table(executions[queue_id])

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

    def set_texture_failure(self, index: int | None) -> None:
        """Fail the actual CreateTexture callback at a deterministic ordinal."""

        self.state.textureCreateCount = 0
        self.state.textureFailureAt = int(index or 0)

    def set_reference_items(self, item_ids: list[int]) -> None:
        self.lua.globals().CROWLEY_DATA.referenceItemIDs = self._to_lua(item_ids)

    def set_reference_load_behavior(
        self,
        *,
        delay: int = 0,
        never: list[int] | None = None,
        failures: list[int] | None = None,
    ) -> None:
        self.state.referenceDelay = delay
        self.state.referenceNever = self._to_lua({item_id: True for item_id in (never or [])})
        self.state.referenceFailures = self._to_lua({item_id: True for item_id in (failures or [])})

    def set_reference_item_info(self, item_id: int, info: dict) -> None:
        self.state.itemInfo[item_id] = self._to_lua(info)

    def set_reference_info_pending(self, item_ids: list[int]) -> None:
        self.state.referenceInfoPending = self._to_lua({item_id: True for item_id in item_ids})

    def set_deposit_error(self, item_id: int, value) -> None:
        self.state.depositErrors[item_id] = value

    def set_deposit_raise(self, item_id: int, message: str = "native failure") -> None:
        self.state.depositRaises[item_id] = message

    @property
    def deposit_calls(self) -> list[dict]:
        return [dict(entry.items()) for entry in self.state.depositCalls.values()]

    @property
    def visible_native_textures(self) -> list[bool]:
        return [bool(texture.shown) for texture in self.state.textures.values()]

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

        self.lua.globals().CROWLEY_DATA.scanPlan = self._to_lua(
            target_scan_plan(self.catalog or load_catalog(), self.target)
        )
        # This is a harness clock convenience only.  It advances to the next
        # real timer deadline after the current callbacks drain, while the
        # production scanner still enforces its 100/60s send window.  Tests
        # that exercise late or out-of-order events disable it through one of
        # the behavior setters above.
        self.state.fastCanonical = True

    def focus_item(self, item_id: int) -> None:
        """Rotate the canonical order so an adversarial fixture starts here."""

        plan = target_scan_plan(self.catalog or load_catalog(), self.target)
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

    def publish_agent(self) -> dict:
        return self._json_table(self.ns.Agent.Publish())

    def snapshot_agent(self) -> dict:
        return self._json_table(self.ns.Agent.Snapshot())

    def update_agent(self, seconds: float = 1.0) -> None:
        surface = self.ns.Agent.Surface()
        surface.onUpdate(surface, seconds)

    @property
    def agent_frame(self) -> bytes:
        value = self.ns.Agent.FrameBytes()
        return value.encode("latin-1") if isinstance(value, str) else bytes(value)

    @property
    def texture_count(self) -> int:
        return len(list(self.state.textures.values()))

    @staticmethod
    def _byte_atlas() -> Image.Image:
        global _BYTE_ATLAS
        if _BYTE_ATLAS is None:
            _BYTE_ATLAS = Image.open(_BYTE_ATLAS_PATH).convert("RGBA")
            if _BYTE_ATLAS.size != (16, 512):
                raise AssertionError("ByteAtlas.tga has an unexpected size")
            if any(_BYTE_ATLAS.getpixel((x, y))[3] != 255 for y in range(512) for x in range(16)):
                raise AssertionError("ByteAtlas.tga contains transparent glyph pixels")
        return _BYTE_ATLAS

    @staticmethod
    def _point_component(value, index: int) -> float:
        if value is None:
            raise AssertionError("native texture/frame point is missing")
        result = value[index]
        if (
            isinstance(result, bool)
            or not isinstance(result, (int, float))
            or not math.isfinite(float(result))
        ):
            raise AssertionError("native texture/frame point is malformed")
        return float(result)

    @staticmethod
    def _assert_frame_point(point, *, name: str, relative_name: str) -> None:
        if point is None or point[1] != "TOPLEFT" or point[3] != "TOPLEFT":
            raise AssertionError(f"native {name} is not anchored TOPLEFT to TOPLEFT")
        if getattr(point[2], "name", None) != relative_name:
            raise AssertionError(f"native {name} has an unexpected relative frame")
        for index in (4, 5):
            value = point[index]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise AssertionError(f"native {name} offset is malformed")

    @classmethod
    def _sample_bilinear(cls, atlas: Image.Image, u: float, v: float) -> tuple[int, int, int]:
        """Sample the recorded atlas UV using the native linear texture model."""

        width, height = atlas.size
        if not 0 <= u <= 1 or not 0 <= v <= 1:
            raise AssertionError("native atlas UV is outside 0..1")
        source_x = u * width - 0.5
        source_y = v * height - 0.5
        x0 = max(0, min(width - 1, math.floor(source_x)))
        y0 = max(0, min(height - 1, math.floor(source_y)))
        x1 = max(0, min(width - 1, x0 + 1))
        y1 = max(0, min(height - 1, y0 + 1))
        fx = source_x - math.floor(source_x)
        fy = source_y - math.floor(source_y)
        top_left = atlas.getpixel((x0, y0))
        top_right = atlas.getpixel((x1, y0))
        bottom_left = atlas.getpixel((x0, y1))
        bottom_right = atlas.getpixel((x1, y1))
        return tuple(
            round(
                top_left[channel] * (1 - fx) * (1 - fy)
                + top_right[channel] * fx * (1 - fy)
                + bottom_left[channel] * (1 - fx) * fy
                + bottom_right[channel] * fx * fy
            )
            for channel in range(3)
        )

    @staticmethod
    def _sample_nearest(atlas: Image.Image, u: float, v: float) -> tuple[int, int, int]:
        """Sample the recorded atlas UV using the native nearest model."""

        width, height = atlas.size
        if not 0 <= u <= 1 or not 0 <= v <= 1:
            raise AssertionError("native atlas UV is outside 0..1")
        x = max(0, min(width - 1, math.floor(u * width)))
        y = max(0, min(height - 1, math.floor(v * height)))
        pixel = atlas.getpixel((x, y))
        return pixel[:3]

    def render_agent_screenshot(self) -> Image.Image:
        """Rasterize the actual recorded frame, texture rectangles, and atlas UVs."""

        atlas = self._byte_atlas()
        screenshot = Image.new("RGB", (self.presentation_width, self.presentation_height), (17, 17, 17))
        surface = self.ns.Agent.Surface()
        if surface is None or not surface.shown:
            raise AssertionError("native telemetry surface is not shown")
        root_factor = float(self.root_height) * float(self.root_effective_scale) / self.presentation_height
        if root_factor <= 0 or not math.isfinite(root_factor):
            raise AssertionError("native root geometry is invalid")
        surface_point = surface.point
        self._assert_frame_point(surface_point, name="telemetry surface", relative_name="UIParent")
        if (
            abs(self._point_component(surface_point, 4)) > 1e-6
            or abs(self._point_component(surface_point, 5)) > 1e-6
        ):
            raise AssertionError("native telemetry surface must have a zero UIParent offset")
        if surface.width != 336 or surface.height != 304:
            raise AssertionError("native telemetry surface does not include the pinned crop offset")
        surface_scale = float(surface.scale)
        if not math.isfinite(surface_scale) or surface_scale <= 0:
            raise AssertionError("native telemetry surface scale is invalid")
        if abs(surface.GetEffectiveScale(surface) - surface_scale) > 1e-6:
            raise AssertionError("native telemetry surface effective scale ignores its parent incorrectly")
        # The harness models anchored-frame coordinates in the frame's own
        # effective scale.  The production surface deliberately uses a zero
        # UIParent offset, removing the unresolved client offset convention.
        surface_x = self._point_component(surface_point, 4) * surface_scale / root_factor
        surface_y = -self._point_component(surface_point, 5) * surface_scale / root_factor
        for texture in self.state.textures.values():
            texture_args = texture.textureArgs
            if (
                not texture.shown
                or texture_args is None
                or texture_args.textureAsset != "Interface\\AddOns\\Crowley\\Textures\\ByteAtlas"
            ):
                raise AssertionError("native glyph texture is missing its atlas")
            if (
                texture_args.wrapHorizontal != "CLAMP"
                or texture_args.wrapVertical != "CLAMP"
                or texture_args.filterMode not in {"NEAREST", "LINEAR"}
            ):
                raise AssertionError("native glyph texture has an unsupported sampling contract")
            filter_mode = texture_args.filterMode
            point = texture.point
            self._assert_frame_point(point, name="glyph texture", relative_name="CrowleyAgentSurface")
            if texture.snap is not True or texture.bias != 0.5:
                raise AssertionError("native glyph texture is missing the pinned pixel snapping")
            local_x = self._point_component(point, 4) * surface_scale / root_factor
            local_y = -self._point_component(point, 5) * surface_scale / root_factor
            width = float(texture.width) * surface_scale / root_factor
            height = float(texture.height) * surface_scale / root_factor
            if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
                raise AssertionError("native glyph texture geometry is invalid")
            left = surface_x + local_x
            top = surface_y + local_y
            x0 = math.floor(left + 0.5)
            y0 = math.floor(top + 0.5)
            x1 = math.floor(left + width + 0.5)
            y1 = math.floor(top + height + 0.5)
            if x1 <= x0 or y1 <= y0:
                raise AssertionError("native glyph texture has no rasterized pixels")
            uv = texture.texCoord
            u0 = self._point_component(uv, 1)
            u1 = self._point_component(uv, 2)
            v0 = self._point_component(uv, 3)
            v1 = self._point_component(uv, 4)
            if not (0 <= u0 < u1 <= 1 and 0 <= v0 < v1 <= 1):
                raise AssertionError("native glyph texture has unsafe atlas UVs")
            pixels = screenshot.load()
            for dy in range(y1 - y0):
                v = v0 + (dy + 0.5) / (y1 - y0) * (v1 - v0)
                for dx in range(x1 - x0):
                    u = u0 + (dx + 0.5) / (x1 - x0) * (u1 - u0)
                    pixel_x, pixel_y = x0 + dx, y0 + dy
                    if 0 <= pixel_x < self.presentation_width and 0 <= pixel_y < self.presentation_height:
                        if filter_mode == "NEAREST":
                            sample = self._sample_nearest(atlas, u, v)
                        else:
                            sample = self._sample_bilinear(atlas, u, v)
                        pixels[pixel_x, pixel_y] = sample
        return screenshot

    def assert_native_geometry(self) -> None:
        """Assert the requested effective scale for a valid native geometry proof."""

        surface = self.ns.Agent.Surface()
        root_factor = float(self.root_height) * float(self.root_effective_scale) / self.presentation_height
        if surface is None or not surface.shown:
            raise AssertionError("native telemetry surface is not shown")
        if not math.isfinite(float(surface.scale)) or float(surface.scale) <= 0:
            raise AssertionError("native telemetry surface scale is invalid")
        if abs(float(surface.scale) - root_factor) > 1e-6:
            raise AssertionError("native telemetry surface scale does not match the requested native factor")

    def render_agent_capture(self) -> Image.Image:
        """Return the pinned crop from an actual full-presentation raster."""

        screenshot = self.render_agent_screenshot()
        return screenshot.crop((80, 48, 80 + 256, 48 + 256))

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
        late event.  Lifecycle tests that need real deadline ordering should use
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
        self.fire("ADDON_LOADED", "Crowley")
        self.fire("PLAYER_LOGIN")
        self.fire("PLAYER_ENTERING_WORLD")

    @property
    def reload_calls(self) -> int:
        return int(self.state.reloadCalls or 0)

    def click_native(self, role: str) -> None:
        names = {
            "construct": "CrowleyNativeConstructionConstruct",
            "restore": "CrowleyNativeConstructionRestore",
        }
        if role not in names:
            raise ValueError(f"unknown native construction control {role}")
        control = self.lua.globals()[names[role]]
        if control is None:
            raise AssertionError(f"native construction {role} control is unavailable")
        control.Click(control)
