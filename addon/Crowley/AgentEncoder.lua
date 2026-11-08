local _, ns = ...

-- Pure fixed-capacity HUD wire encoder.  No native serializer, frame, or WoW
-- namespace is used here; Agent.lua supplies the encoded JSON and payload.
local Encoder = {}
ns.AgentEncoder = Encoder

local CAPACITY = 2048
local WIDTH, HEIGHT = 128, 128

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

function Encoder.Frame(json, payload, sequence, wallTime, uptimeMs)
  if type(json) ~= "string" or type(payload) ~= "string" or type(sequence) ~= "number" or
      type(wallTime) ~= "number" or type(uptimeMs) ~= "number" then error("agent encoder input is malformed") end
  if #payload < 1 or #payload > 2000 then error("Crowley telemetry payload exceeds 2000 bytes") end
  local header = "CRS1" .. string.char(1, 1, WIDTH, HEIGHT) .. string.char(#payload % 256, math.floor(#payload / 256)) ..
    u32(sequence) .. u32(wallTime) .. u32(uptimeMs)
  local checksum = u32(crc32(header .. payload))
  local body = header .. checksum .. payload
  if #body > CAPACITY then error("Crowley telemetry frame exceeds fixed capacity") end
  return body .. string.rep(string.char(0), CAPACITY - #body)
end

ns.RegisterModule("AgentEncoder", { requires = { "Core" }, onLoad = function() end })
