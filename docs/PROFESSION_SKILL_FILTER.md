# Maximum profession skill

The dashboard has one saved maximum-profession-skill selector: 150, 225, or 300.
It applies to both retained professions, Alchemy and Tailoring, and defaults to 225.
The selector limits recipe eligibility; it does not change the assumed character's
current training or erase the existing leveling/training notes.

The market server now calculates the complete profitable candidate pool through
skill 300. Each row includes `eligibility_skill`, which is the exact learning rank
when available or the verified yellow upper bound when the exact rank is unknown.
In the latter case `required_skill` remains null and the existing warning remains.
Recipes with no learning evidence remain excluded. The browser filters the full
pool by this bound and the user's minimum profit, then takes the top ten. Changes
are local, make no additional requests, and retain the current selection during
refreshes or house changes. Both preferences persist independently in localStorage.
The server and browser cache identities distinguish the new pool through 300 from
the previous pool through 225.

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
