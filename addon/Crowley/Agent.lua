local _, ns = ...

-- Lossless, read-only telemetry surface. It is deliberately independent of the
-- scanner's command path: publishing cannot start, stop, or mutate a sweep.
local Agent = {}
ns.Agent = Agent

local WIDTH, HEIGHT, CAPACITY = 128, 128, 2048
local ATLAS_PATH = "Interface\\AddOns\\Crowley\\Textures\\ByteAtlas"
local CROP_X, CROP_Y = 80, 48
local POSITION_LIMIT = 1000000
local FACING_LIMIT = 6.283185307179586
local SPEED_LIMIT = 1000
local RESTORE_READINESS_TIMEOUT = 30
local RESTORE_READINESS_POLL = 0.1
local sequence = 0
local boot
local bootReady = false
local surface
local cells = {}
local frameBytes = nil
local hudHidden = false
local publishingForHudShow = false
local elapsed = 1
local nativePhase = "normal"
local nativeOrdinal = 0
local measurementToken = 0
local safePublish
local lastLuaHeap = { method = "collectgarbage_count", available = false, beforeKiB = 0, afterKiB = 0, netKiB = 0 }
local renderMetrics = {
  constructionSeconds = 0, updateCount = 0, updateP99Ms = 0, textureCount = 0,
  effectiveScale = 0, cellSize = { 2, 2 }, luaHeap = lastLuaHeap,
  nativePhase = nativePhase, nativeOrdinal = nativeOrdinal,
}
local updateSamples = {}
local lastPublishError
local lifecycle = "unloaded"
local reducerState = ns.AgentState.New(lifecycle)
local releaseAdmitted = false
local releaseDeadline = 0
local recoveryAdmitted = false
local recoveryMode = false
local restoreInFlight = false
local restoreAttempt = 0
local measurementCompleted = false
local loadId = 0
local loadInitialized = false
local restoreReadinessOwner = 0
local restoreReadinessDeadline = 0
local restoreReadinessWorldSeen = false
local restoreReadinessFailure
local restoreReadinessPollScheduled = false
local restoreReadinessToken
local restoreReadinessLoad = 0
-- Forward declaration: deferred construction installs the same publication
-- callback as an ordinary boot before its first publish.
local attachSurfaceScript

local function transitionLifecycle(event)
  local nextState, reason = ns.AgentState.Transition(reducerState, event)
  if not nextState then error("agent lifecycle transition " .. tostring(event) .. " refused: " .. tostring(reason)) end
  reducerState = nextState
  lifecycle = nextState.lifecycle
  nativePhase, nativeOrdinal = nextState.nativePhase, nextState.nativeOrdinal
  measurementCompleted = nextState.measurementCompleted
  renderMetrics.nativePhase, renderMetrics.nativeOrdinal = nativePhase, nativeOrdinal
end

local function adoptLifecycle(target)
  if lifecycle == target then return end
  local event = target
  if target == "restore_only" then event = "recover"
  elseif target == "restore_pending" then event = "restore" end
  transitionLifecycle(event)
end

local function marker()
  return ns.db and ns.db.nativeConstruction
end

local function randomBoot()
  return ns.Util.NewID({ ns.AgentNative.Now() }, 100000, 999999)
end

local function nativeBoot(token)
  return "native-construction:" .. tostring(token)
end

local function setMarker(value)
  if type(ns.SetNativeMarker) ~= "function" then return false, "marker_validation_unavailable" end
  local ok, reason = ns.SetNativeMarker(value)
  if not ok then return false, reason end
  return true
end

local function clearSurface()
  ns.AgentRendering.Clear(surface, cells)
  surface, cells, frameBytes = nil, {}, nil
end

local function setSurfaceShown(visible)
  ns.AgentRendering.SetShown(surface, cells, visible)
end

