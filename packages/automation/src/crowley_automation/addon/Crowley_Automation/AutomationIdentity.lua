local _, ns = ...

local Identity = {}
ns.AutomationIdentity = Identity

local MAX_SAFE = 9007199254740991

local function integer(value, minimum)
  return type(value) == "number" and value == math.floor(value) and value >= minimum and value <= MAX_SAFE
end

local function split(value, separator)
  local result = {}
  for part in (value .. separator):gmatch("(.-)" .. separator) do result[#result + 1] = part end
  return result
end

local function canonicalInteger(text, minimum)
  if type(text) ~= "string" then return nil end
  if minimum == 0 then
    if text ~= "0" and not text:match("^[1-9][0-9]*$") then return nil end
  elseif not text:match("^[1-9][0-9]*$") then return nil end
  local value = tonumber(text)
  if not integer(value, minimum) or string.format("%.0f", value) ~= text then return nil end
  return value
end

local function canonicalZeroField(text)
  if text == "" then return 0 end
  return canonicalInteger(text, 0)
end

function Identity.Validate(value)
  if type(value) ~= "string" or #value < 1 or #value > 128 then return false end
  local fields = split(value, ":")
  if canonicalInteger(fields[1], 1) == nil then return false end
  local order, seen, previous = { s = 1, b = 2, l = 3, p = 4 }, {}, 0
  for index = 2, #fields do
    local text = fields[index]
    local tag = text:sub(1, 1)
    local rank = order[tag]
    if not rank or rank <= previous or seen[tag] then return false end
    previous, seen[tag] = rank, true
    local body = text:sub(2)
    if tag == "b" then
      local last = 0
      for bonus in body:gmatch("[^.]+") do
        local parsed = canonicalInteger(bonus, 1)
        if not parsed or parsed < last then return false end
        last = parsed
      end
      if body == "" or body:sub(1, 1) == "." or body:sub(-1) == "." or body:find("..", 1, true) then return false end
    elseif canonicalInteger(body, 1) == nil then
      return false
    end
  end
  return true
end

local function linkIdentity(link)
  if type(link) ~= "string" then return nil end
  local marker = link:find("|Hitem:", 1, true)
  if not marker or link:find("|Hitem:", marker + 1, true) then return nil end
  local text = link:match("^|c[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]|Hitem:([^|]+)|h%b[]|h|r$")
  if not text then return nil end
  local fields = split(text, ":")
  if #fields < 13 then return nil end
  local itemID = canonicalInteger(fields[1], 1)
  local suffix = canonicalZeroField(fields[7])
  local count = canonicalZeroField(fields[13])
  if itemID == nil or suffix == nil or count == nil or count > 100 or #fields < 13 + count then return nil end
  local bonuses = {}
  for index = 14, 13 + count do
    local bonus = canonicalInteger(fields[index], 1)
    if bonus == nil then return nil end
    bonuses[#bonuses + 1] = bonus
  end
  table.sort(bonuses)
  return itemID, suffix, bonuses
end

function Identity.Canonical(nativeKey, link)
  if type(nativeKey) ~= "table" then return nil end
  local itemID = nativeKey.itemID
  local level = nativeKey.itemLevel
  local suffix = nativeKey.itemSuffix
  local pet = nativeKey.battlePetSpeciesID
  if not integer(itemID, 1) or not integer(level, 0) or not integer(suffix, 0) or not integer(pet, 0) then
    return nil
  end
  local linkedID, linkedSuffix, linkedBonuses = linkIdentity(link)
  if linkedID ~= itemID or linkedSuffix ~= suffix then return nil end
  local parts = { tostring(itemID) }
  if suffix > 0 then parts[#parts + 1] = "s" .. tostring(suffix) end
  if #linkedBonuses > 0 then
    local text = {}
    for index, bonus in ipairs(linkedBonuses) do text[index] = tostring(bonus) end
    parts[#parts + 1] = "b" .. table.concat(text, ".")
  end
  if level > 0 then parts[#parts + 1] = "l" .. tostring(level) end
  if pet > 0 then parts[#parts + 1] = "p" .. tostring(pet) end
  local result = table.concat(parts, ":")
  if not Identity.Validate(result) then return nil end
  return result
end
