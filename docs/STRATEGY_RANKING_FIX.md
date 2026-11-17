# Let admitted higher-skill recipes compete in the top ten

The user observed identical results at maximum skill 225 and 300. The selector
correctly admitted more candidates, but an unconditional ranking tier put every
recipe above the assumed Alchemy 225/Tailoring 150 skills after every ready recipe.
Ten ready recipes therefore hid higher-skill recipes even when both their margin
and estimated profit rate were better.

The training tier is removed. Ranking now uses descending estimated profit rate,
descending batch profit, then spell ID for deterministic ties. Training/leveling
notes remain informational. The selected maximum skill still filters eligibility
before the top-ten limit. Activity, budget, cooldowns and profit calculations are
unchanged. New cache policy keys prevent the old ordering from surviving a reload.

For pinned live sweep 914, all 146 candidate records matched exactly before and
after; only order changed. At the 50c profit threshold, 225 admits 102 candidates
and 300 admits 124. The corrected top ten at 300 now include Greater Arcane Elixir
(skill 285), Distilled Firewater (250), and Elixir of the Grizzly (300). Current
client-export teaching requirements confirm Greater Arcane Elixir's rank. Its
if-sold profit is 69,376 copper for five units, or 13,875.2 copper per item, after
the existing material/house-cut/deposit calculation.

Verification: 56 focused summary/dashboard/route tests pass, including higher-skill
recipes outranking ready recipes for both professions with zero or positive measured
activity. Ruff check/format and scoped Pyright pass. A headless browser confirms
different displayed lists at 225 and 300, correct cap enforcement, preference
persistence and rejection of the previous ranked cache, with no API/JavaScript
errors. No screenshots were taken.

Only the server container was recreated. The database, DB/raw mounts and running
scanner/companion processes are unchanged. Private before/after data, calculation
comparison, recipe evidence, wheels and deployment/browser receipts are under
`~/.local/state/crowley/strategy-ranking-20261117/`.
