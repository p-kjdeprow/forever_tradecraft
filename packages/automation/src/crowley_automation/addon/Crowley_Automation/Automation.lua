local ADDON_NAME, ns = ...

local VERSION = "0.3.0"
local WIDTH, HEIGHT = 128, 128
local CROP_X, CROP_Y = 352, 48
local ATLAS_PATH = "Interface\\AddOns\\Crowley_Automation\\Textures\\ByteAtlas"
local sequence = 0
local boot = "automation-" .. tostring(time()) .. "-" .. string.format("%06d", math.random(0, 999999))
local surface, cells, frameBytes, button
local elapsed = 1
local operations, pendingHostBridge
local started = false

local function requiredFunction(owner, name, label)
  if type(owner) ~= "table" or type(owner[name]) ~= "function" then error(label .. " unavailable") end
  return owner[name]
end

local function boundedString(value, name, empty, maximum)
  if type(value) ~= "string" or (not empty and value == "") or #value > maximum then
    error("automation observation requires " .. name)
  end
  return value
end

local function exactInteger(value, minimum, maximum)
  return type(value) == "number" and value == math.floor(value) and value >= minimum and value <= maximum
end

local function finite(value)
  return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

local function clientContext()
  local _, build, _, interface = GetBuildInfo()
  local region = GetCurrentRegion()
  local character = UnitName("player")
  local realm = GetRealmName()
  local faction = UnitFactionGroup("player")
  local zone = GetZoneText()
  if not exactInteger(region, 1, 4294967295) then error("native region is unavailable") end
  return {
    addonVersion = VERSION,
    interface = interface,
    clientBuild = boundedString(build, "client build", false, 128),
    character = boundedString(character, "character", false, 128),
    realm = boundedString(realm, "realm", false, 128),
    playerFaction = boundedString(faction, "player faction", false, 128),
    zone = boundedString(zone, "zone", true, 128),
    nativeRegion = region,
  }
end

local function auctioneer()
  local exists = requiredFunction(_G, "UnitExists", "target presence query")("target")
  if type(exists) ~= "boolean" then error("auctioneer target presence is unavailable") end
  local name, rangeAvailable, withinRange = "", false, false
  if exists then
    name = boundedString(requiredFunction(_G, "UnitName", "target name query")("target"), "target name", false, 128)
    local inRange = requiredFunction(_G, "CheckInteractDistance", "target range query")("target", 2)
    if type(inRange) ~= "boolean" then error("target range is unavailable") end
    rangeAvailable, withinRange = true, inRange
  end
  local binding = requiredFunction(_G, "GetBindingAction", "auctioneer binding query")("CTRL-F12")
  if type(binding) ~= "string" then error("auctioneer binding is unavailable") end
  local interactionMode = "unavailable"
  if type(_G.GetCVar) == "function" then
    local ok, result = pcall(_G.GetCVar, "SoftTargetInteract")
    if ok and type(result) == "string" then
      local modes = { ["0"] = "disabled", ["1"] = "gamepad", ["2"] = "keyboard", ["3"] = "any" }
      interactionMode = modes[result] or "unavailable"
    end
  end
  return {
    targetPresent = exists,
    targetName = name,
    rangeAvailable = rangeAvailable,
    withinTradeRange = withinRange,
    bindingAvailable = true,
    ctrlF12Action = binding,
    interactionMode = interactionMode,
  }
end

local function blockingUI()
  local popup = requiredFunction(_G, "StaticPopup_IsAnyDialogShown", "static popup query")()
  if type(popup) ~= "boolean" then error("static popup state is unavailable") end
  if type(_G.GameMenuFrame) ~= "table" or type(GameMenuFrame.IsShown) ~= "function" then
    error("game menu query unavailable")
  end
  local menu = GameMenuFrame:IsShown()
  if type(menu) ~= "boolean" then error("game menu state is unavailable") end
  return { staticPopupAvailable = true, staticPopupShown = popup, gameMenuAvailable = true, gameMenuShown = menu }
end

local function chatEdit()
  local chat = _G.ChatFrameUtil
  if type(chat) ~= "table" or type(chat.GetActiveWindow) ~= "function" then error("chat active-window query unavailable") end
  local edit = chat.GetActiveWindow()
  if edit == nil then return { active = false, focused = false, text = "" } end
  if type(edit.IsShown) ~= "function" or type(edit.HasFocus) ~= "function" or type(edit.GetText) ~= "function" then
    error("chat input facts unavailable")
  end
  local shown, focused, text = edit:IsShown(), edit:HasFocus(), edit:GetText()
  if type(shown) ~= "boolean" or type(focused) ~= "boolean" or type(text) ~= "string" or #text > 128 then
    error("chat input facts malformed")
  end
  if not shown then return { active = false, focused = false, text = "" } end
  return { active = true, focused = focused, text = text }
end

