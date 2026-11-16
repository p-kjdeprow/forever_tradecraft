local _, ns = ...

local Util = {}
ns.Util = Util

local MAX_SAFE_INTEGER = 9007199254740991

function Util.Copy(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for key, child in pairs(value) do result[key] = Util.Copy(child) end
  return result
end

function Util.ExactKeys(value, allowed)
  if type(value) ~= "table" or type(allowed) ~= "table" then return false end
  for key in pairs(value) do if not allowed[key] then return false end end
  for key in pairs(allowed) do if value[key] == nil then return false end end
  return true
end

function Util.IsFiniteNumber(value, minimum, maximum)
  if type(value) ~= "number" or value ~= value or value == math.huge or value == -math.huge then return false end
  if minimum ~= nil and value < minimum then return false end
  if maximum ~= nil and value > maximum then return false end
  return true
end

function Util.IsInteger(value, minimum, maximum)
  return Util.IsFiniteNumber(value, minimum, maximum) and value == math.floor(value)
end

function Util.IsString(value, minimumLength, maximumLength)
  if type(value) ~= "string" then return false end
  if minimumLength ~= nil and #value < minimumLength then return false end
  if maximumLength ~= nil and #value > maximumLength then return false end
  return true
end

local function byteSortedBefore(left, right)
  local limit = math.min(#left, #right)
  for index = 1, limit do
    local leftByte, rightByte = string.byte(left, index), string.byte(right, index)
    if leftByte ~= rightByte then return leftByte < rightByte end
  end
  return #left < #right
end

function Util.IsDenseUniqueByteSortedStrings(value)
  if type(value) ~= "table" then return false end
  local count = 0
  for key in pairs(value) do
    if type(key) ~= "number" or key < 1 or key ~= math.floor(key) then return false end
    count = count + 1
  end
  local previous
  local seen = {}
  for index = 1, count do
    local item = value[index]
    if not Util.IsString(item, 1, nil) or seen[item] or
        (previous ~= nil and not byteSortedBefore(previous, item)) then
      return false
    end
    seen[item] = true
    previous = item
  end
  return true
end

function Util.NewID(parts, randomMinimum, randomMaximum)
  if type(parts) ~= "table" or #parts == 0 then error("ID parts must be a non-empty array") end
  if not Util.IsInteger(randomMinimum, 0, MAX_SAFE_INTEGER) or
      not Util.IsInteger(randomMaximum, randomMinimum, MAX_SAFE_INTEGER) then
    error("ID random bounds must be finite integers")
  end
  local values = {}
  for index, part in ipairs(parts) do
    if type(part) ~= "string" and type(part) ~= "number" then error("ID parts must be strings or numbers") end
    values[index] = tostring(part)
  end
  return table.concat(values, "-") .. ("-%04d"):format(math.random(randomMinimum, randomMaximum))
end

function Util.NewHexID(prefix, length)
  if not Util.IsString(prefix, 0, nil) or not Util.IsInteger(length, 1, MAX_SAFE_INTEGER) then
    error("hex ID prefix and length are invalid")
  end
  local value = prefix
  for _ = 1, length do value = value .. string.format("%x", math.random(0, 15)) end
  return value
end
