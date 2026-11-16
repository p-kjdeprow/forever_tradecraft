local ADDON_NAME, ns = ...

local VERSION = "0.1.0"
local WIDTH, HEIGHT = 128, 128
local CROP_X, CROP_Y = 352, 48
local ATLAS_PATH = "Interface\\AddOns\\Crowley_Automation\\Textures\\ByteAtlas"
local sequence = 0
local boot = "automation-" .. tostring(time()) .. "-" .. string.format("%06d", math.random(0, 999999))
local surface, cells
local frameBytes
local elapsed = 1
local ahOpen = false

local function stringFact(value, name, allowEmpty)
  if type(value) ~= "string" or (not allowEmpty and value == "") then
    error("automation observation requires " .. name)
  end
  return value
end

local function activeEdit()
  local chat = _G.ChatFrameUtil
  if type(chat) ~= "table" or type(chat.GetActiveWindow) ~= "function" then
    error("chat active-window query is unavailable")
  end
  local edit = chat.GetActiveWindow()
  if edit == nil then
    return { active = false, focused = false, text = "" }
  end

  if type(edit.IsShown) ~= "function" then error("chat edit visibility query is unavailable") end
  local shown = edit:IsShown()
  if type(shown) ~= "boolean" then error("chat edit visibility is unavailable") end
  if not shown then return { active = false, focused = false, text = "" } end

  if type(edit.HasFocus) ~= "function" then error("chat edit focus query is unavailable") end
  if type(edit.GetText) ~= "function" then error("chat edit text query is unavailable") end
  local focused = edit:HasFocus()
  if type(focused) ~= "boolean" then error("chat edit focus is unavailable") end
  local text = edit:GetText()
  if type(text) ~= "string" then error("chat edit text is unavailable") end
  if #text > 128 then error("chat edit text exceeds 128 UTF-8 bytes") end
  return { active = true, focused = focused, text = text }
end

local function auctioneer()
  local exists = UnitExists("target")
  if type(exists) ~= "boolean" then error("auctioneer target presence is unavailable") end
  local name, rangeAvailable, withinRange = "", false, false
  if exists then
    name = stringFact(UnitName("target"), "auctioneer target name", false)
    local range = _G.CheckInteractDistance
    if type(range) == "function" then
      local result = range("target", 2)
      if type(result) == "boolean" then rangeAvailable, withinRange = true, result end
    end
  end
  local bindingAvailable, action = false, ""
  if type(_G.GetBindingAction) == "function" then
    local result = _G.GetBindingAction("CTRL-F12")
    if type(result) == "string" then bindingAvailable, action = true, result end
  end
  local interactionMode = "unavailable"
  if type(_G.GetCVar) == "function" then
    local ok, result = pcall(_G.GetCVar, "SoftTargetInteract")
    if ok and type(result) == "string" then
      local modes = { ["0"] = "disabled", ["1"] = "gamepad", ["2"] = "keyboard", ["3"] = "any" }
      interactionMode = modes[result] or "unavailable"
    end
  end
  return {
    targetPresent = exists, targetName = name, rangeAvailable = rangeAvailable,
    withinTradeRange = withinRange, bindingAvailable = bindingAvailable,
    ctrlF12Action = action, interactionMode = interactionMode,
  }
end

local function blockingUI()
  local popupAvailable, popupShown = false, false
  local popup = _G.StaticPopup_IsAnyDialogShown
  if type(popup) == "function" then
    local ok, value = pcall(popup)
    if ok and type(value) == "boolean" then popupAvailable, popupShown = true, value end
  end
  local menuAvailable, menuShown = false, false
  local menu = _G.GameMenuFrame
  if type(menu) == "table" and type(menu.IsShown) == "function" then
    local ok, value = pcall(function() return menu:IsShown() end)
    if ok and type(value) == "boolean" then menuAvailable, menuShown = true, value end
  end
  return {
    staticPopupAvailable = popupAvailable, staticPopupShown = popupShown,
    gameMenuAvailable = menuAvailable, gameMenuShown = menuShown,
  }
end

local function snapshot()
  local version, build, _, interface = GetBuildInfo()
  local character = stringFact(UnitName("player"), "character", false)
  local realm = stringFact(GetRealmName(), "realm", false)
  local faction = stringFact(UnitFactionGroup("player"), "player faction", false)
  local zone = stringFact(GetZoneText(), "zone", true)
  sequence = sequence + 1
  return {
    schema = 1,
    kind = "automation",
    boot = boot,
    sequence = sequence,
    wallTime = time(),
    uptimeMs = math.floor(GetTime() * 1000) % 4294967296,
    context = {
      addonVersion = VERSION,
      interface = interface,
      clientBuild = build,
      character = character,
      realm = realm,
      playerFaction = faction,
      zone = zone,
    },
    ahOpen = ahOpen,
    auctioneer = auctioneer(),
    blockingUI = blockingUI(),
    chatEdit = activeEdit(),
  }
end

local function publish()
  local value = snapshot()
  local json = C_EncodingUtil.SerializeJSON(value)
  local compressed = C_EncodingUtil.CompressString(json, Enum.CompressionMethod.Deflate)
  local bytes = ns.AutomationEncoder.Frame(json, compressed, value.sequence, value.wallTime, value.uptimeMs)
  ns.AutomationRendering.Update(cells, bytes, WIDTH, HEIGHT)
  frameBytes = bytes
end

local function safePublish()
  pcall(publish)
end

local function start()
  surface, cells = ns.AutomationRendering.Create(
    WIDTH, HEIGHT, CROP_X, CROP_Y, ATLAS_PATH, "CrowleyAutomationSurface"
  )
  surface:SetScript("OnUpdate", function(_, delta)
    elapsed = elapsed + delta
    if elapsed >= 1 then
      elapsed = 0
      safePublish()
    end
  end)
  safePublish()
end

local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("ADDON_LOADED")
eventFrame:RegisterEvent("PLAYER_ENTERING_WORLD")
eventFrame:RegisterEvent("AUCTION_HOUSE_SHOW")
eventFrame:RegisterEvent("AUCTION_HOUSE_CLOSED")
eventFrame:SetScript("OnEvent", function(_, event, name)
  if event == "ADDON_LOADED" then
    if name == ADDON_NAME then start() end
  elseif event == "PLAYER_ENTERING_WORLD" then
    safePublish()
  elseif event == "AUCTION_HOUSE_SHOW" then
    ahOpen = true
    safePublish()
  elseif event == "AUCTION_HOUSE_CLOSED" then
    ahOpen = false
    safePublish()
  end
end)

ns.Automation = {
  version = VERSION,
  snapshot = snapshot,
  frameBytes = function() return frameBytes end,
}
