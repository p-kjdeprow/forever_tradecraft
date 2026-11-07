local _, ns = ...

local Commands = {}
ns.Commands = Commands

local HELP = {
  "/crowley scan [force] - targeted watchlist sweep into the current house",
  "/crowley status - current house, next sweep, stored sweeps, data age",
  "/crowley hud [hide|show|status] - toggle or inspect the telemetry display",
  "  The telemetry HUD is required for agent operation; hiding it is a display preference only.",
  "/crowley set <region|ruleset|faction> <value|auto> - override house detection",
  "/crowley probe - record which AH APIs this client has",
  "/crowley reference - capture item metadata and AH deposits",
  "/crowley account - capture and summarize account state",
  "/crowley queue - show the loaded trade queue and execution results",
  "/crowley arm <queueId> - arm the loaded queue for this session",
  "/crowley disarm - clear the session arm",
  "/crowley skip - skip the current trade action",
}
-- Reserved for the local repeating-session controller.  It is intentionally
-- absent from help and accepts no arguments; the controller must prove this
-- exact preparation command before issuing the normal /logout.
local SESSION_FENCE = "session"

local panel
local constructButton
local restoreButton

local function marker()
  return ns.db and ns.db.nativeConstruction
end

local function show(value)
  if not value then return end
  if value.Show then value:Show() end
end

local function hide(value)
  if not value then return end
  if value.Hide then value:Hide() end
end

local function setText(value, text)
  if value and value.SetText then value:SetText(text) end
end

local function setEnabled(value, enabled)
  if value and value.SetEnabled then value:SetEnabled(enabled == true) end
end

local function updatePanel()
  if not panel or not ns.Agent then return end
  local current = marker()
  local lifecycle = ns.Agent.Lifecycle()
  local visible = current ~= nil or lifecycle ~= "ordinary"
  local restoreInFlight = ns.Agent.RestorePending and ns.Agent.RestorePending() or false
  if visible then show(panel) else hide(panel) end
  local constructVisible = type(current) == "table" and (current.state == "armed" or current.state == "deferred") and
    not (ns.Agent.RecoveryMode and ns.Agent.RecoveryMode())
  if constructVisible then show(constructButton) else hide(constructButton) end
  if visible then show(restoreButton) else hide(restoreButton) end
  setEnabled(constructButton, constructVisible and not restoreInFlight)
  setEnabled(restoreButton, visible and not restoreInFlight)
end

local function ensurePanel()
  if panel then updatePanel() return panel end
  panel = CreateFrame("Frame", "CrowleyNativeConstructionPanel", UIParent)
  panel:SetSize(220, 84)
  panel:SetPoint("TOPRIGHT", UIParent, "TOPRIGHT", -24, -24)
  if panel.SetFrameStrata then panel:SetFrameStrata("DIALOG") end
  constructButton = CreateFrame("Button", "CrowleyNativeConstructionConstruct", panel, "UIPanelButtonTemplate")
  constructButton:SetSize(100, 28)
  constructButton:SetPoint("TOPLEFT", panel, "TOPLEFT", 8, -8)
  setText(constructButton, "Construct")
  constructButton:SetScript("OnClick", function()
    local current = marker()
    if type(current) ~= "table" or current.state ~= "deferred" then
      ns.Print("construct not released")
      return
    end
    local ok, reason = ns.Agent.Construct()
    if not ok then ns.Print("construct refused: " .. tostring(reason)) end
    updatePanel()
  end)
  restoreButton = CreateFrame("Button", "CrowleyNativeConstructionRestore", panel, "UIPanelButtonTemplate")
  restoreButton:SetSize(100, 28)
  restoreButton:SetPoint("TOPLEFT", panel, "TOPLEFT", 112, -8)
  setText(restoreButton, "Restore")
  restoreButton:SetScript("OnClick", function()
    local ok, reason = ns.Agent.Restore()
    if not ok then ns.Print("recovery not admitted") end
    updatePanel()
  end)
  updatePanel()
  return panel
end

function Commands.Panel() return panel end
function Commands.Refresh() updatePanel() end

