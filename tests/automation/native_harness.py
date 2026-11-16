"""Small deterministic WoW model for the fresh native operation addon."""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

from crowley_common.frames import decode_frame
from lupa import lua51

ADDON_ROOT = Path(__file__).parents[2] / "packages/automation/src/crowley_automation/addon"

_RUNTIME_STUBS = r"""
local state = ...
local frames = {}
_G.__frames = frames

local function noOp() end
local function makeTexture()
  local texture = {}
  for _, name in ipairs({"SetSnapToPixelGrid", "SetTexelSnappingBias", "SetSize", "SetPoint", "SetTexture", "SetTexCoord", "Show", "Hide"}) do
    texture[name] = noOp
  end
  return texture
end

function CreateFrame(kind, name, parent)
  local frame = { kind = kind, name = name, parent = parent, events = {}, scripts = {}, shown = true, enabled = false }
  function frame:RegisterEvent(event) self.events[event] = true end
  function frame:SetScript(event, callback) self.scripts[event] = callback end
  function frame:Show() self.shown = true end
  function frame:Hide() self.shown = false end
  function frame:IsShown() return self.shown end
  function frame:IsEnabled() return self.enabled end
  function frame:EnableMouse() end
  function frame:SetIgnoreParentScale() end
  function frame:SetScale() end
  function frame:SetAlpha() end
  function frame:SetFrameStrata(value) self.strata = value end
  function frame:SetFrameLevel(value) self.frameLevel = value end
  function frame:SetSize(width, height) self.width, self.height = width, height end
  function frame:SetPoint(point, relative, relativePoint, x, y)
    self.point, self.relative, self.relativePoint, self.offsetX, self.offsetY = point, relative, relativePoint, x or 0, y or 0
    if self.name == "CrowleyAutomationOperationButton" then
      self.left, self.bottom = (1920 / state.uiScale - self.width) / 2, self.offsetY
    end
  end
  function frame:GetLeft() return self.left or 0 end
  function frame:GetBottom() return self.bottom or 0 end
  function frame:GetWidth() return self.width or 0 end
  function frame:GetHeight() return self.height or 0 end
  function frame:GetEffectiveScale() return 1 end
  function frame:GetOwner() return state.tooltipOwner end
  function frame:GetText() return state.tooltipText end
  function frame:CreateTexture() return makeTexture() end
  function frame:SetText(text) self.text = text end
  function frame:SetEnabled(value) self.enabled = value end
  function frame:RegisterForClicks(...) self.clicks = {...} end
  -- Tests call this shim only to model a physical button-up delivered by WoW.
  function frame:HardwareClick(buttonName, down)
    if self.shown and self.enabled and self.scripts.OnClick then
      self.scripts.OnClick(self, buttonName or "LeftButton", down or false)
    end
  end
  frames[#frames + 1] = frame
  if name then _G[name] = frame end
  return frame
end

UIParent = {
  GetWidth = function() return 1920 / state.uiScale end,
  GetHeight = function() return 1080 / state.uiScale end,
  GetEffectiveScale = function() return state.uiScale end,
}
GameMenuFrame = { IsShown = function() return false end }
GameTooltip = {
  IsShown = function() return state.tooltipShown end,
  GetOwner = function() return state.tooltipOwner end,
}
GameTooltipTextLeft1 = { GetText = function() return state.tooltipText end }
ChatFrameUtil = { GetActiveWindow = function() return nil end }
SlashCmdList = {}

function time() return state.now end
function GetTime() return state.uptime end
function GetBuildInfo() return "1.60.1", state.build, "", state.interface end
function GetCurrentRegion() return state.region end
function UnitName(unit)
  if unit == "player" then return state.character end
  if unit == "target" then return state.targetName end
  if unit == "mouseover" then return state.hoverUnit end
end
function UnitGUID(unit)
  if unit == "target" then return state.targetGUID end
  if unit == "mouseover" then return state.hoverGUID end
end
function UnitExists(unit)
  if unit == "player" then return true end
  if unit == "target" then return state.targetPresent end
  if unit == "mouseover" then return state.hoverUnit ~= "" end
  return false
end
function UnitFactionGroup() return state.faction end
function GetRealmName() return state.realm end
function GetZoneText() return state.zone end
function CheckInteractDistance(unit, index) return state.inRange end
function GetBindingAction(key) return state.bindings[key] end
function StaticPopup_IsAnyDialogShown() return false end
function UnitPosition(unit)
  if unit == "player" then return state.x, state.y, 0, state.instance end
  return nil
end
function GetPlayerFacing() return state.facing end
function GetUnitSpeed() return state.speed end
function UnitIsDeadOrGhost() return state.dead end
function InCombatLockdown() return state.inCombat end
function IsMounted() return state.mounted end
function GetPhysicalScreenSize() return 1920, 1080 end
function GetCursorPosition() return state.cursorX, state.cursorY end
function GetMoney() return state.money end
function GetCVar() return "3" end

AuctionHouseFrame = { IsShown = function() return state.ahOpen end }
MailFrame = { IsShown = function() return state.mailOpen end }
Enum = {
  CompressionMethod = { Deflate = 0 },
  ItemCommodityStatus = { Commodity = 1, NonCommodity = 2 },
  AuctionStatus = { Active = 1, Sold = 2 },
}
ItemLocation = {}
function ItemLocation:CreateFromBagAndSlot(bag, slot)
  state.locationCalls = state.locationCalls + 1
  return { bag = bag, slot = slot }
end

local function bagEntry(location)
  return state.bags[tostring(location.bag) .. ":" .. tostring(location.slot)]
end
local function itemKey(entry)
  return entry and entry.nativeKey or nil
end
local function ownedEntry(index)
  return state.owned[index]
end

C_Container = {
  GetContainerNumSlots = function(bag) return state.bagSizes[bag] or 0 end,
  GetContainerNumFreeSlots = function(...)
    state.freeBagSlotCalls = state.freeBagSlotCalls + 1
    local args = {...}
    state.freeBagSlotArgs[state.freeBagSlotCalls] = args
    if state.freeBagSlotExtra then return state.freeBagSlotsByBag[args[1]] or 0, state.bagFamilies[args[1]] or 0, 0 end
    return state.freeBagSlotsByBag[args[1]] or 0, state.bagFamilies[args[1]] or 0
  end,
  GetContainerItemInfo = function(bag, slot)
    local entry = state.bags[tostring(bag) .. ":" .. tostring(slot)]
    if not entry then return nil end
    return { itemID = entry.itemID, stackCount = entry.count, isLocked = entry.locked, hyperlink = entry.link }
  end,
}
C_Item = {
  IsBound = function(location) return bagEntry(location).bound end,
}
C_AuctionHouse = {
  GetItemKeyFromItem = function(location) return itemKey(bagEntry(location)) end,
  GetItemCommodityStatus = function(location)
    return bagEntry(location).commodity and Enum.ItemCommodityStatus.Commodity or Enum.ItemCommodityStatus.NonCommodity
  end,
  IsSellItemValid = function(location, skipTransient)
    state.sellValidCalls = state.sellValidCalls + 1
    state.sellValidArgs = { location, skipTransient }
    return bagEntry(location).sellValid
  end,
  GetAvailablePostCount = function(location) return bagEntry(location).available end,
  CalculateCommodityDeposit = function(...)
    state.depositCalls[#state.depositCalls + 1] = { kind = "commodity", args = {...} }
    return state.deposit
  end,
  CalculateItemDeposit = function(...)
    state.depositCalls[#state.depositCalls + 1] = { kind = "item", args = {...} }
    return state.deposit
  end,
  HasFullOwnedAuctionResults = function() return state.ownedFull end,
  QueryOwnedAuctions = function(...)
    state.queryOwnedCalls = state.queryOwnedCalls + 1
    state.queryOwnedArgs = {...}
    if state.queryOwnedResult ~= nil then return state.queryOwnedResult end
    if state.queryOwnedUnexpected then return true end
  end,
  GetNumOwnedAuctions = function() return #state.owned end,
  GetOwnedAuctionInfo = function(index) return ownedEntry(index) end,
  CanCancelAuction = function(auctionID) return state.cancelAllowed[auctionID] == true end,
  GetCancelCost = function(auctionID)
    state.cancelCostCalls = state.cancelCostCalls + 1
    return state.cancelCosts[auctionID]
  end,
  PostCommodity = function(...)
    state.nativeCalls[#state.nativeCalls + 1] = { kind = "post_commodity", args = {...} }
    if state.nativeError then error("mock native failure") end
    return state.nativeReturn
  end,
  PostItem = function(...)
    state.nativeCalls[#state.nativeCalls + 1] = { kind = "post_item", args = {...} }
    if state.nativeError then error("mock native failure") end
    return state.nativeReturn
  end,
  CancelAuction = function(...)
    state.nativeCalls[#state.nativeCalls + 1] = { kind = "cancel_auction", args = {...} }
    if state.nativeError then error("mock native failure") end
    return state.nativeReturn
  end,
}

local function mail(index)
  return state.inbox[index]
end
function GetInboxNumItems() return #state.inbox, #state.inbox end
function GetInboxHeaderInfo(index)
  local row = mail(index)
  if not row then return nil end
  local first = row.attachments[1]
  local count = row.itemCount > 0 and row.itemCount or nil
  if state.mailHeaderExtra then
    return 0, 0, row.sender, row.subject, row.money, row.cod, row.daysLeft, count,
      true, row.returned, false, true, row.isGM, first and first.count, first and first.link, false
  end
  return 0, 0, row.sender, row.subject, row.money, row.cod, row.daysLeft, count,
    true, row.returned, false, true, row.isGM, first and first.count, first and first.link
end
function GetInboxItem(index, attachment)
  local row = mail(index)
  local item = row and row.attachments[attachment]
  if not item then return nil end
  return item.name, item.itemID, 0, item.count, 1, true, false
end
function HasInboxItem(index, attachment)
  local row = mail(index)
  return row ~= nil and row.attachments[attachment] ~= nil
end
function InboxItemCanDelete(index)
  local row = mail(index)
  return row ~= nil and row.canDelete
end
function AutoLootMailItem(...)
  state.nativeCalls[#state.nativeCalls + 1] = { kind = "collect_mail", args = {...} }
end

function __fire(event, ...)
  for _, frame in ipairs(frames) do
    if frame.events[event] and frame.scripts.OnEvent then frame.scripts.OnEvent(frame, event, ...) end
  end
end
function __advance(seconds)
  state.now = state.now + seconds
  state.uptime = state.uptime + seconds
  for _, frame in ipairs(frames) do
    if frame.scripts.OnUpdate then frame.scripts.OnUpdate(frame, seconds) end
  end
end
"""


