"""Exact crate-cost, coverage and progression regressions without a live database."""

import json
import subprocess
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path

import pytest
from crowley_market.analysis.merchant_favor import (
    MAX_PRICE_AGE,
    PriceBook,
    _book,
    build_comparison,
    catalog,
    load_merchant_favor,
    scan_item_ids,
)


def comparison(books):
    return build_comparison(
        books,
        house="us-normal-alliance",
        build="1.60.1.70959",
        sweep_id=1,
        finished_epoch=1000,
        now_epoch=1010,
    )


def book(item_id, price, quantity=100):
    return PriceBook(item_id, "priced", 990, str(item_id), ((Fraction(price), quantity),))


def observation(**changes):
    return {
        "query": {"kind": "queried", "startedAt": 990, "finishedAt": 1000},
        "coverage": "full",
        "kind": "commodity",
        "source_complete": True,
        "reason_code": "ok",
        **changes,
    }


def raw(item_id, **changes):
    return {
        "itemID": item_id,
        "itemLevel": 0,
        "itemSuffix": 0,
        "battlePetSpeciesID": 0,
        "bonusIDs": {},
        **changes,
    }


def ownership(**changes):
    return {"owners": {"containsOwnerItem": False, "containsAccountItem": False, **changes}}


def test_catalog_totals_and_all_scan_dependencies_are_core(packaged_catalog):
    d = catalog()
    assert len(d["recipes"]) == 28
    assert sum(r["favor"] for r in d["recipes"]) == 3780
    assert max(r["reputation_total"] for r in d["recipes"]) == 9000
    assert len({r["recipe_item_id"] for r in d["recipes"]}) == 28
    assert len(d["crates"]) == 30
    assert len(scan_item_ids()) == 126
    active_core = {i.item_id for i in packaged_catalog.items if i.status == "active" and i.pool == "core"}
    assert set(scan_item_ids()) <= active_core
    artisan = [c for c in d["crates"] if "Artisan" in c["name"]]
    assert len(artisan) == 8 and all(c["money"] is None and c["reward_status"] == "reported" for c in artisan)


def test_full_quantity_depth_and_empty_crate_are_included_before_refund():
    b = {
        248684: book(248684, 100),
        2589: PriceBook(2589, "priced", 990, "2589", ((Fraction(1), 10), (Fraction(10), 100))),
        2318: book(2318, 20),
    }
    c = next(c for c in comparison(b)["fills"] if c["item_id"] == 248684)
    # Linen: 10*1 + 30*10 =310 materials; plus100 crate less250 refund =160.
    assert c["best"]["item_id"] == 2589
    assert c["best"]["material_cost"] == "310"
    assert c["best"]["gross_cost"] == "410"
    assert c["best"]["net_cost"] == "160"
    assert c["best"]["cost_per_favor"] == "32"
    assert c["best"]["available_turnins"] == 2


def test_missing_crate_insufficient_material_and_unknown_rewards_never_become_free():
    c = next(c for c in comparison({2589: book(2589, 1)})["fills"] if c["item_id"] == 248684)
    assert c["best"] is None
    c = next(
        c
        for c in comparison({248684: book(248684, 100), 2589: book(2589, 1, 39)})["fills"]
        if c["item_id"] == 248684
    )
    assert c["best"] is None
    c = next(
        c
        for c in comparison({248698: book(248698, 100), 14047: book(14047, 1)})["fills"]
        if c["item_id"] == 248698
    )
    assert c["best"] is None and c["money"] is None
    assert any(o["gross_cost"] is not None for o in c["options"])


@pytest.mark.parametrize(
    "state", ["stale", "unqueried", "query_failed", "awaiting_scan", "ambiguous_variant"]
)
def test_unknown_or_stale_books_cannot_quote(state):
    assert PriceBook(1, state, 990, "1", ((Fraction(1), 100),)).quote(1) is None