local function deferConstruction()
  if ns.Agent.HudHidden() then return false, "agent_hud_hidden" end
  local current = marker()
  if ns.Agent.Lifecycle() ~= "ordinary" or not ns.Agent.Surface() then return false, "ordinary_boot_unavailable" end
  if current ~= nil then return false, "native_construction_already_set" end
  if ns.Scanner.InProgress() then return false, "scan_active" end
  if ns.Scanner.NativeConstructionFence and ns.Scanner.NativeConstructionFence() then return false, "native_construction_fence_active" end
  if type(ns.SetNativeMarker) ~= "function" then return false, "marker_validation_unavailable" end
  if type(ns.Scanner.SetNativeConstructionFence) ~= "function" then return false, "native_fence_unavailable" end
  local fence = ns.Scanner.PreLogoutFenceStatus()
  if fence.state ~= "inactive" then return false, "logout_fence_active" end
  local token = ns.Agent.Boot()
  if type(token) ~= "string" or token == "" then return false, "ordinary_boot_unavailable" end
  local ok, reason = ns.SetNativeMarker({ schema = 1, state = "armed", token = token, attempts = 0 })
  if not ok then return false, reason end
  ns.Scanner.SetNativeConstructionFence(true)
  ensurePanel()
  return true
end

local function admitRelease()
  local ok, reason = ns.Agent.AdmitRelease()
  if ok then ensurePanel() end
  return ok, reason
end