local function motion()
  local x, y, _, instance = requiredFunction(_G, "UnitPosition", "player position query")("player")
  local facing = requiredFunction(_G, "GetPlayerFacing", "player facing query")()
  local speed = requiredFunction(_G, "GetUnitSpeed", "player speed query")("player")
  local dead = requiredFunction(_G, "UnitIsDeadOrGhost", "player death query")("player")
  local combat = requiredFunction(_G, "InCombatLockdown", "combat state query")()
  local mounted = requiredFunction(_G, "IsMounted", "mount state query")()
  if not finite(x) or not finite(y) or not exactInteger(instance, 0, 4294967295) or not finite(facing) or
      not finite(speed) or speed < 0 or type(dead) ~= "boolean" or type(combat) ~= "boolean" or type(mounted) ~= "boolean" then
    error("player motion facts malformed")
  end
  local moving = speed > 0
  local targetPresent = requiredFunction(_G, "UnitExists", "mouseover target query")("target")
  if type(targetPresent) ~= "boolean" then error("target presence unavailable") end
  local target = { present = targetPresent, guid = "", name = "", rangeAvailable = false, withinRange = false }
  if targetPresent then
    target.guid = boundedString(requiredFunction(_G, "UnitGUID", "target GUID query")("target"), "target GUID", false, 128)
    target.name = boundedString(requiredFunction(_G, "UnitName", "target name query")("target"), "target name", false, 128)
    local inRange = requiredFunction(_G, "CheckInteractDistance", "target range query")("target", 2)
    if type(inRange) ~= "boolean" then error("target range unavailable") end
    target.rangeAvailable, target.withinRange = true, inRange
  end
  local forward = requiredFunction(_G, "GetBindingAction", "forward binding query")("UP")
  local left = requiredFunction(_G, "GetBindingAction", "left binding query")("LEFT")
  local right = requiredFunction(_G, "GetBindingAction", "right binding query")("RIGHT")
  if type(forward) ~= "string" or type(left) ~= "string" or type(right) ~= "string" then
    error("movement binding facts malformed")
  end
  local physicalWidth, physicalHeight = requiredFunction(_G, "GetPhysicalScreenSize", "physical screen size query")()
  local cursorX, cursorY = requiredFunction(_G, "GetCursorPosition", "cursor position query")()
  if not finite(physicalWidth) or not finite(physicalHeight) or physicalWidth < 1 or physicalHeight < 1 or
      not finite(cursorX) or not finite(cursorY) then error("physical cursor facts malformed") end
  local screenX, screenY = cursorX, physicalHeight - cursorY
  if screenX < 0 or screenX > physicalWidth or screenY < 0 or screenY > physicalHeight then
    error("physical cursor coordinates are outside the screen")
  end
  local hoverKind, hoverName = "none", ""
  local mouseover = requiredFunction(_G, "UnitExists", "mouseover unit query")("mouseover")
  if type(mouseover) ~= "boolean" then error("mouseover unit fact malformed") end
  if mouseover then
    hoverKind = "unit"
    hoverName = boundedString(requiredFunction(_G, "UnitName", "mouseover unit name query")("mouseover"), "mouseover unit name", false, 128)
  else
    local tooltip = _G.GameTooltip
    if type(tooltip) ~= "table" or type(tooltip.IsShown) ~= "function" or type(tooltip.GetOwner) ~= "function" then
      error("world object tooltip facts unavailable")
    end
    local shown = tooltip:IsShown()
    if type(shown) ~= "boolean" then error("world object tooltip visibility malformed") end
    if shown and tooltip:GetOwner() == UIParent then
      local line = _G.GameTooltipTextLeft1
      if type(line) ~= "table" or type(line.GetText) ~= "function" then error("world object tooltip text unavailable") end
      hoverKind = "object"
      hoverName = boundedString(line:GetText(), "world object tooltip", false, 128)
    end
  end
  return {
    x = x,
    y = y,
    instance = instance,
    facing = facing,
    moving = moving,
    dead = dead,
    inCombat = combat,
    mounted = mounted,
    target = target,
    bindings = { forward = forward, left = left, right = right },
    hover = {
      kind = hoverKind,
      name = hoverName,
      screenX = screenX,
      screenY = screenY,
    },
  }
end

local function buttonRect()
  if not button then error("operation button unavailable") end
  local physicalWidth, physicalHeight = GetPhysicalScreenSize()
  local scale = UIParent:GetEffectiveScale()
  local left, bottom, width, height = button:GetLeft(), button:GetBottom(), button:GetWidth(), button:GetHeight()
  if not finite(physicalWidth) or not finite(physicalHeight) or not finite(scale) or scale <= 0 or
      not finite(left) or not finite(bottom) or not finite(width) or not finite(height) or width <= 0 or height <= 0 then
    error("operation button geometry unavailable")
  end
  local x = math.floor(left * scale)
  local y = math.floor(physicalHeight - (bottom + height) * scale)
  local result = { x = x, y = y, width = math.ceil(width * scale), height = math.ceil(height * scale) }
  if result.x < 0 or result.y < 0 or result.x + result.width > physicalWidth or
      result.y + result.height > physicalHeight then error("operation button is outside the physical screen") end
  return result
