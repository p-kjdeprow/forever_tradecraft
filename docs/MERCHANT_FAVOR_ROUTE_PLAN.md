# Actionable Merchant's Favor route

Replace the comparison-first page with a plan for each accepted scan and entered
character progress. The goal is to buy every currently profitable unowned faction
Alchemy recipe, in descending estimated net profit per produced item. Reach only the next recipe’s reputation gate for this visit, while showing the
highest required standing for the full profitable roadmap. Keep all 28 recipes visible,
distinguishing profitable, nonprofitable and unavailable evidence.

1. Reuse client recipe identities and shared material/vendor and AH cut
   calculations (deposits are refunded on a successful sale) to value one craft at the matched AH price. Pin every quote to the
   same house/build/sweep; no price is inferred from absence in the positive pool.
2. Use one bounded joint integer allocation to build a finite AH-visit plan without a gold or Alchemy-skill restriction, as
   explicitly requested. Reserve shared
   crate/material stock through actual price depth. Fund Favor, buy empty crates,
   forge only after Friendly, and reserve/purchase recipes in profit order. Show
   exact quantities, upfront spend, known refunds, projected balances and remaining
   work. Do not multiply the cheapest unit quote beyond its stock.
3. Price a clearly identified, level/faction-eligible starter-writ cohort with
   numeric quest/item requirements and NPC destinations. Exclude completed daily
   quests; never select the same quest twice. Unknown eligibility/refunds remain
   explicit. Admit only the required cohort/dependencies to the canonical scanner
   after checking the query budget; retain existing scan coverage.
4. Put "Do this scan" and the ranked unlock roadmap above collapsible comparison
   tables. Persist ownership and completed writs alongside existing character progress.
   Refresh/recompute on a new accepted scan or changed progress; stale or incomplete
   evidence cannot produce a purchase instruction. Give manual progress updates;
   no game effects or purchases are executed by this feature.
5. Verify profit signs/normalization, eligibility, rep/Favor gates, shared stock,
   marginal prices, daily uniqueness, scan identity, stale data and
   UI persistence. Sol reviews independently. Deploy only the required market and
   canonical data resources, preserving installed input/listing code. Verify fresh
   queries if scan coverage changes and actual live browser actions.

Scope: market analysis, resources, routes/static page, common catalog/target and
generated scanner plan only if new writ IDs are necessary, focused tests/docs.
No database migrations, trading effects, new agent runtime or workflow framework.
