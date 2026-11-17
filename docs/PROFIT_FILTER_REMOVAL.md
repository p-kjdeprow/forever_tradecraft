# Show profitable strategies without an activity gate

The user requires profitable current-price strategies to appear even when activity
is unknown or slow. The dashboard keeps its profession, budget, tradeability, current
price and user-selected minimum-profit requirements. Activity is informational.

The read-only audit of sweep 906 found 406 eligible recipes, of which 269 had fresh
tradeable output prices. Of these, 100 had positive estimated if-sold margin after
materials, auction-house cut and deposit. All 269 lacked usable activity evidence;
the previous activity filter therefore returned zero candidates. Client build 70959
had only a few sweeps separated by gaps too long for valid hot-item comparisons.
The same calculation against the last build 70938 sweep returned 41 activity-qualified
candidates. No data, recipes or prices are relabeled or mixed across builds.

Both the server eligibility gate and browser activity checks are removed. Unknown
activity renders as unknown rather than zero. Existing activity-based batch sizing
and ranking remain; the complete profitable candidate pool is available before the
user's profit threshold and top-ten display limit. Cache policy keys changed so the
previous filtered empty pool cannot suppress a new live request for the same sweep.

Verification: 49 summary/dashboard/route tests pass, including unknown, zero and
sub-six-unit activity, cached rows, invalidation of the old filtered empty cache,
profit thresholds and request races. Ruff check/format and scoped Pyright pass.
The new market wheel and matching unchanged common wheel were checked against source.
Only the server container was recreated; the database container, DB/raw mounts,
desktop installation and running scanner/companion were unchanged.

Live sweep 906 returns 100 candidates and ten top rows. A real headless browser
renders unknown-activity cards, retains the minimum-profit preference across reload,
and ignores an injected old filtered cache without JavaScript/API errors. No browser
screenshots were taken. Refresh a tab loaded before deployment to load the new code.
Private audit and deployment evidence: `~/.local/state/crowley/profit-audit-20261117/`.
