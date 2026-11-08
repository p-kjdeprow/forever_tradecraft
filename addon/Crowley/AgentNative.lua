local _, ns = ...

-- Agent's native capability adapter.  Lifecycle state and HUD bytes remain
-- pure; this module is the only Agent seam that names timers and encoding APIs.
local Native = {}
ns.AgentNative = Native

local function globalFunction(name)
  return type(_G[name]) == "function" and _G[name] or nil
end

function Native.Now() return time() end
function Native.Uptime() return GetTime() end
function Native.ProfileStop() return debugprofilestop() end
function Native.HeapCount() return collectgarbage("count") end

function Native.ActiveEdit()
  local chat = _G.ChatFrameUtil
  if type(chat) ~= "table" or type(chat.GetActiveWindow) ~= "function" then
    return { active = false, focused = false, text = "" }
  end
  local edit = chat.GetActiveWindow()
  if not edit or type(edit.IsShown) ~= "function" or type(edit.HasFocus) ~= "function" or
      type(edit.GetText) ~= "function" then
    return { active = false, focused = false, text = "" }
  end
  local text = edit:GetText()
  if type(text) ~= "string" or #text > 128 then return { active = false, focused = false, text = "" } end
  return { active = edit:IsShown() == true, focused = edit:HasFocus() == true, text = text }
end

function Native.Hover()
  local unitExists, unitName = globalFunction("UnitExists"), globalFunction("UnitName")
  if not unitExists then error("Crowley hover telemetry requires UnitExists") end
  if not unitName then error("Crowley hover telemetry requires UnitName") end
  local tooltip, tooltipText, parent = _G.GameTooltip, _G.GameTooltipTextLeft1, _G.UIParent
  if type(tooltip) ~= "table" or type(tooltip.IsShown) ~= "function" or type(tooltip.GetOwner) ~= "function" then
    error("Crowley hover telemetry requires GameTooltip")
  end
  if type(tooltipText) ~= "table" or type(tooltipText.GetText) ~= "function" then
    error("Crowley hover telemetry requires GameTooltipTextLeft1")
  end
  if parent == nil then error("Crowley hover telemetry requires UIParent") end
  return {
    unitExists = unitExists("mouseover"), unitName = unitName("mouseover"),
    tooltipShown = tooltip:IsShown() == true, tooltipOwner = tooltip:GetOwner(),
    uiParent = parent, objectName = tooltipText:GetText(),
  }
end

function Native.MailShown()
  local frame = _G.MailFrame
  if type(frame) ~= "table" or type(frame.IsShown) ~= "function" then error("Crowley mail telemetry requires MailFrame") end
  return frame:IsShown() == true
end

function Native.Binding(action)
  local fn = globalFunction("GetBindingKey")
  if not fn then error("Crowley movement telemetry requires GetBindingKey") end
  return fn(action)
end

function Native.Position()
  local fn = globalFunction("UnitPosition")
  if not fn then error("Crowley movement telemetry requires UnitPosition") end
  return fn("player")
end
function Native.Facing()
  local fn = globalFunction("GetPlayerFacing")
  if not fn then error("Crowley movement telemetry requires GetPlayerFacing") end
  return fn()
end
function Native.Speed()
  local fn = globalFunction("GetUnitSpeed")
  if not fn then error("Crowley movement telemetry requires GetUnitSpeed") end
  return fn("player")
end
function Native.Combat()
  local fn = globalFunction("UnitAffectingCombat")
  if not fn then error("Crowley movement telemetry requires UnitAffectingCombat") end
  return fn("player")
end
function Native.DeadOrGhost()
  local fn = globalFunction("UnitIsDeadOrGhost")
  if not fn then error("Crowley movement telemetry requires UnitIsDeadOrGhost") end
  return fn("player")
end

function Native.CvarBool()
  local cvar = _G.C_CVar
  if type(cvar) ~= "table" or type(cvar.GetCVarBool) ~= "function" then return nil, "missing" end
  local ok, value = pcall(cvar.GetCVarBool, "autointeract")
  if not ok then return nil, "call_error" end
  if value == nil then return nil, "nil_result" end
  if type(value) ~= "boolean" then return nil, "wrong_type" end
  return value, ""
end

