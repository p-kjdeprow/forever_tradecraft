import hashlib
import json

import pytest
from crowley_common.house import House
from crowley_common.luawriter import render_assignment, to_lua
from crowley_common.scanformat import (
    MAX_SAFE_INTEGER,
    ScanFormatError,
    decode_json_payload,
    decode_strict_json,
    encode_json_payload,
    normalize_observation,
    validate_envelope,
)
from crowley_scanner import savedvars

WOW_STYLE = b"""
CrowleyDB = {
\t["scans"] = {
\t\t["us-pvp-horde-1-Bob-0001"] = {
\t\t\t["name"] = "line\\nbreak",
\t\t\t["n"] = -1.5e3,
\t\t},
\t},
\t["list"] = {
\t\t"a", -- [1]
\t\t"b\\226\\128\\148c", -- [2]
\t},
\t["flag"] = true,
\t["none"] = nil,
}
Other = 31
"""


def test_savedvars_parses_wow_output():
    data = savedvars.loads(WOW_STYLE)
    scan = data["CrowleyDB"]["scans"]["us-pvp-horde-1-Bob-0001"]
    assert scan["name"] == "line\nbreak"
    assert scan["n"] == -1500.0
    assert data["CrowleyDB"]["list"] == ["a", "b—c"]
    assert data["CrowleyDB"]["flag"] is True
    assert data["Other"] == 31


def test_savedvars_errors_have_line_numbers():
    with pytest.raises(savedvars.SavedVariablesError, match="line 2"):
        savedvars.loads(b'X = {\n ["a"] = @ }')


def test_lua_writer_roundtrips_through_parser():
    value = {"generatedAt": 1760000000, "ackedScans": ["a", 'q"uote\n'], "nested": {"x": 1.5}}
    text = render_assignment("CROWLEY_DATA", value, header="hello")
    assert text.isascii()
    assert savedvars.loads(text.encode())["CROWLEY_DATA"] == value
    assert to_lua(True) == "true" and to_lua(None) == "nil"


def test_house_validation():
    house = House("us", "pvp", "horde")
    assert house.key == "us-pvp-horde" and house.ah_cut == 0.05
    assert House.parse("eu-normal-neutral").ah_cut == 0.15
    assert House("beta", "normal", "horde").key == "beta-normal-horde"
    with pytest.raises(ValueError, match="ruleset"):
        House("us", "roleplay", "horde")
    with pytest.raises(ValueError, match="region"):
        House("test", "normal", "horde")
    with pytest.raises(ValueError):
        House.parse("us-normal")


