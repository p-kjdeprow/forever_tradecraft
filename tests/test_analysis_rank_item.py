"""Item-kind outputs, bonding exclusions and the recipe accounting of ``analysis.rank``."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from crowley.analysis.item_books import (
    Auction,
    ItemBook,
    floor_price,
    item_fair,
    item_intervals,
    replay_item_listing,
)
from crowley.analysis.rank import (
    EXCLUSION_REASONS,
    Evaluation,
    evaluate_trades,
    item_levels_snapshot,
)
from crowley.trading.api import ItemFacts
from crowley.trading.fees import deposit
from crowley.trading.strategies.crafting import DEFAULT_PARAMS
from crowley.trading.strategies.pricing import output_target_price
from tests.test_analysis_rank import (
    BOOKS,
    CONFIG,
    RULES,
    STEP,
    consuming_books,
    make_inputs,
    recipe,
    static_book,
)

LONG = 100_000  # seconds of time left: a disappearance cannot be an expiry
QUERY_SECONDS = 60


def wire_row(
    result_id: int,
    price: int,
    quantity: int = 1,
    time_left: int | None = LONG,
    key: str = "3001",
    owner: str | None = None,
    own: bool = False,
) -> dict[str, Any]:
    """An item-kind row as the scanner stores it: an individual owner with a time-left band."""

    row: dict[str, Any] = {
        "resultID": result_id,
        "quantity": quantity,
        "unitPrice": {"numerator": price, "denominator": 1},
        "itemKey": key,
        "rawItemKey": {"itemID": int(key)},
        "owners": {
            "names": {owner or f"owner-{result_id}": True},
            "identityKind": "individual",
            "containsOwnerItem": own,
            "containsAccountItem": False,
            "totalNumberOfOwners": 1,
        },
    }
    if time_left is not None:
        row["timeLeft"] = {"kind": "band", "minimumSeconds": time_left, "maximumSeconds": time_left + 1}
    else:
        row["timeLeft"] = {"kind": "unknown"}
    return row


def item_book(
    index: int,
    rows: list[dict[str, Any]],
    *,
    item_id: int = 3001,
    t: float | None = None,
    plan: str = "plan",
) -> ItemBook:
    start = int(index * STEP if t is None else t)
    observation = {
        "kind": "item",
        "coverage": "full",
        "sourceComplete": True,
        "cutoff": {"kind": "unbounded"},
        "query": {"kind": "queried", "startedAt": start, "finishedAt": start + QUERY_SECONDS},
        "itemID": item_id,
        "rows": rows,
    }
    envelope = {
        "house": {"key": "us-normal-alliance"},
        "context": {"clientVersion": "1.60.1", "clientBuild": "70862"},
        "planHash": plan,
    }
    auctions = tuple(
        Auction(
            row["resultID"],
            Fraction(row["unitPrice"]["numerator"], row["unitPrice"]["denominator"]),
            row["quantity"],
            row["itemKey"],
            next(iter(row["owners"]["names"])),
            row["owners"]["containsOwnerItem"],
        )
        for row in rows
    )
    return ItemBook(
        float(start), item_id, index, auctions, {"observation": observation, "envelope": envelope}
    )


def selling_books(item_id: int = 3001, count: int = BOOKS, first: int = 0) -> list[ItemBook]:
    """A 95 copper auction is bought every interval; one 100 copper auction stays."""

    return [
        item_book(
            index,
            [wire_row(10_000 + index, 95, key=str(item_id)), wire_row(1, 100, key=str(item_id))],
            item_id=item_id,
        )
        for index in range(first, count)
    ]


def item_inputs(recipes, item_books, **overrides):
    values = {
        "observed_kinds": {3001: "item", 3002: "item", 3003: "item"},
        "item_books": item_books,
        "facts": {**make_inputs([]).facts, **{i: ItemFacts(i, 10, None, None, 1, 0) for i in item_books}},
    }
    values.update(overrides)
    return make_inputs(recipes, **values)


SELLS = recipe(100, ((3001, 1),), ((2001, 2),))


# -- evidence ------------------------------------------------------------------------------


def test_walk_counts_sales_skips_possible_expiries_and_stops_at_the_unknown():
    cheap_sale = wire_row(11, 90)
    expiring = wire_row(12, 91, time_left=0)  # could have expired: skipped, never a sale
    sold_out = wire_row(13, 92, quantity=3, owner="Trader")  # owner gone, nothing reposted
    unknown_time = wire_row(14, 93, time_left=None)  # unknowable: ends the walk
    after_sale = wire_row(15, 94)
    stays = wire_row(1, 100)
    before = item_book(0, [cheap_sale, expiring, sold_out, unknown_time, after_sale, stays])
    after = item_book(1, [stays])
    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None
    # 11 sold; 12 skipped as a possible expiry; 13 is a genuine sell-out of 3 units; 14 ends the walk.
    assert interval.consumed == ((Fraction(90), 1), (Fraction(92), 3))
    assert interval.dt == STEP and interval.exposure == STEP

    (skipped,) = item_intervals([item_book(0, [expiring, stays]), item_book(1, [stays])], "3001")
    assert skipped is not None and skipped.consumed == ()


def test_reissue_under_a_new_id_counts_only_the_quantity_drop():
    # Live behaviour: a row of N is reissued with a new resultID when one of its auctions sells.
    stays = wire_row(1, 100)
    before = item_book(0, [wire_row(51, 90, quantity=12, owner="Seller"), stays])
    after = item_book(1, [wire_row(52, 90, quantity=10, owner="Seller"), stays])
    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None and interval.consumed == ((Fraction(90), 2),)  # not 12

    # reissued unchanged (a relist): nothing sold, and the walk stops at the still-listed row
    relist = item_book(1, [wire_row(53, 90, quantity=12, owner="Seller"), wire_row(54, 95), stays])
    (unchanged,) = item_intervals(
        [item_book(0, [wire_row(51, 90, quantity=12, owner="Seller"), wire_row(55, 95), stays]), relist],
        "3001",
    )
    assert unchanged is not None and unchanged.consumed == ()


def test_cancel_and_repost_at_another_price_is_not_a_sale():
    stays = wire_row(1, 100)
    before = item_book(0, [wire_row(61, 30, quantity=6, owner="Poster"), stays])
    after = item_book(1, [wire_row(62, 37, quantity=6, owner="Poster"), stays])
    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None and interval.consumed == ()


def test_merge_into_a_grouped_row_gives_no_evidence():
    merged = wire_row(72, 90, quantity=8)
    merged["owners"].update(identityKind="grouped", totalNumberOfOwners=3, names={"Seller": True, "A": True})
    intervals = item_intervals(
        [item_book(0, [wire_row(71, 90, quantity=5, owner="Seller")]), item_book(1, [merged])], "3001"
    )
    assert intervals == [None]  # the owner's units may sit in the grouped row: undetermined


def test_genuine_sellout_counts_every_unit():
    before = item_book(0, [wire_row(81, 90, quantity=7, owner="Gone"), wire_row(1, 100)])
    after = item_book(1, [wire_row(1, 100)])
    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None and interval.consumed == ((Fraction(90), 7),)


def test_untouched_cheapest_auction_blocks_everything_above_it():
    rows = [wire_row(21, 90), wire_row(22, 95), wire_row(1, 100)]
    # 22 sold, but the cheaper 21 is still listed: nothing above the untouched auction is inferred.
    (interval,) = item_intervals([item_book(0, rows), item_book(1, [rows[0], rows[2]])], "3001")
    assert interval is not None and interval.consumed == ()


def test_changed_identity_and_grouped_scopes_give_no_evidence():
    changed = wire_row(31, 90)
    (interval,) = item_intervals(
        [
            item_book(0, [changed, wire_row(1, 100)]),
            item_book(1, [wire_row(31, 90, quantity=2), wire_row(1, 100)]),
        ],
        "3001",
    )
    assert interval is not None and interval.consumed == ()  # inconsistent id: never a sale

    grouped = wire_row(32, 90, quantity=4)
    grouped["owners"].update(identityKind="grouped", totalNumberOfOwners=4)
    assert item_intervals([item_book(0, [grouped]), item_book(1, [])], "3001") == [None]


def test_gaps_and_plan_changes_break_the_chain():
    rows = [wire_row(1, 100)]
    assert item_intervals([item_book(0, rows), item_book(1, rows, t=STEP + 2400)], "3001") == [None]
    assert item_intervals([item_book(0, rows), item_book(1, rows, plan="other")], "3001") == [None]
    overlapping = item_book(1, rows, t=30)
    assert item_intervals([item_book(0, rows), overlapping], "3001") == [None]


def test_replay_is_decided_by_structure_before_outcome():
    books = selling_books(count=30)
    intervals = item_intervals(books, "3001")
    horizon = 7200
    # a unit sells every interval strictly above 94: two units fill within the horizon
    assert replay_item_listing(books, intervals, 0, 94, 2, horizon) is True
    # ties never fill: 95 is not strictly above 95
    assert replay_item_listing(books, intervals, 0, 95, 1, horizon) is False
    # the horizon runs past the data, so the outcome is unobservable regardless of sales
    assert replay_item_listing(books, intervals, 25, 94, 2, horizon) is None

    gapped = [replace(book, t=book.t + (2400 if index >= 15 else 0)) for index, book in enumerate(books)]
    # shift the observation windows as well, so core.volume sees the hole
    gapped = [
        item_book(index, [wire_row(10_000 + index, 95), wire_row(1, 100)], t=book.t)
        for index, book in enumerate(gapped)
    ]
    broken = item_intervals(gapped, "3001")
    assert broken[14] is None
    assert replay_item_listing(gapped, broken, 10, 94, 2, horizon) is None  # crosses the hole, sold or not
    assert replay_item_listing(gapped, broken, 1, 94, 2, horizon) is True


def test_two_identical_auctions_fill_a_two_unit_listing_in_one_interval():
    twins = [wire_row(41, 95), wire_row(42, 95), wire_row(1, 100)]
    books = [item_book(0, twins), item_book(1, [wire_row(1, 100)]), item_book(2, [wire_row(1, 100)])]
    intervals = item_intervals(books, "3001")
    assert intervals[0] is not None and intervals[0].consumed == ((Fraction(95), 1), (Fraction(95), 1))
    assert replay_item_listing(books, intervals, 0, 94, 2, 1200) is True
    assert replay_item_listing(books, intervals, 0, 94, 3, 1200) is False


# -- pricing -------------------------------------------------------------------------------


def test_item_levels_and_floor_fair_follow_the_commodity_target_rule():
    book = item_book(0, [wire_row(1, 100), wire_row(2, 95), wire_row(3, 95, quantity=2)])
    snapshot = item_levels_snapshot(3001, book, "3001")
    assert [(level.unit_price, level.quantity) for level in snapshot.levels] == [(95, 3), (100, 1)]
    assert snapshot.visible_min == 95 and snapshot.visible_units == 4
    assert floor_price(book, "3001") == 95 and floor_price(book, "other") is None

    books = selling_books(count=20)
    floors = [floor_price(b, "3001") for b in books]
    fair = item_fair(books, floors, 19, min_books=8)
    assert fair["value"] == 95 and fair["n"] == 19
    assert item_fair(books, floors, 5, min_books=8)["value"] is None  # fewer than 8 strictly past
    # the strictly-past window excludes the current book
    spiked = books[:19] + [item_book(19, [wire_row(1, 1000)])]
    assert item_fair(spiked, [floor_price(b, "3001") for b in spiked], 19, min_books=8)["value"] == 95
    target = output_target_price(replace(snapshot, fair=Fraction(95)), DEFAULT_PARAMS)
    assert target == 94  # one copper under the lowest buyout above 0.75 * fair, within [0.9, 1.1] * fair


def test_item_output_end_to_end_row():
    data = item_inputs([SELLS], {3001: selling_books()})
    evaluation = evaluate_trades(data, CONFIG)
    assert evaluation.excluded == ()
    (row,) = evaluation.rows
    assert row.kind == "item"
    assert row.target_prices == ((3001, 94),)
    assert row.sale_probability == 1.0
    assert row.sold_starts == row.independent_starts >= CONFIG.min_independent_starts
    # one unit bought every 600 s of exposure
    assert row.market_units_per_hour == 6
    assert row.crafts_per_hour == Fraction(1, 4) * 6
    batch_deposit = deposit(10, 2, 24, RULES)
    revenue = 2 * 94 * (1 - RULES.seller_cut) - batch_deposit
    assert row.margin_per_craft == (revenue - 40) / 2  # 4 AH units of 2001 at 10
    assert row.target_above_lowest is False


def test_no_sale_evidence_is_reported_not_faked():
    flat = [item_book(i, [wire_row(1, 100)], item_id=3001) for i in range(BOOKS)]
    evaluation = evaluate_trades(item_inputs([SELLS], {3001: flat}), CONFIG)
    assert evaluation.rows == ()
    (exclusion,) = evaluation.excluded
    assert exclusion.reason == "output_no_observed_demand"
    assert "window intervals usable" in exclusion.detail


def book_rows(book: ItemBook) -> list[dict[str, Any]]:
    return book.record["observation"]["rows"]


def test_item_reagents_are_priced_from_the_cheapest_auctions():
    book = item_book(
        BOOKS - 1,
        [
            wire_row(1, 10, key="3005"),
            wire_row(2, 20, quantity=2, key="3005"),
            wire_row(3, 30, quantity=5, key="3005"),
        ],
        item_id=3005,
    )
    uses_item = recipe(101, ((1001, 1),), ((3005, 2),))
    data = item_inputs([uses_item], {3005: [book]})
    (row,) = evaluate_trades(data, CONFIG).rows
    assert row.reagents == ((3005, 2, "ah"),)
    assert row.reagent_cost_per_craft == Fraction(10 + 2 * 20 + 30, 2)  # exact cheapest four units

    own_cheapest = item_book(
        BOOKS - 1,
        [wire_row(1, 5, key="3005", own=True), *book_rows(book)],
        item_id=3005,
    )
    (without_own,) = evaluate_trades(item_inputs([uses_item], {3005: [own_cheapest]}), CONFIG).rows
    assert without_own.reagent_cost_per_craft == row.reagent_cost_per_craft  # our own auction is no supply

    stale = item_book(BOOKS - 2, [wire_row(1, 10, key="3005")], item_id=3005)
    assert evaluate_trades(item_inputs([uses_item], {3005: [stale]}), CONFIG).exclusion_counts() == {
        "reagent_unpriceable": 1
    }
    mixed = replace(book, auctions=(*book.auctions, Auction(9, Fraction(5), 1, "3005:other", "x", False)))
    assert evaluate_trades(item_inputs([uses_item], {3005: [mixed]}), CONFIG).exclusion_counts() == {
        "reagent_item_variants": 1
    }


# -- bonding -------------------------------------------------------------------------------


def test_bind_on_pickup_and_unknown_bonding_are_excluded_before_pricing():
    bop = recipe(110, ((1001, 1),), ((2001, 1),))
    unknown = recipe(111, ((1003, 1),), ((2001, 1),))
    bou = recipe(112, ((1002, 1),), ((2001, 1),))
    data = make_inputs([bop, unknown, bou], observed_kinds={1003: "item"})
    facts = {
        **data.facts,
        1001: ItemFacts(1001, 10, None, None, 20, 1),  # BoP, with a perfectly good book
        1002: ItemFacts(1002, 10, None, None, 20, 3),  # bind on use is tradeable
    }
    del facts[1003]  # no client item data
    evaluation = evaluate_trades(replace(data, facts=facts), CONFIG)
    assert {item.spell_id: item.reason for item in evaluation.excluded} == {
        110: "output_bind_on_pickup",
        111: "output_bonding_unknown",
    }
    assert [row.spell_id for row in evaluation.rows] == [112]

    # the same for an item-kind output
    item_bop = item_inputs([SELLS], {3001: selling_books()})
    item_facts = {**item_bop.facts, 3001: ItemFacts(3001, 10, None, None, 1, 1)}
    assert evaluate_trades(replace(item_bop, facts=item_facts), CONFIG).exclusion_counts() == {
        "output_bind_on_pickup": 1
    }
    odd = {**item_bop.facts, 3001: ItemFacts(3001, 10, None, None, 1, 9)}
    with pytest.raises(ValueError, match="unrecognised client bonding"):
        evaluate_trades(replace(item_bop, facts=odd), CONFIG)


# -- accounting ----------------------------------------------------------------------------


def _assert_accounted(evaluation: Evaluation) -> None:
    assert evaluation.considered == len(evaluation.rows) + len(evaluation.excluded)
    assert evaluation.considered == len(evaluation.rows) + sum(evaluation.exclusion_counts().values())
    spells = [row.spell_id for row in evaluation.rows] + [item.spell_id for item in evaluation.excluded]
    assert len(spells) == len(set(spells))  # exactly one row or one reason each


def _variant_books(item_id: int) -> list[ItemBook]:
    books = selling_books(item_id)
    return [
        replace(
            book, auctions=(*book.auctions, Auction(9_999_999, Fraction(150), 1, f"{item_id}:l9", "x", False))
        )
        if index == BOOKS - 1
        else book
        for index, book in enumerate(books)
    ]


def _wide_band_books(item_id: int) -> list:
    """The reference price is 20 for most books and 200 for the rest: median 20, a far wider band."""

    from crowley.analysis.books import Book, Group

    books = []
    for index in range(BOOKS):
        ref = 20 if index % 20 < 11 else 200
        groups = [Group(f"low-{index}", Fraction(10), 5, LONG, 1), Group("ref", Fraction(ref), 30, LONG, 1)]
        books.append(Book(index * STEP, groups, None, item_id=item_id, sweep_id=index))
    return books


def test_every_recipe_is_a_row_or_exactly_one_reason():
    plain = {
        1: recipe(1, ((1001, 1),), ((2001, 2), (2002, 1))),  # commodity row
        2: recipe(2, ((1004, 1),), ((2001, 1),)),  # output_not_in_plan
        3: recipe(3, ((1005, 1),), ((2001, 1),)),  # output_bind_on_pickup
        4: recipe(4, ((1006, 1),), ((2001, 1),)),  # output_bonding_unknown
        5: recipe(5, ((1007, 1),), ((2001, 1),)),  # output_no_current_book
        6: recipe(6, ((1008, 1),), ((2001, 1),)),  # output_fair_unavailable
        7: recipe(7, ((1009, 1),), ((2001, 1),)),  # output_no_observed_demand
        8: recipe(8, ((1010, 1),), ((2001, 1),)),  # output_fair_untrusted
        9: recipe(9, ((1001, 1),), ((2999, 1),)),  # reagent_unpriceable
        10: recipe(10, (), ((2001, 1),)),  # no_outputs
        11: recipe(11, ((1001, 0),), ((2001, 1),)),  # invalid_output_quantity
        12: recipe(12, ((3001, 1),), ((2001, 2),)),  # item row
        13: recipe(13, ((3002, 1),), ((2001, 1),)),  # insufficient_independent_evidence
        14: recipe(14, ((3003, 1),), ((2001, 1),)),  # output_item_variants
        15: recipe(15, ((1001, 1),), ((3004, 1),)),  # reagent_item_variants
        16: recipe(
            16, ((1001, 1),), ((2001, 1),), profession="tailoring"
        ),  # other profession: not considered
    }
    books = {
        1001: consuming_books(1001, 95),
        1007: static_book(1007, 50)[:50],
        1008: static_book(1008, 50)[-3:],
        1009: static_book(1009, 50),
        1010: _wide_band_books(1010),
        2001: static_book(2001, 10),
    }
    base = make_inputs(list(plain.values()), random_yield_spells=(900, 901), no_output_spells=(902,))
    facts = {
        **base.facts,
        **{i: ItemFacts(i, 10, None, None, 20, 0) for i in (1004, 1007, 1008, 1009, 1010)},
        1005: ItemFacts(1005, 10, None, None, 20, 1),
        3001: ItemFacts(3001, 10, None, None, 1, 0),
        3002: ItemFacts(3002, 10, None, None, 1, 0),
        3003: ItemFacts(3003, 10, None, None, 1, 0),
    }
    del facts[1003]
    item_books = {
        3001: selling_books(3001),
        3002: selling_books(3002, first=BOOKS - 20),
        3003: _variant_books(3003),
        3004: _variant_books(3004),
    }
    data = replace(
        base,
        books=books,
        facts=facts,
        observed_kinds={1005: "item", 1006: "item", 3001: "item", 3002: "item", 3003: "item", 3004: "item"},
        item_books=item_books,
    )
    evaluation = evaluate_trades(data, replace(CONFIG, min_independent_starts=3))
    _assert_accounted(evaluation)
    assert evaluation.considered == 15 + 3
    by_spell = {item.spell_id: item.reason for item in evaluation.excluded}
    assert {row.spell_id: row.kind for row in evaluation.rows} == {1: "commodity", 12: "item"}
    assert by_spell == {
        2: "output_not_in_plan",
        3: "output_bind_on_pickup",
        4: "output_bonding_unknown",
        5: "output_no_current_book",
        6: "output_fair_unavailable",
        7: "output_no_observed_demand",
        8: "output_fair_untrusted",
        9: "reagent_unpriceable",
        10: "no_outputs",
        11: "invalid_output_quantity",
        13: "insufficient_independent_evidence",
        14: "output_item_variants",
        15: "reagent_item_variants",
        900: "random_yield",
        901: "random_yield",
        902: "no_outputs",
    }

    # without item-kind data an item-kind output keeps its original reason
    legacy = replace(data, item_books=None)
    legacy_eval = evaluate_trades(legacy, replace(CONFIG, min_independent_starts=3))
    _assert_accounted(legacy_eval)
    assert {item.reason for item in legacy_eval.excluded if item.spell_id in (12, 13, 14)} == {
        "output_not_commodity"
    }
    seen = set(by_spell.values()) | {item.reason for item in legacy_eval.excluded}
    assert seen == set(EXCLUSION_REASONS)


def test_a_miscounted_evaluation_fails_loudly():
    with pytest.raises(RuntimeError, match="considered"):
        Evaluation((), (), 1)
