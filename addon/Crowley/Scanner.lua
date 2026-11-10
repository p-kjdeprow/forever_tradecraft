local _, ns = ...

-- Targeted retail-style queries only.  The scanner deliberately uses the documented
-- AuctionHouse ItemKey/result structures and never falls back to the old replication API.
local Scanner = {}
ns.Scanner = Scanner
local Native = ns.ScannerNative
local time = Native.Now
local GetTime = Native.Uptime

local TARGET = ns.GeneratedTarget
if type(TARGET) ~= "table" then error("generated launch target is not loaded") end
local CADENCE = TARGET.cadenceSeconds
local ITEM_TIMEOUT = 30
local OWNED_TIMEOUT = 30
local SWEEP_TIMEOUT = TARGET.sweepCapSeconds
local MAX_PAGES = 10
local MAX_SAFE = 9007199254740991
local UINT32_MODULUS = 4294967296
local UINT32_MAX = UINT32_MODULUS - 1
local WRAPPED_TIME_LEFT_MIN = UINT32_MODULUS - 300
local QUERY_TRACE_VERSION = 1
local QUERY_TRACE_CAP = 256
local CANONICAL_PLAN_HASH = TARGET.planHash
local CANONICAL_CLIENT_BUILD = TARGET.fullBuild
local CANONICAL_CLIENT_VERSION = TARGET.clientVersion
local CANONICAL_BUILD = TARGET.nativeBuild
local CANONICAL_INTERFACE = TARGET.interface
local CANONICAL_COUNT = TARGET.count
local CANONICAL_LEVELS = TARGET.initialPriceLevels
local CANONICAL_COLD_SLICES = TARGET.coldSlices
local CANONICAL_SCHEMA = TARGET.schema
local CANONICAL_ID = TARGET.id
local CANONICAL_REVISION = TARGET.revision
local CANONICAL_CATALOG_HASH = TARGET.catalogHash
-- One compact, source-catalog-derived representation: each entry is the positive
-- delta from the prior item ID and its canonical pool (0=core, 1=discovery, 2=cold).
local CANONICAL_PLAN_ENTRIES = TARGET.planEntries
local PRE_LOGOUT_FENCE_COMMAND = "/crowley session fence"
local SCHEDULE_TRACE_VERSION = 1
local SCHEDULE_TRACE_CAP = 64
local SCHEDULE_TRACE_STRING_LIMIT = 32
local state
local generation = 0
local scheduleToken = 0
local scheduleBlocked = false
local nativeConstructionFence = false
local scheduleFence = { state = "inactive", reason = "", command = PRE_LOGOUT_FENCE_COMMAND }
local internalSendDepth = 0
local keyFields

local function nativeDecision()
  if type(ns.NativeDecision) ~= "function" then return { kind = "unknown", state = "unknown", reason = "not_loaded" } end
  return ns.NativeDecision()
end

local function nativeBlocked()
  if ns.Agent and type(ns.Agent.Lifecycle) == "function" then
    local lifecycle = ns.Agent.Lifecycle()
    if lifecycle == "ordinary" or lifecycle == "finalized" then return false end
  end
  local decision = nativeDecision()
  return decision.kind ~= "ordinary" and decision.kind ~= "finalized"
end

local function effectiveFence()
  if scheduleBlocked then return { state = scheduleFence.state, reason = scheduleFence.reason, command = scheduleFence.command } end
  if nativeConstructionFence then
    -- Native construction is an internal admission fence.  Keep the public
    -- wire projection on the existing armed/empty-reason contract; the
    -- scanner entry points below still refuse while this private bit is set.
    return { state = "armed", reason = "", command = PRE_LOGOUT_FENCE_COMMAND }
  end
  return { state = scheduleFence.state, reason = scheduleFence.reason, command = scheduleFence.command }
end

function Scanner.SetNativeConstructionFence(armed)
  nativeConstructionFence = armed == true
  if not nativeConstructionFence and not scheduleBlocked and ns.ahOpen and not state then
    Scanner.Schedule("native_construction_release")
  end
end

function Scanner.NativeConstructionFence()
  return nativeConstructionFence
end

function Scanner.LogoutFenceActive()
  return scheduleBlocked
end

local function scheduleFinite(value)
  if not ns.Util.IsFiniteNumber(value, nil, nil) then return 0 end
  return value
end

local function scheduleInteger(value)
  value = scheduleFinite(value)
  if value < 0 then return 0 end
  return math.floor(value)
end

local function scheduleString(value)
  if not ns.Util.IsString(value, 0, nil) then return "" end
  return value:sub(1, SCHEDULE_TRACE_STRING_LIMIT)
end

