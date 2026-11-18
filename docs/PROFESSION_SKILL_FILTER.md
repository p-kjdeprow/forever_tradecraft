# Maximum profession skill

The dashboard has one saved maximum-profession-skill selector: 150, 225, or 300.
It applies to both retained professions, Alchemy and Tailoring, and defaults to 225.
The selector limits recipe eligibility; it does not change the assumed character's
current training or erase the existing leveling/training notes.
The subsequent [ranking correction](STRATEGY_RANKING_FIX.md) removes the old
unconditional training penalty, so admitted higher-skill recipes can reach the displayed results.

The market server now calculates the complete profitable candidate pool through
skill 300. Each row includes `eligibility_skill`, which is the exact learning rank
when available or the verified yellow upper bound when the exact rank is unknown.
In the latter case `required_skill` remains null and the existing warning remains.
Recipes with no learning evidence remain excluded. The browser filters the full
pool by this bound and the user's minimum profit, then groups results into Alchemy
and Tailoring sections with up to 25 strategies each. Ranking remains unchanged
within each profession, and displayed ranks start at one in each section. Each
section reports the matching count, including an explicit empty state. Changes
are local, make no additional requests, and retain the current selection during
refreshes or house changes. Both preferences persist independently in localStorage.
The server and browser cache identities distinguish the new pool through 300 from
the previous pool through 225.

The Nov 18 grouping update uses the existing complete `candidates` pool; the API's
legacy `opportunities` projection remains unchanged. Saved full-pool data also
uses the new grouping. Older saved top-ten data remains explicitly labeled limited
until refreshed. Its 55 focused dashboard/summary tests include independent limits
for both professions, filtering before limiting, per-profession rank numbering,
cache reload and empty groups. The live browser checks all three skill limits
against three profit thresholds, preference persistence and 390px layout without
screenshots. Only the website is redeployed; desktop services continue collecting.

Verification: 52 focused summary/dashboard/route tests pass. These include both
professions at exact skill boundaries, the unknown-rank bound, filtering before
the top-ten limit, invalid/missing/blocked preference storage, profit composition,
reload persistence and no extra requests on selection changes. Ruff check/format
and scoped Pyright pass. A real headless browser verified all three live settings,
their expected cards and saved selections, with no API/JavaScript errors or overflow
at 390px width. No screenshots were taken.

Live sweep 912 has 149 profitable candidates: 72 fit skill 150, 125 fit 225, and all
149 fit 300, before the minimum-profit preference. Only the server container was
recreated. The database container, DB/raw mounts and running host/companion processes
are unchanged. The matching source wheels, previous image and private receipts are
retained under `~/.local/state/crowley/profession-skill-20261117/`.
