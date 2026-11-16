local _, ns = ...

local HELP = {
  "/crowley scan [force] - targeted watchlist sweep into the current house",
  "/crowley status - current house, next sweep, stored sweeps and data age",
  "/crowley set <region|ruleset|faction> <value|auto> - override house detection",
  "/crowley probe - record scanner API facts",
}

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

  local scanner = ns.Scanner.Status()
  ns.Print(("scanner: %s, ordinal %d/%d, pending %d, loaded acks %d"):format(
    scanner.phase, scanner.ordinal or 0, scanner.planned or 0, #(scanner.pending or {}), #(scanner.loaded or {})))

  if CROWLEY_DATA.generatedAt > 0 then
    ns.Print(("scan plan: %d min old"):format(math.floor((time() - CROWLEY_DATA.generatedAt) / 60)))
  else
    ns.Print("scan plan: none yet (start the companion, then /reload)")
  end
end

local function set(field, value)
  local valid = field and ns.Context.VALID[field]
  if not valid then return ns.Print("usage: /crowley set <region|ruleset|faction> <value|auto>") end
  value = value and value:lower() or ""
  local setting
  if value == "auto" then
    setting = nil
  elseif valid[value] then
    setting = value
  else
    local options = {}
    for key in pairs(valid) do options[#options + 1] = key end
    table.sort(options)
    return ns.Print(("%s must be one of: %s, auto"):format(field, table.concat(options, ", ")))
  end
  if ns.Scanner.InProgress() then ns.Scanner.Abort("house_changed") end
  ns.db.settings[field] = setting
  ns.Print(("house is now |cffffd200%s|r"):format(ns.Context.House().key))
end

SLASH_CROWLEY1 = "/crowley"
SlashCmdList.CROWLEY = function(msg)
  local args = {}
  for word in (msg or ""):gmatch("%S+") do args[#args + 1] = word end
  local cmd = (args[1] or ""):lower()

  if cmd == "scan" and #args <= 2 and (not args[2] or args[2] == "force") then
    ns.Scanner.Start(args[2] == "force")
  elseif cmd == "status" and #args == 1 then
    status()
  elseif cmd == "set" and #args == 3 then
    set(args[2] and args[2]:lower(), args[3])
  elseif cmd == "probe" and #args == 1 then
    ns.Probe()
  else
    for _, line in ipairs(HELP) do ns.Print(line) end
  end
end
