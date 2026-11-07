local _, ns = ...

-- Lossless, read-only telemetry surface. It is deliberately independent of the
-- scanner's command path: publishing cannot start, stop, or mutate a sweep.
local Agent = {}
ns.Agent = Agent

local WIDTH, HEIGHT, CAPACITY = 128, 128, 2048
local BYTES_PER_ROW = WIDTH / 8
local GLYPH_WIDTH, GLYPH_HEIGHT = 16, 2
local ATLAS_HEIGHT = 512
local PRESENTATION_WIDTH, PRESENTATION_HEIGHT = 1920, 1080
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

local function marker()
  return ns.db and ns.db.nativeConstruction
end

local function randomBoot()
  return ns.Util.NewID({ time() }, 100000, 999999)
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
  if surface then
    if surface.SetScript then surface:SetScript("OnUpdate", nil) end
    surface:Hide()
    for _, texture in pairs(cells) do texture:Hide() end
  end
  surface, cells, frameBytes = nil, {}, nil
end

local function setSurfaceShown(visible)
  if not surface then return end
  if visible then
    for _, texture in pairs(cells) do texture:Show() end
    surface:Show()
  else
    if surface.SetScript then surface:SetScript("OnUpdate", nil) end
    surface:Hide()
    for _, texture in pairs(cells) do texture:Hide() end
  end
end

local function u32(value)
  value = value % 4294967296
  return string.char(value % 256, math.floor(value / 256) % 256, math.floor(value / 65536) % 256, math.floor(value / 16777216) % 256)
end

local function xor32(left, right)
  local result, bit = 0, 1
  while bit <= 2147483648 do
    local l = left % (bit * 2) >= bit
    local r = right % (bit * 2) >= bit
    if l ~= r then result = result + bit end
    bit = bit * 2
  end
  return result
end

local function crc32(bytes)
  local crc = 4294967295
  for index = 1, #bytes do
    crc = xor32(crc, string.byte(bytes, index))
    for _ = 1, 8 do
      local odd = crc % 2
      crc = math.floor(crc / 2)
      if odd == 1 then crc = xor32(crc, 3988292384) end
    end
  end
  return xor32(crc, 4294967295) % 4294967296
end

