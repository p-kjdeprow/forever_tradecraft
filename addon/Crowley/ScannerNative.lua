local _, ns = ...

-- Scanner's sole Auction House/item/timer adapter.  ScannerState and the
-- encoder do not depend on this module; this file is the only scanner seam
-- that names the native query APIs.
local Native = {}
ns.ScannerNative = Native

function Native.Now() return time() end
function Native.Uptime() return GetTime() end
function Native.BuildInfo()
  if type(GetBuildInfo) ~= "function" then return nil end
  return GetBuildInfo()
end
function Native.On(event, handler) return ns.On(event, handler) end
function Native.Off(event, handler) return ns.Off(event, handler) end
function Native.ItemClass(name)
  if type(Enum) ~= "table" or type(Enum.ItemClass) ~= "table" then return nil end
  return Enum.ItemClass[name]
end
function Native.SortOrder(name)
  if type(Enum) ~= "table" or type(Enum.AuctionHouseSortOrder) ~= "table" then error("Crowley requires AuctionHouseSortOrder") end
  return Enum.AuctionHouseSortOrder[name]
end
function Native.AuctionStatus(name)
  if type(Enum) ~= "table" or type(Enum.AuctionStatus) ~= "table" then error("Crowley requires AuctionStatus") end
  return Enum.AuctionStatus[name]
end

local function auction(name)
  if type(C_AuctionHouse) ~= "table" or type(C_AuctionHouse[name]) ~= "function" then error("Crowley requires C_AuctionHouse." .. name) end
  return C_AuctionHouse[name]
end

function Native.After(delay, callback)
  if type(C_Timer) ~= "table" or type(C_Timer.After) ~= "function" then error("Crowley requires C_Timer.After") end
  return C_Timer.After(delay, callback)
end

function Native.InstallHooks(searchCallback, ownedCallback)
  if type(hooksecurefunc) ~= "function" then error("Crowley requires hooksecurefunc for Auction House query attribution") end
  hooksecurefunc(C_AuctionHouse, "SendSellSearchQuery", searchCallback)
  hooksecurefunc(C_AuctionHouse, "SendSearchQuery", searchCallback)
  hooksecurefunc(C_AuctionHouse, "RequestMoreCommoditySearchResults", searchCallback)
  hooksecurefunc(C_AuctionHouse, "RequestMoreItemSearchResults", searchCallback)
  hooksecurefunc(C_AuctionHouse, "QueryOwnedAuctions", ownedCallback)
end

function Native.IsThrottledReady() return auction("IsThrottledMessageSystemReady")() end
function Native.SendSearchQuery(key, sorts) return auction("SendSearchQuery")(key, sorts, false) end
function Native.SendSellSearchQuery(key, sorts) return auction("SendSellSearchQuery")(key, sorts, false) end
function Native.RequestMoreCommodity(itemID) return auction("RequestMoreCommoditySearchResults")(itemID) end
function Native.RequestMoreItem(key) return auction("RequestMoreItemSearchResults")(key) end
function Native.QueryOwned(sorts) return auction("QueryOwnedAuctions")(sorts) end
function Native.MakeItemKey(itemID) return auction("MakeItemKey")(itemID) end
function Native.ItemKeyInfo(key) return auction("GetItemKeyInfo")(key, false) end
function Native.HasFullCommodity(itemID) return auction("HasFullCommoditySearchResults")(itemID) == true end
function Native.HasFullItem(key) return auction("HasFullItemSearchResults")(key) == true end
function Native.GetCommodityResult(itemID, index) return auction("GetCommoditySearchResultInfo")(itemID, index) end
function Native.GetItemResult(key, index) return auction("GetItemSearchResultInfo")(key, index) end
function Native.GetCommodityCount(itemID) return auction("GetNumCommoditySearchResults")(itemID) end
function Native.GetItemCount(key) return auction("GetNumItemSearchResults")(key) end
function Native.GetCommodityQuantity(itemID) return auction("GetCommoditySearchResultsQuantity")(itemID) end
function Native.GetItemQuantity(key) return auction("GetItemSearchResultsQuantity")(key) end
function Native.HasSearchResults(key) return auction("HasSearchResults")(key) end
function Native.HasSearchResultsAvailable()
  return type(C_AuctionHouse) == "table" and C_AuctionHouse.HasSearchResults ~= nil
end
function Native.FullOwned() return auction("HasFullOwnedAuctionResults")() end
function Native.OwnedCount() return auction("GetNumOwnedAuctions")() end
function Native.OwnedInfo(index) return auction("GetOwnedAuctionInfo")(index) end
function Native.TimeLeftBandInfo(band)
  if type(C_AuctionHouse.GetTimeLeftBandInfo) ~= "function" then return nil end
  return C_AuctionHouse.GetTimeLeftBandInfo(band)
end

function Native.ItemClassInfo(itemID)
  if type(C_Item) ~= "table" or type(C_Item.GetItemInfoInstant) ~= "function" then return nil end
  return C_Item.GetItemInfoInstant(itemID)
end

function Native.RequestItemData(itemID)
  if type(C_Item) ~= "table" or type(C_Item.RequestLoadItemDataByID) ~= "function" then error("Crowley requires C_Item.RequestLoadItemDataByID") end
  return C_Item.RequestLoadItemDataByID(itemID)
end

function Native.DeserializeJSON(source)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.DeserializeJSON == nil then error("Crowley requires C_EncodingUtil.DeserializeJSON") end
  return C_EncodingUtil.DeserializeJSON(source)
end
function Native.SerializeJSON(value)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.SerializeJSON == nil then error("Crowley requires C_EncodingUtil.SerializeJSON") end
  return C_EncodingUtil.SerializeJSON(value)
end
function Native.CompressString(value)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.CompressString == nil then error("Crowley requires C_EncodingUtil.CompressString") end
  if type(Enum) ~= "table" or type(Enum.CompressionMethod) ~= "table" or Enum.CompressionMethod.Deflate == nil then error("Crowley requires Enum.CompressionMethod.Deflate") end
  return C_EncodingUtil.CompressString(value, Enum.CompressionMethod.Deflate)
end
function Native.EncodeBase64(value)
  if type(C_EncodingUtil) ~= "table" or C_EncodingUtil.EncodeBase64 == nil then error("Crowley requires C_EncodingUtil.EncodeBase64") end
  return C_EncodingUtil.EncodeBase64(value)
end

ns.RegisterModule("ScannerNative", { requires = { "Core" }, onLoad = function() end })