local function sortedKeys(tableValue)
  local result = {}
  for key in pairs(tableValue or {}) do result[#result + 1] = key end
  table.sort(result)
  return result
end

local function activeEdit()
  return ns.AgentNative.ActiveEdit()
end

local function planInfo(plan)
  plan = plan or {}
  return {
    hash = plan.hash or "", count = plan.count or 0,
    cadenceSeconds = plan.cadenceSeconds or 0,
    sweepCapSeconds = plan.sweepCapSeconds or 600,
    initialPriceLevels = plan.initialPriceLevels or 0,
  }
end

local function finiteNumber(value, name, minimum, maximum)
  if not ns.Util.IsFiniteNumber(value, minimum, maximum) then
    local detail = ""
    if name == "facing" then
      local text = tostring(value)
      if #text > 32 then text = text:sub(1, 32) end
      detail = " (" .. type(value) .. ":" .. text .. ")"
    end
    error("Crowley movement telemetry has invalid " .. name .. detail)
  end
  return value
end

local function nativeBoolean(value, name)
  if type(value) ~= "boolean" then error("Crowley movement telemetry has invalid " .. name) end
  return value
end

local NATIVE_FAILURES = { "call_error", "missing", "wrong_type", "nil_result" }

local function nativeFailure(reason)
  for _, allowed in ipairs(NATIVE_FAILURES) do
    if reason == allowed then return reason end
  end
  error("Crowley movement telemetry has invalid native failure state")
end

local function ctmState()
  local result = {
    accessor = "C_CVar.GetCVarBool", cvar = "autointeract", state = "unknown", reason = "missing",
  }
  local value, reason = ns.AgentNative.CvarBool()
  if reason == "missing" then
    return result
  elseif reason ~= "" then
    result.reason = nativeFailure(reason)
  else
    result.state = value and "enabled" or "disabled"
    result.reason = ""
  end
  return result
end

local GROUND_PREDICATES = {
  { "flying", "IsFlying", false },
  { "falling", "IsFalling", false },
  { "swimming", "IsSwimming", false },
  { "submerged", "IsSubmerged", false },
  { "mounted", "IsMounted", false },
  { "taxi", "UnitOnTaxi", true },
}

local function groundState()
  local values = {
    state = "unknown", falling = "unknown", flying = "unknown", swimming = "unknown",
    submerged = "unknown", mounted = "unknown", taxi = "unknown", reason = "",
  }
  local anyTrue = false
  local firstFailureRank = 99
  local failureRank = { falling = 1, flying = 2, swimming = 3, submerged = 4, mounted = 5, taxi = 6 }
  local function markFailure(field, reason)
    local rank = failureRank[field]
    if rank < firstFailureRank then
      firstFailureRank, values.reason = rank, reason
    end
  end
  for _, entry in ipairs(GROUND_PREDICATES) do
    local field, accessor, playerArgument = entry[1], entry[2], entry[3]
    local value, reason = ns.AgentNative.GroundPredicate(accessor, playerArgument)
    if reason ~= "" then
      values[field] = "unknown"
      markFailure(field, reason)
    else
      values[field] = value and "true" or "false"
      anyTrue = anyTrue or value
    end
  end
  if values.reason ~= "" then
    values.state = "unknown"
  else
    values.state = anyTrue and "not_grounded" or "grounded"
  end
  return values
end

local function nativeBinding(action, field)
  -- GetBindingKey's first return is the primary binding.  Preserve the exact
  -- native spelling; normalization would hide a client contract mismatch.
  local value = ns.AgentNative.Binding(action)
  if type(value) ~= "string" or #value < 1 or #value > 32 or
      value:find("[^A-Z0-9%-]") or value:sub(1, 1) == "-" or value:sub(-1) == "-" or
      value:find("%-%-") then
    error("Crowley movement telemetry has invalid " .. field .. " binding")
  end
  return value
end

local function hoverTelemetry()
  local facts = ns.AgentNative.Hover()
  local kind, name = "none", ""
  if facts.unitExists then
    kind, name = "unit", facts.unitName
  elseif facts.tooltipShown and facts.tooltipOwner == facts.uiParent then
    local objectName = facts.objectName
    if type(objectName) == "string" and #objectName > 0 then
      kind, name = "object", objectName
    end
  end
  if kind ~= "none" and (type(name) ~= "string" or #name == 0) then
    error("Crowley hover telemetry has invalid " .. kind .. " name")
  end
  if type(name) ~= "string" or #name > 48 then
    error("Crowley hover telemetry has an overlong name")
  end
  return { kind = kind, name = name }
end

local function mailTelemetry()
  return ns.AgentNative.MailShown()
end

local function executorTelemetry()
  if not ns.Executor or type(ns.Executor.Telemetry) ~= "function" then
    error("Crowley executor telemetry requires Executor")
  end
  return ns.Executor.Telemetry()
end

local function movementTelemetry()
  local x, y, z, instance = ns.AgentNative.Position()
  if type(instance) ~= "number" or instance ~= math.floor(instance) or instance < 0 or instance > 4294967295 then
    error("Crowley movement telemetry has invalid position.instance")
  end
  local position = {
    x = finiteNumber(x, "position.x", -POSITION_LIMIT, POSITION_LIMIT),
    y = finiteNumber(y, "position.y", -POSITION_LIMIT, POSITION_LIMIT),
    z = finiteNumber(z, "position.z", -POSITION_LIMIT, POSITION_LIMIT),
    instance = instance,
  }
  local currentSpeed, runSpeed, flightSpeed, swimSpeed = ns.AgentNative.Speed()
  local native = {
    ctm = ctmState(),
    ground = groundState(),
    speedTuple = {
      current = finiteNumber(currentSpeed, "speedTuple.current", 0, SPEED_LIMIT),
      run = finiteNumber(runSpeed, "speedTuple.run", 0, SPEED_LIMIT),
      flight = finiteNumber(flightSpeed, "speedTuple.flight", 0, SPEED_LIMIT),
      swim = finiteNumber(swimSpeed, "speedTuple.swim", 0, SPEED_LIMIT),
      unitStatus = "unverified",
    },
  }
  return {
    position = position,
    facing = finiteNumber(ns.AgentNative.Facing(), "facing", 0, FACING_LIMIT),
    speed = native.speedTuple.current,
    combat = nativeBoolean(ns.AgentNative.Combat(), "combat"),
    deadOrGhost = nativeBoolean(ns.AgentNative.DeadOrGhost(), "deadOrGhost"),
    bindings = {
      moveForward = nativeBinding("MOVEFORWARD", "moveForward"),
      moveBackward = nativeBinding("MOVEBACKWARD", "moveBackward"),
      strafeLeft = nativeBinding("STRAFELEFT", "strafeLeft"),
      strafeRight = nativeBinding("STRAFERIGHT", "strafeRight"),
      turnLeft = nativeBinding("TURNLEFT", "turnLeft"),
      jump = nativeBinding("JUMP", "jump"),
      turnRight = nativeBinding("TURNRIGHT", "turnRight"),
    },
    native = native,
  }
end

local function auctioneerTelemetry()
  -- These facts are sourced only from the native client APIs.  A missing API
  -- or unusable result remains explicit so the executor cannot confuse it
  -- with an empty binding or an out-of-range target.
  return ns.AgentNative.TargetFacts()
end

local function blockingUITelemetry()
  -- Normal world context does not prove that Return cannot activate a native
  -- popup. Publish explicit availability and visibility for both known
  -- blockers; consumers refuse input if either API is unavailable.
  return ns.AgentNative.BlockingUI()
end

function Agent.Snapshot()
  if not bootReady or not surface then error("agent_unconstructed") end
  local context = ns.Context.Snapshot()
  local house = ns.Context.House()
  local scanner = ns.Scanner.Status()
  local stored = scanner.pending or {}
  local acked = scanner.loaded or {}
  local last = scanner.lastSaved or {}
  local now = ns.AgentNative.Now()
  local uptime = math.floor((ns.AgentNative.Uptime() or 0) * 1000) % 4294967296
  local scannerValue = {
    phase = scanner.phase, activeSweepId = "", startedAt = scanner.startedAt or 0,
    dueAt = scanner.dueAt or 0, ordinal = scanner.ordinal or 0, planned = scanner.planned,
    cursorItemID = scanner.cursorItemID or 0,
  }
  -- The fence proof is part of every frame, including the idle state.  This
  -- keeps controller admission independent of whether a previous frame saw a
  -- fence attempt.
  scannerValue.scheduleFence = ns.Util.Copy(scanner.scheduleFence)
  local value = {
    schema = 6, boot = boot, sequence = sequence + 1, wallTime = now, uptimeMs = uptime,
    context = {
      addonVersion = context.addonVersion or ns.VERSION, interface = context.interface or 0,
      clientBuild = context.clientBuild or "", character = context.character or "", realm = context.realm or "",
      playerFaction = context.playerFaction or "", zone = context.zone or "", subzone = context.subzone or "",
      locale = context.locale or "", clientVersion = context.clientVersion or "",
    },
    house = { key = house.key, source = house.source }, plan = planInfo(scanner.plan), ahOpen = ns.ahOpen == true,
    hover = hoverTelemetry(), mailOpen = mailTelemetry(),
    executor = executorTelemetry(),
    auctioneer = auctioneerTelemetry(), blockingUI = blockingUITelemetry(),
    scanner = scannerValue,
    lastSaved = {
      memoryUid = last.memoryUid or "", finishedAt = last.finishedAt or 0, planned = last.planned or 0,
      attempted = last.attempted or 0, failures = last.failures or 0, rows = last.rows or 0, persistence = "unknown",
    },
    pending = {
      ids = ns.Util.Copy(stored), count = #stored, cap = 6, blocked = scanner.phase == "blocked",
      reason = scanner.reason or (scanner.phase == "blocked" and "queue_full" or ""),
    },
    loaded = { ackedIds = ns.Util.Copy(acked), generatedAt = CROWLEY_DATA.generatedAt or 0 }, chatEdit = activeEdit(),
    motion = movementTelemetry(),
    render = ns.Util.Copy(renderMetrics),
  }
  sequence = value.sequence
  return ns.Util.Copy(value)
end

local function buildSurface()
  local started = ns.AgentNative.ProfileStop()
  surface, cells, renderMetrics.effectiveScale = ns.AgentRendering.Create(WIDTH, HEIGHT, CROP_X, CROP_Y, ATLAS_PATH)
  renderMetrics.constructionSeconds = (ns.AgentNative.ProfileStop() - started) / 1000
  renderMetrics.textureCount = CAPACITY
  renderMetrics.cellSize = { 2, 2 }
end

local function createSurface()
  clearSurface()
  local ok, reason = pcall(buildSurface)
  if ok then return true end
  clearSurface()
  return false, reason
end

local function updateSurface(bytes)
  frameBytes = bytes
  ns.AgentRendering.Update(cells, bytes, WIDTH, HEIGHT)
end

local function countLuaMemory()
  local ok, value = pcall(ns.AgentNative.HeapCount)
  if not ok or type(value) ~= "number" or value ~= value or value == math.huge or value == -math.huge or value < 0 then return nil end
  return value
end

local function setLuaHeap(beforeMemory, afterMemory)
  if beforeMemory == nil or afterMemory == nil then
    lastLuaHeap = { method = "collectgarbage_count", available = false, beforeKiB = 0, afterKiB = 0, netKiB = 0 }
  else
    lastLuaHeap = {
      method = "collectgarbage_count", available = true,
      beforeKiB = beforeMemory, afterKiB = afterMemory, netKiB = afterMemory - beforeMemory,
    }
  end
  renderMetrics.luaHeap = ns.Util.Copy(lastLuaHeap)
end

local function setNativeState(phase, ordinal)
  local nextState
  if phase == "normal" then
    nextState = ns.AgentState.Transition(reducerState, "resume")
  else
    nextState = ns.AgentState.Measurement(reducerState, ordinal)
  end
  if not nextState then error("agent native state transition was refused") end
  reducerState = nextState
  nativePhase, nativeOrdinal = nextState.nativePhase, nextState.nativeOrdinal
  measurementCompleted = nextState.measurementCompleted
  renderMetrics.nativePhase, renderMetrics.nativeOrdinal = nativePhase, nativeOrdinal
end

function Agent.Publish()
  if hudHidden and not publishingForHudShow then error("agent_hud_hidden") end
  if not bootReady or not surface then error("agent_unconstructed") end
  renderMetrics.luaHeap = ns.Util.Copy(lastLuaHeap)
  renderMetrics.nativePhase, renderMetrics.nativeOrdinal = nativePhase, nativeOrdinal
  local beforeMemory = countLuaMemory()
  local result, errorText
  local ok, failure = pcall(function()
    local started = ns.AgentNative.ProfileStop()
    local snapshot = Agent.Snapshot()
    local json = ns.AgentNative.SerializeJSON(snapshot)
    local payload = ns.AgentNative.CompressString(json)
    local bytes = ns.AgentEncoder.Frame(json, payload, snapshot.sequence, snapshot.wallTime, snapshot.uptimeMs)
    updateSurface(bytes)
    local elapsedMs = ns.AgentNative.ProfileStop() - started
    local afterMemory = countLuaMemory()
    setLuaHeap(beforeMemory, afterMemory)
    renderMetrics.updateCount = renderMetrics.updateCount + 1
    updateSamples[((renderMetrics.updateCount - 1) % 100) + 1] = elapsedMs
    if renderMetrics.updateCount >= 100 then
      local ordered = {}
      for index = 1, 100 do ordered[index] = updateSamples[index] end
      table.sort(ordered)
      renderMetrics.updateP99Ms = ordered[math.max(1, math.ceil(#ordered * 0.99))]
    end
    result = ns.Util.Copy(snapshot)
  end)
  if not ok then errorText = failure end
  if errorText then error(errorText) end
  return result
end

-- The restore and finalize transitions have a publication boundary whose
-- result is part of the transaction.  Keep the existing best-effort publisher
-- for periodic telemetry, but make these two commit points observable.
local function publishChecked()
  return pcall(Agent.Publish)
end

function Agent.FrameBytes()
  if hudHidden then return nil end
  return frameBytes
end
function Agent.Surface() return surface end

function Agent.HudHidden() return hudHidden end

function Agent.HudStatus()
  return {
    hidden = hudHidden,
    available = surface ~= nil,
    visible = not hudHidden and surface ~= nil,
  }
end

local function nativeHudAdmission()
  if hudHidden then return false, "agent_hud_hidden" end
  return true
end

local function visibilityMutationBlocked()
  return not bootReady or lifecycle ~= "ordinary" or marker() ~= nil or
    nativePhase ~= "normal" or restoreInFlight
end

function Agent.SetHudHidden(hidden)
  if type(hidden) ~= "boolean" then return false, "hud_visibility_requires_boolean" end
  if not ns.db or type(ns.db.settings) ~= "table" then return false, "agent_not_loaded" end
  if visibilityMutationBlocked() then return false, "native_lifecycle_owns_surface" end

  if hidden then
    if hudHidden then
      frameBytes = nil
      elapsed = 0
      setSurfaceShown(false)
      ns.db.settings.hudHidden = true
      return true, "already_hidden"
    end
    hudHidden = true
    frameBytes = nil
    elapsed = 0
    setSurfaceShown(false)
    ns.db.settings.hudHidden = true
    return true, "hidden"
  end

  if not hudHidden then
    if not surface then return false, "agent_surface_unavailable" end
    return true, "already_visible"
  end

  if not surface then
    local created, createFailure = createSurface()
    if not created then return false, "surface_create_failed: " .. tostring(createFailure) end
    -- BuildSurface uses the same pinned native geometry as an ordinary boot.
    -- Keep the newly allocated cells off screen until fresh bytes are written.
    setSurfaceShown(false)
  end

  frameBytes = nil
  elapsed = 0
  publishingForHudShow = true
  local published, publication = pcall(Agent.Publish)
  publishingForHudShow = false
  if not published then
    clearSurface()
    hudHidden = true
    ns.db.settings.hudHidden = true
    return false, "publication_failed: " .. tostring(publication)
  end
  local shown, showFailure = pcall(function()
    setSurfaceShown(true)
    attachSurfaceScript()
  end)
  if not shown then
    clearSurface()
    hudHidden = true
    ns.db.settings.hudHidden = true
    return false, "surface_show_failed: " .. tostring(showFailure)
  end

  hudHidden = false
  ns.db.settings.hudHidden = false
  return true, "shown"
end

function Agent.ToggleHud()
  return Agent.SetHudHidden(not hudHidden)
end

function Agent.NativeState()
  return { phase = nativePhase, ordinal = nativeOrdinal }
end

function Agent.Boot() return boot end
function Agent.Lifecycle() return lifecycle end
function Agent.ReleaseAdmitted()
  return releaseAdmitted and ns.AgentNative.Uptime() < releaseDeadline
end
function Agent.RestorePending() return restoreInFlight end
function Agent.AdmitRelease()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  local current = marker()
  if type(current) ~= "table" or current.state ~= "deferred" or current.attempts ~= 0 then
    return false, "release_not_deferred"
  end
  if lifecycle ~= "deferred" or not ns.Scanner or not ns.Scanner.NativeConstructionFence or
      not ns.Scanner.NativeConstructionFence() then
    return false, "release_not_admitted"
  end
  releaseAdmitted, releaseDeadline = true, ns.AgentNative.Uptime() + 10
  return true
end
function Agent.RecoveryAdmitted() return recoveryAdmitted end
function Agent.BeginRecovery()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  -- The exact /crowley agent recover command is the addon-owned admission
  -- actor.  A future helper/daemon may add an external guard before invoking
  -- this public path, but there is no hidden manual admission step.
  local current = marker()
  if restoreInFlight or lifecycle == "restore_pending" then
    return false, "recovery_pending"
  end
  if current == nil and lifecycle ~= "finalized" then return false, "recovery_not_required" end
  recoveryAdmitted = true
  recoveryMode, releaseAdmitted = true, false
  adoptLifecycle("restore_only")
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
  if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
  return true
end
function Agent.RecoveryMode() return recoveryMode end

local function constructDeferred()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  local current = marker()
  if surface then return false, "agent_already_constructed" end
  if type(current) ~= "table" or current.state ~= "deferred" or current.attempts ~= 0 then
    return false, "construct_not_deferred"
  end
  if not Agent.ReleaseAdmitted() then return false, "construction_not_released" end
  current.state, current.attempts = "constructing", 1
  local saved, reason = setMarker(current)
  if not saved then return false, reason end
  transitionLifecycle("release")
  local ok, failure = createSurface()
  if not ok then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    transitionLifecycle("failed")
    releaseAdmitted = false
    return false, "constructor_error"
  end
  -- The reducer remains in constructing until the publication commit below.
  attachSurfaceScript()
  local published, publicationFailure = pcall(Agent.Publish)
  if not published then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    transitionLifecycle("failed")
    releaseAdmitted = false
    return false, "publication_error"
  end
  current.state = "constructed"
  local committed, commitReason = setMarker(current)
  if not committed then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    transitionLifecycle("failed")
    releaseAdmitted = false
    return false, commitReason
  end
  transitionLifecycle("published")
  releaseAdmitted = false
  if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
  return true
end

function Agent.Construct()
  return constructDeferred()
end

function Agent.Restore()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  if not recoveryAdmitted then return false, "recovery_not_admitted" end
  if not ns.db then return false, "agent_not_loaded" end
  if not ns.AgentNative.ReloadAvailable() then return false, "reload_unavailable" end
  local token = ns.Util.NewHexID("recovery:", 24)
  local saved, reason = setMarker({ schema = 1, state = "restore_pending", token = token, attempts = 0 })
  if not saved then return false, reason end
  releaseAdmitted, recoveryAdmitted = false, false
  restoreInFlight = true
  restoreAttempt = restoreAttempt + 1
  local attempt = restoreAttempt
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
  transitionLifecycle("restore")
  ns.AgentNative.Reload()
  local restoreLoad = loadId
  ns.AgentNative.After(2, function()
    local current = marker()
    if type(current) == "table" and current.state == "restore_pending" and current.token == token and
        bootReady and loadId == restoreLoad and restoreAttempt == attempt then
      recoveryAdmitted = true
      recoveryMode = true
      adoptLifecycle("restore_only")
      restoreInFlight = false
      if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
    end
  end)
  return true
end

function Agent.Finalize()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  local current = marker()
  if type(current) ~= "table" or current.state ~= "constructed" or current.attempts ~= 1 then
    return false, "finalize_not_constructed"
  end
  if not measurementCompleted or nativePhase ~= "normal" or nativeOrdinal ~= 0 then return false, "finalize_requires_resume" end
  if ns.Scanner and ns.Scanner.InProgress and ns.Scanner.InProgress() then return false, "finalize_scan_active" end
  if type(boot) ~= "string" or boot:sub(1, 20) ~= "native-construction:" then return false, "finalize_boot_mismatch" end
  if current.token ~= boot:sub(21) then return false, "finalize_boot_mismatch" end
  if ns.Scanner and ns.Scanner.LogoutFenceActive and ns.Scanner.LogoutFenceActive() then
    return false, "finalize_logout_fence_active"
  end
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
  local published = publishChecked()
  if not published then
    if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
    -- The reducer remains constructed while the publication transaction is retried.
    if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
    return false, "publication_error"
  end
  ns.db.nativeConstruction = nil
  transitionLifecycle("finalize")
  if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
  return true
end

local function scheduleMeasurementStep(token)
  ns.AgentNative.After(1, function()
    if token ~= measurementToken or nativePhase ~= "updates" then return end
    local nextOrdinal = nativeOrdinal + 1
    if nextOrdinal >= 100 then
      setNativeState("held", 100)
      safePublish()
      return
    end
    setNativeState("updates", nextOrdinal)
    safePublish()
    scheduleMeasurementStep(token)
  end)
end

function Agent.Hold()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  if not surface then return false, "agent_unconstructed" end
  if nativePhase ~= "normal" then return false, "agent_not_normal" end
  measurementToken = measurementToken + 1
  elapsed = 0
  setNativeState("held", 0)
  safePublish()
  return true
end

function Agent.Measure()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  if not surface then return false, "agent_unconstructed" end
  if nativePhase ~= "held" or nativeOrdinal ~= 0 then return false, "agent_measure_requires_held0" end
  measurementToken = measurementToken + 1
  local token = measurementToken
  setNativeState("updates", 1)
  safePublish()
  scheduleMeasurementStep(token)
  return true
end

function Agent.Resume()
  local admitted, reason = nativeHudAdmission()
  if not admitted then return false, reason end
  if not surface then return false, "agent_unconstructed" end
  if nativePhase ~= "held" then return false, "agent_resume_requires_held" end
  local completed = nativeOrdinal == 100
  measurementToken = measurementToken + 1
  elapsed = 0
  setNativeState("normal", 0)
  measurementCompleted = completed
  safePublish()
  return true
end

safePublish = function()
  local ok, errorText = pcall(Agent.Publish)
  if ok then
    lastPublishError = nil
  elseif ns.Print then
    local text = tostring(errorText)
    if text ~= lastPublishError then
      lastPublishError = text
      ns.Print("agent telemetry blocked: " .. text)
    end
  end
end

attachSurfaceScript = function()
  if not surface then return end
  ns.AgentRendering.Attach(surface, function(_, delta)
    if hudHidden or nativePhase ~= "normal" then return end
    elapsed = elapsed + (delta or 0)
    if elapsed >= 1 then elapsed = 0 safePublish() end
  end)
end

local function restoreOwnerActive(owner, expectedLoad, expectedToken)
  local current = marker()
  return restoreInFlight and lifecycle == "restore_pending" and bootReady and
    restoreReadinessOwner == owner and loadId == expectedLoad and type(current) == "table" and
    current.state == "restore_pending" and current.token == expectedToken
end

local function failRestoreReadiness(owner, expectedLoad, expectedToken, reason)
  if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
  restoreReadinessOwner = restoreReadinessOwner + 1
  clearSurface()
  local failed = marker()
  if type(failed) == "table" then
    failed.state, failed.attempts = "failed", 1
    setMarker(failed)
  end
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
  transitionLifecycle("failed")
  restoreInFlight = false
  recoveryAdmitted, recoveryMode = false, false
  local failure = "world readiness: " .. tostring(reason)
  ns.db.agentRestoreFailure = failure
  if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
  if ns.Print then ns.Print("agent restore failed: " .. failure) end
end

local function restoreCommit()
  restoreReadinessOwner = restoreReadinessOwner + 1
  local ok, failure = createSurface()
  if ok then
    attachSurfaceScript()
    -- The pending marker and armed fence remain in force through the first
    -- proof.  A fresh ordinary frame is then published with the fence
    -- released before the marker is committed away.
    local firstPublished, firstFailure = publishChecked()
    if not firstPublished then
      clearSurface()
      local failed = marker()
      if type(failed) == "table" then
        failed.state, failed.attempts = "failed", 1
        setMarker(failed)
      end
      transitionLifecycle("failed")
      restoreInFlight = false
      if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
      ns.db.agentRestoreFailure = "first publication: " .. tostring(firstFailure)
      if ns.Print then ns.Print("agent restore failed: first publication: " .. tostring(firstFailure)) end
    else
      if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
      transitionLifecycle("ready")
      local secondPublished, secondFailure = publishChecked()
      if secondPublished then
        ns.db.nativeConstruction = nil
        restoreInFlight = false
        if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
        if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
      else
        -- A failed unarmed proof must leave a recoverable, nonordinary boot.
        -- Clear every frame-owned object before rearming the fence so a
        -- partially written surface cannot be mistaken for acceptance.
        clearSurface()
        local failed = marker()
        if type(failed) == "table" then
          failed.state, failed.attempts = "failed", 1
          setMarker(failed)
        end
        if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
        transitionLifecycle("failed")
        restoreInFlight = false
        recoveryAdmitted, recoveryMode = false, false
        if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
        ns.db.agentRestoreFailure = "second publication: " .. tostring(secondFailure)
        if ns.Print then ns.Print("agent restore failed: second publication: " .. tostring(secondFailure)) end
      end
    end
  else
    local failed = marker()
    if type(failed) == "table" then
      failed.state, failed.attempts = "failed", 1
      setMarker(failed)
    end
    transitionLifecycle("failed")
    restoreInFlight = false
    if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
    ns.db.agentRestoreFailure = "createSurface: " .. tostring(failure)
    if ns.Print then ns.Print("agent restore failed: createSurface: " .. tostring(failure)) end
  end
end

local probeRestoreReadiness

local function scheduleRestorePoll(owner, expectedLoad, expectedToken)
  if restoreReadinessPollScheduled then return end
  restoreReadinessPollScheduled = true
  ns.AgentNative.After(RESTORE_READINESS_POLL, function()
    if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
    restoreReadinessPollScheduled = false
    probeRestoreReadiness(owner, expectedLoad, expectedToken)
  end)
end

probeRestoreReadiness = function(owner, expectedLoad, expectedToken)
  if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
  if ns.AgentNative.Uptime() >= restoreReadinessDeadline then
    local reason = restoreReadinessWorldSeen and
      (restoreReadinessFailure or "restore readiness deadline exceeded before native probe") or
      "missing PLAYER_ENTERING_WORLD"
    failRestoreReadiness(owner, expectedLoad, expectedToken, reason)
    return
  end
  if not restoreReadinessWorldSeen then return end
  local ready, failure = pcall(movementTelemetry)
  if ready then
    restoreCommit()
    return
  end
  restoreReadinessFailure = tostring(failure)
  scheduleRestorePoll(owner, expectedLoad, expectedToken)
end

local function initializeLoad()
  local decision = type(ns.NativeDecision) == "function" and ns.NativeDecision() or
    { kind = "unknown", state = "unknown", marker = nil }
  loadId = loadId + 1
  restoreReadinessOwner = restoreReadinessOwner + 1
  restoreReadinessPollScheduled = false
  sequence, measurementCompleted, releaseAdmitted = 0, false, false
  recoveryAdmitted, recoveryMode = false, false
  restoreInFlight = false
  bootReady = false
  clearSurface()
  local state = decision.kind
  if type(ns.db.settings) ~= "table" then
    adoptLifecycle("restore_only")
    if ns.Print then ns.Print("agent telemetry unavailable: invalid settings table") end
    return
  end
  if ns.db.settings.hudHidden == nil then ns.db.settings.hudHidden = false end
  if type(ns.db.settings.hudHidden) ~= "boolean" then
    adoptLifecycle("restore_only")
    if ns.Print then ns.Print("agent telemetry unavailable: invalid HUD visibility preference") end
    return
  end
  if state == "ordinary" then
    boot = randomBoot()
    bootReady = true
    adoptLifecycle("ordinary")
    hudHidden = ns.db.settings.hudHidden
    if hudHidden then return end
    local ok, failure = createSurface()
    if not ok then
      adoptLifecycle("restore_only")
      if ns.Print then ns.Print("agent telemetry unavailable: " .. tostring(failure)) end
      return
    end
    attachSurfaceScript()
    -- Ordinary startup keeps its surface and periodic retry semantics.  A
    -- transient telemetry failure is diagnostic, while allocation failure
    -- above remains a loud restore-only startup refusal.
    safePublish()
    return
  end
  -- An interrupted native lifecycle owns a visible publication surface so its
  -- existing recovery path can complete. The saved display preference remains
  -- intact and applies again after the next ordinary load.
  hudHidden = false
  local current = marker()
  if type(current) == "table" and type(current.token) == "string" and current.state ~= "restore_pending" then
    boot = nativeBoot(current.token)
  else
    boot = randomBoot()
  end
  bootReady = true
  if state == "ordinary" then
    adoptLifecycle("ordinary")
  elseif state == "deferred" then
    adoptLifecycle("deferred")
  elseif state == "constructing" then
    adoptLifecycle("constructing")
  elseif state == "failed" then
    adoptLifecycle("failed")
  elseif state == "restore_pending" then
    adoptLifecycle("restore_pending")
  elseif state == "constructed" then
    adoptLifecycle("restore_only")
  else
    adoptLifecycle("restore_only")
  end
  if state == "constructed" then
    -- A persisted successful attempt is never replayed on load. It remains
    -- recoverable through Restore until a fresh ordinary boot is proven.
  elseif state == "restore_pending" then
    ns.db.agentRestoreFailure = nil
    restoreInFlight = true
    restoreReadinessWorldSeen = false
    restoreReadinessFailure = nil
    restoreReadinessDeadline = ns.AgentNative.Uptime() + RESTORE_READINESS_TIMEOUT
    restoreReadinessToken = current.token
    restoreReadinessLoad = loadId
    local owner, expectedLoad, expectedToken = restoreReadinessOwner, restoreReadinessLoad, restoreReadinessToken
    ns.AgentNative.After(RESTORE_READINESS_TIMEOUT, function()
      if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
      local reason = restoreReadinessWorldSeen and
        (restoreReadinessFailure or "restore readiness deadline exceeded before native probe") or
        "missing PLAYER_ENTERING_WORLD"
      failRestoreReadiness(owner, expectedLoad, expectedToken, reason)
    end)
  end
end

ns.RegisterModule("Agent", {
  requires = { "Core", "Scanner", "Executor", "AgentState", "AgentEncoder", "AgentRendering", "AgentNative" },
  onLoad = function()
    if loadInitialized then return end
    loadInitialized = true
    ns.db.agentLastSaved = ns.db.agentLastSaved or {
      memoryUid = "", finishedAt = 0, planned = 0, attempted = 0, failures = 0, rows = 0,
    }
    initializeLoad()
  end,
})

ns.AgentNative.On("PLAYER_ENTERING_WORLD", function()
  if not restoreInFlight or lifecycle ~= "restore_pending" then return end
  local owner, expectedLoad, expectedToken = restoreReadinessOwner, restoreReadinessLoad, restoreReadinessToken
  if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
  restoreReadinessWorldSeen = true
  probeRestoreReadiness(owner, expectedLoad, expectedToken)
end)
