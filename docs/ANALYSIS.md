# Commodity time-to-sale analysis

The offline analysis uses only corrected format-2 commodity observations from
one explicitly selected house. It groups each observation by persistent
`result_id` price level, keeps exact rational unit prices, and compares
consecutive observations only when their start times are strictly increasing
and at most 1,260 seconds apart.

For each valid interval, the bottom walk examines retained groups from the
cheapest price upward. A depletion is counted as observed consumption only
when the walk reaches it; an expiry is skipped. Bounded observations use the
common ceiling of the two books. A hypothetical listing receives all observed
consumption above its price, and a configurable share of consumption at the
same price (`--tie-share`, default zero). A listing is censored when the chain
breaks, the book ceiling makes its price unobservable, the data ends, or the
24-hour horizon is reached.

The rule table evaluates an undercut price and reference prices at 0.8, 0.9,
1.0, 1.1, 1.25, and 1.5 times the 20th-cheapest unit. Quantities are 1, 20,
and 200. Kaplan–Meier estimates report the probability of a complete sale by
2, 6, 12, and 24 hours, together with starts, events, censoring, median sale
time, and the mean observed filled fraction at 24 hours.

Validation splits at the supplied epoch or at the median valid-interval start
time. Training replays stop at the split, so a training interval cannot use
test-period demand. Test examples are real price-level groups first seen in a
valid interval. They are bucketed by relative price and log-nearest quantity;
the report includes model and item-level-baseline Brier scores, equal-count
calibration bins, and per-bucket counts, using a quantity-conditioned
item-level baseline. It reports two outcome conventions:
`cancellation_censored` keeps `cancellation_or_expiry` as right-censoring,
while `cancellation_unsold` counts a cancellation before the horizon as an
observed not-sold outcome; other censor reasons remain unknown in both. The
second convention is useful as a sensitivity analysis, but cancellations are
seller-selected rather than random—unsold listings are more likely to be
cancelled—so treating them as unsold changes the estimand and does not remove
selection bias. Each scoring marks single-class results as not informative.

Run it with:

```sh
uv run python -m crowley.analysis \
  --database-url 'postgresql://user:password@db.example/crowley' \
  --house beta-normal-alliance \
  --out analysis.json
```

`003_item_unit_price_and_time_left.sql` must be recorded in
`schema_migrations`. The loader refuses older databases and refuses commodity
exact time-left values above 172800 seconds. The loader performs one read-only
transaction and never writes to the database.

## Cold items

With a cold pool (`coldSlices` N > 0 in the plan), core and discovery items are observed every sweep but each
cold item is observed only once every N sweeps, in a strict rotation (`market_sweeps.context.coldSlice`
records which slice a sweep took). The consecutive-observation interval rule above therefore never links two
observations of a cold item at the 900 s cadence: cold-item intervals are about N times longer than the
1,260 s limit, so they yield no valid intervals and cold items are priced, not time-to-sale modelled.

`python -m crowley.analysis rank --candidates` (`analysis/candidates.py`) is the cold-tier promotion view: recipes
with a cold output or reagent, plus recipes rank excluded for sale evidence or fair trust, priced at each item's latest
*attempted* observation in the lookback. The cold pool is read from `pools.cold` of the plan each sweep in the window
actually ran (`market_sweeps.plan_hash` to `market_plans.plan_body`); a plan from before the cold tier has none, and a
plan that cannot be read fails the run. Unqueried and incomplete entries of aborted sweeps are not observations; an
attempted item with no listings is an empty observation.

Per output: observation counts, lowest-price min/median/max, supply at the latest observation (item-kind excludes our
own auctions; commodity levels carry no per-unit ownership; `~` marks a bounded book, visible below its cutoff) and
distinct sellers (item-kind, fully individual scope only). The target price uses rank's fair rule (median of prior
reference prices, at least `min_fair_books` non-empty books) over `--fair-lookback-hours` (candidates only, default 72;
rank uses 24) because a cold item has about 16 books a day. Without enough books the output has no target: target
margin and score are shown as unavailable and the median-price margin and the flag still work.

The depletion column is the observed bottom depletion between consecutive observations of any gap length (commodity
bottom walk; item-kind owner-continuity walk, which skips possible expiries). It is a weak signal, not demand: it can
over-count (cancels; partial commodity expiries) and under-count (same-price refills; listings posted and sold inside
one gap). Recipes are flagged when the margin at the median lowest price is positive, every output has at least
`--min-observations` (4) observations and listings in at least half of them. The score is the target margin per craft
times the limiting output's crafts, min(depletion, supply) / units per craft.

