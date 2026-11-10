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
