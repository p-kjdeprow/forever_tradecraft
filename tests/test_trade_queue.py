import json
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from crowley.core import savedvars
from crowley.core.luawriter import render_assignment
from crowley.core.queue import (
    MAX_CHARACTER_BYTES,
    MAX_HOUSE_BYTES,
    MAX_MESSAGES,
    MAX_QUEUE_ID_BYTES,
    QueueFormatError,
    build_queue,
    render_queue_lua,
    validate_execution_record,
    validate_queue,
)
from crowley.trading import __main__ as trading_main
from crowley.trading import queue_cli
from crowley.trading.queue_cli import (
    QueueCLIError,
    map_pending_intents,
    preview_spec,
    publish_spec,
    reconcile_queue,
)
from tests.wow_harness import WowClient

SERVER_URL = "http://test"


def test_trading_server_url_prefers_explicit_value_or_host_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trading_main, "host_config_path", lambda: Path("host.toml"))
    monkeypatch.setattr(
        trading_main,
        "load_host_config",
        lambda path: (
            SimpleNamespace(server_url="http://configured")
            if path == Path("host.toml")
            else pytest.fail(f"unexpected host path: {path}")
        ),
    )

    assert trading_main._server_url("http://explicit") == "http://explicit"
    assert trading_main._server_url(None) == "http://configured"


def queue_spec(**overrides):
    value = {
        "house": "us-normal-alliance",
        "character": "Tester",
        "generatedAt": 1_700_000_000,
        "expiresAt": 1_700_000_600,
        "actions": [
            {"seq": 1, "kind": "buy_commodity", "itemID": 2589, "quantity": 20, "maxUnitPrice": 25},
            {
                "seq": 2,
                "kind": "post_commodity",
                "itemID": 2589,
                "quantity": 20,
                "unitPrice": 30,
                "durationHours": 12,
            },
        ],
    }
    value.update(overrides)
    return value


def test_build_and_render_round_trip_and_empty_queue():
    queue = build_queue(queue_spec())
    loaded = savedvars.loads(render_queue_lua(queue).encode())
    assert loaded["CROWLEY_QUEUE"] == queue
    assert savedvars.loads(render_queue_lua(None).encode()) == {"CROWLEY_QUEUE": False}


def _lua_queue_validation(queue: dict) -> tuple[bool, str | None]:
    """Execute the exact rendered assignment through the addon Lua validator."""

    source = render_assignment("CROWLEY_QUEUE", queue, header="Round-3 boundary fixture")
    client = WowClient()
    client.lua.execute(source)
    result = client.ns.Executor.ValidateQueue(client.lua.globals().CROWLEY_QUEUE)
    return result if isinstance(result, tuple) else (result, None)