local function sortedKeys(tableValue)
  local result = {}
  for key in pairs(tableValue or {}) do result[#result + 1] = key end
  table.sort(result)
  return result
end

local function activeEdit()
  if not ChatFrameUtil or type(ChatFrameUtil.GetActiveWindow) ~= "function" then
    return { active = false, focused = false, text = "" }
  end
  local edit = ChatFrameUtil.GetActiveWindow()
  if not edit then return { active = false, focused = false, text = "" } end
  -- The readback is a proof of the actual active EditBox.  Missing methods
  -- are an unavailable proof, never an implicit ``shown`` value.
  if type(edit.IsShown) ~= "function" or type(edit.HasFocus) ~= "function" or
      type(edit.GetText) ~= "function" then
    return { active = false, focused = false, text = "" }
  end
  local shown = edit:IsShown() == true
  local focused = edit:HasFocus() == true
  local text = edit:GetText()
  if type(text) ~= "string" or #text > 128 then
    -- Unbounded or malformed chat text cannot enter the telemetry frame and
    -- must force the executor to cancel before it can press Enter.
    return { active = false, focused = false, text = "" }
  end
  return { active = shown, focused = focused, text = text }
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
  if type(C_CVar) ~= "table" or type(C_CVar.GetCVarBool) ~= "function" then return result end
  local ok, value = pcall(C_CVar.GetCVarBool, "autointeract")
  if not ok then
    result.reason = nativeFailure("call_error")
  elseif value == nil then
    result.reason = nativeFailure("nil_result")
  elseif type(value) ~= "boolean" then
    result.reason = nativeFailure("wrong_type")
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
    local fn = _G[accessor]
    if type(fn) ~= "function" then
      values[field] = "unknown"
      markFailure(field, "missing")
    else
      local ok, value
      if playerArgument then ok, value = pcall(fn, "player") else ok, value = pcall(fn) end
      if not ok then
        values[field] = "unknown"
        markFailure(field, "call_error")
      elseif value == nil then
        values[field] = "unknown"
        markFailure(field, "nil_result")
      elseif type(value) ~= "boolean" then
        values[field] = "unknown"
        markFailure(field, "wrong_type")
      else
        values[field] = value and "true" or "false"
        anyTrue = anyTrue or value
      end
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
  if type(GetBindingKey) ~= "function" then
    error("Crowley movement telemetry requires GetBindingKey")
  end
  -- GetBindingKey's first return is the primary binding.  Preserve the exact
  -- native spelling; normalization would hide a client contract mismatch.
  local value = GetBindingKey(action)
  if type(value) ~= "string" or #value < 1 or #value > 32 or
      value:find("[^A-Z0-9%-]") or value:sub(1, 1) == "-" or value:sub(-1) == "-" or
      value:find("%-%-") then
    error("Crowley movement telemetry has invalid " .. field .. " binding")
  end
  return value
end

local function hoverTelemetry()
  if type(UnitExists) ~= "function" then
    error("Crowley hover telemetry requires UnitExists")
  end
  if type(UnitName) ~= "function" then
    error("Crowley hover telemetry requires UnitName")
  end
  if not GameTooltip or type(GameTooltip.IsShown) ~= "function" or
      type(GameTooltip.GetOwner) ~= "function" then
    error("Crowley hover telemetry requires GameTooltip")
  end
  if not GameTooltipTextLeft1 or type(GameTooltipTextLeft1.GetText) ~= "function" then
    error("Crowley hover telemetry requires GameTooltipTextLeft1")
  end
  if not UIParent then error("Crowley hover telemetry requires UIParent") end

  local kind, name = "none", ""
  if UnitExists("mouseover") then
    kind, name = "unit", UnitName("mouseover")
  elseif GameTooltip:IsShown() and GameTooltip:GetOwner() == UIParent then
    local objectName = GameTooltipTextLeft1:GetText()
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
  if not MailFrame or type(MailFrame.IsShown) ~= "function" then
    error("Crowley mail telemetry requires MailFrame")
  end
  return MailFrame:IsShown() == true
end

local function executorTelemetry()
  if not ns.Executor or type(ns.Executor.Telemetry) ~= "function" then
    error("Crowley executor telemetry requires Executor")
  end
  return ns.Executor.Telemetry()
end

local function movementTelemetry()
  if type(UnitPosition) ~= "function" then error("Crowley movement telemetry requires UnitPosition") end
  if type(GetPlayerFacing) ~= "function" then error("Crowley movement telemetry requires GetPlayerFacing") end
  if type(GetUnitSpeed) ~= "function" then error("Crowley movement telemetry requires GetUnitSpeed") end
  if type(UnitAffectingCombat) ~= "function" then error("Crowley movement telemetry requires UnitAffectingCombat") end
  if type(UnitIsDeadOrGhost) ~= "function" then error("Crowley movement telemetry requires UnitIsDeadOrGhost") end
  local x, y, z, instance = UnitPosition("player")
  if type(instance) ~= "number" or instance ~= math.floor(instance) or instance < 0 or instance > 4294967295 then
    error("Crowley movement telemetry has invalid position.instance")
  end
  local position = {
    x = finiteNumber(x, "position.x", -POSITION_LIMIT, POSITION_LIMIT),
    y = finiteNumber(y, "position.y", -POSITION_LIMIT, POSITION_LIMIT),
    z = finiteNumber(z, "position.z", -POSITION_LIMIT, POSITION_LIMIT),
    instance = instance,
  }
  local currentSpeed, runSpeed, flightSpeed, swimSpeed = GetUnitSpeed("player")
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
    facing = finiteNumber(GetPlayerFacing(), "facing", 0, FACING_LIMIT),
    speed = native.speedTuple.current,
    combat = nativeBoolean(UnitAffectingCombat("player"), "combat"),
    deadOrGhost = nativeBoolean(UnitIsDeadOrGhost("player"), "deadOrGhost"),
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
  local exists = UnitExists("target")
  if type(exists) ~= "boolean" then error("auctioneer_target_presence_unavailable") end
  local name, rangeAvailable, inTradeRange = "", false, false
  if exists then
    name = UnitName("target")
    if type(name) ~= "string" or name == "" then error("auctioneer_target_name_unavailable") end
    if type(CheckInteractDistance) == "function" then
      -- Blizzard's trade interaction uses distance index 2.  Index 1 is the
      -- shorter melee check and can reject a valid Auction House approach.
      local measured = CheckInteractDistance("target", 2)
      if type(measured) == "boolean" then
        rangeAvailable, inTradeRange = true, measured
      end
    end
  end
  local bindingAvailable, action = false, ""
  if type(GetBindingAction) == "function" then
    local measured = GetBindingAction("CTRL-F12")
    if type(measured) == "string" then
      bindingAvailable, action = true, measured
    end
  end
  local interactionMode = "unavailable"
  if type(GetCVar) == "function" then
    local ok, measured = pcall(GetCVar, "SoftTargetInteract")
    if ok and type(measured) == "string" then
      local modes = {
        ["0"] = "disabled", ["1"] = "gamepad", ["2"] = "keyboard", ["3"] = "any",
      }
      interactionMode = modes[measured] or "unavailable"
    end
  end
  return {
    targetPresent = exists, targetName = name,
    rangeAvailable = rangeAvailable, withinTradeRange = inTradeRange,
    bindingAvailable = bindingAvailable, ctrlF12Action = action,
    interactionMode = interactionMode,
  }
end

local function blockingUITelemetry()
  -- Normal world context does not prove that Return cannot activate a native
  -- popup. Publish explicit availability and visibility for both known
  -- blockers; consumers refuse input if either API is unavailable.
  local popupAvailable, popupShown = false, false
  if type(StaticPopup_IsAnyDialogShown) == "function" then
    local ok, measured = pcall(StaticPopup_IsAnyDialogShown)
    if ok and type(measured) == "boolean" then
      popupAvailable, popupShown = true, measured
    end
  end
  local menuAvailable, menuShown = false, false
  local menu = rawget(_G, "GameMenuFrame")
  if menu ~= nil and type(menu.IsShown) == "function" then
    local ok, measured = pcall(function() return menu:IsShown() end)
    if ok and type(measured) == "boolean" then
      menuAvailable, menuShown = true, measured
    end
  end
  return {
    staticPopupAvailable = popupAvailable, staticPopupShown = popupShown,
    gameMenuAvailable = menuAvailable, gameMenuShown = menuShown,
  }
end

function Agent.Snapshot()
  if not bootReady or not surface then error("agent_unconstructed") end
  local context = ns.Context.Snapshot()
  local house = ns.Context.House()
  local scanner = ns.Scanner.Status()
  local stored = scanner.pending or {}
  local acked = scanner.loaded or {}
  local last = scanner.lastSaved or {}
  local now = time()
  local uptime = math.floor((GetTime() or 0) * 1000) % 4294967296
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
  local started = debugprofilestop()
  if not UIParent or type(UIParent.GetWidth) ~= "function" or type(UIParent.GetHeight) ~= "function" or
      type(UIParent.GetEffectiveScale) ~= "function" then
    error("Crowley telemetry requires UIParent geometry")
  end
  local rootWidth = UIParent:GetWidth()
  local rootHeight = UIParent:GetHeight()
  local rootEffective = UIParent:GetEffectiveScale()
  if type(rootWidth) ~= "number" or type(rootHeight) ~= "number" or type(rootEffective) ~= "number" or
      rootWidth <= 0 or rootHeight <= 0 or rootEffective <= 0 or
      rootWidth ~= rootWidth or rootHeight ~= rootHeight or rootEffective ~= rootEffective or
      rootWidth == math.huge or rootHeight == math.huge or rootEffective == math.huge or
      rootWidth == -math.huge or rootHeight == -math.huge or rootEffective == -math.huge then
    error("Crowley telemetry has invalid UIParent geometry")
  end
  local aspect = rootWidth / rootHeight
  if math.abs(aspect - PRESENTATION_WIDTH / PRESENTATION_HEIGHT) > 0.01 then
    error("Crowley telemetry requires the pinned 1920x1080 UIParent aspect")
  end
  -- GetPhysicalScreenSize is stale on the verified Wine client.  The root's
  -- logical extent and effective scale are the only native inputs used here;
  -- the presentation profile is pinned by the runtime identity at 1920x1080.
  local uiUnitsPerNativePixel = rootHeight * rootEffective / PRESENTATION_HEIGHT
  if uiUnitsPerNativePixel <= 0 or uiUnitsPerNativePixel ~= uiUnitsPerNativePixel or
      uiUnitsPerNativePixel == math.huge or uiUnitsPerNativePixel == -math.huge then
    error("Crowley telemetry has invalid UI units per native pixel")
  end
  surface = CreateFrame("Frame", "CrowleyAgentSurface", UIParent)
  surface:SetIgnoreParentScale(true)
  surface:SetScale(uiUnitsPerNativePixel)
  surface:SetAlpha(1)
  surface:SetFrameStrata("HIGH")
  surface:EnableMouse(false)
  local effective = surface:GetEffectiveScale()
  if type(effective) ~= "number" or effective <= 0 or effective ~= effective or
      effective == math.huge or effective == -math.huge then
    error("Crowley telemetry surface has invalid effective scale")
  end
  surface:SetSize(CROP_X + WIDTH * 2, CROP_Y + HEIGHT * 2)
  surface:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
  if math.abs(effective - uiUnitsPerNativePixel) > 0.000001 then
    error("Crowley telemetry surface scale does not match the requested native factor")
  end
  for row = 0, HEIGHT - 1 do
    for column = 0, BYTES_PER_ROW - 1 do
      local texture = surface:CreateTexture(nil, "ARTWORK")
      texture:SetSnapToPixelGrid(true)
      texture:SetTexelSnappingBias(0.5)
      texture:SetSize(GLYPH_WIDTH, GLYPH_HEIGHT)
      texture:SetPoint(
        "TOPLEFT", surface, "TOPLEFT", CROP_X + column * GLYPH_WIDTH, -(CROP_Y + row * GLYPH_HEIGHT)
      )
      texture:SetTexture(ATLAS_PATH, "CLAMP", "CLAMP", "NEAREST")
      texture:SetTexCoord(0, 1, 0.5 / ATLAS_HEIGHT, 1.5 / ATLAS_HEIGHT)
      texture:Show()
      cells[row * BYTES_PER_ROW + column + 1] = texture
    end
  end
  surface:Show()
  renderMetrics.constructionSeconds = (debugprofilestop() - started) / 1000
  renderMetrics.textureCount = CAPACITY
  renderMetrics.effectiveScale = effective
  renderMetrics.cellSize = { 2, 2 }
end

local function createSurface()
  clearSurface()
  local ok, reason = pcall(buildSurface)
  if ok then return true end
  clearSurface()
  return false, reason
end

local function setGlyph(texture, value)
  local top = (value * GLYPH_HEIGHT + 0.5) / ATLAS_HEIGHT
  local bottom = (value * GLYPH_HEIGHT + GLYPH_HEIGHT - 0.5) / ATLAS_HEIGHT
  texture:SetTexCoord(0, 1, top, bottom)
end

local function updateSurface(bytes)
  frameBytes = bytes
  for row = 0, HEIGHT - 1 do
    for column = 0, BYTES_PER_ROW - 1 do
      local byte = string.byte(bytes, row * BYTES_PER_ROW + column + 1)
      if byte == nil then error("Crowley telemetry frame is shorter than its fixed capacity") end
      setGlyph(cells[row * BYTES_PER_ROW + column + 1], byte)
    end
  end
end

local function countLuaMemory()
  local ok, value = pcall(function() return collectgarbage("count") end)
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
  nativePhase, nativeOrdinal = phase, ordinal
  renderMetrics.nativePhase, renderMetrics.nativeOrdinal = phase, ordinal
end

function Agent.Publish()
  if hudHidden and not publishingForHudShow then error("agent_hud_hidden") end
  if not bootReady or not surface then error("agent_unconstructed") end
  renderMetrics.luaHeap = ns.Util.Copy(lastLuaHeap)
  renderMetrics.nativePhase, renderMetrics.nativeOrdinal = nativePhase, nativeOrdinal
  local beforeMemory = countLuaMemory()
  local result, errorText
  local ok, failure = pcall(function()
    local started = debugprofilestop()
    local snapshot = Agent.Snapshot()
    local json = C_EncodingUtil.SerializeJSON(snapshot)
    local payload = C_EncodingUtil.CompressString(json, Enum.CompressionMethod.Deflate)
    if #payload < 1 or #payload > 2000 then error("Crowley telemetry payload exceeds 2000 bytes") end
    local header = "CRS1" .. string.char(1, 1, WIDTH, HEIGHT) .. string.char(#payload % 256, math.floor(#payload / 256)) ..
      u32(snapshot.sequence) .. u32(snapshot.wallTime) .. u32(snapshot.uptimeMs)
    local checksum = u32(crc32(header .. payload))
    local bytes = header .. checksum .. payload .. string.rep(string.char(0), CAPACITY - #header - #checksum - #payload)
    updateSurface(bytes)
    local elapsedMs = debugprofilestop() - started
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
  return releaseAdmitted and GetTime() < releaseDeadline
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
  releaseAdmitted, releaseDeadline = true, GetTime() + 10
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
  recoveryMode, releaseAdmitted, lifecycle = true, false, "restore_only"
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
  lifecycle = "constructing"
  local ok, failure = createSurface()
  if not ok then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    lifecycle = "failed"
    releaseAdmitted = false
    return false, "constructor_error"
  end
  lifecycle = "constructing"
  attachSurfaceScript()
  local published, publicationFailure = pcall(Agent.Publish)
  if not published then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    lifecycle = "failed"
    releaseAdmitted = false
    return false, "publication_error"
  end
  current.state = "constructed"
  local committed, commitReason = setMarker(current)
  if not committed then
    clearSurface()
    current.state = "failed"
    setMarker(current)
    lifecycle = "failed"
    releaseAdmitted = false
    return false, commitReason
  end
  lifecycle = "constructed"
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
  if type(ReloadUI) ~= "function" then return false, "reload_unavailable" end
  local token = ns.Util.NewHexID("recovery:", 24)
  local saved, reason = setMarker({ schema = 1, state = "restore_pending", token = token, attempts = 0 })
  if not saved then return false, reason end
  releaseAdmitted, recoveryAdmitted = false, false
  restoreInFlight = true
  restoreAttempt = restoreAttempt + 1
  local attempt = restoreAttempt
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(true) end
  lifecycle = "restore_pending"
  ReloadUI()
  local restoreLoad = loadId
  C_Timer.After(2, function()
    local current = marker()
    if type(current) == "table" and current.state == "restore_pending" and current.token == token and
        bootReady and loadId == restoreLoad and restoreAttempt == attempt then
      recoveryAdmitted = true
      recoveryMode = true
      lifecycle = "restore_only"
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
    lifecycle = "constructed"
    if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
    return false, "publication_error"
  end
  ns.db.nativeConstruction = nil
  lifecycle = "finalized"
  if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
  if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
  return true
end

local function scheduleMeasurementStep(token)
  C_Timer.After(1, function()
    if token ~= measurementToken or nativePhase ~= "updates" then return end
    nativeOrdinal = nativeOrdinal + 1
    if nativeOrdinal >= 100 then
      setNativeState("held", 100)
      safePublish()
      return
    end
    setNativeState("updates", nativeOrdinal)
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
  nativeOrdinal = 1
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
  surface:SetScript("OnUpdate", function(_, delta)
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
  lifecycle = "failed"
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
      lifecycle = "failed"
      restoreInFlight = false
      if ns.Commands and ns.Commands.Refresh then ns.Commands.Refresh() end
      ns.db.agentRestoreFailure = "first publication: " .. tostring(firstFailure)
      if ns.Print then ns.Print("agent restore failed: first publication: " .. tostring(firstFailure)) end
    else
      if ns.Scanner and ns.Scanner.SetNativeConstructionFence then ns.Scanner.SetNativeConstructionFence(false) end
      lifecycle = "ordinary"
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
        lifecycle = "failed"
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
    lifecycle = "failed"
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
  C_Timer.After(RESTORE_READINESS_POLL, function()
    if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
    restoreReadinessPollScheduled = false
    probeRestoreReadiness(owner, expectedLoad, expectedToken)
  end)
end

probeRestoreReadiness = function(owner, expectedLoad, expectedToken)
  if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
  if GetTime() >= restoreReadinessDeadline then
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
    lifecycle = "restore_only"
    if ns.Print then ns.Print("agent telemetry unavailable: invalid settings table") end
    return
  end
  if ns.db.settings.hudHidden == nil then ns.db.settings.hudHidden = false end
  if type(ns.db.settings.hudHidden) ~= "boolean" then
    lifecycle = "restore_only"
    if ns.Print then ns.Print("agent telemetry unavailable: invalid HUD visibility preference") end
    return
  end
  if state == "ordinary" then
    boot = randomBoot()
    bootReady, lifecycle = true, "ordinary"
    hudHidden = ns.db.settings.hudHidden
    if hudHidden then return end
    local ok, failure = createSurface()
    if not ok then
      lifecycle = "restore_only"
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
  lifecycle = state
  if state == "constructed" then
    -- A persisted successful attempt is never replayed on load. It remains
    -- recoverable through Restore until a fresh ordinary boot is proven.
    lifecycle = "restore_only"
  elseif state == "restore_pending" then
    ns.db.agentRestoreFailure = nil
    restoreInFlight = true
    restoreReadinessWorldSeen = false
    restoreReadinessFailure = nil
    restoreReadinessDeadline = GetTime() + RESTORE_READINESS_TIMEOUT
    restoreReadinessToken = current.token
    restoreReadinessLoad = loadId
    local owner, expectedLoad, expectedToken = restoreReadinessOwner, restoreReadinessLoad, restoreReadinessToken
    C_Timer.After(RESTORE_READINESS_TIMEOUT, function()
      if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
      local reason = restoreReadinessWorldSeen and
        (restoreReadinessFailure or "restore readiness deadline exceeded before native probe") or
        "missing PLAYER_ENTERING_WORLD"
      failRestoreReadiness(owner, expectedLoad, expectedToken, reason)
    end)
  end
end

ns.RegisterModule("Agent", {
  requires = { "Core", "Scanner", "Executor" },
  onLoad = function()
    if loadInitialized then return end
    loadInitialized = true
    ns.db.agentLastSaved = ns.db.agentLastSaved or {
      memoryUid = "", finishedAt = 0, planned = 0, attempted = 0, failures = 0, rows = 0,
    }
    initializeLoad()
  end,
})

ns.On("PLAYER_ENTERING_WORLD", function()
  if not restoreInFlight or lifecycle ~= "restore_pending" then return end
  local owner, expectedLoad, expectedToken = restoreReadinessOwner, restoreReadinessLoad, restoreReadinessToken
  if not restoreOwnerActive(owner, expectedLoad, expectedToken) then return end
  restoreReadinessWorldSeen = true
  probeRestoreReadiness(owner, expectedLoad, expectedToken)
end)