def _plan(item_ids: list[int] | None = None) -> tuple[str, str]:
    item_ids = item_ids or [10]
    body = {
        "schemaVersion": 2,
        "id": "first-run",
        "revision": 1,
        "catalogHash": "a" * 64,
        "targetClientBuild": "1.60.1.1",
        "cadenceSeconds": 900,
        "initialPriceLevels": 20,
        "itemIDs": item_ids,
        "pools": {"core": item_ids, "discovery": []},
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


def _encode_native_payload(value: dict) -> str:
    """Encode an internal payload using WoW's empty keyed-table wire shape."""

    def native(child):
        if isinstance(child, dict):
            return {
                key: []
                if key in {"bonusIDs", "names", "rows"} and isinstance(grandchild, dict) and not grandchild
                else native(grandchild)
                for key, grandchild in child.items()
            }
        if isinstance(child, list):
            return [native(grandchild) for grandchild in child]
        return child

    return encode_json_payload(native(value))


def raw_scan(**overrides):
    plan_json, plan_hash = _plan()
    observation = {
        "itemID": 10,
        "ordinal": 1,
        "kind": "commodity",
        "query": {"kind": "queried", "startedAt": 1759999990, "finishedAt": 1760000000},
        "coverage": "full",
        "sourceComplete": True,
        "cutoff": {"kind": "unbounded"},
        "higherPriceWitness": {"kind": "unknown"},
        "reasonCode": "ok",
        "rowCount": 1,
        "rows": {
            "row:1": {
                "resultID": 1,
                "itemID": 10,
                "itemKey": "10",
                "rawItemKey": {
                    "itemID": 10,
                    "itemLevel": 0,
                    "itemSuffix": 0,
                    "battlePetSpeciesID": 0,
                    "bonusIDs": [],
                },
                "link": {"kind": "unknown"},
                "quantity": 2,
                "price": {"kind": "unit", "copper": 300},
                "unitPrice": {"numerator": 300, "denominator": 1},
                "owners": {
                    "names": [],
                    "totalNumberOfOwners": 0,
                    "containsOwnerItem": False,
                    "containsAccountItem": False,
                    "identityKind": "grouped",
                },
                "timeLeft": {"kind": "unknown"},
            }
        },
    }
    raw = {
        "id": "s1",
        "format": 2,
        "startedAt": 1759999990,
        "finishedAt": 1760000000,
        "numRows": 1,
        "numObservations": 1,
        "house": {
            "region": "us",
            "ruleset": "normal",
            "faction": "alliance",
            "source": "auto",
            "key": "us-normal-alliance",
        },
        "context": {
            "character": "Tester",
            "realm": "Normal",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
            "clientBuild": "70338",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "payload": encode_json_payload(
            {
                "planHash": plan_hash,
                "planJSON": plan_json,
                "observations": {"10": observation},
                "owned": {
                    "status": "unqueried",
                    "query": {"kind": "unqueried"},
                    "reasonCode": "not_started",
                    "rowCount": 0,
                    "rows": [],
                },
            }
        ),
    }
    raw.update(overrides)
    return raw


def test_format2_validates_exact_payload_and_counts():
    raw = raw_scan()
    original_payload = raw["payload"]
    parsed = validate_envelope(raw)
    assert parsed["plan"]["cadenceSeconds"] == 900
    assert parsed["decoded"]["observations"]["10"]["rowCount"] == 1
    assert parsed["decoded"]["observations"]["10"]["rows"]["row:1"]["rawItemKey"]["bonusIDs"] == {}
    assert raw["payload"] == original_payload


@pytest.mark.parametrize(
    ("collection", "invalid"),
    [
        ("bonusIDs", {}),
        ("bonusIDs", [1]),
        ("names", {}),
        ("names", ["Seller"]),
        ("observation_rows", {}),
        ("observation_rows", ["row:1"]),
        ("owned_rows", {}),
        ("owned_rows", ["77"]),
    ],
)
def test_native_keyed_collections_reject_non_native_empty_and_array_shapes(collection, invalid):
    raw = raw_scan()
    payload = decode_json_payload(raw["payload"])
    if collection == "bonusIDs":
        payload["observations"]["10"]["rows"]["row:1"]["rawItemKey"]["bonusIDs"] = invalid
    elif collection == "names":
        payload["observations"]["10"]["rows"]["row:1"]["owners"]["names"] = invalid
    elif collection == "observation_rows":
        payload["observations"]["10"]["rows"] = invalid
        payload["observations"]["10"]["rowCount"] = 0
    else:
        payload["owned"]["rows"] = invalid
        payload["owned"]["rowCount"] = 0
    raw["payload"] = encode_json_payload(payload)
    with pytest.raises(ScanFormatError, match="empty array or non-empty object"):
        validate_envelope(raw)


def test_generic_payload_encoder_does_not_repair_an_empty_object():
    raw = raw_scan()
    payload = decode_json_payload(raw["payload"])
    payload["owned"]["rows"] = {}
    raw["payload"] = encode_json_payload(payload)
    with pytest.raises(ScanFormatError, match="empty array or non-empty object"):
        validate_envelope(raw)


def test_format2_rejects_old_format_and_malformed_scope():
    with pytest.raises(ScanFormatError, match="formats 2 and 3"):
        validate_envelope(raw_scan(format=1))
    malformed = raw_scan(numRows=2)
    with pytest.raises(ScanFormatError, match="counts"):
        validate_envelope(malformed)


def test_format4_is_rejected():
    with pytest.raises(ScanFormatError, match="formats 2 and 3"):
        validate_envelope(raw_scan(format=4))


def test_format2_rejects_fraction_not_reduced_and_unknown_fields():
    raw = raw_scan()
    decoded = validate_envelope(raw)["decoded"]
    row = decoded["observations"]["10"]["rows"]["row:1"]
    row["unitPrice"] = {"numerator": 2, "denominator": 2}
    raw["payload"] = _encode_native_payload(decoded)
    with pytest.raises(ScanFormatError, match="not reduced"):
        validate_envelope(raw)


def test_outer_json_decoder_rejects_duplicate_keys():
    with pytest.raises(ScanFormatError, match="duplicate"):
        decode_strict_json('{"id":"a","id":"b"}')


def test_unqueried_abort_tail_preserves_reason_and_invariants():
    raw = raw_scan()
    plan_json, plan_hash = _plan([10, 11])
    payload = validate_envelope(raw)["decoded"]
    payload["planJSON"] = plan_json
    payload["planHash"] = plan_hash
    payload["observations"]["10"]["ordinal"] = 1
    payload["observations"]["11"] = {
        "itemID": 11,
        "ordinal": 2,
        "kind": "unknown",
        "query": {"kind": "unqueried"},
        "coverage": "unqueried",
        "sourceComplete": False,
        "cutoff": {"kind": "unknown"},
        "higherPriceWitness": {"kind": "unknown"},
        "reasonCode": "logout",
        "rowCount": 0,
        "rows": [],
    }
    raw["numObservations"] = 2
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["11"]["reasonCode"] == "logout"


def test_full_depth_over_plan_is_rejected():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    rows = {}
    for index in range(1, 22):
        row = dict(observation["rows"]["row:1"])
        row["resultID"] = index
        row["price"] = {"kind": "unit", "copper": 100 + index}
        row["unitPrice"] = {"numerator": 100 + index, "denominator": 1}
        rows[f"row:{index}"] = row
    observation["rows"] = rows
    observation["rowCount"] = 21
    raw["numRows"] = 21
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="plan depth"):
        validate_envelope(raw)


@pytest.mark.parametrize("field,value", [("context", None), ("startedAt", "bad"), ("numRows", None)])
def test_malformed_wire_types_raise_scan_format_error(field, value):
    raw = raw_scan(**{field: value})
    with pytest.raises(ScanFormatError):
        validate_envelope(raw)


def test_item_buyout_quantity_uses_reduced_fraction_without_float_division():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    observation["kind"] = "item"
    row = observation["rows"]["row:1"]
    row["link"] = {"kind": "known", "value": "|Hitem:10:0:0:0:0:0:0:0:1:0:0:0:0|h[x]|h"}
    row["quantity"] = 2
    row["price"] = {"kind": "auction", "copper": 3}
    row["unitPrice"] = {"numerator": 3, "denominator": 2}
    row["owners"] = {
        "names": {"Seller": True},
        "totalNumberOfOwners": 1,
        "containsOwnerItem": False,
        "containsAccountItem": False,
        "identityKind": "individual",
    }
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["10"]["rows"]["row:1"]["unitPrice"] == {
        "numerator": 3,
        "denominator": 2,
    }


def _format3_item_raw(*, unit_price: tuple[int, int] = (1500, 1), quantity: int = 4) -> dict:
    raw = raw_scan(format=3)
    payload = decode_json_payload(raw["payload"])
    observation = payload["observations"]["10"]
    observation["kind"] = "item"
    row = observation["rows"]["row:1"]
    row["link"] = {"kind": "known", "value": "|Hitem:10:0:0:0:0:0:0:0:1:0:0:0:0|h[x]|h"}
    row["quantity"] = quantity
    row["price"] = {"kind": "auction", "copper": 1500}
    row["unitPrice"] = {"numerator": unit_price[0], "denominator": unit_price[1]}
    row["owners"] = {
        "names": [],
        "totalNumberOfOwners": 0,
        "containsOwnerItem": False,
        "containsAccountItem": False,
        "identityKind": "grouped",
    }
    raw["payload"] = _encode_native_payload(payload)
    return raw


def test_format3_corrects_item_units_while_format2_keeps_historical_rule():
    format3 = _format3_item_raw()
    assert validate_envelope(format3)["decoded"]["observations"]["10"]["rows"]["row:1"]["unitPrice"] == {
        "numerator": 1500,
        "denominator": 1,
    }
    with pytest.raises(ScanFormatError, match="item auction price"):
        validate_envelope(_format3_item_raw(unit_price=(375, 1)))

    format2 = _format3_item_raw(unit_price=(375, 1))
    format2["format"] = 2
    assert validate_envelope(format2)["decoded"]["observations"]["10"]["rows"]["row:1"]["unitPrice"] == {
        "numerator": 375,
        "denominator": 1,
    }
    format2_corrected = _format3_item_raw(unit_price=(1500, 1))
    format2_corrected["format"] = 2
    with pytest.raises(ScanFormatError, match="reduced buyout / quantity"):
        validate_envelope(format2_corrected)


@pytest.mark.parametrize("seconds", [-301, 172801, 4_294_967_293])
def test_format3_rejects_out_of_range_exact_time_left(seconds):
    raw = raw_scan(format=3)
    payload = decode_json_payload(raw["payload"])
    payload["observations"]["10"]["rows"]["row:1"]["timeLeft"] = {
        "kind": "exact",
        "seconds": seconds,
    }
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="seconds"):
        validate_envelope(raw)