def _lua_boundary_queue() -> dict:
    return {
        "format": 1,
        "queueId": "é" * (MAX_QUEUE_ID_BYTES // 2),
        "generatedAt": 1_700_000_000,
        "expiresAt": 1_700_000_600,
        "house": "h" * MAX_HOUSE_BYTES,
        "character": "c" * MAX_CHARACTER_BYTES,
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": MAX_MESSAGES}],
    }


@pytest.mark.parametrize("field", ["queueId", "house", "character"], ids=["queue-id", "house", "character"])
def test_rendered_lua_accepts_byte_bound_and_rejects_bound_plus_one(field):
    queue = _lua_boundary_queue()
    assert _lua_queue_validation(queue)[0] is True
    queue[field] = queue[field] + "x"
    assert _lua_queue_validation(queue)[0] is False


def test_rendered_lua_accepts_uint32_max_messages_and_rejects_plus_one():
    queue = _lua_boundary_queue()
    assert _lua_queue_validation(queue)[0] is True
    queue["actions"][0]["maxMessages"] = MAX_MESSAGES + 1
    assert _lua_queue_validation(queue)[0] is False


def test_python_queue_bounds_match_lua_wire_limits():
    queue = build_queue(queue_spec(actions=[{"seq": 1, "kind": "mail_collect", "maxMessages": MAX_MESSAGES}]))
    assert validate_queue(queue)["actions"][0]["maxMessages"] == MAX_MESSAGES
    rendered = savedvars.loads(render_queue_lua(queue).encode())["CROWLEY_QUEUE"]
    client = WowClient(queue=rendered)
    client.login()
    assert client.snapshot_agent()["executor"]["availability"] == "valid"
    with pytest.raises(QueueFormatError, match="queue.house"):
        build_queue(queue_spec(house="h" * (MAX_HOUSE_BYTES + 1)))
    with pytest.raises(QueueFormatError, match="queue.character"):
        build_queue(queue_spec(character="c" * (MAX_CHARACTER_BYTES + 1)))
    cases = [
        ("queueId", "q" * MAX_QUEUE_ID_BYTES + "é", "queue.queueId"),
        ("house", "h" * (MAX_HOUSE_BYTES + 1), "queue.house"),
        ("character", "c" * (MAX_CHARACTER_BYTES + 1), "queue.character"),
    ]
    for field, value, path in cases:
        malformed = dict(queue)
        malformed[field] = value
        with pytest.raises(QueueFormatError, match=path):
            validate_queue(malformed)

    malformed = dict(queue)
    malformed["actions"] = [{"seq": 1, "kind": "mail_collect", "maxMessages": MAX_MESSAGES + 1}]
    with pytest.raises(QueueFormatError, match="maxMessages"):
        validate_queue(malformed)
    with pytest.raises(QueueFormatError, match="maxMessages"):
        render_queue_lua(malformed)


@pytest.mark.parametrize(
    "change",
    [
        {"format": 2},
        {"expiresAt": 1_700_000_000},
        {"actions": [{"seq": 2, "kind": "vendor_buy", "itemID": 1, "quantity": 1}]},
        {
            "actions": [
                {
                    "seq": 1,
                    "kind": "post_commodity",
                    "itemID": 1,
                    "quantity": 1,
                    "unitPrice": 1,
                    "durationHours": 6,
                }
            ]
        },
        {"actions": [{"seq": 1, "kind": "vendor_buy", "itemID": 0, "quantity": 1}]},
    ],
)
def test_queue_rejects_contract_violations(change):
    spec = queue_spec(**change)
    if "format" in change:
        queue = build_queue(queue_spec())
        queue["format"] = change["format"]
        with pytest.raises(QueueFormatError):
            validate_queue(queue)
    else:
        with pytest.raises(QueueFormatError):
            build_queue(spec)


def test_queue_rejects_more_than_fifty_actions():
    actions = [{"seq": index, "kind": "vendor_buy", "itemID": 1, "quantity": 1} for index in range(1, 52)]
    with pytest.raises(QueueFormatError):
        build_queue(queue_spec(actions=actions))


def test_execution_record_matches_action_and_requires_block_reason():
    queue = build_queue(queue_spec())
    execution = {
        "queueId": queue["queueId"],
        "house": queue["house"],
        "character": queue["character"],
        "results": [
            {"seq": 1, "kind": "buy_commodity", "status": "blocked", "at": 1_700_000_001, "detail": {}},
        ],
    }
    with pytest.raises(QueueFormatError):
        validate_execution_record(execution, queue)
    execution["results"][0]["detail"] = {"reason": "scanner_active"}
    assert validate_execution_record(execution, queue)["results"] == execution["results"]


def test_historical_execution_parser_keeps_unbounded_record_identity_fields():
    character = "c" * (MAX_CHARACTER_BYTES + 1)
    execution = {
        "queueId": f"us-normal-alliance-{character}-1700000000-0000",
        "house": "us-normal-alliance",
        "character": character,
        "results": [],
    }
    assert validate_execution_record(execution)["character"] == character


def test_paper_intents_map_cancel_to_preview_skip():
    actions, skipped = map_pending_intents(
        {
            "strategy": [
                {"type": "Buy", "item_id": 2589, "quantity": 2, "max_unit_price": 25},
                {"type": "Post", "item_id": 2589, "quantity": 2, "unit_price": 30, "duration_hours": 24},
                {"type": "VendorSell", "item_id": 1, "quantity": 2},
                {"type": "VendorBuy", "item_id": 2, "quantity": 3},
                {"type": "Craft", "spell_id": 123, "times": 1},
                {"type": "Cancel", "listing_id": 999},
            ]
        }
    )
    assert [action["kind"] for action in actions] == [
        "buy_commodity",
        "post_commodity",
        "vendor_sell",
        "vendor_buy",
        "craft",
    ]
    assert [action["seq"] for action in actions] == [1, 2, 3, 4, 5]
    assert skipped[0]["reason"]


def test_armed_publish_requires_confirmation_before_network(monkeypatch):
    def fail_client(*args, **kwargs):
        raise AssertionError("network must not be reached")

    monkeypatch.setattr("crowley.trading.queue_cli._client", fail_client)
    armed_queue = build_queue(queue_spec(armed=True))
    with pytest.raises(QueueCLIError, match="--confirm-real-trades <queueId>"):
        publish_spec(armed_queue, server_url=SERVER_URL, armed=True)


@pytest.mark.parametrize("armed", ["false", 1])
def test_truthy_non_boolean_armed_is_rejected(armed):
    with pytest.raises(QueueFormatError):
        build_queue(queue_spec(armed=armed))


def test_preview_writes_exact_queue_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    output = tmp_path / "queue.json"
    queue = preview_spec(queue_spec(), output)
    assert json.loads(output.read_text()) == queue
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert f"queueId={queue['queueId']}" in capsys.readouterr().out
    with pytest.raises(QueueCLIError, match="overwrite"):
        preview_spec(queue_spec(), output)


def test_armed_publish_of_minimal_spec_requires_preview_file(monkeypatch):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(QueueCLIError, match="queue preview --armed --out"):
        publish_spec(queue_spec(), server_url=SERVER_URL, armed=True, confirm_real_trades="anything")


def test_armed_publish_previews_exact_final_queue_before_confirmation_error(monkeypatch, capsys):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    armed_queue = build_queue(queue_spec(armed=True))
    with pytest.raises(QueueCLIError, match="<queueId>"):
        publish_spec(armed_queue, server_url=SERVER_URL, armed=True)
    output = capsys.readouterr().out
    assert '"armed": true' in output
    assert armed_queue["queueId"] in output


@pytest.mark.parametrize("confirmation", ["wrong-full-queue-id", "us-norma"])
def test_armed_publish_rejects_nonmatching_confirmation_without_network(monkeypatch, confirmation):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    armed_queue = build_queue(queue_spec(armed=True))
    with pytest.raises(QueueCLIError, match="full queueId"):
        publish_spec(armed_queue, server_url=SERVER_URL, armed=True, confirm_real_trades=confirmation)


def test_armed_publish_submits_the_previewed_file_with_full_confirmation(tmp_path, monkeypatch, capsys):
    posted = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, path, json):
            posted.append((path, json))
            return httpx.Response(200, json=json, request=httpx.Request("POST", "http://test/v1/queues"))

    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: Client())
    output = tmp_path / "armed-queue.json"
    preview_spec(queue_spec(), output, armed=True)
    armed_queue = json.loads(output.read_text())
    published = publish_spec(
        armed_queue,
        server_url=SERVER_URL,
        armed=True,
        confirm_real_trades=armed_queue["queueId"],
    )
    assert posted[0][1] == {
        **armed_queue,
        "source": "spec",
        "confirmRealTrades": armed_queue["queueId"],
    }
    assert published["armed"] is True
    assert armed_queue["queueId"] in capsys.readouterr().out