## Metrics

`crowley.analysis.metrics` computes pre-model metrics for each commodity item in
one explicitly selected house. It uses the same exact rational price levels,
common ceilings, valid intervals, expiry rule, and bottom walk as the
time-to-sale analysis.

The reference is the unit price of the 20th-cheapest unit. Fair value at a
sweep is the median reference from the strictly past 24-hour window, only when
at least eight references are defined; the report also includes the 25th and
75th percentiles, sample count, and Theil–Sen log-price trend per day. Depth
reports exact cost, VWAP, marginal price, and fair-relative units for 1, 20,
100, and 500 units. Bounded books remain unknown when their ceiling or
in-scope quantity cannot establish a value.

Demand is the bottom-walk consumed units and copper per covered day. The
non-bottom removal metric is the all non-expiring in-scope removal upper bound
minus the bottom walk. Competition reports new-group undercuts, cheapest-group
tenure with Kaplan–Meier censoring, and seller counts/owners at the cheapest
five price levels. Opportunities report new groups at 0.6x, 0.7x, and 0.8x
fair value, including arrival, units, discount value, next-sweep survival, and
lifetime. Risk reports reference volatility scaled by the square root of hours,
fair-value mean reversion, and its half-life. Capacity reports bottom-walk
units consumed strictly above 0.9x, 1.0x, and 1.1x fair value.

Known metric biases include interval-resolution timing, invisible activity
between sweeps, cancellations that can resemble demand, bounded-book censoring,
and seller-selected observations. Arrival survival and tenure are likewise
right-censored at chain breaks and data ends; they should not be interpreted as
random-sample estimates of all marketplace activity.

Known biases are intentional: purchases of listings posted and bought between
sweeps are invisible and therefore understate demand; bottom cancellations can
look like purchases and overstate demand; the counterfactual assumes the new
listing does not change other sellers' behaviour; and interval-resolution event
times are upper bounds.

## Advisor (`python -m crowley.advisor`, `crowley-advise`, `GET /v1/houses/{key}/advice`)

On-demand ranking of trading pathways from the stored scan history, one house and build as of one
sweep. Pathways sit in sections whose values mean different things and are never mixed:
`ranked` (crafting and recipe purchases, profit/h = margin x crafts/h x P(sale)), `unmeasured`
(cold-tier crafts and vendor to AH, profit/h at P = 1, an upper bound), `one_off` (flips and
vendor-sell arbitrage, expected profit once) and `farming` (market value/h, a capacity and not income).
For rank and crafting advice, crafts/h is capped at `min(share × market units/h ÷ output qty,
3600 ÷ cooldown seconds)` when the recipe has a cooldown. Recipes in one cooldown group share one
timer per character, so their pathways are alternatives and their rates are not additive.

- **recipe_purchase** profit/h is the income the recipe unlocks and excludes the recipe's cost;
  the cost is in `capital`, and `payback_crafts` / `payback_hours` say how long it takes to earn back.
- **flip** fill is the per-trade unit fill within the horizon, each item shrunk toward the pool of
  the other items. Past starts assume the cheap units can be bought at t (optimistic) and keep
  intervals that reached the price cutoff (a lower bound on fill).
- The route caches the unfiltered evaluation per (house, build, latest sweep, character, options);
  `min_profit` and `min_volume` apply per request. Books are pinned to the keyed sweep, a character
  capture is read at the request's `as_of`. `advisor.service.warm` can precompute after an upload.

### Forecasts (`python -m crowley.forecast validate`, advisor `--estimates forecast`)

The advisor reads prices and demand through one estimate provider. `--estimates snapshot` reports
the present: the current lowest listing and the trailing bottom-walk rate. `--estimates forecast`
is the default. It replaces two values with validated forecasts: the **sale price** (what a listing
made now competes with at the end of the horizon) and **units/h** over the horizon. Buying always
uses the current lowest listing.

- **Models.** Price: `last` (baseline), `median_24h`, `ewma_6h` (6 h half-life) and `trend_24h`
  (Theil–Sen over hourly medians). Demand: `trailing` (baseline, exactly the snapshot rate over
  the lookback), `trailing_6h` and `hour_profile` (trailing × the market's UTC hour-of-day
  multiplier, a ratio of sums over the past 72 h).