def test_reader_distinguishes_no_listing_failed_stale_and_variants():
    assert _book(1, observation(), [], 1010).state == "not_listed"
    assert _book(1, observation(coverage="bounded"), [], 1010).state == "insufficient_depth"
    assert _book(1, observation(query={"kind": "unqueried"}), [], 1010).state == "unqueried"
    assert _book(1, observation(reason_code="timeout"), [], 1010).state == "query_failed"
    assert _book(1, observation(kind="item", source_complete=False), [], 1010).state == "query_failed"
    assert _book(1, observation(), [], 990 + MAX_PRICE_AGE + 1).state == "stale"
    assert _book(1, observation(), [("1", raw(1), 1, 1, 10, ownership())], 1010).quote(5) == 5
    assert (
        _book(1, observation(), [("1:s1", raw(1, itemSuffix=1), 1, 1, 10, ownership())], 1010).state
        == "ambiguous_variant"
    )
    assert (
        _book(
            1,
            observation(),
            [("1", raw(1), 1, 1, 10, ownership()), ("1:l5", raw(1, itemLevel=5), 1, 1, 10, ownership())],
            1010,
        ).state
        == "ambiguous_variant"
    )
    for malformed in [("1", raw(1), 1, 0, 10, ownership()), ("1", raw(1), 1, 1, True, ownership())]:
        with pytest.raises(ValueError):
            _book(1, observation(), [malformed], 1010)


@pytest.mark.parametrize("flag", ["containsOwnerItem", "containsAccountItem"])
def test_owned_listings_and_mixed_owner_groups_are_not_buyable(flag):
    own = ("1", raw(1), 1, 1, 100, ownership(**{flag: True}))
    other = ("1", raw(1), 20, 1, 30, ownership())
    b = _book(1, observation(), [own, other], 1010)
    assert b.quote(10) == 200 and b.supply == 30
    assert _book(1, observation(), [own], 1010).state == "no_buyable_supply"
    assert _book(1, observation(), [own], 1010, exclude_owned=False).quote(1) == 1


def test_loader_pins_house_build_and_sweep_without_writes():
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class Connection:
        calls = []

        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            assert not any(word in sql.upper().split() for word in ["INSERT", "UPDATE", "DELETE"])
            if "SET TRANSACTION" in sql:
                return Result([])
            if "JOIN houses" in sql:
                assert params == ("us-normal-alliance",)
                return Result([(7, 1000, "1.60.1.70959")])
            assert params == (7, scan_item_ids())
            if "market_observations" in sql:
                return Result(
                    [
                        (
                            248684,
                            {
                                "kind": "commodity",
                                "query": {"kind": "queried", "startedAt": 990, "finishedAt": 1000},
                                "coverage": "full",
                                "reasonCode": "ok",
                                "sourceComplete": True,
                            },
                        )
                    ]
                )
            return Result([(248684, "248684", raw(248684), 50, 1, 10, ownership())])

    conn = Connection()
    d = load_merchant_favor(conn, "us-normal-alliance", now_epoch=1010)
    assert d["sweep_id"] == 7
    assert next(r for r in d["forgeries"] if r["item_id"] == 248684)["crate_price"] == "50"
    assert len(conn.calls) == 4
    with pytest.raises(ValueError):
        load_merchant_favor(conn, "us-normal-horde", now_epoch=1010)


def test_page_progress_migrates_and_never_sends_skill_or_gold_limits():
    html = (
        Path(__file__).parents[1] / "packages/market/src/crowley_market/server/static/merchant-favor.html"
    ).read_text()
    pure = html.split("/* FAVOR PURE LOGIC START */")[1].split("/* FAVOR PURE LOGIC END */")[0]
    script = (
        pure
        + """
const p=progress({...DEFAULT_PROGRESS,owned:[123]});
let invalid=0;for(const x of [{...p,rep:3000},{...p,favor:-1},{...p,level:61}]){try{progress(x);}catch(_){invalid++;}}
console.log(JSON.stringify({query:routeQuery(p),invalid,owned:p.owned,writs:p.completedWrits,money:money('750/50')}));
"""
    )
    actual = json.loads(
        subprocess.run(["node"], input=script, text=True, capture_output=True, check=True).stdout
    )
    assert actual["invalid"] == 3
    assert actual["owned"] == [123] and actual["writs"] == []
    assert actual["money"] == "15c"
    assert "owned=123" in actual["query"]
    assert "skill" not in actual["query"] and "gold" not in actual["query"]
