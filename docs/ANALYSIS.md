# Retained market analysis

The market package serves the main `/dashboard`, its latest-scan status, trading summary,
and recipe watch. These are read-only public GETs; scan/probe ingestion requires bearer
credentials. Analysis reads one house and build at a time. It does not control the game.

The dashboard ranks positive-margin recipes using exact integer copper and rational prices.
It reports batch quantity, reagent costs, list price, 12-hour deposit, net proceeds if every
output sells, and net profit per output item. Alchemy 225 and Tailoring 150 are the reported
skills, with planning through 225. The 10 g budget and recipe ownership are assumptions;
required station/tool/focus access is explicit, and empty capabilities mean unavailable.
Crafting, buying and vendor execution are outside the automation scope.

Every ranked output needs at least 6 observed market units/hour in the trailing 24 hours.
The exact rate is tested before rounding, with exactly 6 accepted. Its denominator is the
sum of usable interval hours rather than a full day or a missing-time-as-zero estimate.
There is no minimum full-day or 70% coverage threshold. Unknown, zero and slower activity
is excluded before ranking. Cached browser rows use the same floor.

Each consecutive observation pair uses the immutable plan stored with each endpoint sweep.
Its maximum gap is1.4 times the larger scheduled interval: base cadence for hot items,
base cadence multiplied by coldSlices for cold items. Both endpoints must lie inside the
window. Missing or malformed historical plan metadata fails the request. Collection outages
and time spent walking or listing therefore create gaps, rather than evidence of no activity.

Commodity comparison uses exact rational price levels, common visible ceilings, bottom
walks and expiry guards. Item comparison additionally requires usable individual scopes,
unchanged plans and owner/price continuity. Grouped rows cannot identify sellers. Native
result IDs can change after a sale or repost; disappearing IDs alone do not prove sales.
Observed listing depletion can include cancellations and can miss refills or activity
between sweeps. It is descriptive activity, not confirmed revenue or a sale probability.

The API returns the full ranked candidates and the first ten as opportunities. The browser
filters exact profit_per_item inclusively at 50, 100, 300, 500 or 1,000 copper, then selects ten,
backfilling from lower ranks. The default is 50. Filter changes make no requests. Cooldown
rows sharing a group are alternatives, not additive hourly income. All profit assumes sale.

Last successful recommendations are cached by house, build and profile. They remain visible
with their source sweep/time and advancing age during loading, timeout or refresh failure.
A valid successful empty response replaces them. Latest-scan status is independent. Legacy
browser v1 records lacking the full candidates pool display the limited saved view while a
full refresh runs; v2 records hold the full pool. New accepted sweeps warm the retained
summary cache; duplicates do not. Warm failures do not fail ingestion.

Recipe watch requires a current successful priced recipe observation at most 30 minutes old
and six earlier priced observations from distinct sweeps in the preceding24 hours. Its
exact-copper baseline median excludes the current offer. An offer at or below 70% of that
median alerts. Missing, unqueried or failed latest observations never reuse an older offer.
Kinds and itemKey variants remain separate. Potion activity uses the same recorded-cadence,
ceiling, expiry and gap rules; a one-to-two-unit pilot is qualitative, not expected revenue.

Historical deployment receipts remain private under `.archive/`. They describe their exact
source/image/sweep, not the current checkout. The package refactor does not change the
selected scanner catalog or the applied SQL migration bytes.