local function status()
  local house = ns.Context.House()
  ns.Print(("house: |cffffd200%s|r (%s)"):format(house.key, house.source))

  local stored = 0
  for _ in pairs(ns.db.scans) do stored = stored + 1 end
  local wait = ns.Scanner.CooldownRemaining()
  local plan = CROWLEY_DATA.scanPlan
  local planText = plan and plan.planHash and plan.planHash:sub(1, 12) or "none"
  ns.Print(("stored sweeps: %d, next sweep: %s%s, plan: %s, cursor: %s, last: %d/%d attempted, %d failed"):format(stored,
    wait > 0 and ("%dm%02ds"):format(math.floor(wait / 60), wait % 60) or "ready",
    ns.Scanner.InProgress() and " (scan running)" or "", planText, ns.db.cursorItemID or "start",
    ns.db.lastSweepAttempted or 0, ns.db.lastSweepPlanned or 0, ns.db.lastSweepFailures or ns.db.failureCount or 0))
  if ns.db.storageBlocked then ns.Print("storage blocked: upload acknowledged sweeps, then /reload again to resume") end
  ns.Print("sweeps upload from SavedVariables on /reload; loaded acknowledgements resume collection after a second /reload")

  if ns.Agent and ns.Scanner.Status then
    local agent = ns.Scanner.Status()
    ns.Print(("agent: %s, ordinal %d/%d, pending %d, loaded acks %d"):format(
      agent.phase, agent.ordinal or 0, agent.planned or 0, #(agent.pending or {}), #(agent.loaded or {})))
  end

  if CROWLEY_DATA.generatedAt > 0 then
    ns.Print(("strategy data: %d min old"):format(math.floor((time() - CROWLEY_DATA.generatedAt) / 60)))
  else
    ns.Print("strategy data: none yet (start the companion, then /reload)")
  end
end

local function set(field, value)
  local valid = field and ns.Context.VALID[field]
  if not valid then
    return ns.Print("usage: /crowley set <region|ruleset|faction> <value|auto>")
  end
  value = value and value:lower() or ""
  local setting
  if value == "auto" then
    setting = nil
  elseif valid[value] then
    setting = value
  else
    local options = {}
    for k in pairs(valid) do options[#options + 1] = k end
    table.sort(options)
    return ns.Print(("%s must be one of: %s, auto"):format(field, table.concat(options, ", ")))
  end
  if ns.Scanner.InProgress() then ns.Scanner.Abort("house_changed") end
  ns.db.settings[field] = setting
  ns.Print(("house is now |cffffd200%s|r"):format(ns.Context.House().key))
end

local HUD_NOTICE = "The telemetry HUD is required for agent operation; this display preference does not pause the controller or change scanner mode."

local function hudStatus()
  local state = ns.Agent.HudStatus()
  local visibility = state.hidden and "hidden" or (state.visible and "visible" or "unavailable")
  ns.Print(("telemetry HUD is %s. %s"):format(visibility, HUD_NOTICE))
end

local function hudCommand(action)
  if action == "status" then
    hudStatus()
    return
  end

  local ok, result
  if action == "toggle" then
    ok, result = ns.Agent.ToggleHud()
  else
    ok, result = ns.Agent.SetHudHidden(action == "hide")
  end
  if ok then
    local visibility = action == "hide" and "hidden" or action == "show" and "visible" or
      (ns.Agent.HudHidden() and "hidden" or "visible")
    ns.Print(("telemetry HUD %s. %s"):format(visibility, HUD_NOTICE))
  else
    ns.Print(("telemetry HUD %s refused: %s"):format(action, tostring(result)))
  end
end

SLASH_CROWLEY1 = "/crowley"
SlashCmdList.CROWLEY = function(msg)
  local args = {}
  for word in (msg or ""):gmatch("%S+") do args[#args + 1] = word end
  local cmd = (args[1] or ""):lower()

  if cmd == "scan" then
    ns.Scanner.Start(args[2] == "force")
  elseif cmd == SESSION_FENCE and args[2] == "fence" and not args[3] then
    local armed, reason = ns.Scanner.PreLogoutFence()
    if armed then
      ns.Print("session fence armed: automatic starts revoked")
    else
      ns.Print("session fence refused: " .. tostring(reason))
    end
  elseif cmd == "agent" and not args[3] and ns.Agent then
    local ok, reason
    if args[2] == "defer" then
      ok, reason = deferConstruction()
    elseif args[2] == "release" then
      ok, reason = admitRelease()
    elseif args[2] == "recover" then
      ok, reason = ns.Agent.BeginRecovery()
      if ok then updatePanel() end
    elseif args[2] == "finalize" then
      ok, reason = ns.Agent.Finalize()
    elseif args[2] == "hold" then
      ok, reason = ns.Agent.Hold()
    elseif args[2] == "measure" then
      ok, reason = ns.Agent.Measure()
    elseif args[2] == "resume" then
      ok, reason = ns.Agent.Resume()
    end
    if ok then
      ns.Print("agent " .. args[2] .. " accepted")
    elseif reason then
      ns.Print("agent " .. tostring(args[2]) .. " refused: " .. tostring(reason))
    else
      for _, line in ipairs(HELP) do ns.Print(line) end
    end
  elseif cmd == "hud" and ns.Agent then
    local action = args[2] and args[2]:lower()
    if #args == 1 then
      hudCommand("toggle")
    elseif #args == 2 and (action == "hide" or action == "show" or action == "status") then
      hudCommand(action)
    else
      ns.Print("usage: /crowley hud [hide|show|status]")
      ns.Print(HUD_NOTICE)
    end
  elseif cmd == "status" then
    status()
  elseif cmd == "set" then
    set(args[2] and args[2]:lower(), args[3])
  elseif cmd == "probe" then
    ns.Probe()
  elseif cmd == "reference" and not args[2] then
    local ok, reason = ns.Reference.Start()
    if not ok then ns.Print("reference refused: " .. tostring(reason)) end
  elseif cmd == "account" and not args[2] then
    ns.Print(ns.Account.Summary())
  elseif cmd == "queue" and not args[2] and ns.Executor then
    ns.Executor.QueueCommand()
  elseif cmd == "arm" and args[2] and not args[3] and ns.Executor then
    ns.Executor.Arm(args[2])
  elseif cmd == "disarm" and not args[2] and ns.Executor then
    ns.Executor.Disarm()
  elseif cmd == "skip" and not args[2] and ns.Executor then
    ns.Executor.Skip()
  else
    for _, line in ipairs(HELP) do ns.Print(line) end
  end
end

ns.RegisterModule("Commands", {
  requires = { "Core", "Agent" },
  onLoad = function() ensurePanel() end,
})