- **Walk-forward.** Origins sit on whole UTC hours over the 72 h before as-of. A model sees only
  books at or before its origin, and a target counts only when it was fully observed by as-of.
- **Scoring.**
  - Price error is the mean absolute log error.
  - Demand error is the squared error, because demand is intermittent and absolute error would
    reward predicting zero.
  - Each challenger is compared with the baseline on the origins both of them scored.
- **Choice rule.**
  - A challenger is used for an item only when it has at least 12 paired origins, at least 5%
    skill, positive skill in both the earlier and the later half of those origins, and can
    predict at as-of.
  - Otherwise the baseline is used, so with no validated skill, forecast advice equals snapshot
    advice.
- **What is not forecast.**
  - Item-kind outputs.
  - Cold-tier crafts: they're priced from their latest cold observation.
  - Crafting crafts/h: it stays rank's measured rate.
  - Flip evidence: it stays its own replay.
- **Validation runs, 2026-11-11.** On beta (as-of 2026-10-22 12:00 CDT) and on live 70862
  history, `last` stays the price forecast for about 90% of items. The challengers' median skill
  is negative, so most of the market is best priced at its current lowest, and a challenger is
  trusted only for the minority where it is stable.
- `python -m crowley.forecast validate --house KEY [--build] [--as-of] [--items]` prints the
  per-model table and the items where a challenger qualifies.

### Trading dashboard

Open deployed `/dashboard` on the loopback server, or forward it from a laptop with
`ssh -N -L 18080:127.0.0.1:8080 <user>@<host>` and browse to `http://127.0.0.1:18080/dashboard`. The latest-scan
and trading-summary GETs are read-only and do not require an API token; write endpoints continue to require bearer
authentication. The page defaults to `us-normal-alliance` and Alchemy 225 / Tailoring 150, while allowing planning
targets through Tailoring 225. It renders the latest scan before waiting for the summary. The page shows:

The dashboard uses a rolling 24-hour observed-activity window. The deployed server runs source commit
`3519ddab70947b5562a0025a8321e13627dd742d` on image
`sha256:59bc3239aab673b1d700dd5adb3aa46dc767d29294cb97d49126aa7f26031da5`; the installed client scan plan and addon
remain revision 9.

- latest scan time and planned / queried / successful counts;
- up to ten profitable strategies only when positive listing depletion is observed in usable intervals in the rolling
  24-hour window, with the rate divided by summed usable hours rather than the full day; batch
  quantity, reagent costs, list price, 12-hour deposit, batch net proceeds and net profit per output item;
- the measured hours and interval count behind the 24-hour rate, plus reagent and profession-access details;
  unknown, zero, grouped, expired/reposted, out-of-scope or over-gap evidence is excluded;
- the assumed 10 g budget, recipe-ownership assumption and a link to Advanced research at `/dashboard/advanced`.

Each interval uses the immutable plan recorded for both endpoint sweeps. The allowed gap is 1.4 times the larger
scheduled interval: base cadence for hot items and base cadence times `coldSlices` when an endpoint's plan places
the item in the cold pool. Both observations must fall inside the 24-hour window. Missing or invalid historical plan
metadata is an API error; item-kind comparisons also retain their existing plan-change guard. A multi-hour gap beyond
the recorded schedule is not treated as evidence. There is no full-day minimum or fixed 70% coverage threshold.
Positive activity with less than six hours of measured data can qualify; zero or unknown evidence is removed before
the top ten.

The API returns exact `profit_per_item` and the full ranked `candidates` pool; legacy `opportunities` remains the first
ten. A remembered minimum-profit filter defaults to 50 copper, with 100, 300, 500 and 1,000 copper choices. It uses
inclusive exact per-item values, filters before the top-ten slice and backfills from lower ranks. Batch costs, deposits,
sale proceeds and profit remain unchanged and assume all listed output items sell. The v2 cache stores the full pool;
older v1 snapshots without candidates restore as a limited prior view while the browser refreshes the full pool.
Valid v1 records already containing candidates are validated and restored as complete pools. Filters trigger no network
request. These additions leave activity, profile, ranking, budget, warming and recipe-watch behavior unchanged.

Ready-now positive-margin strategies with observed activity sort before marked leveling targets. The empty state
reports that no qualified candidates meet the selected per-item minimum. For a limited v1 cache, it reports only
that no saved top-ten rows match and lower ranks are unavailable until the full pool refreshes. The page does not suggest items
from asking prices alone. Rows sharing a cooldown group are alternatives; their rates are not additive. Profits
assume every listed unit sells and are not expected income. Observed listing depletion is not confirmed sales, and
sale probability remains unknown.

