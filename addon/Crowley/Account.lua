local _, ns = ...

-- Account.lua is deliberately independent from Scanner.lua.  It only reads the
-- native account surfaces listed in the account-state contract and keeps its
-- result in CrowleyDB.account, which the session reader ignores.
local Account = {}
ns.Account = Account

local MAX_SAFE = 9007199254740991
local INTEGER_MAX = 2147483647
local BAG_MAX_SLOTS = 36
local MAX_INBOX_MESSAGES = 100
local MAX_MAIL_ATTACHMENTS = 16

local function invalid(field)
  return { kind = "invalid_native", field = field }
end

local function failure(detail)
  return { kind = "error", detail = tostring(detail) }
end

local function safeCall(fn, ...)
  local ok, a, b, c, d, e, f, g, h, i, j, k, l, m = pcall(fn, ...)
  if not ok then return false, tostring(a) end
  return true, a, b, c, d, e, f, g, h, i, j, k, l, m
end

local function bagSection()
  if not C_Container then error("C_Container is unavailable") end
  local bags = {}
  for bag = 0, 4 do
    local ok, numSlots = safeCall(C_Container.GetContainerNumSlots, bag)
    if not ok then error(numSlots) end
    if not ns.Util.IsInteger(numSlots, 0, BAG_MAX_SLOTS) then return invalid("bags.numSlots") end
    local okFree, freeSlots = safeCall(C_Container.GetContainerNumFreeSlots, bag)
    if not okFree then error(freeSlots) end
    if not ns.Util.IsInteger(freeSlots, 0, numSlots) then return invalid("bags.freeSlots") end
    local record = { numSlots = numSlots, freeSlots = freeSlots, slots = {} }
    for slot = 1, numSlots do
      local okInfo, info = safeCall(C_Container.GetContainerItemInfo, bag, slot)
      if not okInfo then error(info) end
      if info ~= nil then
        if type(info) ~= "table" then return invalid("bags.slots.info") end
        if not ns.Util.IsInteger(info.itemID, 1, MAX_SAFE) then return invalid("bags.slots.itemID") end
        if not ns.Util.IsInteger(info.stackCount, 1, INTEGER_MAX) then return invalid("bags.slots.count") end
        if type(info.isBound) ~= "boolean" then return invalid("bags.slots.bound") end
        if not ns.Util.IsInteger(info.quality, 0, 32767) then return invalid("bags.slots.quality") end
        record.slots[tostring(slot)] = {
          itemID = info.itemID,
          count = info.stackCount,
          bound = info.isBound,
          quality = info.quality,
        }
      end
    end
    bags[tostring(bag)] = record
  end
  return bags
end

local function professionSection()
  local ok, first, second, third, fourth, fifth = safeCall(GetProfessions)
  if not ok then error(first) end
  local indices = { first, second, third, fourth, fifth }
  local professions = {}
  local seen = {}
  for position = 1, 5 do
    local index = indices[position]
    if index ~= nil then
      if not ns.Util.IsInteger(index, 1, INTEGER_MAX) then return invalid("professions.index") end
      if seen[index] then return invalid("professions.index") end
      seen[index] = true
      local infoOK, name, _, level, maxLevel, _, _, skillLineID = safeCall(GetProfessionInfo, index)
      if not infoOK then error(name) end
      if type(name) ~= "string" or name == "" then return invalid("professions.name") end
      if not ns.Util.IsInteger(skillLineID, 1, INTEGER_MAX) then return invalid("professions.skillLineID") end
      if not ns.Util.IsInteger(level, 0, INTEGER_MAX) then return invalid("professions.level") end
      if not ns.Util.IsInteger(maxLevel, 0, INTEGER_MAX) then return invalid("professions.maxLevel") end
      if level > maxLevel then return invalid("professions.level") end
      professions[#professions + 1] = {
        name = name,
        skillLineID = skillLineID,
        level = level,
        maxLevel = maxLevel,
      }
    end
  end
  table.sort(professions, function(left, right) return left.skillLineID < right.skillLineID end)
  return professions
