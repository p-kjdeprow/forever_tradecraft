local _, ns = ...

local Context = {}
ns.Context = Context

local TARGET = ns.GeneratedTarget
if type(TARGET) ~= "table" then error("generated launch target is not loaded") end

-- Goblin-run houses shared by both factions (15% cut), matched against zone and subzone text.
local NEUTRAL_ZONES = {
  ["Booty Bay"] = true,
  ["Gadgetzan"] = true,
  ["Everlook"] = true,
}

Context.VALID = {
  region = { us = true, eu = true, kr = true, tw = true, cn = true, beta = true },
  ruleset = { normal = true, pvp = true, rp = true, hardcore = true },
  faction = { alliance = true, horde = true, neutral = true },
}

local function validate(field, value, source)
  if value == nil or not Context.VALID[field][value] then
    error(("invalid %s %s %q"):format(source, field, tostring(value)))
  end
  return value
end

local DETECT = {
  region = function()
    local id = GetCurrentRegion()
    if id ~= TARGET.nativeRegionId then
      error(("native region id %s does not match target %s"):format(tostring(id), tostring(TARGET.nativeRegionId)))
    end
    return TARGET.region
  end,
  ruleset = function(checkNative)
    if checkNative then
      local realm = GetRealmName()
      if realm ~= TARGET.realm then
        error(("native realm %q does not match target %q"):format(tostring(realm), tostring(TARGET.realm)))
      end
    end
    return TARGET.ruleset
  end,
  faction = function()
    if NEUTRAL_ZONES[GetZoneText()] or NEUTRAL_ZONES[GetSubZoneText()] then return "neutral" end
    local faction = UnitFactionGroup("player")
    if not faction then error("unable to detect native faction") end
    return faction:lower()
  end,
}

-- The house a scan made right now belongs to. `/crowley set` overrides win over detection;
-- `source` records which fields were overridden so attribution can be audited.
function Context.House()
  local settings = ns.db.settings
  local house = {}
  local overridden = {}

  -- Validate persisted overrides before consulting any native value. An invalid
  -- setting must never be silently treated as auto detection.
  for _, field in ipairs({ "region", "ruleset", "faction" }) do
    local setting = settings[field]
    if setting ~= nil then validate(field, setting, "saved override") end
  end

  for _, field in ipairs({ "region", "ruleset", "faction" }) do
    local setting = settings[field]
    if setting ~= nil then
      house[field] = setting
      overridden[#overridden + 1] = field
    else
      if field == "ruleset" then
        house[field] = DETECT[field](settings.region == nil)
      else
        house[field] = DETECT[field]()
      end
    end
    validate(field, house[field], "detected house")
  end
  house.key = ("%s-%s-%s"):format(house.region, house.ruleset, house.faction)
  house.source = #overridden > 0 and ("override: " .. table.concat(overridden, ",")) or "auto"
  return house
end

-- Raw client facts stored with every scan, so attribution can be re-derived server side.
function Context.Snapshot()
  local playerFaction = UnitFactionGroup("player")
  if type(playerFaction) ~= "string" or playerFaction == "" then
    error("native player faction must be a non-empty string")
  end
  local version, build, _, interface = GetBuildInfo()
  return {
    character = UnitName("player"),
    realm = GetRealmName(),
    playerFaction = playerFaction,
    zone = GetZoneText(),
    subzone = GetSubZoneText(),
    locale = GetLocale(),
    clientVersion = version,
    clientBuild = build,
    interface = interface,
    addonVersion = ns.VERSION,
  }
end