end

local function buttonAvailable()
  if not button or type(button.IsShown) ~= "function" or type(button.IsEnabled) ~= "function" then
    return false
  end
  local shown, enabled = button:IsShown(), button:IsEnabled()
  return type(shown) == "boolean" and shown and type(enabled) == "boolean" and enabled
end

local function snapshot()
  local context = clientContext()
  sequence = sequence + 1
  local value = {
    schema = 3,
    kind = "automation",
    boot = boot,
    sequence = sequence,
    wallTime = time(),
    uptimeMs = math.floor(GetTime() * 1000) % 4294967296,
    context = context,
    ahOpen = operations.ahOpen,
    auctioneer = auctioneer(),
    blockingUI = blockingUI(),
    chatEdit = chatEdit(),
    goldCopper = GetMoney(),
    operation = operations:Snapshot(buttonRect(), buttonAvailable()),
    sellerMarket = operations:SellerMarket(),
    inspection = operations:Inspection(),
    motion = motion(),
    mailOpen = operations.mailOpen,
    ownership = operations:Ownership(),
  }
  if not exactInteger(value.goldCopper, 0, 9007199254740991) then error("player money observation malformed") end
  return value
end

local function publish()
  operations:Update()
  button:SetText(operations:ButtonLabel())
  button:SetEnabled(operations:ClickReady())
  local value = snapshot()
  local json = C_EncodingUtil.SerializeJSON(value)
  local compressed = C_EncodingUtil.CompressString(json, Enum.CompressionMethod.Deflate)
  frameBytes = ns.AutomationEncoder.Frame(json, compressed, value.sequence, value.wallTime, value.uptimeMs)
  ns.AutomationRendering.Update(cells, frameBytes, WIDTH, HEIGHT)
end

local function safePublish()
  pcall(publish)
end

local function createButton()
  button = CreateFrame("Button", "CrowleyAutomationOperationButton", UIParent, "UIPanelButtonTemplate")
  button:SetFrameStrata("TOOLTIP")
  button:SetFrameLevel(1001)
  button:SetSize(220, 44)
  button:SetPoint("BOTTOM", UIParent, "BOTTOM", 0, 96)
  button:SetText("Crowley: no operation")
  button:RegisterForClicks("LeftButtonUp")
  button:SetScript("OnClick", function(frame, mouseButton, down)
    operations:OnClick(frame, mouseButton, down)
    safePublish()
  end)
end

local function start()
  if started then return end
  started = true
  operations = ns.AutomationOperations.New()
  if pendingHostBridge then
    operations:SetHostBridge(pendingHostBridge)
    pendingHostBridge = nil
  end
  createButton()
  surface, cells = ns.AutomationRendering.Create(
    WIDTH, HEIGHT, CROP_X, CROP_Y, ATLAS_PATH, "CrowleyAutomationSurface"
  )
  SLASH_CAUTO1 = "/cauto"
  SlashCmdList.CAUTO = function(message)
    local accepted = operations:Command(message)
    safePublish()
    return accepted
  end
  surface:SetScript("OnUpdate", function(_, delta)
    elapsed = elapsed + delta
    local interval = operations:Interval()
    if elapsed >= interval then
      elapsed = 0
      safePublish()
    end
  end)
  safePublish()
end

local events = CreateFrame("Frame")
for _, event in ipairs({
  "ADDON_LOADED",
  "PLAYER_ENTERING_WORLD",
  "AUCTION_HOUSE_SHOW",
  "AUCTION_HOUSE_CLOSED",
  "AUCTION_HOUSE_AUCTION_CREATED",
  "OWNED_AUCTIONS_UPDATED",
  "AUCTION_CANCELED",
  "COMMODITY_SEARCH_RESULTS_UPDATED",
  "COMMODITY_SEARCH_RESULTS_ADDED",
  "AUCTION_HOUSE_THROTTLED_MESSAGE_SENT",
  "AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED",
  "PLAYER_MONEY",
  "MAIL_SHOW",
  "MAIL_CLOSED",
  "MAIL_INBOX_UPDATE",
  "MAIL_SUCCESS",
  "MAIL_FAILED",
  "BAG_UPDATE_DELAYED",
}) do
  events:RegisterEvent(event)
end
events:SetScript("OnEvent", function(_, event, ...)
  if event == "ADDON_LOADED" and ... == ADDON_NAME then
    start()
  elseif not operations then
    return
  elseif event == "PLAYER_ENTERING_WORLD" then
    safePublish()
  else
    operations:OnEvent(event, ...)
    safePublish()
  end
end)

_G.CrowleyAutomation = {
  protocol = 3,
  snapshot = function() return operations and snapshot() or nil end,
  frameBytes = function() return frameBytes end,
  setHostBridge = function(value)
    if operations then return operations:SetHostBridge(value) end
    if type(value) ~= "table" or type(value.suspend) ~= "function" or
        type(value.quiescent) ~= "function" or type(value.resume) ~= "function" then
      return false
    end
    pendingHostBridge = value
    return true
  end,
}