The browser restores last-success recommendations by house/build/profile before network results, retains their source
sweep/time and advancing age on errors or timeout, and keeps latest-scan status separate. Valid empty success replaces
them; identities are not mixed. New scans warm the shared summary cache before optional advice warming; duplicates do
not warm and logged warm errors do not affect uploads.

**Current live proof, sweep 722.** The current server returned 69 fully qualified candidates while preserving the
legacy top ten; all per-item values were exact and all candidates had positive known activity. Served HTML matched the
wheel. Live JavaScript verified the five thresholds, backfilled 2/2/5/5/7 lower-ranked rows at 50/100/300/500/1,000
copper, made zero requests for filtering, restored the selected filter across reload and kept cards/status through a
failed refresh and filter change. The first post-restart summary took 49.264 s; no calculation-speedup claim is made.
Recipe watch returned HTTP 200. The server-only deployment preserved the native plan and dependencies and left the DB,
scanner and companion unchanged. Proofs: `.archive/profit-per-item-20261115/ops/parent-live-proof.json`,
`server-only-deploy-proof.json` and `parent-artifact-proof.json`.

The pinned sweep-703 SQL profile covered 608,951 rows: removing the global sort reduced that query from 3,554 ms to
378 ms, while full uncached summaries changed from 48.235 s to 45.599 s. The whole request is not 9× faster. Proofs:
`.archive/strategy-retention-20261114/parent-query-proof.json` and
`.archive/strategy-retention-20261114/parent-full-summary-proof.json`.

**Historical retained-summary release, sweep 705.** Its first uncached request took 47.487 s and the repeat cache hit
took 0.006 s; that response exposed only the then-current top ten. It predates the full-candidate filter release.
Proof: `.archive/strategy-retention-20261114/ops/parent-live-proof.json`.

**Historical live HTTP check, sweep 597.** Source commit `a0c533c9fd9846fef4d8bb680b510825bfff75cb` and image
`sha256:3f078b72b0f322cc1efd48c40d40ec4c7de050c145aa80e308ba4922995f5738` returned HTTP 200 for house
`us-normal-alliance`, build `1.60.1.70938`, with a 24-hour activity window and ten positive known-activity rows. The
profile remained Alchemy 225 / Tailoring 150, planning through Tailoring 225, with the assumed 10 g budget and
recipe ownership. Shadow Oil showed 51.36 observed units/hour across 14.0175 usable hours and 56 intervals; batch
cost was 9,886 copper, net proceeds if all sell were 15,456 copper, and if-sold profit was 5,570 copper. Frost Oil
and Goblin Rocket Fuel also had positive observed activity (20.62 and 13.44 units/hour). A read-only audit found
positive usable cold-pool evidence for IDs 2770, 2771 and Thorium Ore 10620, each including one accepted interval
longer than the hot-item limit under the recorded cold cadence. These are observed listing depletions and current
book economics, not confirmed sales or a sale-probability forecast. Proof: `.archive/daily-turnover-20261113/ops/parent-live-api-proof.json`,
`parent-live-activity-audit.json` and `parent-artifact-verification.json`.

**Historical live HTTP check, 2026-11-13 18:33 UTC.** Deployed commit `a15d388` and image
`e81b402cab34ef5e10bc8277877310463a7f6a7beba17f76a7f437a3d7ec8d1a` served the unauthenticated summary with
HTTP 200, ten rows, and an 8.03 s response. It reported house `us-normal-alliance`, build `1.60.1.70938`, latest
accepted sweep 583 (17:47:03 UTC; 484 planned, 484 queried, 484 successful). Profile, assumed budget and
cost/proceeds/profit arithmetic were checked. Both dashboard pages returned HTTP 200; an unauthenticated protected
queue write returned 401. The server stayed loopback-only and scan/companion services were active. Potion of
Predatory Senses showed 2 crafts / 2 units, 3,238 copper cost, 10,885 copper net proceeds if sold, 7,647 copper
if-sold profit and 12.57 observed units/hour. Shadow Oil showed 4 crafts / 8 units, 8,237 copper cost,
12,478 copper net proceeds, 4,241 copper if-sold profit and 49.17 observed units/hour. These are current-book
margin and descriptive-depletion figures, not confirmed sales. At verification time a post-unlock scan was running,
but sweep 583 remained the latest accepted sweep. Source details are in `.archive/dashboard-trading-20261113/`.

