local ADDON_NAME, ns = ...

ns.VERSION = "0.3.0"
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
  if type(ns.GeneratedWireSchemas) ~= "table" or type(ns.GeneratedWireSchemas.crowleyData) ~= "table" then
    error("generated CROWLEY_DATA wire schema is unavailable")
  end
  if not ns.Util.ExactKeys(CROWLEY_DATA, ns.GeneratedWireSchemas.crowleyData) then
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

ns.On("ADDON_LOADED", function(_, name)
  if name ~= ADDON_NAME then return end
  if CrowleyDB == nil then CrowleyDB = {} end
  rejectLegacyStorage()
  for k, v in pairs(defaults) do
    if CrowleyDB[k] == nil then CrowleyDB[k] = v end
  end
  ns.db = CrowleyDB
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
  ns.Scanner.Initialize()
  ns.ScanStatus.Initialize()
end)

ns.On("AUCTION_HOUSE_SHOW", function()
  local house = ns.Context.House()
  ns.Print(("AH open - scans will be saved to |cffffd200%s|r (%s). /crowley scan to start.")
    :format(house.key, house.source))
end)
