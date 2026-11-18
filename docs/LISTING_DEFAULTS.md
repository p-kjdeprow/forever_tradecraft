# Two-hour listings and per-item batches

New seller-policy items and manual posting actions default omitted `durationHours`
to 2. Explicit choices are 2, 8 and 24 hours. Canonical inspection includes the
resolved number before hashing/authorization; explicit null, boolean, 12 and 48 are
rejected. An existing valid explicit duration is preserved. No approved/stored
policy is migrated or rewritten.

Both posting kinds use the same mapping for native deposit calculation and posting:
2 hours → index 1, 8 hours → index 2, 24 hours → index 3. Native staging remains
explicit; the game UI's remembered duration is never used as a default. Model,
policy/schema/example, receipt validation and common frame validation agree with
the Lua producer. This source change does not install/reload addons or execute a trade.

Each exact item has its own `batchSize` (1–200) and total admitted `quantity`.
A new posting contains `min(batchSize, admitted bag quantity)`. Different items
can use 2, 5 or 20 units, for example; the last remainder may be smaller. No demand
heuristic, automatic stack-size growth, policy mutation or new workflow is added.
Matching price, one post per item per minute, spending and stock limits are retained.

## Source evidence

The installed Auctionator version 341 loads `Source_Forever/Constants.lua` for
Camelot; its short/medium/long durations are 2/8/24. The shared duration radio retains
internal values 12/24/48, which the modern selling adapter maps to native indices
1/2/3. Those internal values are not Forever's hours. Read-only source inspection
established this mapping; no screen capture, game input or protected API trial was
performed.

Observed file SHA256 values:

- `Source_Forever/Constants.lua`: `e898a6176528b533583a54f484c24208b6e6f2840680e999a7a379283850487b`
- `Source/Components/Frames/Duration.xml`: `a7e22f8f1aac773b76fa2353f61855d52685f0bb9bfc74d886cab4bc7c822fe7`
- `Source_ModernAH/Tabs/Selling/Mixins/SaleItem.lua`: `7c098b9a4b6622597a588032c5a5d6337e174234f52fd17e00590c867fc8eb79`

The Forever [shared sell-frame source](https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_AuctionHouseUI/Shared/Blizzard_AuctionHouseSellFrame.lua)
also enumerates the three duration labels in native ordinal order. Its API
[documentation](https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/AuctionHouseDocumentation.lua)
types deposit/post duration as a Lua index.

Offline regressions cover omitted-versus-explicit canonical identity, strict invalid
values, different item batch sizes and a final remainder, both native posting kinds,
and identical deposit/post indices across all three durations with decoded wire
readback. Existing real-Lua and seller-loop tests cover receipts, conservation and
immutable child-policy derivation. Live effect qualification remains separate.

Verification: 413 affected automation, frame, scanner-observer, combined-addon and
package-boundary checks pass. Scoped Ruff check/format and Pyright pass. No running
service, installed addon, policy ledger or deployment configuration was changed.
