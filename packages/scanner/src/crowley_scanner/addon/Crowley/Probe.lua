local _, ns = ...

-- Record only the client APIs and identity facts used by scan collection.
local NAMESPACES = {
  "C_AuctionHouse",
  "C_EncodingUtil",
  "C_GameRules",
  "C_Item",
  "C_RealmInfo",
}

local function functionNames(value)
  local keys = {}
  for key, child in pairs(value) do
    if type(child) == "function" then keys[#keys + 1] = key end
  end
  table.sort(keys)
  return keys
end

function ns.Probe()
  local realmName = GetRealmName()
  local region = GetCurrentRegion()
  local regionName = GetCurrentRegionName()
  local snapshotOK, contextOrError = pcall(ns.Context.Snapshot)
  local _, build, _, interface = GetBuildInfo()
  local result = {
    at = time(),
    context = snapshotOK and contextOrError or false,
    region = region,
    regionName = regionName,
    realmName = realmName,
    namespaces = {},
  }
  if not snapshotOK then result.contextError = tostring(contextOrError) end

  local houseOK, houseOrError = pcall(ns.Context.House)
  if houseOK then
    result.house = houseOrError
  else
    result.house = false
    result.attributionError = tostring(houseOrError)
  end

  for _, name in ipairs(NAMESPACES) do
    result.namespaces[name] = _G[name] and functionNames(_G[name]) or false
  end
  ns.db.probe = result
  local houseText = result.house and result.house.key or ("attribution error: " .. (result.attributionError or "unknown"))
  local messageInterface = result.context and result.context.interface or interface
  local messageBuild = result.context and result.context.clientBuild or build
  ns.Print(("probe: interface %d build %s, house %s - /reload so the companion uploads it")
    :format(messageInterface, messageBuild, houseText))
end