@pytest.mark.parametrize(
    ("queue_armed", "flag_armed"),
    [(False, True), (True, False)],
)
def test_publish_rejects_armed_flag_file_mismatch(monkeypatch, queue_armed, flag_armed):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    queue = build_queue(queue_spec(armed=queue_armed))
    with pytest.raises(QueueCLIError):
        publish_spec(
            queue,
            server_url=SERVER_URL,
            armed=flag_armed,
            confirm_real_trades=queue["queueId"] if flag_armed else None,
        )


def test_spec_cannot_arm_without_cli_flag(monkeypatch):
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(QueueCLIError, match="requires --armed"):
        publish_spec(queue_spec(armed=True), server_url=SERVER_URL)


def test_from_paper_publish_is_always_unarmed(monkeypatch):
    pending = {
        "strategy": [
            {"type": "Buy", "item_id": 2589, "quantity": 2, "max_unit_price": 25},
            {"type": "Cancel", "listing_id": 7},
        ]
    }
    posted = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, path, json):
            posted.append(json)
            return httpx.Response(200, json=json, request=httpx.Request("POST", "http://test/v1/queues"))

    monkeypatch.setattr(queue_cli, "_paper_pending", lambda *_args: ("us-normal-alliance", pending))
    monkeypatch.setattr(queue_cli, "_client", lambda *_args, **_kwargs: Client())
    queue, skipped, _ = queue_cli.from_paper("unused", "run", "Tester", server_url=SERVER_URL)
    assert queue["armed"] is False
    assert posted[0]["armed"] is False
    assert skipped and skipped[0]["intent"]["type"] == "Cancel"


def test_reconcile_flags_price_and_missing_terminal_result():
    queue = build_queue(queue_spec())
    execution = {
        "queueId": queue["queueId"],
        "house": queue["house"],
        "character": queue["character"],
        "results": [
            {
                "seq": 1,
                "kind": "buy_commodity",
                "status": "succeeded",
                "at": 1_700_000_001,
                "detail": {"unitPrice": 26, "totalPrice": 520},
            },
            {
                "seq": 2,
                "kind": "post_commodity",
                "status": "started",
                "at": 1_700_000_002,
                "detail": {},
            },
        ],
    }
    report = reconcile_queue(queue, execution)
    assert {issue["issue"] for issue in report["issues"]} == {
        "price_above_max",
        "missing_terminal_result",
    }
    assert not report["ok"]