end

local function tradeSection(existingRecipes, existingCooldowns)
  if not C_TradeSkillUI then error("C_TradeSkillUI is unavailable") end
  local readyOK, ready = safeCall(C_TradeSkillUI.IsTradeSkillReady)
  if not readyOK then error(ready) end
  if type(ready) ~= "boolean" then return invalid("recipes.ready") end
  if not ready then return { kind = "not_ready" } end

  local baseOK, base = safeCall(C_TradeSkillUI.GetBaseProfessionInfo)
  if not baseOK then error(base) end
  if type(base) ~= "table" then return invalid("recipes.baseProfessionInfo") end
  local skillLineID = base.skillLineID
  if not ns.Util.IsInteger(skillLineID, 1, INTEGER_MAX) then return invalid("recipes.skillLineID") end

  local idsOK, recipeIDs = safeCall(C_TradeSkillUI.GetAllRecipeIDs)
  if not idsOK then error(recipeIDs) end
  if type(recipeIDs) ~= "table" then return invalid("recipes.recipeIDs") end
  local ids = {}
  local seen = {}
  for index = 1, #recipeIDs do
    local recipeID = recipeIDs[index]
    if not ns.Util.IsInteger(recipeID, 1, INTEGER_MAX) then return invalid("recipes.recipeID") end
    if seen[recipeID] then return invalid("recipes.recipeID") end
    seen[recipeID] = true
    ids[#ids + 1] = recipeID
  end
  for key in pairs(recipeIDs) do
    if type(key) ~= "number" or key < 1 or key > #recipeIDs or key ~= math.floor(key) then
      return invalid("recipes.recipeIDs")
    end
  end
  table.sort(ids)

  local learned = {}
  local active = {}
  for _, recipeID in ipairs(ids) do
    local infoOK, info = safeCall(C_TradeSkillUI.GetRecipeInfo, recipeID)
    if not infoOK then error(info) end
    if type(info) ~= "table" or type(info.learned) ~= "boolean" then return invalid("recipes.learned") end
    if info.learned then
      learned[#learned + 1] = recipeID
      if not C_Spell or type(C_Spell.GetSpellCooldown) ~= "function" then error("C_Spell.GetSpellCooldown is unavailable") end
      local cooldownOK, cooldown = safeCall(C_Spell.GetSpellCooldown, recipeID)
      if not cooldownOK then error(cooldown) end
      if type(cooldown) ~= "table" then return invalid("cooldowns.info") end
      if not ns.Util.IsFiniteNumber(cooldown.startTime, 0, nil) then return invalid("cooldowns.start") end
      if not ns.Util.IsFiniteNumber(cooldown.duration, 0, nil) then return invalid("cooldowns.duration") end
      if cooldown.duration > 0 then
        active[tostring(recipeID)] = { start = cooldown.startTime, duration = cooldown.duration }
      end
    end
  end

  -- Error markers are section values, not recipe maps.  Never merge one into
  -- a successful capture or the next envelope would contain mixed schemas.
  local recipes = {}
  if type(existingRecipes) == "table" and existingRecipes.kind == nil then recipes = ns.Util.Copy(existingRecipes) end
  local old = recipes[tostring(skillLineID)]
  local cooldowns = {}
  if type(existingCooldowns) == "table" and existingCooldowns.kind == nil then cooldowns = ns.Util.Copy(existingCooldowns) end
  if type(old) == "table" and type(old.learned) == "table" then
    for _, oldRecipeID in ipairs(old.learned) do cooldowns[tostring(oldRecipeID)] = nil end
  end
  for key, value in pairs(active) do cooldowns[key] = value end
  recipes[tostring(skillLineID)] = { capturedAt = time(), learned = learned }
  return { kind = "ok", recipes = recipes, cooldowns = cooldowns }
end

local function mailSection()
  if not MailFrame or type(MailFrame.IsShown) ~= "function" or not MailFrame:IsShown() then return nil end
  local okCount, count = safeCall(GetInboxNumItems)
  if not okCount then error(count) end
  if not ns.Util.IsInteger(count, 0, MAX_INBOX_MESSAGES) then return invalid("mail.count") end
  local messages = {}
  for index = 1, count do
    local headerOK, packageIcon, stationeryIcon, sender, subject, money, cod, daysLeft, hasItem =
      safeCall(GetInboxHeaderInfo, index)
    if not headerOK then error(packageIcon) end
    if not ns.Util.IsInteger(money, 0, MAX_SAFE) then return invalid("mail.money") end
    if not ns.Util.IsFiniteNumber(daysLeft, 0, nil) then return invalid("mail.daysLeft") end
    if type(hasItem) ~= "boolean" then return invalid("mail.hasItem") end
    local items = {}
    if hasItem then
      for attachment = 1, MAX_MAIL_ATTACHMENTS do
        local itemOK, itemName, itemID, texture, itemCount = safeCall(GetInboxItem, index, attachment)
        if not itemOK then error(itemName) end
        if itemID == nil then break end
        if not ns.Util.IsInteger(itemID, 1, MAX_SAFE) then return invalid("mail.items.itemID") end
        if not ns.Util.IsInteger(itemCount, 1, INTEGER_MAX) then return invalid("mail.items.count") end
        items[#items + 1] = { itemID = itemID, count = itemCount }
      end
    end
    messages[#messages + 1] = { money = money, daysLeft = daysLeft, items = items }
  end
  return { kind = "observed", capturedAt = time(), messages = messages }
end

local function newEnvelope()
  local house = ns.Context.House()
  local context = ns.Context.Snapshot()
  local capturedAt = time()
  if type(context.character) ~= "string" or context.character == "" then error("invalid context character") end
  return {
    format = 1,
    id = ns.Util.NewID({ house.key, context.character, capturedAt }, 0, 9999),
    house = house,
    context = context,
    capturedAt = capturedAt,
    gold = 0,
    bags = {},
    professions = {},
    recipes = {},
    cooldowns = {},
    mail = { kind = "unknown" },
  }
end

local function capture(sections)
  if not ns.db then return false end
  local ok, account = safeCall(newEnvelope)
  if not ok then
    ns.Print("account capture refused: " .. tostring(account))
    return false
  end
  local previous = ns.db.account
  if type(previous) == "table" then account = ns.Util.Copy(previous) end
  local houseOK, house = safeCall(ns.Context.House)
  local contextOK, context = safeCall(ns.Context.Snapshot)
  if not houseOK or not contextOK then
    ns.Print("account capture refused: invalid context")
    return false
  end
  account.format = 1
  account.house = house
  account.context = context
  account.capturedAt = time()
  account.id = ns.Util.NewID({ house.key, context.character, account.capturedAt }, 0, 9999)

  for _, section in ipairs(sections) do
    if section == "gold" then
      local goldOK, gold = safeCall(GetMoney)
      if not goldOK then account.gold = failure(gold)
      elseif not ns.Util.IsInteger(gold, 0, MAX_SAFE) then account.gold = invalid("gold")
      else account.gold = gold end
    elseif section == "bags" then
      local bagsOK, bags = safeCall(bagSection)
      account.bags = bagsOK and bags or failure(bags)
    elseif section == "professions" then
      local professionsOK, professions = safeCall(professionSection)
      account.professions = professionsOK and professions or failure(professions)
    elseif section == "trade" then
      local tradeOK, trade = safeCall(tradeSection, account.recipes, account.cooldowns)
      if not tradeOK then
        account.recipes = failure(trade)
        account.cooldowns = failure(trade)
      elseif type(trade) ~= "table" then
        account.recipes = failure("invalid trade capture result")
        account.cooldowns = failure("invalid trade capture result")
      elseif trade.kind == "not_ready" then
        -- The trade-skill UI is not ready; retain all skill lines.
      elseif trade.kind == "ok" then
        account.recipes = trade.recipes
        account.cooldowns = trade.cooldowns
      elseif trade.kind == "invalid_native" then
        account.recipes = ns.Util.Copy(trade)
        local field = tostring(trade.field)
        if field:sub(1, 8) == "recipes." then
          field = "cooldowns." .. field:sub(9)
        elseif field:sub(1, 10) ~= "cooldowns." then
          field = "cooldowns." .. field
        end
        account.cooldowns = invalid(field)
      elseif trade.kind == "error" then
        account.recipes = ns.Util.Copy(trade)
        account.cooldowns = failure(trade.detail)
      else
        account.recipes = failure("invalid trade capture result")
        account.cooldowns = failure("invalid trade capture result")
      end
    elseif section == "mail" then
      local mailOK, mail = safeCall(mailSection)
      if mailOK and mail ~= nil then account.mail = mail
      elseif not mailOK then account.mail = failure(mail) end
    end
  end
  ns.db.account = account
  return true
end

function Account.CaptureAll()
  return capture({ "gold", "bags", "professions", "trade", "mail" })
end

function Account.Summary()
  local account = ns.db and ns.db.account
  if type(account) ~= "table" then return "account: unavailable" end
  local gold = type(account.gold) == "number" and account.gold or 0
  local used, free = 0, 0
  if type(account.bags) == "table" then
    for _, bag in pairs(account.bags) do
      if type(bag) == "table" then
        free = free + (type(bag.freeSlots) == "number" and bag.freeSlots or 0)
        if type(bag.numSlots) == "number" and type(bag.slots) == "table" then
          local slots = 0
          for _ in pairs(bag.slots) do slots = slots + 1 end
          used = used + slots
        end
      end
    end
  end
  local professions = type(account.professions) == "table" and #account.professions or 0
  local recipes = 0
  if type(account.recipes) == "table" then
    for _, record in pairs(account.recipes) do
      if type(record) == "table" and type(record.learned) == "table" then recipes = recipes + #record.learned end
    end
  end
  local mail = type(account.mail) == "table" and account.mail.kind or "unknown"
  return ("account: gold=%d, used/free slots=%d/%d, professions=%d, recipes=%d, mail=%s")
    :format(gold, used, free, professions, recipes, tostring(mail))
end

local bagTimer = false

ns.On("PLAYER_LOGIN", function()
  local ok, detail = pcall(Account.CaptureAll)
  if not ok then ns.Print("account capture failed: " .. tostring(detail)) end
end)

ns.On("PLAYER_MONEY", function()
  local ok, detail = pcall(function() capture({ "gold" }) end)
  if not ok then ns.Print("account gold capture failed: " .. tostring(detail)) end
end)

ns.On("BAG_UPDATE_DELAYED", function()
  if bagTimer then return end
  if not C_Timer or type(C_Timer.After) ~= "function" then
    if ns.db and type(ns.db.account) == "table" then ns.db.account.bags = failure("C_Timer.After is unavailable") end
    ns.Print("account bag capture failed: C_Timer.After is unavailable")
    return
  end
  bagTimer = true
  local ok, detail = pcall(C_Timer.After, 0.25, function()
    bagTimer = false
    local captured, reason = pcall(function() capture({ "bags" }) end)
    if not captured then ns.Print("account bag capture failed: " .. tostring(reason)) end
  end)
  if not ok then
    bagTimer = false
    if ns.db and type(ns.db.account) == "table" then ns.db.account.bags = failure(detail) end
    ns.Print("account bag capture failed: " .. tostring(detail))
  end
end)

ns.On("TRADE_SKILL_LIST_UPDATE", function()
  local ok, detail = pcall(function() capture({ "trade" }) end)
  if not ok then ns.Print("account recipe capture failed: " .. tostring(detail)) end
end)

ns.On("TRADE_SKILL_SHOW", function()
  local ok, detail = pcall(function() capture({ "trade" }) end)
  if not ok then ns.Print("account recipe capture failed: " .. tostring(detail)) end
end)

ns.On("MAIL_INBOX_UPDATE", function()
  local ok, detail = pcall(function() capture({ "mail" }) end)
  if not ok then ns.Print("account mail capture failed: " .. tostring(detail)) end
end)

