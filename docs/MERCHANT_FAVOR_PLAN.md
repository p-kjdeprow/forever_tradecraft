# Merchant's Favor dashboard

Implement a read-only Alliance/Azeroth Commerce Authority planner alongside the
crafting dashboard. No purchasing, filling, forging, quest acceptance or travel
automation is authorized by this feature.

1. Pin a sourced catalog: 28 current Alchemy recipes (3,780 Favor, highest gate
   Honored), 30 empty Waylaid Crates and each alternative fill bundle. Preserve
   recipe/spell/item IDs, Favor costs, skill and reputation gates. Record unknown
   Artisan rewards explicitly; never rank an assumed reward as verified.
2. Price one complete turn-in from fresh, successful observations in a single
   house/build/sweep. Include crate acquisition, full required material quantities,
   available depth and gold refunds. Missing/unqueried/failed/stale data is unknown,
   never free. Preserve itemKey variants; honor the native one-unit auction groups
   and their actual available quantity rather than inventing stack splitting.
   Rank observed fill choices per net Favor. Price forged returns separately:
   empty crate + 15 Favor -> 50 base reputation + 1 silver, unlocked at Friendly.
   Include the cost of earning the spent Favor; do not count forging as Favor gain.
3. Add a Merchant's Favor navigation tab, dedicated page and API. Show all recipe
   unlocks, total costs, cheapest observed fill/forge routes, missing coverage,
   freshness and editable character progress. Persist user preferences. Separate
   repeatable per-turn-in comparisons from bulk budget estimates; do not imply
   unlimited cheap stock or a globally optimal bulk shopping cart.
4. Admit the crates and all fill materials into the canonical scan catalog, promote
   required goods to the regular core cadence, regenerate target artifacts and
   verify plan identity. Keep existing crafting/recipe-watch coverage and cadence.
5. Test costing, stock boundaries, unknown data, item identity, unlock gates, Favor
   accounting, filter persistence and route scope. Review with Sol. Deploy the
   compatible package/plan set at a quiet scan boundary with rollback artifacts;
   verify actual new queries, an accepted sweep, prices and live browser behavior.

Implementation scope: market analysis/resources/routes/static page and packaging;
common catalog and target-derived artifacts; focused tests and documentation.
No database schema change, scanner query algorithm or game input backend change.
Keep previously unqualified input/listing changes out of this deployment.
