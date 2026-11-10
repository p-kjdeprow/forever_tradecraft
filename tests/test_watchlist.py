import json

import pytest
from pydantic import ValidationError

from crowley.core import savedvars
from crowley.core.luawriter import render_assignment
from crowley.core.watchlist import Catalog, load_catalog


def test_shipped_plan_covers_professions_and_preserves_review_states():
    catalog = load_catalog()
    active = [item for item in catalog.items if item.status == "active"]
    plan = catalog.scan_plan()
    assert len(catalog.items) == 662
    assert len(active) == 481
    assert sum(item.status == "research" for item in catalog.items) == 139
    assert sum(item.status == "excluded" for item in catalog.items) == 42
    assert plan["revision"] == 4
    assert plan["targetClientBuild"] == "1.60.1.70450"
    assert plan["catalogHash"] == "9ebdbd5d6308d5de1888cb65c2a275633e2e87f39ed3b99f3b171c3e05dbf025"
    assert plan["planHash"] == "00de6a4b612f62245637108ba6fa0b70d90cf128dc437c1eff5375b7d12dcc42"
    assert len(plan["pools"]["core"]) == 423
    assert len(plan["pools"]["discovery"]) == 58
    professions = {profession for item in active for profession in item.professions}
    assert professions >= {
        "alchemy",
        "blacksmithing",
        "cooking",
        "enchanting",
        "engineering",
        "first-aid",
        "leatherworking",
        "tailoring",
        "mining",
        "fishing",
        "skinning",
    }
    assert any(item.category == "equipment" and item.pool == "discovery" for item in active)
    assert any(item.status == "research" for item in catalog.items)
    assert any(item.status == "excluded" for item in catalog.items)
    # Known BoP reagents must remain visible for review, never auction searches.
    assert not {12938, 18240} & set(catalog.scan_plan()["itemIDs"])


@pytest.mark.parametrize("invalid", ["duplicate_id", "unknown_field", "bop_active", "nontradeable_active"])
def test_catalog_rejects_ambiguous_or_unscannable_admissions(invalid):
    data = load_catalog().model_dump(mode="json")
    if invalid == "duplicate_id":
        data["items"].append(data["items"][-1].copy())
    elif invalid == "unknown_field":
        data["items"][0]["enabled"] = True
    else:
        item = data["items"][0]
        item["status"] = "active"
        if invalid == "bop_active":
            item["binding"] = "bind_on_pickup"
        else:
            item["auction_eligibility"] = "not_tradeable"
    with pytest.raises(ValidationError):
        Catalog.model_validate_json(json.dumps(data))


def test_removing_item_changes_plan_identity_and_roundtrips_through_lua():
    original = load_catalog()
    data = original.model_dump(mode="json")
    item = next(item for item in data["items"] if item["status"] == "active")
    removed_id = item["item_id"]
    item["status"] = "research"
    revised = Catalog.model_validate_json(json.dumps(data))
    plan = revised.scan_plan()
    assert removed_id not in plan["itemIDs"]
    assert plan["catalogHash"] != original.scan_plan()["catalogHash"]
    assert len(plan["itemIDs"]) == len(original.scan_plan()["itemIDs"]) - 1
    text = render_assignment("CROWLEY_DATA", {"scanPlan": plan}, header="test")
    assert savedvars.loads(text.encode())["CROWLEY_DATA"]["scanPlan"] == plan