local function ensureScheduleTrace()
  if not ns.db then return nil end
  local trace = ns.db.scheduleTrace
  if type(trace) ~= "table" then trace = {} ns.db.scheduleTrace = trace end
  trace.version = SCHEDULE_TRACE_VERSION
  trace.cap = SCHEDULE_TRACE_CAP
  trace.events = type(trace.events) == "table" and trace.events or {}
  local retained = {}
  local first = math.max(1, #trace.events - SCHEDULE_TRACE_CAP + 1)
  for index = first, #trace.events do
    if type(trace.events[index]) == "table" then retained[#retained + 1] = trace.events[index] end
  end
  trace.events = retained
  trace.sequence = scheduleInteger(trace.sequence)
  trace.overflow = scheduleInteger(trace.overflow)
  trace.loadSequence = scheduleInteger(trace.loadSequence)
  trace.load = scheduleString(trace.load)
  return trace
end

local function beginScheduleLoad()
  local trace = ensureScheduleTrace()
  if not trace then return end
  trace.loadSequence = trace.loadSequence + 1
  trace.load = scheduleString(("load-%d-%d"):format(scheduleInteger(time()), trace.loadSequence))
end

local function scheduleEvent(eventType, fields)
  local trace = ensureScheduleTrace()
  if not trace or trace.load == "" then return end
  trace.sequence = trace.sequence + 1
  local event = {
    sequence = trace.sequence,
    type = scheduleString(eventType),
    wall = scheduleFinite(time()),
    uptime = scheduleFinite(GetTime()),
    load = trace.load,
    token = scheduleInteger(fields and fields.token or scheduleToken),
    currentToken = scheduleInteger(fields and fields.currentToken or scheduleToken),
    reason = scheduleString(fields and fields.reason),
    source = scheduleString(fields and fields.source),
    due = scheduleFinite(fields and fields.due),
    delay = scheduleFinite(fields and fields.delay),
  }
  if #trace.events >= SCHEDULE_TRACE_CAP then
    table.remove(trace.events, 1)
    trace.overflow = trace.overflow + 1
  end
  trace.events[#trace.events + 1] = event
end

local function traceIdentity(key)
  local itemID, itemLevel, itemSuffix, battlePetSpeciesID = keyFields(key)
  if not itemID then return nil end
  return { itemID = itemID, itemLevel = itemLevel, itemSuffix = itemSuffix, battlePetSpeciesID = battlePetSpeciesID }
end

local function traceEvent(eventType, s, fields)
  local trace = state and state.queryTrace
  if not trace then return end
  trace.sequence = trace.sequence + 1
  if #trace.events >= trace.cap then
    trace.dropped = trace.dropped + 1
    return
  end
  local event = { sequence = trace.sequence, type = eventType, at = time(), generation = state.generation }
  if s then
    local owned = s.ownedSentRequested ~= nil
    event.itemID, event.attempt = s.itemID, s.pages or 0
    event.kind = s.kind or "unknown"
    event.identity = traceIdentity(s.itemKey)
    event.barriers = {
      sentRequested = owned and s.ownedSentRequested == true or s.sentRequested == true,
      sent = owned and s.ownedSentBarrier == true or s.sentBarrier == true,
      response = owned and s.ownedResponseBarrier == true or s.responseBarrier == true,
      awaiting = owned and s.ownedAwaiting == true or s.awaiting == true,
    }
  end
  for key, value in pairs(fields or {}) do event[key] = value end
  trace.events[#trace.events + 1] = event
end

local function traceNativeCall(name, fn, ...)
  if type(fn) ~= "function" then return { status = "unsupported" } end
  local ok, value = pcall(fn, ...)
  if not ok then return { status = "error" } end
  local result = { status = "ok" }
  if type(value) == "boolean" or type(value) == "number" then result.value = value
  else result.valueType = type(value) end
  return result
end

local function traceReadiness(s, phase)
  if not s then return end
  local key = s.itemKey
  local readiness = {
    hasSearchResults = Native.HasSearchResultsAvailable() and
      traceNativeCall("HasSearchResults", Native.HasSearchResults, key) or { status = "unsupported" },
  }
  if s.kind == "commodity" then
    readiness.hasFull = traceNativeCall("HasFullCommoditySearchResults", Native.HasFullCommodity, s.itemID)
    readiness.quantity = traceNativeCall("GetCommoditySearchResultsQuantity", Native.GetCommodityQuantity, s.itemID)
    readiness.count = traceNativeCall("GetNumCommoditySearchResults", Native.GetCommodityCount, s.itemID)
  elseif s.kind == "item" then
    readiness.hasFull = traceNativeCall("HasFullItemSearchResults", Native.HasFullItem, key)
    readiness.quantity = traceNativeCall("GetItemSearchResultsQuantity", Native.GetItemQuantity, key)
    readiness.count = traceNativeCall("GetNumItemSearchResults", Native.GetItemCount, key)
  end
  traceEvent("readiness", s, { phase = phase, readiness = readiness })
end

local function markForeignSearch()
  if internalSendDepth > 0 or not state then return end
  traceEvent("foreign_search", state.item, { source = "hooksecurefunc" })
  if state.item then state.item.reasonCode = "identity_mismatch" end
end

local function markForeignOwned()
  if internalSendDepth > 0 or not state or not state.ownedHandler then return end
  traceEvent("foreign_owned", state, { source = "hooksecurefunc" })
  state.ownedExternal = true
end

Native.InstallHooks(markForeignSearch, markForeignOwned)

ns.ahOpen = false
Native.On("AUCTION_HOUSE_SHOW", function() ns.ahOpen = true Scanner.Schedule("ah_show") end)
Native.On("AUCTION_HOUSE_CLOSED", function()
  ns.ahOpen = false
  local previousToken = scheduleToken
  scheduleToken = scheduleToken + 1
  scheduleEvent("cancel", { reason = "ah_closed", source = "event", token = previousToken, currentToken = scheduleToken })
  if state then Scanner.Abort("ah_closed") end
end)
Native.On("PLAYER_LOGOUT", function()
  -- PLAYER_LOGOUT is the final client-owned scheduling fence.  In-flight
  -- queries are marked as logout-tailed, and a cooldown timer that was
  -- created by the completed sweep cannot start a new sweep while the client
  -- is leaving the world.
  local previousToken = scheduleToken
  scheduleToken = scheduleToken + 1
  scheduleEvent("cancel", { reason = "logout", source = "event", token = previousToken, currentToken = scheduleToken })
  ns.ahOpen = false
  if state then Scanner.Abort("logout") end
end)
ns.RegisterModule("Scanner", {
  requires = { "Core", "ScannerState", "ScanEncoder", "ScannerNative", "GeneratedWireSchemas" },
  onLoad = function()
    beginScheduleLoad()
    local decision = nativeDecision()
    nativeConstructionFence = decision.kind ~= "ordinary" and decision.kind ~= "finalized"
    ns.db.agentLastSaved = ns.db.agentLastSaved or {
      memoryUid = "", finishedAt = 0, planned = 0, attempted = 0, failures = 0, rows = 0,
    }
  end,
})

local function safeProduct(a, b)
  if type(a) ~= "number" or type(b) ~= "number" or a < 0 or b < 0 or a > MAX_SAFE or b > MAX_SAFE then return nil end
  if a ~= 0 and b > math.floor(MAX_SAFE / a) then return nil end
  return a * b
end

local canonicalPlan

local function canonicalPlanDefinition()
  if canonicalPlan then return canonicalPlan end
  local ids, core, discovery, cold = {}, {}, {}, {}
  local previous = 0
  for token in string.gmatch(CANONICAL_PLAN_ENTRIES, "[^,]+") do
    local deltaText, pool = string.match(token, "^(%d+):([012])$")
    local delta = tonumber(deltaText)
    if not delta or delta <= 0 then return nil end
    previous = previous + delta
    ids[#ids + 1] = previous
    if pool == "0" then core[#core + 1] = previous
    elseif pool == "1" then discovery[#discovery + 1] = previous
    else cold[#cold + 1] = previous end
  end
  if #ids ~= CANONICAL_COUNT then return nil end
  if (CANONICAL_COLD_SLICES > 0) ~= (#cold > 0) then return nil end
  canonicalPlan = { itemIDs = ids, pools = { core = core, discovery = discovery, cold = cold } }
  return canonicalPlan
end

local canonicalPlanText

local function canonicalNumberArray(values)
  local parts = {}
  for index, value in ipairs(values) do parts[index] = tostring(value) end
  return "[" .. table.concat(parts, ",") .. "]"
end

local function canonicalPlanJSON()
  if canonicalPlanText then return canonicalPlanText end
  local definition = canonicalPlanDefinition()
  if not definition then return nil end
  canonicalPlanText = '{"cadenceSeconds":' .. CADENCE .. ',"catalogHash":"' .. CANONICAL_CATALOG_HASH ..
    '","coldSlices":' .. CANONICAL_COLD_SLICES .. ',"id":"' .. CANONICAL_ID .. '","initialPriceLevels":' .. CANONICAL_LEVELS .. ',"itemIDs":' ..
    canonicalNumberArray(definition.itemIDs) .. ',"pools":{"cold":' ..
    canonicalNumberArray(definition.pools.cold) .. ',"core":' ..
    canonicalNumberArray(definition.pools.core) .. ',"discovery":' ..
    canonicalNumberArray(definition.pools.discovery) .. '},"revision":' .. CANONICAL_REVISION .. ',"schemaVersion":' .. CANONICAL_SCHEMA .. ',"targetClientBuild":"' ..
    CANONICAL_CLIENT_BUILD .. '"}'
  return canonicalPlanText
end

local function sameArray(left, right)
  if type(left) ~= "table" or type(right) ~= "table" or #left ~= #right then return false end
  for index = 1, #left do
    if left[index] ~= right[index] then return false end
  end
  return true
end

local function planMatchesCanonical(source, loaded)
  local definition = canonicalPlanDefinition()
  if not definition or type(source) ~= "table" or type(loaded) ~= "table" then return false end
  if not ns.Util.ExactKeys(loaded, {
    schemaVersion = true, id = true, revision = true, catalogHash = true,
    targetClientBuild = true, cadenceSeconds = true, initialPriceLevels = true,
    itemIDs = true, pools = true, coldSlices = true,
  }) then return false end
  if loaded.schemaVersion ~= CANONICAL_SCHEMA or loaded.id ~= CANONICAL_ID or
      loaded.revision ~= CANONICAL_REVISION or loaded.catalogHash ~= CANONICAL_CATALOG_HASH or
      loaded.targetClientBuild ~= CANONICAL_CLIENT_BUILD or loaded.cadenceSeconds ~= CADENCE or
      loaded.initialPriceLevels ~= CANONICAL_LEVELS or loaded.coldSlices ~= CANONICAL_COLD_SLICES or
      not sameArray(loaded.itemIDs, definition.itemIDs) then return false end
  if not ns.Util.ExactKeys(loaded.pools, { core = true, discovery = true, cold = true }) or
      not sameArray(loaded.pools.core, definition.pools.core) or
      not sameArray(loaded.pools.cold, definition.pools.cold) or
      not sameArray(loaded.pools.discovery, definition.pools.discovery) then return false end
  if source.planHash ~= CANONICAL_PLAN_HASH or source.schemaVersion ~= loaded.schemaVersion or
      source.id ~= loaded.id or source.revision ~= loaded.revision or
      source.catalogHash ~= loaded.catalogHash or source.targetClientBuild ~= loaded.targetClientBuild or
      source.cadenceSeconds ~= loaded.cadenceSeconds or source.initialPriceLevels ~= loaded.initialPriceLevels or
      source.coldSlices ~= loaded.coldSlices or not sameArray(source.itemIDs, loaded.itemIDs) then return false end
  if not ns.Util.ExactKeys(source.pools, { core = true, discovery = true, cold = true }) or
      not sameArray(source.pools.core, loaded.pools.core) or
      not sameArray(source.pools.cold, loaded.pools.cold) or
      not sameArray(source.pools.discovery, loaded.pools.discovery) then return false end
  if (source.sweepCapSeconds == nil) ~= (loaded.sweepCapSeconds == nil) or
      (source.sweepCapSeconds ~= nil and source.sweepCapSeconds ~= loaded.sweepCapSeconds) or
      (loaded.sweepCapSeconds ~= nil and loaded.sweepCapSeconds ~= SWEEP_TIMEOUT) then return false end
  return true
end

local function runtimeMatchesCanonical()
  local version, build, _, interface = Native.BuildInfo()
  return type(version) == "string" and type(build) == "string" and type(interface) == "number" and
    version == CANONICAL_CLIENT_VERSION and build == CANONICAL_BUILD and interface == CANONICAL_INTERFACE
end

local function loadPlan()
  local source = CROWLEY_DATA and CROWLEY_DATA.scanPlan
  if type(source) ~= "table" or type(source.planJSON) ~= "string" or source.planJSON == "" then
    return nil, "no scan plan loaded"
  end
  if source.planJSON ~= canonicalPlanJSON() then return nil, "plan_invalid" end
  local ok, loaded = ns.ScanEncoder.Decode(source.planJSON, Native.DeserializeJSON)
  if not ok or type(loaded) ~= "table" then return nil, "plan_invalid" end
  if not planMatchesCanonical(source, loaded) then return nil, "plan_invalid" end
  if not runtimeMatchesCanonical() then return loaded, "client_invalid" end
  return loaded
end

local function compare(a, b)
  local left = safeProduct(a.numerator, b.denominator)
  local right = safeProduct(b.numerator, a.denominator)
  if not left or not right then return nil end
  if left < right then return -1 elseif left > right then return 1 end
  return 0
end

local function gcd(a, b)
  while b ~= 0 do a, b = b, a % b end
  return a
end

local function rational(numerator, denominator)
  if type(numerator) ~= "number" or type(denominator) ~= "number" or numerator < 0 or denominator <= 0 or
      numerator > MAX_SAFE or denominator > MAX_SAFE or numerator ~= math.floor(numerator) or denominator ~= math.floor(denominator) then return nil end
  local divisor = gcd(numerator, denominator)
  return { numerator = numerator / divisor, denominator = denominator / divisor }
end

keyFields = function(key)
  if type(key) ~= "table" then return nil end
  local itemID, itemLevel, itemSuffix, pet = key.itemID, key.itemLevel, key.itemSuffix, key.battlePetSpeciesID
  if type(itemID) ~= "number" or type(itemLevel) ~= "number" or type(itemSuffix) ~= "number" or type(pet) ~= "number" then return nil end
  if itemID <= 0 or itemID > MAX_SAFE or itemLevel < 0 or itemLevel > MAX_SAFE or itemSuffix < 0 or itemSuffix > MAX_SAFE or pet < 0 or pet > MAX_SAFE or
      itemID ~= math.floor(itemID) or itemLevel ~= math.floor(itemLevel) or itemSuffix ~= math.floor(itemSuffix) or pet ~= math.floor(pet) then return nil end
  return itemID, itemLevel, itemSuffix, pet
end

local function classifyItem(itemID)
  local ok, _, _, _, _, _, classID = pcall(Native.ItemClassInfo, itemID)
  if not ok then return nil end
  if type(classID) ~= "number" or classID < 0 or classID > MAX_SAFE or classID ~= math.floor(classID) then return nil end
  local container, weapon, armor, recipe, profession, tradegoods = Native.ItemClass("Container"), Native.ItemClass("Weapon"),
    Native.ItemClass("Armor"), Native.ItemClass("Recipe"), Native.ItemClass("Profession"), Native.ItemClass("Tradegoods")
  if not container or not weapon or not armor or not recipe or not profession or not tradegoods then return nil end
  local classes = { Container = container, Weapon = weapon, Armor = armor, Recipe = recipe, Profession = profession, Tradegoods = tradegoods }
  if type(classes.Weapon) ~= "number" or type(classes.Armor) ~= "number" or type(classes.Profession) ~= "number" then return nil end
  return classID, classID == classes.Weapon or classID == classes.Armor or classID == classes.Profession
end

local function keysEqual(left, right)
  local li, ll, ls, lp = keyFields(left)
  local ri, rl, rs, rp = keyFields(right)
  return li and ri and li == ri and ll == rl and ls == rs and lp == rp
end

local function ownerObject(info, commodity)
  local names = {}
  local listed = info.owners
  if type(listed) ~= "table" then return nil end
  for _, owner in ipairs(listed) do
    if type(owner) ~= "string" or owner == "" then return nil end
    names[owner] = true
  end
  local count = info.totalNumberOfOwners
  if type(count) ~= "number" or count < 0 or count > MAX_SAFE or count ~= math.floor(count) or count < #listed then return nil end
  local containsOwner = info.containsOwnerItem
  local containsAccount = info.containsAccountItem
  if type(containsOwner) ~= "boolean" or type(containsAccount) ~= "boolean" then return nil end
  local grouped = commodity or count > 1 or #listed > 1 or containsOwner or containsAccount
  local identity = grouped and "grouped" or (#listed == 1 and count == 1 and "individual" or "unknown")
  return { names = names, totalNumberOfOwners = count, containsOwnerItem = containsOwner,
    containsAccountItem = containsAccount, identityKind = identity }
end

local function timeLeft(info)
  if type(info.timeLeftSeconds) == "number" then
    local seconds = info.timeLeftSeconds
    if seconds < 0 or seconds > MAX_SAFE or seconds ~= math.floor(seconds) then return nil end
    if seconds >= WRAPPED_TIME_LEFT_MIN and seconds <= UINT32_MAX then seconds = seconds - UINT32_MODULUS
    elseif seconds > 172800 then return nil end
    return { kind = "exact", seconds = seconds }
  end
  if info.timeLeft ~= nil then
    local minimum, maximum = Native.TimeLeftBandInfo(info.timeLeft)
    if type(info.timeLeft) == "number" and info.timeLeft >= 0 and info.timeLeft <= 3 and minimum and maximum and
        minimum >= 0 and maximum >= minimum and minimum <= MAX_SAFE and maximum <= MAX_SAFE and
        minimum == math.floor(minimum) and maximum == math.floor(maximum) then
      return { kind = "band", band = info.timeLeft, minimumSeconds = minimum, maximumSeconds = maximum }
    end
  end
  return { kind = "unknown" }
end

-- Parse only the identity fields carried in an item link.  Bonus IDs are not exposed by
-- ItemSearchResultInfo; they are therefore taken from the documented link when present.
local function linkIdentity(link)
  if type(link) ~= "string" then return nil end
  local text = link:match("item:([%-%d:]+)")
  if not text then return nil end
  local fields = {}
  for field in (text .. ":"):gmatch("([^:]*):") do fields[#fields + 1] = field end
  local itemID = tonumber(fields[1])
  local suffix = tonumber(fields[7]) or 0
  local number = tonumber(fields[13]) or 0
  if not itemID or not suffix or not number or itemID <= 0 or itemID > MAX_SAFE or suffix < 0 or suffix > MAX_SAFE or number < 0 or number > 100 or
      itemID ~= math.floor(itemID) or suffix ~= math.floor(suffix) or number ~= math.floor(number) then return nil end
  local bonuses = {}
  for i = 14, 13 + number do
    local bonus = tonumber(fields[i])
    if not bonus or bonus <= 0 or bonus > MAX_SAFE or bonus ~= math.floor(bonus) then return nil end
    bonuses[tostring(i - 14)] = bonus
  end
  return itemID, suffix, bonuses
end

local function rawKey(itemKey, itemID, link)
  local keyID, level, suffix, pet = keyFields(itemKey)
  if not keyID or keyID ~= itemID then return nil end
  local linkedID, linkedSuffix, bonuses = linkIdentity(link)
  if linkedID and (linkedID ~= itemID or linkedSuffix ~= suffix) then return nil end
  return { itemID = itemID, itemLevel = level, itemSuffix = suffix, battlePetSpeciesID = pet, bonusIDs = bonuses or {} }
end

function Scanner.ItemKey(link, raw)
  if type(raw) ~= "table" then return nil end
  local itemID, level, suffix, pet = raw.itemID, raw.itemLevel, raw.itemSuffix, raw.battlePetSpeciesID
  if type(itemID) ~= "number" or type(level) ~= "number" or type(suffix) ~= "number" or type(pet) ~= "number" then return nil end
  local linkedID, linkedSuffix, linkedBonuses = linkIdentity(link)
  if not linkedID or linkedID ~= itemID or linkedSuffix ~= suffix then return nil end
  local parts = { tostring(itemID) }
  if suffix ~= 0 then parts[#parts + 1] = "s" .. suffix end
  local bonusValues = {}
  for _, bonus in pairs(raw.bonusIDs or {}) do bonusValues[#bonusValues + 1] = bonus end
  table.sort(bonusValues)
  if #bonusValues > 0 then parts[#parts + 1] = "b" .. table.concat(bonusValues, ".") end
  if level ~= 0 then parts[#parts + 1] = "l" .. level end
  if pet ~= 0 then parts[#parts + 1] = "p" .. pet end
  -- The parser and client use the same canonical form; reject a mismatching link instead
  -- of silently collapsing a variant.  An empty bonus table is valid for ordinary items.
  local linkedValues = {}
  for _, bonus in pairs(linkedBonuses or {}) do linkedValues[#linkedValues + 1] = bonus end
  table.sort(linkedValues)
  if #linkedValues ~= #bonusValues then return nil end
  for i = 1, #linkedValues do if linkedValues[i] ~= bonusValues[i] then return nil end end
  return table.concat(parts, ":")
end

local function rowMap(rows)
  local result = {}
  for i, row in ipairs(rows) do result["row:" .. i] = row end
  return result
end

local function unqueried(itemID, ordinal, reason)
  return { itemID = itemID, ordinal = ordinal, kind = "unknown", query = { kind = "unqueried" },
    coverage = "unqueried", sourceComplete = false, cutoff = { kind = "unknown" },
    higherPriceWitness = { kind = "unknown" }, reasonCode = reason or "not_started", rowCount = 0, rows = {} }
end

local function disconnectQueryEvents(s)
  if not s or not s.handlers then return end
  for event, handler in pairs(s.handlers) do Native.Off(event, handler) end
  s.handlers = {}
end

local function sessionAlive(token, owner)
  if not state or state.generation ~= token then return false end
  if owner == state then return true end
  return state.item == owner
end

local function reduceState(previous, event)
  local nextState = ns.ScannerState.Reduce(previous, event)
  if ns.db and nextState.queryTrace then ns.db.activeQueryTrace = nextState.queryTrace end
  return nextState
end

local function sendMessage(owner, fn)
  if type(Native.IsThrottledReady) ~= "function" then error("Crowley requires IsThrottledMessageSystemReady") end
  ns.db.querySendTimes = ns.db.querySendTimes or {}
  local now = time()
  local kept = {}
  for _, sent in ipairs(ns.db.querySendTimes) do if now - sent < 60 then kept[#kept + 1] = sent end end
  ns.db.querySendTimes = kept
  local token = state and state.generation
  if #kept >= 100 or not Native.IsThrottledReady() then
    Native.After(1, function() if sessionAlive(token, owner) then sendMessage(owner, fn) end end)
    return false
  end
  ns.db.querySendTimes[#ns.db.querySendTimes + 1] = now
  fn()
  return true
end

local function markTail(reason)
  if not state then return end
  for i = state.nextOrdinal, #state.order do
    local itemID = state.order[i]
    if not state.observations[tostring(itemID)] then state.observations[tostring(itemID)] = unqueried(itemID, i, reason) end
  end
end

local function normalizeRows(s, reason)
  local unsafe = false
  local unexpected = false
  for left = 1, #s.rows do
    for right = left + 1, #s.rows do
      local relation = compare(s.rows[left].unitPrice, s.rows[right].unitPrice)
      if not relation then
        unsafe = true
      elseif s.kind == "commodity" and relation > 0 then
        unexpected = true
      end
    end
  end
  if unsafe then return "numeric_range" end
  if unexpected and not reason then reason = "unexpected_order" end
  table.sort(s.rows, function(a, b)
    local relation = compare(a.unitPrice, b.unitPrice)
    return relation and relation < 0 or false
  end)
  return reason
end

local function failCurrent(reason)
  if not state or not state.item then return end
  local s = state.item
  disconnectQueryEvents(s)
  reason = normalizeRows(s, reason)
  local finished = time()
  local observation = {
    itemID = s.itemID, ordinal = s.ordinal, kind = s.kind or "unknown",
    query = { kind = "queried", startedAt = s.queryStarted, finishedAt = finished }, coverage = "incomplete",
    sourceComplete = false, cutoff = { kind = "unknown" }, higherPriceWitness = { kind = "unknown" },
    reasonCode = reason, rowCount = #s.rows, rows = rowMap(s.rows),
  }
  state = reduceState(state, {
    type = "item_completed", itemID = s.itemID, observation = observation,
    cursor = s.itemID, nextOrdinal = s.ordinal + 1,
  })
  ns.db.failureCount = (ns.db.failureCount or 0) + 1
  traceEvent("query_outcome", s, { outcome = "incomplete", reason = reason, rowCount = #s.rows })
end

local function sourceComplete(s)
  if s.kind == "commodity" then return Native.HasFullCommodity(s.itemID) end
  return Native.HasFullItem(s.itemKey)
end

local function finishItem(s)
  if not state or state.item ~= s then return end
  disconnectQueryEvents(s)
  local reason = normalizeRows(s, s.reasonCode)
  local complete = false
  if not reason then complete = sourceComplete(s) end
  local coverageState = ns.ScannerState.Coverage(s.rows, s.planLevels, complete, reason)
  local coverage, source = coverageState.coverage, coverageState.sourceComplete
  local cutoff, witness = coverageState.cutoff, coverageState.higherPriceWitness
  if coverage == "bounded" then
    local retained = {}
    for _, row in ipairs(s.rows) do if compare(row.unitPrice, cutoff) <= 0 then retained[#retained + 1] = row end end
    s.rows = retained
  end
  if s.kind == "item" and coverage == "bounded" and not complete then coverage, source, cutoff, witness = "incomplete", false, { kind = "unknown" }, { kind = "unknown" } end
  local cutoffWire = cutoff and cutoff.numerator and { kind = "price", numerator = cutoff.numerator, denominator = cutoff.denominator } or
    (coverage == "incomplete" and { kind = "unknown" } or { kind = "unbounded" })
  local witnessWire = witness and witness.numerator and { kind = "known", numerator = witness.numerator, denominator = witness.denominator } or { kind = "unknown" }
  local observation = {
    itemID = s.itemID, ordinal = s.ordinal, kind = s.kind,
    query = { kind = "queried", startedAt = s.queryStarted, finishedAt = time() }, coverage = coverage,
    sourceComplete = source, cutoff = cutoffWire, higherPriceWitness = witnessWire, reasonCode = reason or "ok",
    rowCount = #s.rows, rows = rowMap(s.rows),
  }
  state = reduceState(state, {
    type = "item_completed", itemID = s.itemID, observation = observation,
    cursor = s.itemID, nextOrdinal = s.ordinal + 1,
  })
  traceEvent("query_outcome", s, { outcome = coverage, reason = reason or "ok", rowCount = #s.rows })
  Scanner.Next()
end

local function resultInfo(s, index)
  local info
  if s.kind == "commodity" then info = Native.GetCommodityResult(s.itemID, index)
  else info = Native.GetItemResult(s.itemKey, index) end
  if not info then return nil, "missing_result" end
  local link = info.itemLink
  local rowItemID, key, raw
  if s.kind == "commodity" then
    rowItemID = info.itemID
    if type(rowItemID) ~= "number" or rowItemID ~= s.itemID then return nil, "identity_mismatch" end
    key = tostring(rowItemID)
    raw = { itemID = rowItemID, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0, bonusIDs = {} }
  else
    local fields = info.itemKey and keyFields(info.itemKey)
    if not fields then return nil, "identity_mismatch" end
    rowItemID = fields
    if rowItemID ~= s.itemID or not info.itemLink then return nil, "missing_item_link" end
    raw = rawKey(info.itemKey, rowItemID, info.itemLink)
    key = raw and Scanner.ItemKey(info.itemLink, raw)
    if not key then return nil, "identity_mismatch" end
  end
  local resultID = info.auctionID
  local quantity = info.quantity
  local copper = s.kind == "commodity" and info.unitPrice or info.buyoutAmount
  if type(resultID) ~= "number" or resultID <= 0 or resultID > MAX_SAFE or resultID ~= math.floor(resultID) or
      type(quantity) ~= "number" or quantity <= 0 or quantity ~= math.floor(quantity) or
      type(copper) ~= "number" or copper <= 0 or copper ~= math.floor(copper) or copper > MAX_SAFE or quantity > MAX_SAFE then
    -- A bid-only item is outside the eligible buyout source and is ignored.  Other
    -- malformed source values are surfaced to the observation as incomplete.
    if s.kind == "item" and (info.buyoutAmount == nil or info.buyoutAmount == 0) then return nil, "eligible_skip" end
    return nil, "numeric_range"
  end
  local unit = rational(copper, 1)
  if not unit then return nil, "numeric_range" end
  local owners = ownerObject(info, s.kind == "commodity")
  if not owners then return nil, "identity_mismatch" end
  local left = timeLeft(info)
  if not left then return nil, "numeric_range" end
  return {
    resultID = resultID, itemID = rowItemID, itemKey = key, rawItemKey = raw,
    link = s.kind == "commodity" and { kind = "unknown" } or { kind = "known", value = info.itemLink },
    quantity = quantity,
    price = { kind = s.kind == "commodity" and "unit" or "auction", copper = copper }, unitPrice = unit,
    owners = owners, timeLeft = left,
  }
end

local function sameNameMap(left, right)
  left, right = left or {}, right or {}
  for name in pairs(left) do
    if not right[name] then return false end
  end
  for name in pairs(right) do
    if not left[name] then return false end
  end
  return true
end

local function sameOwners(left, right)
  return left.totalNumberOfOwners == right.totalNumberOfOwners and
    left.containsOwnerItem == right.containsOwnerItem and
    left.containsAccountItem == right.containsAccountItem and
    left.identityKind == right.identityKind and sameNameMap(left.names, right.names)
end

local function sameRow(left, right)
  local a, b = left.rawItemKey, right.rawItemKey
  return left.itemKey == right.itemKey and left.quantity == right.quantity and
    left.price.kind == right.price.kind and left.price.copper == right.price.copper and
    left.unitPrice.numerator == right.unitPrice.numerator and left.unitPrice.denominator == right.unitPrice.denominator and
    a.itemID == b.itemID and a.itemLevel == b.itemLevel and a.itemSuffix == b.itemSuffix and
    a.battlePetSpeciesID == b.battlePetSpeciesID and sameOwners(left.owners, right.owners)
end

local function collectResults(s)
  if not state or state.item ~= s or not s.awaiting or not s.sentBarrier or not s.responseBarrier then return end
  s.awaiting, s.sentBarrier, s.responseBarrier, s.sentRequested = false, false, false, false
  if s.reasonCode then return finishItem(s) end
  if ns.Context.House().key ~= state.house.key then return Scanner.Abort("house_changed") end
  local count = s.kind == "commodity" and Native.GetCommodityCount(s.itemID) or Native.GetItemCount(s.itemKey)
  local seenThisCall = {}
  for i = 1, count do
    local row, reason = resultInfo(s, i)
    if row then
      if not s.seen[row.resultID] then s.seen[row.resultID] = row s.rows[#s.rows + 1] = row
      elseif not sameRow(s.seen[row.resultID], row) then s.reasonCode = "identity_mismatch" end
    elseif reason ~= "eligible_skip" then
      s.reasonCode = s.reasonCode or (reason == "missing_result" and "identity_mismatch" or reason)
    end
  end
  if s.reasonCode then return finishItem(s) end
  local full = sourceComplete(s)
  local distinct = {}
  for _, row in ipairs(s.rows) do distinct[row.unitPrice.numerator .. ":" .. row.unitPrice.denominator] = true end
  local levelCount = 0 for _ in pairs(distinct) do levelCount = levelCount + 1 end
  if s.kind == "commodity" and levelCount > s.planLevels then
    local partialReason = normalizeRows(s, nil)
    if partialReason then s.reasonCode = partialReason return finishItem(s) end
    local cutoff, witness, levels, previous
    for _, row in ipairs(s.rows) do
      local marker = row.unitPrice.numerator .. ":" .. row.unitPrice.denominator
      if marker ~= previous then
        levels, previous = (levels or 0) + 1, marker
        if levels == s.planLevels then cutoff = row.unitPrice end
      elseif not levels then
        levels = 1
      end
      if cutoff and compare(row.unitPrice, cutoff) > 0 then witness = row.unitPrice break end
    end
    if cutoff and witness then return finishItem(s) end
  end
  if full then return finishItem(s) end
  if s.pages >= MAX_PAGES then failCurrent("page_limit") return Scanner.Next() end
  s.pages = s.pages + 1
  local sent = sendMessage(s, function()
    if state and state.item == s then
      s.sentRequested, s.awaiting, s.sentBarrier, s.responseBarrier = true, false, false, false
      traceEvent("query_send", s, { operation = "page" })
      internalSendDepth = internalSendDepth + 1
      if s.kind == "commodity" then Native.RequestMoreCommodity(s.itemID)
      else Native.RequestMoreItem(s.itemKey) end
      internalSendDepth = internalSendDepth - 1
    end
  end)
end

local function bindSearchEvents(s)
  local function commodityEvent(eventName, itemID)
    local matching = state and state.item == s and s.kind == "commodity" and type(itemID) == "number" and itemID == s.itemID
    traceEvent("dispatch", s, {
      event = "commodity_result", nativeEvent = eventName, matching = matching == true, resultItemID = itemID,
    })
    if matching then traceReadiness(s, "result_event") collectResults(s) end
  end
  local function itemEvent(eventName, itemKey)
    local matching = state and state.item == s and s.kind == "item" and keysEqual(itemKey, s.itemKey)
    traceEvent("dispatch", s, {
      event = "item_result", nativeEvent = eventName, matching = matching == true, resultIdentity = traceIdentity(itemKey),
    })
    if matching then traceReadiness(s, "result_event") collectResults(s) end
  end
  local sentEvent = function()
    traceEvent("throttle_sent", s, { matching = state ~= nil and state.item == s and s.sentRequested == true })
    if state and state.item == s and s.sentRequested then s.sentBarrier, s.awaiting = true, true end
  end
  local responseEvent = function()
    traceEvent("throttle_response", s, { matching = state ~= nil and state.item == s and s.sentRequested == true and s.sentBarrier == true })
    if state and state.item == s and s.sentRequested and s.sentBarrier then s.responseBarrier = true end
  end
  s.handlers.AUCTION_HOUSE_THROTTLED_MESSAGE_SENT = sentEvent
  s.handlers.AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED = responseEvent
  s.handlers.COMMODITY_SEARCH_RESULTS_ADDED, s.handlers.COMMODITY_SEARCH_RESULTS_UPDATED = commodityEvent, commodityEvent
  s.handlers.ITEM_SEARCH_RESULTS_ADDED, s.handlers.ITEM_SEARCH_RESULTS_UPDATED = itemEvent, itemEvent
  Native.On("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT", sentEvent)
  Native.On("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED", responseEvent)
  Native.On("COMMODITY_SEARCH_RESULTS_ADDED", commodityEvent) Native.On("COMMODITY_SEARCH_RESULTS_UPDATED", commodityEvent)
  Native.On("ITEM_SEARCH_RESULTS_ADDED", itemEvent) Native.On("ITEM_SEARCH_RESULTS_UPDATED", itemEvent)
end

local function failItemStart(s, reason)
  if state ~= nil and state.item == s then
    if s.metadataHandler then
      Native.Off("ITEM_KEY_ITEM_INFO_RECEIVED", s.metadataHandler)
      s.handlers.ITEM_KEY_ITEM_INFO_RECEIVED, s.metadataHandler = nil, nil
    end
    failCurrent(reason)
    Scanner.Next()
  end
end

local function startSearch(s)
  if not state or state.item ~= s then return end
  local nativeKey = Native.MakeItemKey(s.itemID)
  if not nativeKey then return failItemStart(s, "metadata_unavailable") end
  local metadata = Native.ItemKeyInfo(nativeKey)
  if not metadata then return end
  if type(metadata.isCommodity) ~= "boolean" then return failItemStart(s, "metadata_unavailable") end
  local classID, equipment = classifyItem(s.itemID)
  if not classID then return failItemStart(s, "metadata_unavailable") end
  if metadata.isCommodity then equipment = false end
  s.classID, s.isEquipment = classID, equipment
  s.itemKey = equipment and { itemID = s.itemID, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0 } or nativeKey
  s.kind = metadata.isCommodity and "commodity" or "item"
  local sort = s.kind == "commodity" and Native.SortOrder("Price") or Native.SortOrder("Buyout")
  s.sorts = { { sortOrder = sort, reverseSort = false } }
  bindSearchEvents(s)
  local sent = sendMessage(s, function()
    if state and state.item == s then
      s.sentRequested, s.awaiting, s.sentBarrier, s.responseBarrier = true, false, false, false
      traceEvent("query_send", s, {
        operation = "search",
        nativeMethod = (s.kind == "commodity" or not s.isEquipment) and "SendSearchQuery" or "SendSellSearchQuery",
        classID = s.classID, equipment = s.isEquipment, nativeKey = traceIdentity(s.itemKey),
      })
      internalSendDepth = internalSendDepth + 1
      if s.kind == "commodity" or not s.isEquipment then
        Native.SendSearchQuery(s.itemKey, s.sorts)
      else
        Native.SendSellSearchQuery(s.itemKey, s.sorts)
      end
      internalSendDepth = internalSendDepth - 1
    end
  end)
end

local function disconnectOwned(sweep)
  if not sweep then return end
  if sweep.ownedHandler then Native.Off("OWNED_AUCTIONS_UPDATED", sweep.ownedHandler) end
  if sweep.ownedSentHandler then Native.Off("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT", sweep.ownedSentHandler) end
  if sweep.ownedResponseHandler then Native.Off("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED", sweep.ownedResponseHandler) end
end

local function failOwned(sweep, reason)
  if state ~= sweep or sweep.owned then return end
  sweep.ownedAwaiting = false
  disconnectOwned(sweep)
  local owned = {
    status = "incomplete", query = { kind = "queried", startedAt = sweep.ownedStarted, finishedAt = time() },
    reasonCode = reason, rowCount = 0, rows = {},
  }
  sweep = reduceState(sweep, { type = "owned_completed", owned = owned })
  state = sweep
  traceEvent("owned_outcome", sweep, { outcome = "incomplete", reason = reason, rowCount = 0 })
  Scanner.Finish()
end

local function beginOwned(sweep)
  sweep.ownedStarted = time()
  sweep.ownedAwaiting = false
  sweep.ownedHandler = function()
    if state ~= sweep then return end
    traceEvent("dispatch", sweep, { event = "owned_result", matching = sweep.ownedAwaiting == true })
    if ns.Context.House().key ~= sweep.house.key then return failOwned(sweep, "house_changed") end
    if not sweep.ownedAwaiting or not sweep.ownedSentBarrier or not sweep.ownedResponseBarrier or sweep.ownedExternal then return end
    if not Native.FullOwned() then return end
    sweep.ownedAwaiting, sweep.ownedSentBarrier, sweep.ownedResponseBarrier, sweep.ownedSentRequested = false, false, false, false
    local rows, invalid = {}, false
      for i = 1, Native.OwnedCount() do
        local info = Native.OwnedInfo(i)
        local itemID, keyFieldsValue = info and info.itemKey and keyFields(info.itemKey)
        if not info or type(info.auctionID) ~= "number" or info.auctionID <= 0 or info.auctionID > MAX_SAFE or info.auctionID ~= math.floor(info.auctionID) or
            not itemID or type(info.quantity) ~= "number" or info.quantity <= 0 or info.quantity > MAX_SAFE or info.quantity ~= math.floor(info.quantity) or
            (info.status ~= Native.AuctionStatus("Active") and info.status ~= Native.AuctionStatus("Sold")) or not info.itemLink then
          invalid = true
        else
          local raw = rawKey(info.itemKey, itemID, info.itemLink)
          local key = raw and Scanner.ItemKey(info.itemLink, raw)
          local buyout = info.buyoutAmount
          if not key or (buyout ~= nil and (type(buyout) ~= "number" or buyout < 0 or buyout ~= math.floor(buyout) or buyout > MAX_SAFE)) then invalid = true
          else rows[#rows + 1] = { auctionID = info.auctionID, itemID = itemID, itemKey = key, rawItemKey = raw,
            itemLink = info.itemLink, quantity = info.quantity,
            buyout = buyout and buyout > 0 and { kind = "known", copper = buyout } or { kind = "unknown" },
            status = info.status == Native.AuctionStatus("Sold") and "sold" or "active" }
          end
        end
      end
      local owned = { status = invalid and "incomplete" or "full", query = { kind = "queried", startedAt = sweep.ownedStarted, finishedAt = time() },
        reasonCode = invalid and "query_timeout" or "ok", rowCount = #rows, rows = (function() local out = {} for _, row in ipairs(rows) do out[tostring(row.auctionID)] = row end return out end)() }
      sweep = reduceState(sweep, { type = "owned_completed", owned = owned })
      state = sweep
      traceEvent("owned_outcome", sweep, { outcome = sweep.owned.status, reason = sweep.owned.reasonCode, rowCount = sweep.owned.rowCount })
      disconnectOwned(sweep)
      Scanner.Finish()
  end
  sweep = reduceState(sweep, { type = "owned_started", handler = sweep.ownedHandler })
  state = sweep
  sweep.ownedSentHandler = function()
    traceEvent("throttle_sent", sweep, { matching = state == sweep and sweep.ownedSentRequested == true, operation = "owned" })
    if state == sweep and sweep.ownedSentRequested then sweep.ownedSentBarrier, sweep.ownedAwaiting = true, true end
  end
  sweep.ownedResponseHandler = function()
    traceEvent("throttle_response", sweep, { matching = state == sweep and sweep.ownedSentRequested == true and sweep.ownedSentBarrier == true, operation = "owned" })
    if state == sweep and sweep.ownedSentRequested and sweep.ownedSentBarrier then sweep.ownedResponseBarrier = true end
  end
  Native.On("AUCTION_HOUSE_THROTTLED_MESSAGE_SENT", sweep.ownedSentHandler)
  Native.On("AUCTION_HOUSE_THROTTLED_MESSAGE_RESPONSE_RECEIVED", sweep.ownedResponseHandler)
  Native.On("OWNED_AUCTIONS_UPDATED", sweep.ownedHandler)
  Native.After(OWNED_TIMEOUT, function()
    if state == sweep and not sweep.owned then
      failOwned(sweep, "query_timeout")
    end
  end)
  local sent = sendMessage(sweep, function()
    if state == sweep then
      sweep.ownedSentRequested, sweep.ownedAwaiting, sweep.ownedSentBarrier, sweep.ownedResponseBarrier = true, false, false, false
      traceEvent("query_send", sweep, { operation = "owned" })
      internalSendDepth = internalSendDepth + 1
      sweep.ownedSorts = { { sortOrder = Native.SortOrder("Buyout"), reverseSort = false } }
      Native.QueryOwned(sweep.ownedSorts)
      internalSendDepth = internalSendDepth - 1
    end
  end)
end

local function beginItem(itemID, ordinal)
  local itemKey = Native.MakeItemKey(itemID)
  local s = { itemID = itemID, ordinal = ordinal, itemKey = itemKey, rows = {}, seen = {}, pages = 1,
    handlers = {}, queryStarted = time(), planLevels = state.plan.initialPriceLevels }
  state = reduceState(state, { type = "item_started", item = s, cursor = itemID })
  local metadata = Native.ItemKeyInfo(itemKey)
  if not metadata then
    s.metadataWaiting = true
    s.metadataHandler = function(_, readyID)
      if state and state.item == s and readyID == itemID then
        s.metadataWaiting = false
        Native.Off("ITEM_KEY_ITEM_INFO_RECEIVED", s.metadataHandler)
        s.handlers.ITEM_KEY_ITEM_INFO_RECEIVED, s.metadataHandler = nil, nil
        startSearch(s)
      end
    end
    s.handlers.ITEM_KEY_ITEM_INFO_RECEIVED = s.metadataHandler
    Native.On("ITEM_KEY_ITEM_INFO_RECEIVED", s.metadataHandler)
    Native.RequestItemData(itemID)
  else startSearch(s) end
  Native.After(ITEM_TIMEOUT, function()
    if state and state.item == s then failCurrent(s.metadataWaiting and "metadata_unavailable" or "query_timeout") Scanner.Next() end
  end)
end

function Scanner.Next()
  if not state then return end
  if ns.Context.House().key ~= state.house.key then
    if state.item then failCurrent("house_changed") end
    markTail("house_changed") return Scanner.Finish()
  end
  if time() - state.startedAt >= SWEEP_TIMEOUT then
    if state.item then failCurrent("sweep_timeout") end
    if state.ownedHandler then
      disconnectOwned(state)
      state = reduceState(state, {
        type = "owned_completed",
        owned = { status = "incomplete", query = { kind = "queried", startedAt = state.ownedStarted or time(), finishedAt = time() }, reasonCode = "sweep_timeout", rowCount = 0, rows = {} },
      })
    end
    markTail("sweep_timeout") return Scanner.Finish()
  end
  if state.nextOrdinal > #state.order then
    if state.owned then return Scanner.Finish() end
    return beginOwned(state)
  end
  beginItem(state.order[state.nextOrdinal], state.nextOrdinal)
end

function Scanner.InProgress() return state ~= nil end
function Scanner.CooldownRemaining() if not ns.db.lastSweepStart then return 0 end return math.max(0, CADENCE - (time() - ns.db.lastSweepStart)) end
function Scanner.Due() return Scanner.CooldownRemaining() == 0 end
function Scanner.Schedule(source)
  source = source or "unknown"
  scheduleEvent("request", { source = source })
  if scheduleBlocked then scheduleEvent("refusal", { reason = "logout_fence", source = source }) return end
  if nativeBlocked() or nativeConstructionFence then
    scheduleEvent("refusal", { reason = "native_construction_fence", source = source })
    return
  end
  if not ns.ahOpen then scheduleEvent("refusal", { reason = "ah_closed", source = source }) return end
  if state then scheduleEvent("refusal", { reason = "sweep_active", source = source }) return end
  if ns.db.storageBlocked then scheduleEvent("refusal", { reason = "storage_blocked", source = source }) return end
  local _, planError = loadPlan()
  if planError then scheduleEvent("refusal", { reason = planError, source = source }) return end
  scheduleToken = scheduleToken + 1
  local token = scheduleToken
  local delay = Scanner.CooldownRemaining()
  local timerCallback = function()
    scheduleEvent("timer_callback", { source = "timer", token = token, currentToken = scheduleToken })
    if token ~= scheduleToken then
      scheduleEvent("stale_token", { reason = "token_changed", source = "timer", token = token, currentToken = scheduleToken })
    end
    if not ns.ahOpen then
      scheduleEvent("ah_closed_discard", { reason = "ah_closed", source = "timer", token = token, currentToken = scheduleToken })
    end
    if token ~= scheduleToken or not ns.ahOpen then
      return
    end
    Scanner.Start(false, "timer_callback")
  end
  Native.After(delay, timerCallback)
  scheduleEvent("timer_create", {
    source = source, token = token, currentToken = scheduleToken, delay = delay, due = time() + delay,
  })
end

-- The slice the next sweep takes: db.coldSliceNext (0..N-1), advanced by Finish. First use starts at 0.
-- A stored count that differs from N, or a stored value outside 0..N-1, resets to 0 loudly (print,
-- db.coldSliceReset and a cold_slice_reset query-trace event); it is never clamped.
local function peekColdSlice(slices)
  local nextIndex, count = ns.db.coldSliceNext, ns.db.coldSliceCount
  if nextIndex == nil and count == nil then return 0 end
  if count ~= slices then return 0, "count_changed" end
  if type(nextIndex) ~= "number" or nextIndex < 0 or nextIndex >= slices or nextIndex ~= math.floor(nextIndex) then
    return 0, "out_of_range"
  end
  return nextIndex
end

function Scanner.ColdSliceForSweep(slices)
  local nextIndex, count = ns.db.coldSliceNext, ns.db.coldSliceCount
  local index, reason = peekColdSlice(slices)
  if not reason then return index end
  ns.db.coldSliceNext, ns.db.coldSliceCount = 0, slices
  ns.db.coldSliceReset = { reason = reason, at = time(), stored = tostring(nextIndex), storedCount = tostring(count) }
  ns.Print("cold slice counter reset to 0: " .. reason)
  return 0, reason
end

function Scanner.Start(force, source)
  source = source or "manual"
  if state then return ns.Print("a sweep is already running") end
  if scheduleBlocked then return ns.Print("scan refused: logout fence active") end
  if nativeBlocked() or nativeConstructionFence then return ns.Print("scan refused: native construction fence active") end
  if not ns.ahOpen then return ns.Print("open the auction house first") end
  if ns.PruneStorage then ns.PruneStorage() end
  local plan, errorText = loadPlan()
  if not plan or errorText then return ns.Print("scan refused: " .. (errorText or "plan_invalid")) end
  if ns.db.storageBlocked then return ns.Print("scan paused: storage blocked until acknowledgements are loaded") end
  if not force and not Scanner.Due() then
    local remaining = Scanner.CooldownRemaining()
    if source == "timer_callback" then
      scheduleEvent("due_refusal", {
        reason = "cooldown_active", source = source, due = time() + remaining, delay = remaining,
      })
      if ns.ahOpen and not scheduleBlocked and not state then Scanner.Schedule("timer_rearm") end
      return
    end
    return ns.Print(("scan due in %ds"):format(remaining))
  end
  local cursor = ns.db.cursorItemID
  local found = false for _, itemID in ipairs(plan.itemIDs) do if itemID == cursor then found = true break end end
  if ns.db.cursorPlanHash and ns.db.cursorPlanHash ~= CROWLEY_DATA.scanPlan.planHash then
    if not found then ns.Print("scan plan changed; cursor reset to first item") cursor = nil else ns.Print("scan plan changed; continuing after cursor") end
  elseif cursor and not found then ns.Print("scan cursor no longer exists; reset to first item") cursor = nil end
  scheduleToken = scheduleToken + 1
  generation = generation + 1
  local sweepGeneration = generation
  local sweepPlanHash = CROWLEY_DATA.scanPlan.planHash
  local sweepPlanJSON = CROWLEY_DATA.scanPlan.planJSON
  local sweepState = ns.ScannerState.NewSweep(plan, ns.Context.House(), ns.Context.Snapshot(), time(), sweepGeneration)
  sweepState.planHash, sweepState.planJSON, sweepState.cursor = sweepPlanHash, sweepPlanJSON, cursor
  -- This sweep attempts every hot item plus exactly one cold slice, k = the persisted rotation counter.
  local sliceIndex, resetReason
  if plan.coldSlices > 0 then sliceIndex, resetReason = Scanner.ColdSliceForSweep(plan.coldSlices) end
  local sweepIDs, coldSlice = ns.ScannerState.SweepItems(plan, sliceIndex)
  sweepState.coldSlice = coldSlice
  -- The cursor names an item of the whole plan; resume after the last swept item at or before it.
  local sweepCursor
  if cursor ~= nil then
    for _, itemID in ipairs(sweepIDs) do
      if itemID <= cursor then sweepCursor = itemID else break end
    end
  end
  sweepState.order = ns.ScannerState.PlanOrder(sweepIDs, sweepCursor)
  sweepState.queryTrace = { version = QUERY_TRACE_VERSION, cap = QUERY_TRACE_CAP, sequence = 0, dropped = 0, events = {} }
  state = sweepState
  ns.db.activeQueryTrace = state.queryTrace
  traceEvent("sweep_start", nil, { planned = #state.order, house = state.house.key })
  if resetReason then traceEvent("cold_slice_reset", nil, { reason = resetReason, count = plan.coldSlices }) end
  ns.db.lastSweepStart = state.startedAt
  scheduleEvent("start_commit", {
    source = source, token = scheduleToken, currentToken = scheduleToken, due = state.startedAt + CADENCE,
  })
  ns.Print(("sweep started for %s (%d planned items)"):format(state.house.key, #state.order))
  local sweep = state
  Native.After(SWEEP_TIMEOUT, function()
    if state and state.generation == sweepGeneration then
      if state.item then failCurrent("sweep_timeout") end
      if state.ownedHandler then
        disconnectOwned(state)
        if not state.owned then
          state = reduceState(state, {
            type = "owned_completed",
            owned = { status = "incomplete", query = { kind = "queried", startedAt = state.ownedStarted or time(), finishedAt = time() }, reasonCode = "sweep_timeout", rowCount = 0, rows = {} },
          })
        end
      end
      markTail("sweep_timeout") Scanner.Finish()
    end
  end)
  Scanner.Next()
end

function Scanner.PreLogoutFence()
  -- Lua event/timer callbacks are serialized.  Check the private active state
  -- and revoke the timer token in this one handler, so a due callback cannot
  -- slip between an idle check and token cancellation.  A live item/owned
  -- query is refused and remains untouched; ordinary PLAYER_LOGOUT below
  -- still preserves its partial-save behavior.
  if nativeBlocked() or nativeConstructionFence then
    scheduleEvent("fence_refusal", { reason = "native_construction_fence", source = "pre_logout_fence" })
    return false, "native_construction_fence"
  end
  if state then
    scheduleEvent("fence_refusal", { reason = "scan_active", source = "pre_logout_fence" })
    scheduleFence = { state = "refused", reason = "scan_active", command = PRE_LOGOUT_FENCE_COMMAND }
    traceEvent("pre_logout_fence_refused", state, { command = PRE_LOGOUT_FENCE_COMMAND, reason = "scan_active" })
    return false, "scan_active"
  end
  local previousToken = scheduleToken
  scheduleToken = scheduleToken + 1
  scheduleBlocked = true
  scheduleEvent("cancel", {
    reason = "pre_logout_fence", source = "pre_logout_fence", token = previousToken, currentToken = scheduleToken,
  })
  scheduleFence = { state = "armed", reason = "", command = PRE_LOGOUT_FENCE_COMMAND }
  traceEvent("pre_logout_fence_armed", nil, { command = PRE_LOGOUT_FENCE_COMMAND })
  return true, nil
end

function Scanner.PreLogoutFenceStatus()
  return effectiveFence()
end

function Scanner.Abort(reason)
  if not state then return end
  if state.item then failCurrent(reason) end
  if state.ownedHandler then
    disconnectOwned(state)
    state = reduceState(state, {
      type = "owned_completed",
      owned = { status = "incomplete", query = { kind = "queried", startedAt = state.ownedStarted or time(), finishedAt = time() }, reasonCode = reason, rowCount = 0, rows = {} },
    })
  end
  markTail(reason)
  Scanner.Finish()
end

function Scanner.Finish()
  if not state then return end
  traceEvent("sweep_finish", nil, { planned = #state.order })
  local s = state
  state = reduceState(state, { type = "finished" })
  s = state
  state = nil
  ns.db.lastQueryTrace, ns.db.activeQueryTrace = s.queryTrace, nil
  local owned = s.owned or { status = "unqueried", query = { kind = "unqueried" }, reasonCode = "not_started", rowCount = 0, rows = {} }
  local payload = ns.ScanEncoder.Payload({ planHash = s.planHash, planJSON = s.planJSON, observations = s.observations, owned = owned })
  local encodedPayload = ns.ScanEncoder.Encode(
    payload, Native.SerializeJSON, Native.CompressString, Native.EncodeBase64, true
  )
  local id = ns.Util.NewID({ s.house.key, s.startedAt, s.context.character }, 0, 9999)
  ns.db.scans[id] = ns.ScanEncoder.Envelope(s, id, time(), 0, #s.order, encodedPayload)
  for _, observation in pairs(s.observations) do ns.db.scans[id].numRows = ns.db.scans[id].numRows + observation.rowCount end
  local stored = 0 for _ in pairs(ns.db.scans) do stored = stored + 1 end
  local attempted, failures = 0, 0
  for _, observation in pairs(s.observations) do
    if observation.query.kind == "queried" then attempted = attempted + 1 end
    if observation.coverage == "incomplete" or observation.coverage == "unqueried" then failures = failures + 1 end
  end
  ns.db.lastSweepPlanned, ns.db.lastSweepAttempted, ns.db.lastSweepFailures = #s.order, attempted, failures
  local savedRows = ns.db.scans[id].numRows
  ns.db.agentLastSaved = {
    memoryUid = id, finishedAt = ns.db.scans[id].finishedAt, planned = #s.order,
    attempted = attempted, failures = failures, rows = savedRows,
  }
  ns.db.storageBlocked = stored >= ns.MAX_STORED_SCANS
  ns.db.cursorItemID, ns.db.cursorPlanHash = s.cursor, s.planHash
  -- Completed and aborted sweeps alike advance the cold rotation.
  if s.coldSlice then
    ns.db.coldSliceNext, ns.db.coldSliceCount = (s.coldSlice.index + 1) % s.coldSlice.count, s.coldSlice.count
  end
  ns.Print(("sweep saved: %d observations, %d rows for %s - /reload to upload"):format(#s.order, ns.db.scans[id].numRows, s.house.key))
  Scanner.Schedule("finish")
end

-- A copied, read-only projection for Agent.lua. The projection intentionally keeps
-- the empty active UID while a scan is running: this native scanner allocates its
-- stable UID only when Finish writes CrowleyDB.scans.
function Scanner.Status()
  local db = ns.db or {}
  local plan, planError
  if state then plan = state.plan else plan, planError = loadPlan() end
  local status = {
    phase = "ready", activeSweepId = "", startedAt = 0, dueAt = 0, ordinal = 0,
    planned = 0, cursorItemID = db.cursorItemID or 0,
    reason = "",
  }
  if plan and not state then
    -- Idle: the size of the item set the next sweep will attempt (hot plus the next cold slice).
    local ids = ns.ScannerState.SweepItems(plan, (plan.coldSlices or 0) > 0 and (peekColdSlice(plan.coldSlices)) or nil)
    status.planned = #ids
  end
  if state then
    status.phase = state.ownedHandler and "owned" or "item"
    status.startedAt = state.startedAt or 0
    status.dueAt = status.startedAt + CADENCE
    status.ordinal = state.item and state.item.ordinal or math.min(state.nextOrdinal or 1, #state.order)
    status.planned = #state.order
    status.cursorItemID = state.cursor or status.cursorItemID
  elseif not plan or planError then
    status.phase = "blocked"
    status.reason = "plan_invalid"
  elseif db.storageBlocked then
    status.phase = "blocked"
    status.reason = "queue_full"
  elseif ns.ahOpen and Scanner.CooldownRemaining() > 0 then
    status.phase = "scheduled"
    status.dueAt = (db.lastSweepStart or time()) + CADENCE
  end
  local planProjection = plan and {
    hash = state and state.planHash or CANONICAL_PLAN_HASH,
    count = #(plan.itemIDs or {}), cadenceSeconds = plan.cadenceSeconds or 0,
    sweepCapSeconds = plan.sweepCapSeconds or SWEEP_TIMEOUT,
    initialPriceLevels = plan.initialPriceLevels or 0,
  } or nil
  local last = db.agentLastSaved or {}
  local pending = {}
  local pendingSeen = {}
  for id in pairs(db.scans or {}) do pendingSeen[id] = true end
  for id in pairs(pendingSeen) do pending[#pending + 1] = id end
  table.sort(pending)
  local acked = {}
  if type(CROWLEY_DATA) ~= "table" or not ns.Util.IsDenseUniqueByteSortedStrings(CROWLEY_DATA.ackedScans) then
    error("CROWLEY_DATA.ackedScans must be a dense array of unique byte-sorted non-empty strings")
  end
  for _, id in ipairs(CROWLEY_DATA.ackedScans) do acked[#acked + 1] = id end
  table.sort(acked)
  local function copyArray(array)
    local result = {} for i, value in ipairs(array) do result[i] = value end return result
  end
  return {
    phase = status.phase, activeSweepId = status.activeSweepId, startedAt = status.startedAt,
    dueAt = status.dueAt, ordinal = status.ordinal, planned = status.planned,
    cursorItemID = status.cursorItemID, reason = status.reason, plan = planProjection,
    scheduleFence = Scanner.PreLogoutFenceStatus(), lastSaved = {
      memoryUid = last.memoryUid or "", finishedAt = last.finishedAt or 0,
      planned = last.planned or 0, attempted = last.attempted or 0, failures = last.failures or 0,
      rows = last.rows or 0, persistence = "unknown",
    }, pending = copyArray(pending), loaded = copyArray(acked),
  }
end
