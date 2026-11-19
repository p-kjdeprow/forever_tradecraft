"""One finite next-unlock allocation, with shared AH stock and daily quests."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .merchant_favor import PriceBook, catalog


def allocate(
    books: dict[int, PriceBook],
    *,
    reputation: int,
    favor: int,
    level: int,
    goal_rep: int,
    goal_favor: int,
    writs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Maximize reachable reputation, then Favor, then minimize known net cost.

    Each action is integral. Every purchased unit is shared across all actions,
    priced at its observed tier, and constrained by supply. Unknown writ payouts
    are excluded from the cost objective. No money or profession-skill cap exists.
    """
    rep_gap = max(0, goal_rep - reputation)
    max_forges = math.ceil(rep_gap / 50)
    usable = {i: b for i, b in books.items() if b.state == "priced"}
    actions: list[dict[str, Any]] = []

    def add(
        kind: str, data: dict[str, Any], req: dict[int, int], rep: int, gain: int, refund: int, limit: int
    ):
        if limit > 0 and all(i in usable and usable[i].quote(q) is not None for i, q in req.items()):
            actions.append(
                dict(kind=kind, data=data, req=req, rep=rep, gain=gain, refund=refund, limit=limit)
            )

    max_fill_favor = max(c["favor"] for c in catalog()["crates"] if c["money"] is not None)
    favor_needed = max(0, goal_favor + 15 * max_forges - favor)
    for w in writs:
        if rep_gap:
            add(
                "writ",
                w,
                {w["writ_item_id"]: 1, w["required_item_id"]: w["quantity"]},
                w["base_rep"],
                0,
                w["coin_refund_copper"] or 0,
                1,
            )
    for c in catalog()["crates"]:
        add("forge", c, {c["item_id"]: 1}, 50, -15, 100, max_forges)
        if c["money"] is None or c["min_level"] > level or not favor_needed:
            continue
        for option in c["options"]:
            add(
                "fill",
                {**c, "option": option},
                {c["item_id"]: 1, option["item_id"]: option["quantity"]},
                0,
                c["favor"],
                c["money"],
                math.ceil(favor_needed / c["favor"]),
            )
    if not actions:
        return {"actions": [], "rep_shortfall": rep_gap, "favor_shortfall": max(0, goal_favor - favor)}

    # Variables: action counts, exact purchased price tiers, two deficits, Friendly reached.
    costs = [float(-a["refund"]) for a in actions]
    upper = [a["limit"] for a in actions]
    tiers: dict[int, list[int]] = {}
    for item_id in sorted({i for a in actions for i in a["req"]}):
        tiers[item_id] = []
        for price, quantity in sorted(usable[item_id].levels):
            tiers[item_id].append(len(costs))
            costs.append(float(price))
            upper.append(quantity)
    fill_flags = {}
    for j, action in enumerate(actions):
        if action["kind"] == "fill":
            fill_flags[j] = len(costs)
            costs.append(0.0)
            upper.append(1)
    rep_slack, favor_slack, friendly = len(costs), len(costs) + 1, len(costs) + 2
    forging = len(costs) + 3
    costs.extend([0.0, 0.0, 0.0, 0.0])
    upper.extend([rep_gap, goal_favor, 1, 1])
    rows, lower_rows, upper_rows = [], [], []

    def constraint(coefficients: dict[int, int], low: float, high: float) -> None:
        row = np.zeros(len(costs))
        for index, coefficient in coefficients.items():
            row[index] = coefficient
        rows.append(row)
        lower_rows.append(low)
        upper_rows.append(high)

    for item_id, indices in tiers.items():
        coefficients = {j: a["req"].get(item_id, 0) for j, a in enumerate(actions)}
        coefficients.update({j: -1 for j in indices})
        constraint(coefficients, 0, 0)
    rep_coeff = {j: a["rep"] for j, a in enumerate(actions)}
    rep_coeff[rep_slack] = 1
    constraint(rep_coeff, rep_gap, math.inf)
    # Avoid unrelated farming beyond the next gate (one writ/receipt overshoot is allowed).
    constraint(
        {j: a["rep"] for j, a in enumerate(actions)},
        0,
        rep_gap + max([a["rep"] for a in actions] + [1]) - 1 if rep_gap else 0,
    )
    favor_coeff = {j: a["gain"] for j, a in enumerate(actions)}
    favor_coeff[favor_slack] = 1
    constraint(favor_coeff, goal_favor - favor, math.inf)
    # Each selected fill must be necessary: removing one batch must miss the
    # final Favor requirement. This prevents negative-cost, unrelated farming.
    max_net_gain = favor_needed + max_fill_favor - 1
    constraint({j: a["gain"] for j, a in enumerate(actions)}, -math.inf, max_net_gain)
    for j, flag in fill_flags.items():
        action = actions[j]
        constraint({j: 1, flag: -action["limit"]}, -math.inf, 0)
        constraint({j: 1, flag: -1}, 0, math.inf)
        upper_if_selected = goal_favor - favor + action["gain"] - 1
        relax = max(0, max_net_gain - upper_if_selected)
        coefficients = {k: a["gain"] for k, a in enumerate(actions)}
        coefficients[flag] = relax
        constraint(coefficients, -math.inf, upper_if_selected + relax)
    forge_counts = {j: 1 for j, a in enumerate(actions) if a["kind"] == "forge"}
    constraint({**forge_counts, forging: -max_forges}, -math.inf, 0)
    constraint({**forge_counts, forging: -1}, 0, math.inf)
    # A selected receipt must be needed even after all selected writ rewards.
    max_rep_gain = rep_gap + max([a["rep"] for a in actions] + [1]) - 1
    relax_rep = max(0, max_rep_gain - (rep_gap + 49))
    constraint(
        {**{j: a["rep"] for j, a in enumerate(actions)}, forging: relax_rep},
        -math.inf,
        rep_gap + 49 + relax_rep,
    )
    starter_gap = max(0, 3000 - reputation)
    starter_coeff = {j: a["rep"] for j, a in enumerate(actions) if a["kind"] == "writ"}
    starter_coeff[friendly] = -starter_gap
    constraint(starter_coeff, 0, math.inf)
    forge_coeff = {j: 1 for j, a in enumerate(actions) if a["kind"] == "forge"}
    forge_coeff[friendly] = -max_forges
    constraint(forge_coeff, -math.inf, 0)

    matrix = np.array(rows)
    low, high = np.array(lower_rows), np.array(upper_rows)
    objectives = []
    for index in (rep_slack, favor_slack):
        objective = np.zeros(len(costs))
        objective[index] = 1
        objectives.append(objective)
    objectives.append(np.array(costs))
    solution = None
    for objective in objectives:
        solution = milp(
            objective,
            integrality=np.ones(len(costs)),
            bounds=Bounds(0, upper),  # pyright: ignore[reportArgumentType] -- SciPy accepts arrays.
            constraints=LinearConstraint(matrix, low, high),  # pyright: ignore[reportArgumentType]
            options={"time_limit": 2.0, "mip_rel_gap": 0.0, "node_limit": 100000},
        )
        if solution.status != 0:
            return {"actions": [], "error": "Route optimization did not finish; wait for the next scan."}
        values = np.rint(solution.x).astype(np.int64)
        if np.max(np.abs(solution.x - values)) > 1e-5:
            raise ValueError("route optimizer returned fractional action counts")
        if objective is not objectives[-1]:
            index = rep_slack if objective is objectives[0] else favor_slack
            fixed = np.zeros(len(costs))
            fixed[index] = 1
            matrix = np.vstack((matrix, fixed))
            low = np.append(low, values[index])
            high = np.append(high, values[index])
    assert solution is not None
    actual = matrix @ values
    if np.any(actual < low - 1e-5) or np.any(actual > high + 1e-5):
        raise ValueError("route optimizer violated stock or progress constraints")
    return {
        "actions": [{**a, "count": int(values[j])} for j, a in enumerate(actions) if values[j]],
        "rep_shortfall": int(values[rep_slack]),
        "favor_shortfall": int(values[favor_slack]),
        "method": (
            "Minimum known net cost for the best reachable next unlock within quoted stock "
            "and the 11 qualified daily writs."
        ),
    }
