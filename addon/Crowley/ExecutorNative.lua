local _, ns = ...

-- Executor's native capability adapter.  Queue validation and execution state
-- do not name WoW namespaces; only this module resolves protected APIs,
-- frames, bag/mail/profession facts, and clock/unit facts.
local Native = {}
ns.ExecutorNative = Native

local function namespace(name)
  local value = _G[name]
  return type(value) == "table" and value or nil
end

local function functionFrom(value, name)
  return value and type(value[name]) == "function" and value[name] or nil
end

function Native.AuctionFunction(name) return functionFrom(namespace("C_AuctionHouse"), name) end
function Native.ContainerFunction(name) return functionFrom(namespace("C_Container"), name) end
function Native.TradeSkillFunction(name) return functionFrom(namespace("C_TradeSkillUI"), name) end
function Native.GlobalFunction(name) return type(_G[name]) == "function" and _G[name] or nil end
function Native.AuctionAction(name) return "C_AuctionHouse." .. name end
function Native.ContainerAction(name) return "C_Container." .. name end
function Native.TradeSkillAction(name) return "C_TradeSkillUI." .. name end
function Native.GlobalAction(name) return name end

function Native.TradeSkillReady()
  local fn = Native.TradeSkillFunction("IsTradeSkillReady")
  return fn and fn() == true or false
end

function Native.MailFrameShown()
  local frame = _G.MailFrame
  if type(frame) ~= "table" or type(frame.IsShown) ~= "function" then return false end
  local ok, shown = pcall(function() return frame:IsShown() end)
  return ok and shown == true
end

function Native.UnitName(unit) return UnitName(unit) end
function Native.Time() return time() end
function Native.Uptime() return GetTime() end
function Native.UIParent() return UIParent end
function Native.CreateButton(name, parent, template) return CreateFrame("Button", name, parent, template) end
function Native.On(event, handler) return ns.On(event, handler) end
function Native.Off(event, handler) return ns.Off(event, handler) end
function Native.ConfigureButton(button, parent, onClick)
  button:SetSize(260, 34)
  button:SetPoint("CENTER", parent, "CENTER", 0, 0)
  if button.SetMovable then button:SetMovable(true) end
  if button.EnableMouse then button:EnableMouse(true) end
  if button.RegisterForDrag then button:RegisterForDrag("LeftButton") end
  if button.SetScript then
    button:SetScript("OnDragStart", function(self) if self.StartMoving then self:StartMoving() end end)
    button:SetScript("OnDragStop", function(self) if self.StopMovingOrSizing then self:StopMovingOrSizing() end end)
    button:SetScript("OnClick", onClick)
  end
  button:Hide()
end
function Native.SetButtonText(button, text) button:SetText(text) end
function Native.SetButtonShown(button, visible)
  if visible then button:Show() else button:Hide() end
end
function Native.ButtonIsShown(button)
  if type(button.IsShown) ~= "function" then return false end
  local ok, shown = pcall(function() return button:IsShown() end)
  return ok and shown == true
end
function Native.CreateItemLocation(bag, slot)
  local locations = _G.ItemLocation
  if type(locations) ~= "table" or type(locations.CreateFromBagAndSlot) ~= "function" then return nil end
  return locations:CreateFromBagAndSlot(bag, slot)
end

ns.RegisterModule("ExecutorNative", { requires = { "Core" }, onLoad = function() end })
