local ADDON_NAME, ns = ...

ns.VERSION = "0.2.0"
ns.MAX_STORED_SCANS = 6

local defaults = {
  -- Manual overrides for the house a scan is attributed to. nil = auto-detect.
  settings = {},
  scans = {}, -- legacy entries are retained only for the flush transition
  cursorItemID = nil,
  cursorPlanHash = nil,
  failureCount = 0,
  lastSweepPlanned = 0,
  lastSweepAttempted = 0,
  lastSweepFailures = 0,
}

-- Native construction is decided once, by Core's first ADDON_LOADED handler.
-- Agent and Scanner consume this immutable per-load decision; malformed
-- SavedVariables are never repaired by a later handler.
local NATIVE_STATES = {
  armed = true, deferred = true, constructing = true, constructed = true,
  failed = true, restore_pending = true,
}

local function validToken(token, allowRecovery)
  if not ns.Util.IsString(token, 1, 64) then return false end
  if token:find("[%c]") then return false end
  if allowRecovery then return token:match("^recovery:[0-9a-f]+$") ~= nil and #token == 33 end
  return token:find("%s") == nil
end

local function validateNativeMarker(marker)
  if type(marker) ~= "table" or not ns.Util.ExactKeys(marker, { schema = true, state = true, token = true, attempts = true }) then
    return nil, "malformed"
  end
  if marker.schema ~= 1 or not ns.Util.IsInteger(marker.attempts, 0, 1) then
    return nil, "malformed"
  end
  local state = marker.state
  if not NATIVE_STATES[state] then return nil, "malformed" end
  if state == "restore_pending" then
    if marker.attempts ~= 0 or not validToken(marker.token, true) then return nil, "malformed" end
  elseif (state == "armed" or state == "deferred") and marker.attempts ~= 0 then
    return nil, "malformed"
  elseif (state == "constructing" or state == "constructed" or state == "failed") and marker.attempts ~= 1 then
    return nil, "malformed"
  elseif not validToken(marker.token, false) then
    return nil, "malformed"
  end
  return ns.Util.Copy(marker), nil
end

local function decideNativeConstruction()
  local marker = ns.db.nativeConstruction
  if marker == nil then
    ns.nativeDecision = { kind = "ordinary", state = "ordinary", marker = nil, reason = "" }
    return
  end
  local valid, reason = validateNativeMarker(marker)
  if not valid then
    ns.nativeDecision = { kind = "restore_only", state = "unknown", marker = ns.Util.Copy(marker), reason = reason }
    return
  end
  -- armed is the only pre-reload spelling. The next load owns the
  -- canonical deferred spelling while retaining the exact boot token.
  if valid.state == "armed" then
    valid.state = "deferred"
    ns.db.nativeConstruction = ns.Util.Copy(valid)
  end
  ns.nativeDecision = { kind = valid.state, state = valid.state, marker = valid, reason = "" }
end

function ns.NativeDecision()
  return ns.Util.Copy(ns.nativeDecision or { kind = "unknown", state = "unknown", marker = nil, reason = "not_loaded" })
end

function ns.NativeMarkerValid(marker)
  return validateNativeMarker(marker)
end

function ns.SetNativeMarker(marker)
  local valid, reason = validateNativeMarker(marker)
  if not valid then return false, reason end
  ns.db.nativeConstruction = ns.Util.Copy(valid)
  return true
end

function ns.Print(...)
  local parts = { ... }
  for i = 1, #parts do parts[i] = tostring(parts[i]) end
  DEFAULT_CHAT_FRAME:AddMessage("|cff4fc3f7Crowley|r: " .. table.concat(parts, " "))
end