class NativeHarness:
    def __init__(
        self,
        *,
        coordinated: bool = False,
        scanner_available: bool = False,
        scan_active: bool = False,
        saved_pending: dict[str, Any] | None = None,
        ui_scale: float = 1.0,
        mode_source: str | None = None,
        omit_mode: bool = False,
        defer_start: bool = False,
    ):
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True, encoding="latin-1")
        self.state = self.lua.table_from(
            {
                "now": 1_900_000_000,
                "uptime": 100.0,
                "build": "70938",
                "interface": 16001,
                "region": 110,
                "character": "Tester",
                "realm": "Normal",
                "faction": "Alliance",
                "zone": "Stormwind",
                "ahOpen": False,
                "mailOpen": False,
                "targetPresent": False,
                "targetGUID": "Creature-0-0-0-0-1-1",
                "targetName": "Auctioneer Jaxon",
                "inRange": True,
                "bindings": self.lua.table_from(
                    {
                        "CTRL-F12": "INTERACTTARGET",
                        "UP": "MOVEFORWARD",
                        "LEFT": "TURNLEFT",
                        "RIGHT": "TURNRIGHT",
                    }
                ),
                "x": 10.0,
                "y": 20.0,
                "instance": 0,
                "facing": 1.25,
                "moving": False,
                "speed": 0.0,
                "dead": False,
                "inCombat": False,
                "mounted": False,
                "cursorX": 960.0,
                "cursorY": 540.0,
                "uiScale": ui_scale,
                "hoverUnit": "",
                "hoverGUID": "",
                "tooltipShown": False,
                "tooltipOwner": None,
                "tooltipText": "",
                "money": 10_000_000,
                "freeBagSlotsByBag": self.lua.table_from({0: 16, 1: 0, 2: 0, 3: 0, 4: 0}),
                "bagFamilies": self.lua.table_from({0: 0, 1: 0, 2: 0, 3: 0, 4: 0}),
                "freeBagSlotCalls": 0,
                "freeBagSlotArgs": self.lua.table(),
                "freeBagSlotExtra": False,
                "bagSizes": self.lua.table_from({0: 4, 1: 0, 2: 0, 3: 0, 4: 0}),
                "bags": self.lua.table(),
                "locationCalls": 0,
                "sellValidCalls": 0,
                "sellValidArgs": self.lua.table(),
                "deposit": 100,
                "depositCalls": self.lua.table(),
                "owned": self.lua.table(),
                "ownedFull": True,
                "queryOwnedCalls": 0,
                "queryOwnedArgs": self.lua.table(),
                "queryOwnedResult": None,
                "queryOwnedUnexpected": False,
                "cancelAllowed": self.lua.table(),
                "cancelCosts": self.lua.table(),
                "cancelCostCalls": 0,
                "nativeCalls": self.lua.table(),
                "nativeReturn": True,
                "nativeError": False,
                "inbox": self.lua.table(),
                "mailHeaderExtra": False,
            }
        )
        self.lua.execute("return function(...) " + _RUNTIME_STUBS + " end")(self.state)
        self.orphan_db = self.lua.table()
        self.lua.globals().CrowleyAutomationDB = self.orphan_db
        self.saved_db = None
        self._install_codec()
        self.ns = self.lua.table()
        self._load_addon(coordinated=coordinated, mode_source=mode_source, omit_mode=omit_mode)
        if scanner_available:
            self.install_scanner_bridge(scan_active=scan_active)
        restored = {"pending": saved_pending} if saved_pending is not None else {}
        self.saved_db = self._to_lua(restored)
        self.lua.globals().CrowleyAutomationDB = self.saved_db
        if not defer_start:
            self.fire("ADDON_LOADED", "Crowley_Automation")

    def _install_codec(self) -> None:
        def as_bytes(value: Any) -> bytes:
            return value.encode("latin-1") if isinstance(value, str) else bytes(value)

        def compress(value: Any, method: Any) -> bytes:
            assert method == 0
            compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            return compressor.compress(as_bytes(value)) + compressor.flush()

        def convert(value: Any) -> Any:
            if hasattr(value, "items"):
                pairs = list(value.items())
                if not pairs:
                    return []
                if all(type(key) in (int, float) for key, _ in pairs):
                    return [convert(child) for _, child in sorted(pairs)]
                return {str(key): convert(child) for key, child in pairs}
            if isinstance(value, str):
                raw = value.encode("latin-1")
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return value
            return value

        self.lua.globals().C_EncodingUtil = self.lua.table_from(
            {
                "SerializeJSON": lambda value: json.dumps(
                    convert(value), sort_keys=True, separators=(",", ":")
                ),
                "CompressString": compress,
            }
        )

    def _load_addon(self, *, coordinated: bool, mode_source: str | None, omit_mode: bool) -> None:
        package = ADDON_ROOT / "Crowley_Automation"
        for filename in package.joinpath("Crowley_Automation.toc").read_text().splitlines():
            if not filename.endswith(".lua"):
                continue
            if filename == "Mode.lua" and omit_mode:
                continue
            source = package.joinpath(filename).read_text()
            if filename == "Mode.lua" and mode_source is not None:
                source = mode_source
            elif coordinated and filename == "Mode.lua":
                source = source.replace("ns.Coordinated = false", "ns.Coordinated = true")
            loaded = self.lua.globals().loadstring(source, "@" + filename)
            chunk = loaded[0] if isinstance(loaded, tuple) else loaded
            if chunk is None:
                raise SyntaxError(f"failed to compile {filename}")
            chunk("Crowley_Automation", self.ns)
            if filename == "AutomationOperations.lua":
                wrap = self.lua.eval(
                    "function(original) return function(...) "
                    "local value = original(...); _G.__operations = value; return value "
                    "end end"
                )
                self.ns.AutomationOperations.New = wrap(self.ns.AutomationOperations.New)

    def install_scanner_bridge(self, *, scan_active: bool) -> None:
        scanner = self.lua.table()
        scanner.protocol = 1
        self.lua.globals().CrowleyScanner = scanner
        self.lua.globals()._scan_active = scan_active
        self.lua.execute(
            r"""
            CrowleyScanner.suspend = function(owner)
              _owner = _owner or owner
              if _owner ~= owner then return false, "owned_by_other" end
              if _scan_active then return false, "scan_active" end
              return true
            end
            CrowleyScanner.quiescent = function(owner) return _owner == owner and not _scan_active end
            CrowleyScanner.resume = function(owner)
              if _owner ~= owner then return false, "not_owner" end
              if _scan_active then return false, "scan_active" end
              _owner = nil
              return true
            end
            """
        )
        path = ADDON_ROOT / "Crowley_Host" / "Host.lua"
        host_ns = self.lua.table()
        loaded = self.lua.globals().loadstring(path.read_text(), "@Host.lua")
        chunk = loaded[0] if isinstance(loaded, tuple) else loaded
        chunk("Crowley_Host", host_ns)

    def _to_lua(self, value: Any) -> Any:
        if isinstance(value, dict):
            table = self.lua.table()
            for key, child in value.items():
                table[key] = self._to_lua(child)
            return table
        if isinstance(value, list):
            table = self.lua.table()
            for index, child in enumerate(value, 1):
                table[index] = self._to_lua(child)
            return table
        return value

    @staticmethod
    def _from_lua(value: Any) -> Any:
        if hasattr(value, "items"):
            pairs = list(value.items())
            if not pairs:
                return []
            if all(type(key) in (int, float) for key, _ in pairs):
                return [NativeHarness._from_lua(child) for _, child in sorted(pairs)]
            return {str(key): NativeHarness._from_lua(child) for key, child in pairs}
        if isinstance(value, str):
            try:
                return value.encode("latin-1").decode("utf-8")
            except UnicodeDecodeError:
                return value
        return value

    def add_item(
        self,
        *,
        bag: int = 0,
        slot: int = 1,
        item_key: dict[str, int] | None = None,
        link: str | None = None,
        count: int = 20,
        commodity: bool = False,
        available: int | None = None,
        locked: bool = False,
        bound: bool = False,
        sell_valid: bool = True,
    ) -> None:
        key = item_key or {"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
        link = link or item_link(key["itemID"], suffix=key.get("itemSuffix", 0))
        entry = {
            "nativeKey": self._to_lua(key),
            "itemID": key["itemID"],
            "link": link,
            "count": count,
            "commodity": commodity,
            "available": available if available is not None else count,
            "locked": locked,
            "bound": bound,
            "sellValid": sell_valid,
        }
        self.state.bags[f"{bag}:{slot}"] = self._to_lua(entry)

    def add_owned(
        self,
        *,
        auction_id: int = 1,
        item_key: dict[str, int] | None = None,
        link: str | None = None,
        quantity: int = 1,
        buyout: int = 100,
        status: int = 1,
        can_cancel: bool = True,
        cancel_cost: int = 50,
    ) -> None:
        key = item_key or {"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
        row = {
            "auctionID": auction_id,
            "itemKey": self._to_lua(key),
            "itemLink": link or item_link(key["itemID"], suffix=key.get("itemSuffix", 0)),
            "quantity": quantity,
            "buyoutAmount": buyout,
            "status": status,
        }
        self.state.owned[len(self.state.owned) + 1] = self._to_lua(row)
        self.state.cancelAllowed[auction_id] = can_cancel
        self.state.cancelCosts[auction_id] = cancel_cost

    def add_mail(
        self,
        *,
        sender: str = "Auction House",
        subject: str = "Auction successful",
        money: int = 0,
        cod: int = 0,
        attachments: list[tuple[int, int]] | None = None,
        returned: bool = False,
        can_delete: bool = True,
        is_gm: bool = False,
        days_left: float = 30.0,
    ) -> None:
        attachment_rows = []
        for item_id, count in attachments or []:
            attachment_rows.append(
                {"name": f"item-{item_id}", "itemID": item_id, "count": count, "link": item_link(item_id)}
            )
        row = {
            "sender": sender,
            "subject": subject,
            "money": money,
            "cod": cod,
            "returned": returned,
            "canDelete": can_delete,
            "isGM": is_gm,
            "daysLeft": days_left,
            "itemCount": len(attachment_rows),
            "attachments": self._to_lua(attachment_rows),
        }
        self.state.inbox[len(self.state.inbox) + 1] = self._to_lua(row)

    def fire(self, event: str, *args: Any) -> None:
        if event == "AUCTION_HOUSE_SHOW":
            self.state.ahOpen = True
        elif event == "AUCTION_HOUSE_CLOSED":
            self.state.ahOpen = False
        elif event == "MAIL_SHOW":
            self.state.mailOpen = True
        elif event == "MAIL_CLOSED":
            self.state.mailOpen = False
        self.lua.globals()["__fire"](event, *args)

    def advance(self, seconds: float) -> None:
        self.lua.globals()["__advance"](seconds)

    def slash(self, command: str) -> bool:
        result = self.lua.globals().SlashCmdList.CAUTO(command)
        decode_frame(self.frame_bytes, expected_kind="automation")
        return result is not False

    def hardware_click(self, *, button: str = "LeftButton", down: bool = False) -> None:
        frame = self.lua.globals().CrowleyAutomationOperationButton
        frame.HardwareClick(frame, button, down)

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._from_lua(self.lua.globals().CrowleyAutomation.snapshot())

    @property
    def native_calls(self) -> list[dict[str, Any]]:
        return self._from_lua(self.state.nativeCalls)

    @property
    def frame_bytes(self) -> bytes:
        return self.lua.globals().CrowleyAutomation.frameBytes().encode("latin-1")


def item_link(item_id: int, *, suffix: int = 0, level: int = 0, bonuses: tuple[int, ...] = ()) -> str:
    fields = [item_id, 0, 0, 0, 0, 0, suffix, 0, level, 0, 0, 0, len(bonuses), *bonuses]
    return "|cffffffff|Hitem:" + ":".join(str(value) for value in fields) + "|h[Item]|h|r"
