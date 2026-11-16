local _, ns = ...

-- Scanner-only read-only publication. It uses no game-control module and is
-- useful when Crowley_Automation is not installed.
local WIDTH, HEIGHT, CAPACITY = 128, 128, 2048
local CROP_X, CROP_Y = 80, 48
local ATLAS_PATH = "Interface\\AddOns\\Crowley\\Textures\\ByteAtlas"
local sequence = 0
local boot = ns.Util.NewHexID("scanner-", 16)
local surface, cells
local elapsed = 1
local frameBytes

local function copyArray(values)
  local result = {}
  for index, value in ipairs(values or {}) do result[index] = value end
  return result
end

local function snapshot()
  local now = time()
  local uptime = math.floor(GetTime() * 1000) % 4294967296
  local status = ns.Scanner.Status()
  local context = ns.Context.Snapshot()
  local house = ns.Context.House()
  local plan = status.plan or {}
  local last = status.lastSaved or {}
  local pending = status.pending or {}
  local loaded = status.loaded or {}
  sequence = sequence + 1
  return {
    schema = 1,
    kind = "scanner",
    boot = boot,
    sequence = sequence,
    wallTime = now,
    uptimeMs = uptime,
    addonVersion = context.addonVersion or ns.VERSION,
    house = { key = house.key, source = house.source },
    plan = {
      hash = plan.hash or "", count = plan.count or 0,
      cadenceSeconds = plan.cadenceSeconds or 0,
      sweepCapSeconds = plan.sweepCapSeconds or 0,
      initialPriceLevels = plan.initialPriceLevels or 0,
    },
    scanner = {
      phase = status.phase,
      startedAt = status.startedAt or 0,
      dueAt = status.dueAt or 0,
      ordinal = status.ordinal or 0,
      planned = status.planned or 0,
      cursorItemID = status.cursorItemID or 0,
      reason = status.reason or "",
    },
    lastSaved = {
      memoryUid = last.memoryUid or "", finishedAt = last.finishedAt or 0,
      planned = last.planned or 0, attempted = last.attempted or 0,
      failures = last.failures or 0, rows = last.rows or 0,
    },
    pending = {
      ids = copyArray(pending), count = #pending, cap = ns.MAX_STORED_SCANS,
      blocked = status.phase == "blocked", reason = status.reason or "",
    },
    loaded = {
      ackedIds = copyArray(loaded), generatedAt = CROWLEY_DATA.generatedAt or 0,
    },
  }
end

local function publish()
  local value = snapshot()
  local json = ns.ScannerNative.SerializeJSON(value)
  local compressed = ns.ScannerNative.CompressString(json)
  local bytes = ns.ScanFrameEncoder.Frame(json, compressed, value.sequence, value.wallTime, value.uptimeMs)
  ns.ScanRendering.Update(cells, bytes, WIDTH, HEIGHT)
  frameBytes = bytes
end

local function safePublish()
  pcall(publish)
end

local function attach()
  surface:SetScript("OnUpdate", function(_, delta)
    elapsed = elapsed + delta
    if elapsed >= 1 then
      elapsed = 0
      safePublish()
    end
  end)
end

local function start()
  surface, cells = ns.ScanRendering.Create(WIDTH, HEIGHT, CROP_X, CROP_Y, ATLAS_PATH, "CrowleyScanSurface")
  attach()
  safePublish()
end

function ns.ScanStatusSnapshot()
  return snapshot()
end

function ns.ScanStatusFrameBytes()
  return frameBytes
end

ns.ScanStatus = { Initialize = start }
