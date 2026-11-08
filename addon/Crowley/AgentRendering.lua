local _, ns = ...

-- Native HUD/texture adapter.  Agent state and byte encoding stay outside this
-- file; this is the only module that owns the telemetry frame and glyph cells.
local Rendering = {}
ns.AgentRendering = Rendering

function Rendering.Clear(surface, cells)
  if surface then
    if surface.SetScript then surface:SetScript("OnUpdate", nil) end
    surface:Hide()
    for _, texture in pairs(cells or {}) do texture:Hide() end
  end
end

function Rendering.SetShown(surface, cells, visible)
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

function Rendering.Create(width, height, cropX, cropY, atlasPath)
  if not UIParent or type(UIParent.GetWidth) ~= "function" or type(UIParent.GetHeight) ~= "function" or
      type(UIParent.GetEffectiveScale) ~= "function" then error("Crowley telemetry requires UIParent geometry") end
  local rootWidth, rootHeight, rootEffective = UIParent:GetWidth(), UIParent:GetHeight(), UIParent:GetEffectiveScale()
  if type(rootWidth) ~= "number" or type(rootHeight) ~= "number" or type(rootEffective) ~= "number" or
      rootWidth <= 0 or rootHeight <= 0 or rootEffective <= 0 or rootWidth ~= rootWidth or rootHeight ~= rootHeight or rootEffective ~= rootEffective or
      rootWidth == math.huge or rootHeight == math.huge or rootEffective == math.huge or rootWidth == -math.huge or rootHeight == -math.huge or rootEffective == -math.huge then
    error("Crowley telemetry has invalid UIParent geometry")
  end
  if math.abs(rootWidth / rootHeight - 1920 / 1080) > 0.01 then error("Crowley telemetry requires the pinned 1920x1080 UIParent aspect") end
  local uiUnitsPerNativePixel = rootHeight * rootEffective / 1080
  if uiUnitsPerNativePixel <= 0 or uiUnitsPerNativePixel ~= uiUnitsPerNativePixel or uiUnitsPerNativePixel == math.huge or uiUnitsPerNativePixel == -math.huge then
    error("Crowley telemetry has invalid UI units per native pixel")
  end
  local surface = CreateFrame("Frame", "CrowleyAgentSurface", UIParent)
  local cells = {}
  local effective
  local ok, failure = pcall(function()
    surface:SetIgnoreParentScale(true)
    surface:SetScale(uiUnitsPerNativePixel)
    surface:SetAlpha(1)
    surface:SetFrameStrata("HIGH")
    surface:EnableMouse(false)
    effective = surface:GetEffectiveScale()
    if type(effective) ~= "number" or effective <= 0 or effective ~= effective or effective == math.huge or effective == -math.huge then error("Crowley telemetry surface has invalid effective scale") end
    surface:SetSize(cropX + width * 2, cropY + height * 2)
    surface:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
    if math.abs(effective - uiUnitsPerNativePixel) > 0.000001 then error("Crowley telemetry surface scale does not match the requested native factor") end
    for row = 0, height - 1 do
      for column = 0, width / 8 - 1 do
        local texture = surface:CreateTexture(nil, "ARTWORK")
        texture:SetSnapToPixelGrid(true)
        texture:SetTexelSnappingBias(0.5)
        texture:SetSize(16, 2)
        texture:SetPoint("TOPLEFT", surface, "TOPLEFT", cropX + column * 16, -(cropY + row * 2))
        texture:SetTexture(atlasPath, "CLAMP", "CLAMP", "NEAREST")
        texture:SetTexCoord(0, 1, 0.5 / 512, 1.5 / 512)
        texture:Show()
        cells[row * (width / 8) + column + 1] = texture
      end
    end
    surface:Show()
  end)
  if not ok then
    pcall(Rendering.Clear, surface, cells)
    error(failure)
  end
  return surface, cells, effective
end

function Rendering.Update(cells, bytes, width, height)
  local bytesPerRow = width / 8
  for row = 0, height - 1 do
    for column = 0, bytesPerRow - 1 do
      local value = string.byte(bytes, row * bytesPerRow + column + 1)
      if value == nil then error("Crowley telemetry frame is shorter than its fixed capacity") end
      local texture = cells[row * bytesPerRow + column + 1]
      local top = (value * 2 + 0.5) / 512
      texture:SetTexCoord(0, 1, top, (value * 2 + 2 - 0.5) / 512)
    end
  end
end

function Rendering.Attach(surface, callback)
  if not surface then error("Crowley telemetry surface is unavailable") end
  surface:SetScript("OnUpdate", callback)
end

ns.RegisterModule("AgentRendering", { requires = { "Core" }, onLoad = function() end })