#### Water-walking watch (live; initial server receipts historical)

The token-free, read-only `GET /v1/houses/{key}/recipe-watch` endpoint and `/dashboard` panel remain live on current
server source `3519ddab70947b5562a0025a8321e13627dd742d`, image
`sha256:59bc3239aab673b1d700dd5adb3aa46dc767d29294cb97d49126aa7f26031da5`; the client plan/addon remain revision 9.
The current watch returned HTTP 200 (`parent-live-proof.json`). Previous server releases included the retained-summary
image `sha256:06fc81f8c9a4e08225e318387a9e733f67d417ba6e1f4a28941fc0f1f0a500e1` and the initial revision-9 server
deployment used
source `df71fa2ac81d73353fc988ca19e9ba3510235b16` / image
`sha256:28afedd954e419393d2a29ce8ff30b11ff54fe150aa04ac51d960b58b18f7c2d`; its sweep and watch receipts below are
historical. The later server-only update did not change the native plan.

Sweeps 688 and 689 each matched all 470 planned IDs and queried all successfully, including all 403 hot IDs and recipe
288206. Their runtimes were 612 and 550 seconds; starts were 900 seconds apart and per-hot-item query gaps were
834–900 seconds (median 884). Both were saved/uploaded and ACKs were written. Client-loaded ACK was not independently
verified. Two runtime samples are not a p95. Receipts: `.archive/water-walking-alert-20261114/ops/parent-sweep-688.json`,
`parent-sweep-689.json` and `parent-two-sweep-proof.json`.

The sweep-689 recipe was listed at 500,800 copper (50 g 8 s), quantity 2, with `building_history` at 1/6 prior priced
observations and no alert. Native evidence confirms item-kind, full, source-complete `ok` coverage with two rows
(`parent-native-recipe-first-proof.json`). This observed ask is not a bargain conclusion. Potion 8827 was 11,500 copper
with retained supply 54. Its 24-hour book showed 232 depletion units (27 partial reductions, 205 whole-group
disappearances) over 23.331 usable hours (9.94 observed units/hour). The recent six-hour book showed 31 units over
5.332 usable hours: 9 partial reductions and 22 ambiguous disappearances. These are listing changes, not confirmed
purchases; confirmed sales remain null, and the 1–2 unit pilot remains qualitative. Sweep-687's pre-query page state,
where recipe 288206 had `awaiting_scan` and no history, is historical (`parent-live-http-initial-proof.json`).

Recipe alerts require a current successful priced observation no older than 30 minutes and at least six previous
priced observations from distinct sweeps in the preceding 24 hours. The exact copper median excludes the current
offer; an alert triggers at or below 70% of that median. Missing, unqueried or failed latest recipe queries never fall
back to an older offer. Recipe observations follow their recorded item or commodity kind and never mix item-key
variants. Potion demand retains recorded cadence, price ceilings, expiry handling and gap rules; confirmed sales stay
null and the one-to-two potion pilot remains qualitative.

The main strategy summary's first request timed out after 40 seconds, then recovered with HTTP 200, ten positive
known-activity rows and a 0.013-second response. The canceled query-performance proposal was not applied. This watch
does not change the main strategy ranking or turnover model.

### Advanced advice view

The prior research page remains available at `/dashboard/advanced`. Its **Advice** view sends one
`GET /v1/houses/{key}/advice` request with every option, including `estimates`. Live mode polls
`GET /v1/houses/{key}/sweeps/latest` every minute and loads advice when the sweep changes. Scan uploads warm default
advice after a new sweep is ingested; the freshness line warns when the latest sweep is over 45 minutes old. It shows
the forecast summary, the four CLI sections, pathway details and forecast notes, and per-strategy exclusion counts.

The advice endpoint returned 200 after export 4 loaded; at sweep 546 (2026-11-13 08:22:10 UTC) it had 0 ranked and
38 unmeasured pathways. Unmeasured profit/hour assumes P(sale)=1 and is an upper bound, not income. The prior
interface filters professions but does not enforce the skill-225 planning ceiling. Default ranked crafting needs
six usable hours of commodity demand and four independent two-hour sale trials; additional data alone does not solve
grouped-gear sale identity. Matching-build research, current margins and early descriptive flow are in
`.archive/advice-refresh-20261113/`.