function Native.GroundPredicate(accessor, playerArgument)
  local fn = globalFunction(accessor)
  if not fn then return nil, "missing" end
  local ok, value
  if playerArgument then
    ok, value = pcall(fn, "player")
  else
    ok, value = pcall(fn)
  end
  if not ok then return nil, "call_error" end
  if value == nil then return nil, "nil_result" end
  if type(value) ~= "boolean" then return nil, "wrong_type" end
  return value, ""
end

function Native.TargetFacts()
  local existsFn, nameFn = globalFunction("UnitExists"), globalFunction("UnitName")
  if not existsFn or not nameFn then error("auctioneer_target_presence_unavailable") end
  local exists = existsFn("target")
  if type(exists) ~= "boolean" then error("auctioneer_target_presence_unavailable") end
  local name, rangeAvailable, inTradeRange = "", false, false
  if exists then
    name = nameFn("target")
    if type(name) ~= "string" or name == "" then error("auctioneer_target_name_unavailable") end
    local range = globalFunction("CheckInteractDistance")
    if range then
      local measured = range("target", 2)
      if type(measured) == "boolean" then rangeAvailable, inTradeRange = true, measured end
    end
  end
  local bindingAvailable, action = false, ""
  local binding = globalFunction("GetBindingAction")
  if binding then
    local measured = binding("CTRL-F12")
    if type(measured) == "string" then bindingAvailable, action = true, measured end
  end
  local interactionMode = "unavailable"
  local cvar = globalFunction("GetCVar")
  if cvar then
    local ok, measured = pcall(cvar, "SoftTargetInteract")
    if ok and type(measured) == "string" then
      local modes = { ["0"] = "disabled", ["1"] = "gamepad", ["2"] = "keyboard", ["3"] = "any" }
      interactionMode = modes[measured] or "unavailable"
    end
  end
  return {
    targetPresent = exists, targetName = name, rangeAvailable = rangeAvailable,
    withinTradeRange = inTradeRange, bindingAvailable = bindingAvailable,
    ctrlF12Action = action, interactionMode = interactionMode,
  }
end

function Native.BlockingUI()
  local popupAvailable, popupShown = false, false
  local popup = globalFunction("StaticPopup_IsAnyDialogShown")
  if popup then
    local ok, measured = pcall(popup)
    if ok and type(measured) == "boolean" then popupAvailable, popupShown = true, measured end
  end
  local menuAvailable, menuShown = false, false
  local menu = _G.GameMenuFrame
  if type(menu) == "table" and type(menu.IsShown) == "function" then
    local ok, measured = pcall(function() return menu:IsShown() end)
    if ok and type(measured) == "boolean" then menuAvailable, menuShown = true, measured end
  end
  return {
    staticPopupAvailable = popupAvailable, staticPopupShown = popupShown,
    gameMenuAvailable = menuAvailable, gameMenuShown = menuShown,
  }
end

function Native.BuildInfo()
  local fn = globalFunction("GetBuildInfo")
  if not fn then return nil end
  return fn()
end
function Native.On(event, handler) return ns.On(event, handler) end
function Native.Off(event, handler) return ns.Off(event, handler) end
function Native.Reload()
  local fn = globalFunction("ReloadUI")
  if not fn then return false end
  fn()
  return true
end
function Native.ReloadAvailable() return globalFunction("ReloadUI") ~= nil end

function Native.After(delay, callback)
  if type(C_Timer) ~= "table" or type(C_Timer.After) ~= "function" then error("Crowley requires C_Timer.After") end
  return C_Timer.After(delay, callback)
end

function Native.SerializeJSON(value)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.SerializeJSON == nil then
    error("Crowley requires C_EncodingUtil.SerializeJSON")
  end
  return C_EncodingUtil.SerializeJSON(value)
end

function Native.CompressString(value)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.CompressString == nil then
    error("Crowley requires C_EncodingUtil.CompressString")
  end
  if type(Enum) ~= "table" or type(Enum.CompressionMethod) ~= "table" or Enum.CompressionMethod.Deflate == nil then
    error("Crowley requires Enum.CompressionMethod.Deflate")
  end
  return C_EncodingUtil.CompressString(value, Enum.CompressionMethod.Deflate)
end

ns.RegisterModule("AgentNative", { requires = { "Core" }, onLoad = function() end })