def test_format3_accepts_decoded_negative_exact_time_left():
    raw = raw_scan(format=3)
    payload = decode_json_payload(raw["payload"])
    payload["observations"]["10"]["rows"]["row:1"]["timeLeft"] = {
        "kind": "exact",
        "seconds": -3,
    }
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["10"]["rows"]["row:1"]["timeLeft"] == {
        "kind": "exact",
        "seconds": -3,
    }


def test_format2_wrapped_time_left_normalizes_without_mutating_input():
    raw = raw_scan()
    payload = decode_json_payload(raw["payload"])
    observation = payload["observations"]["10"]
    observation["rows"]["row:1"]["timeLeft"] = {"kind": "exact", "seconds": 4_294_967_293}
    original = json.loads(json.dumps(observation))
    normalized = normalize_observation(2, "commodity", observation)
    assert normalized["rows"]["row:1"]["timeLeft"] == {"kind": "exact", "seconds": -3}
    assert observation == original

    item_raw = _format3_item_raw(unit_price=(375, 1))
    item_observation = decode_json_payload(item_raw["payload"])["observations"]["10"]
    item_observation["rows"]["row:1"]["timeLeft"] = {"kind": "exact", "seconds": 4_294_967_293}
    item_original = json.loads(json.dumps(item_observation))
    item_normalized = normalize_observation(2, "item", item_observation)
    assert item_normalized["rows"]["row:1"]["unitPrice"] == {"numerator": 1500, "denominator": 1}
    assert item_normalized["rows"]["row:1"]["timeLeft"] == {"kind": "exact", "seconds": -3}
    assert item_observation == item_original


