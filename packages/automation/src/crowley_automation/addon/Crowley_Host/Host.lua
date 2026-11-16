local ADDON_NAME = ...

local function scannerAPI()
  local scanner = _G.CrowleyScanner
  if type(scanner) ~= "table" or scanner.protocol ~= 1 or type(scanner.suspend) ~= "function" or
      type(scanner.quiescent) ~= "function" or type(scanner.resume) ~= "function" then
    return nil
  end
  return scanner
end

local function coordinator()
  return {
    suspend = function(owner)
      local scanner = scannerAPI()
      if not scanner then return false, "scanner_unavailable" end
      return scanner.suspend(owner)
    end,
    quiescent = function(owner)
      local scanner = scannerAPI()
      if not scanner then return false end
      return scanner.quiescent(owner) == true
    end,
    resume = function(owner)
      local scanner = scannerAPI()
      if not scanner then return false, "scanner_unavailable" end
      return scanner.resume(owner)
    end,
  }
end

local function register()
  local automation = _G.CrowleyAutomation
  if type(automation) ~= "table" or automation.protocol ~= 3 or type(automation.setHostBridge) ~= "function" then
    return false
  end
  return automation.setHostBridge(coordinator()) == true
end

local events = CreateFrame("Frame")
events:RegisterEvent("ADDON_LOADED")
events:SetScript("OnEvent", function(_, event, name)
  if event == "ADDON_LOADED" and (name == ADDON_NAME or name == "Crowley" or name == "Crowley_Automation") then
    register()
  end
end)

register()
