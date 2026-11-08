local _, ns = ...

-- SavedVariables encoder.  Serialization, compression and base64 are explicit
-- capabilities supplied by the native adapter; this module owns only payload
-- and envelope shape.
local Encoder = {}
ns.ScanEncoder = Encoder

local function exactKeys(value, allowed)
  if type(value) ~= "table" or type(allowed) ~= "table" then return false end
  for key in pairs(value) do if not allowed[key] then return false end end
  for key in pairs(allowed) do if value[key] == nil then return false end end
  return true
end

function Encoder.Payload(sweep)
  if type(sweep) ~= "table" then error("scan encoder sweep is malformed") end
  local payload = { planHash = sweep.planHash, planJSON = sweep.planJSON, observations = sweep.observations, owned = sweep.owned }
  if not ns.GeneratedWireSchemas or not exactKeys(payload, ns.GeneratedWireSchemas.scanPayload) then
    error("scan encoder payload does not match generated wire schema")
  end
  return payload
end

function Encoder.Envelope(sweep, id, finishedAt, rowCount, observationCount, encodedPayload)
  if type(sweep) ~= "table" or type(id) ~= "string" or type(finishedAt) ~= "number" or
      type(encodedPayload) ~= "string" then error("scan encoder envelope is malformed") end
  local envelope = {
    id = id, format = 3, house = sweep.house, context = sweep.context, startedAt = sweep.startedAt,
    finishedAt = finishedAt, numRows = rowCount, numObservations = observationCount, payload = encodedPayload,
  }
  if not ns.GeneratedWireSchemas or not exactKeys(envelope, ns.GeneratedWireSchemas.savedScanEnvelope) then
    error("scan encoder envelope does not match generated wire schema")
  end
  return envelope
end

function Encoder.Encode(payload, serialize, compress, base64, compressionMethod)
  if type(payload) ~= "table" or type(serialize) ~= "function" or type(compress) ~= "function" or
      type(base64) ~= "function" then error("scan encoder capabilities are unavailable") end
  local compressed = compress(serialize(payload), compressionMethod)
  return base64(compressed)
end

function Encoder.Decode(source, deserialize)
  if type(source) ~= "string" or type(deserialize) ~= "function" then error("scan decoder capabilities are unavailable") end
  return pcall(deserialize, source)
end

ns.RegisterModule("ScanEncoder", { requires = { "Core", "GeneratedWireSchemas" }, onLoad = function() end })