-- Scans the companion has confirmed uploading are listed in CROWLEY_DATA.ackedScans;
-- drop them so SavedVariables does not grow without bound.
local function pruneScans(db)
  if type(CROWLEY_DATA) ~= "table" or not ns.Util.IsDenseUniqueByteSortedStrings(CROWLEY_DATA.ackedScans) then
    error("CROWLEY_DATA.ackedScans must be a dense array of unique byte-sorted non-empty strings")
  end
  local acked = {}
  for _, id in ipairs(CROWLEY_DATA.ackedScans) do acked[id] = true end

  local remaining = {}
  local dropped = 0
  for id, scan in pairs(db.scans or {}) do
    if acked[id] then
      dropped = dropped + 1
    else
      remaining[#remaining + 1] = scan
    end
  end

  table.sort(remaining, function(a, b) return a.finishedAt > b.finishedAt end)
  db.scans = {}
  for i = 1, #remaining do
    db.scans[remaining[i].id] = remaining[i]
  end
  db.storageBlocked = #remaining >= ns.MAX_STORED_SCANS

  if #remaining > ns.MAX_STORED_SCANS then
    ns.Print(("storage blocked: %d unacknowledged legacy scan(s); flush and reload before v2 collection")
      :format(#remaining - ns.MAX_STORED_SCANS))
  end
  if dropped > 0 then
    ns.Print(("cleared %d uploaded scan(s)"):format(dropped))
  end
end

function ns.PruneStorage()
  if not ns.db then return end
  pruneScans(ns.db)
end

local modules = {}
local moduleOrder = {}
local lifecycleStarted = false

function ns.RegisterModule(name, specification)
  if type(name) ~= "string" or name == "" then error("module name must be a non-empty string") end
  if type(specification) ~= "table" then error("module " .. name .. " registration must be a table") end
  if type(specification.requires) ~= "table" then error("module " .. name .. " requires must be an array") end
  if type(specification.onLoad) ~= "function" then error("module " .. name .. " onLoad must be a function") end
  if modules[name] then error("duplicate module registration: " .. name) end
  if lifecycleStarted then error("module registered after addon lifecycle started: " .. name) end
  modules[name] = { requires = specification.requires, onLoad = specification.onLoad }
  moduleOrder[#moduleOrder + 1] = name
end

local function loadModules()
  local states, ordered = {}, {}
  local function visit(name, path)
    local module = modules[name]
    if not module then error("unknown module dependency: " .. table.concat(path, " -> ") .. " -> " .. name) end
    if states[name] == 1 then error("module dependency cycle: " .. table.concat(path, " -> ") .. " -> " .. name) end
    if states[name] == 2 then return end
    states[name] = 1
    local nextPath = ns.Util.Copy(path)
    nextPath[#nextPath + 1] = name
    for _, requirement in ipairs(module.requires) do
      if type(requirement) ~= "string" or requirement == "" then
        error("module " .. name .. " has an invalid dependency")
      end
      visit(requirement, nextPath)
    end
    states[name] = 2
    ordered[#ordered + 1] = name
  end
  for _, name in ipairs(moduleOrder) do visit(name, {}) end
  for _, name in ipairs(ordered) do modules[name].onLoad() end
end

local function rejectLegacyStorage()
  if type(CrowleyDB) ~= "table" then
    error("CrowleyDB must be a table or nil before ADDON_LOADED")
  end
  if CrowleyDB.sweeps ~= nil then
    error("CrowleyDB contains legacy key sweeps; remove it before loading Crowley")
  end
  if CrowleyDB.ackedSweeps ~= nil then
    error("CrowleyDB contains legacy key ackedSweeps; remove it before loading Crowley")
  end
  if type(CROWLEY_DATA) ~= "table" then
    error("CROWLEY_DATA.ackedScans must be a dense array of unique byte-sorted non-empty strings")
  end
  if CROWLEY_DATA.ackedSweeps ~= nil then
    error("CROWLEY_DATA contains legacy key ackedSweeps; refresh Data.lua before loading Crowley")
  end
  if not ns.Util.ExactKeys(CROWLEY_DATA, {
    generatedAt = true,
    ackedScans = true,
    scanPlan = true,
    referenceItemIDs = true,
  }) then
    error("CROWLEY_DATA has unknown or missing keys; refresh Data.lua before loading Crowley")
  end
  if not ns.Util.IsDenseUniqueByteSortedStrings(CROWLEY_DATA.ackedScans) then
    error("CROWLEY_DATA.ackedScans must be a dense array of unique byte-sorted non-empty strings")
  end
end

ns.frame = CreateFrame("Frame")
ns.handlers = {}

function ns.On(event, fn)
  if not ns.handlers[event] then
    ns.handlers[event] = {}
    ns.frame:RegisterEvent(event)
  end
  table.insert(ns.handlers[event], fn)
end

function ns.Off(event, fn)
  local list = ns.handlers[event]
  for i = #list, 1, -1 do
    if list[i] == fn then table.remove(list, i) end
  end
  if #list == 0 then
    ns.handlers[event] = nil
    ns.frame:UnregisterEvent(event)
  end
end

ns.frame:SetScript("OnEvent", function(_, event, ...)
  -- Copy so handlers can unregister themselves mid-dispatch.
  local snapshot = { unpack(ns.handlers[event]) }
  for _, fn in ipairs(snapshot) do fn(event, ...) end
end)

ns.RegisterModule("Core", {
  requires = {},
  onLoad = function()
    ns.db = CrowleyDB
    decideNativeConstruction()
    ns.db.cursorItemID = ns.db.cursorItemID
    ns.db.cursorPlanHash = ns.db.cursorPlanHash
    ns.db.failureCount = ns.db.failureCount or 0
    ns.PruneStorage()
    local plan = CROWLEY_DATA.scanPlan
    if plan and plan.planHash then
      ns.Print(("plan loaded: %s (%d item(s))"):format(plan.planHash:sub(1, 12), #(plan.itemIDs or {})))
    else
      ns.Print("plan not loaded; collection is paused until the companion writes Data.lua")
    end
  end,
})

ns.On("ADDON_LOADED", function(_, name)
  if name ~= ADDON_NAME then return end
  if CrowleyDB == nil then CrowleyDB = {} end
  rejectLegacyStorage()
  for k, v in pairs(defaults) do
    if CrowleyDB[k] == nil then CrowleyDB[k] = v end
  end
  lifecycleStarted = true
  loadModules()
end)

ns.On("AUCTION_HOUSE_SHOW", function()
  local house = ns.Context.House()
  ns.Print(("AH open - scans will be saved to |cffffd200%s|r (%s). /crowley scan to start.")
    :format(house.key, house.source))
end)