def test_format2_bounded_item_observation_cannot_be_normalized():
    raw = _format3_item_raw(unit_price=(375, 1))
    observation = decode_json_payload(raw["payload"])["observations"]["10"]
    observation["coverage"] = "bounded"
    with pytest.raises(ScanFormatError, match="bounded"):
        normalize_observation(2, "item", observation)


def test_numeric_range_partial_preserves_safe_fractional_rows_without_cross_product():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    observation.update(
        {
            "kind": "item",
            "coverage": "incomplete",
            "sourceComplete": False,
            "cutoff": {"kind": "unknown"},
            "higherPriceWitness": {"kind": "unknown"},
            "reasonCode": "numeric_range",
        }
    )
    link = {"kind": "known", "value": "|Hitem:10:0:0:0:0:0:0:0:1:0:0:0:0|h[x]|h"}
    owners = {
        "names": {"Seller": True},
        "totalNumberOfOwners": 1,
        "containsOwnerItem": False,
        "containsAccountItem": False,
        "identityKind": "individual",
    }
    first_copper = MAX_SAFE_INTEGER
    first_quantity = MAX_SAFE_INTEGER - 1
    second_copper = MAX_SAFE_INTEGER - 1
    second_quantity = MAX_SAFE_INTEGER - 2
    observation["rows"] = {
        "row:1": {
            **observation["rows"]["row:1"],
            "resultID": 1,
            "link": link,
            "owners": owners,
            "quantity": first_quantity,
            "price": {"kind": "auction", "copper": first_copper},
            "unitPrice": {
                "numerator": first_copper,
                "denominator": first_quantity,
            },
        },
        "row:2": {
            **observation["rows"]["row:1"],
            "resultID": 2,
            "link": link,
            "owners": owners,
            "quantity": second_quantity,
            "price": {"kind": "auction", "copper": second_copper},
            "unitPrice": {
                "numerator": second_copper,
                "denominator": second_quantity,
            },
        },
    }
    observation["rowCount"] = 2
    raw["numRows"] = 2
    raw["payload"] = _encode_native_payload(payload)
    parsed = validate_envelope(raw)
    assert parsed["decoded"]["observations"]["10"]["reasonCode"] == "numeric_range"
    assert parsed["decoded"]["observations"]["10"]["rowCount"] == 2


def test_successful_scope_rejects_unsafe_nonadjacent_price_product():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    observation["kind"] = "item"
    link = {"kind": "known", "value": "|Hitem:10:0:0:0:0:0:0:0:1:0:0:0:0|h[x]|h"}
    owners = {
        "names": {"Seller": True},
        "totalNumberOfOwners": 1,
        "containsOwnerItem": False,
        "containsAccountItem": False,
        "identityKind": "individual",
    }
    fractions = [
        (MAX_SAFE_INTEGER - 1, MAX_SAFE_INTEGER),
        (1, 1),
        (MAX_SAFE_INTEGER, MAX_SAFE_INTEGER - 1),
    ]
    rows = {}
    for index, (copper, quantity) in enumerate(fractions, 1):
        row = dict(observation["rows"]["row:1"])
        row.update(
            {
                "resultID": index,
                "link": link,
                "owners": owners,
                "quantity": quantity,
                "price": {"kind": "auction", "copper": copper},
                "unitPrice": {"numerator": copper, "denominator": quantity},
            }
        )
        rows[f"row:{index}"] = row
    observation["rows"] = rows
    observation["rowCount"] = len(rows)
    raw["numRows"] = len(rows)
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="unsafe"):
        validate_envelope(raw)


def test_incomplete_partial_may_exceed_depth_and_preserve_unordered_rows():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    observation.update(
        {
            "coverage": "incomplete",
            "sourceComplete": False,
            "cutoff": {"kind": "unknown"},
            "higherPriceWitness": {"kind": "unknown"},
            "reasonCode": "page_limit",
        }
    )
    rows = {}
    for index in range(1, 22):
        row = dict(observation["rows"]["row:1"])
        row["resultID"] = index
        row["price"] = {"kind": "unit", "copper": 1000 - index}
        row["unitPrice"] = {"numerator": 1000 - index, "denominator": 1}
        rows[f"row:{index}"] = row
    observation["rows"] = rows
    observation["rowCount"] = len(rows)
    raw["numRows"] = len(rows)
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["10"]["rowCount"] == 21


def test_successful_equal_price_rows_use_numeric_ordinals_and_allow_any_tie_order():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    rows = {}
    for index in range(1, 13):
        row = dict(observation["rows"]["row:1"])
        row["resultID"] = index
        rows[f"row:{index}"] = row
    observation["rows"] = rows
    observation["rowCount"] = len(rows)
    raw["numRows"] = len(rows)
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["10"]["rowCount"] == 12


def test_bounded_success_requires_ok_and_exact_plan_depth():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    observation = payload["observations"]["10"]
    observation.update(
        {
            "coverage": "bounded",
            "sourceComplete": False,
            "cutoff": {"kind": "price", "numerator": 20, "denominator": 1},
            "higherPriceWitness": {"kind": "known", "numerator": 21, "denominator": 1},
        }
    )
    rows = {}
    for index in range(1, 21):
        row = dict(observation["rows"]["row:1"])
        row["resultID"] = index
        row["price"] = {"kind": "unit", "copper": index}
        row["unitPrice"] = {"numerator": index, "denominator": 1}
        rows[f"row:{index}"] = row
    observation["rows"] = rows
    observation["rowCount"] = len(rows)
    raw["numRows"] = len(rows)
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["observations"]["10"]["coverage"] == "bounded"
    last_row = dict(rows["row:20"])
    del observation["rows"]["row:20"]
    observation["rowCount"] = 19
    raw["numRows"] = 19
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="bounded"):
        validate_envelope(raw)
    observation["rows"]["row:20"] = last_row
    observation["rowCount"] = 20
    raw["numRows"] = 20
    observation["reasonCode"] = "page_limit"
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="bounded"):
        validate_envelope(raw)


def test_owned_house_changed_is_an_incomplete_queried_batch():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    payload["owned"] = {
        "status": "incomplete",
        "query": {"kind": "queried", "startedAt": 1759999990, "finishedAt": 1760000000},
        "reasonCode": "house_changed",
        "rowCount": 0,
        "rows": [],
    }
    raw["payload"] = _encode_native_payload(payload)
    assert validate_envelope(raw)["decoded"]["owned"]["reasonCode"] == "house_changed"


def test_owned_unqueried_status_requires_unqueried_query():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    payload["owned"]["query"] = {"kind": "queried", "startedAt": 1759999990, "finishedAt": 1760000000}
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="unqueried"):
        validate_envelope(raw)


def test_owned_queried_status_requires_queried_query():
    raw = raw_scan()
    payload = validate_envelope(raw)["decoded"]
    payload["owned"] = {
        "status": "incomplete",
        "query": {"kind": "unqueried"},
        "reasonCode": "not_started",
        "rowCount": 0,
        "rows": [],
    }
    raw["payload"] = _encode_native_payload(payload)
    with pytest.raises(ScanFormatError, match="unqueried"):
        validate_envelope(raw)
