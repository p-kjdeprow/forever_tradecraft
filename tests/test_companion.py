import json
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from crowley.companion.config import CompanionSettings
from crowley.companion.sync import (
    MAX_HISTORY_ENTRIES,
    MAX_REJECTIONS,
    REJECTION_RETENTION_DAYS,
    UPLOAD_RETENTION_DAYS,
    Companion,
    FailureClass,
    RejectionKey,
    RejectionLedgerFull,
    State,
    StateError,
    UploadResponseError,
    classify_http_status,
    classify_transport_error,
)
from crowley.core import savedvars
from crowley.core.companion_state import (
    STATE_VERSION,
    CompanionState,
    CompanionStateError,
    LegacyCompanionStateError,
    dump_bytes,
    parse_bytes,
    upgrade_legacy_state_bytes,
)
from crowley.core.game_target import load_game_target, target_scan_plan
from crowley.core.luawriter import render_assignment
from crowley.core.queue import build_queue, render_queue_lua
from crowley.core.watchlist import load_catalog

from .test_core import raw_scan
from .wow_harness import WowClient

FIXTURE = Path(__file__).parent / "fixtures/native-construction-unknown-savedvars.lua"
TARGET_FIXTURE = Path(__file__).parent / "fixtures/targets/verified-beta.json"


def _settings_for(
    root: Path,
    *,
    target=None,
    account: str = "Test",
    house_key: str | None = None,
    wow_dir: Path | None = None,
) -> CompanionSettings:
    selected = load_game_target(TARGET_FIXTURE) if target is None else target
    catalog = load_catalog()
    plan = target_scan_plan(catalog, selected)
    flavor = Path(wow_dir) if wow_dir is not None else root / selected.product_dir
    account_dir = flavor / "WTF" / "Account" / account
    addon = flavor / "Interface" / "AddOns" / "Crowley_Data"
    return CompanionSettings(
        target=selected,
        catalog=catalog,
        canonical_plan=plan,
        route_provenance=selected.route_provenance,
        wow_dir=flavor,
        account_dir=account_dir,
        server_url="http://test",
        api_token="test-token",
        wtf_account=account,
        house_key=house_key,
        state_file=root / "state" / "companion.json",
        saved_variables=account_dir / "SavedVariables" / "Crowley.lua",
        data_lua=addon / "Data.lua",
        queue_lua=addon / "Queue.lua",
        addons_dir=flavor / "Interface" / "AddOns",
    )


def write_savedvars(path, scans, probe=None, reference=None, account=None, executions=None):
    db = {"scans": {scan["id"]: scan for scan in scans}}
    if probe is not None:
        db["probe"] = probe
    if reference is not None:
        db["reference"] = reference
    if account is not None:
        db["account"] = account
    if executions is not None:
        db["executions"] = executions
    path.write_text(render_assignment("CrowleyDB", db, header="test"))


@pytest.fixture
def companion(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = load_game_target(TARGET_FIXTURE)
    wow_dir = tmp_path / target.product_dir
    settings = _settings_for(tmp_path, target=target, wow_dir=wow_dir)
    saved = wow_dir / "WTF/Account/Test/SavedVariables/Crowley.lua"
    saved.parent.mkdir(parents=True)
    settings.data_lua.parent.mkdir(parents=True)
    instance = Companion(settings)
    instance.client.close()
    try:
        yield instance, saved
    finally:
        instance.client.close()


def test_companion_settings_reject_stale_target_identity():
    target = load_game_target(TARGET_FIXTURE)
    expected_wow_dir = Path("fixture") / target.product_dir
    with pytest.raises(ValueError, match="wow_dir"):
        _settings_for(Path("fixture"), target=target, wow_dir=Path("stale-wow"))
    with pytest.raises(ValueError, match="house_key"):
        _settings_for(Path("fixture"), target=target, wow_dir=expected_wow_dir, house_key="us-normal-horde")


@pytest.mark.parametrize("failure", ["s1", "s2", "probe"])
def test_retries_failed_file_without_reuploading_successful_scans(companion, failure):
    instance, saved = companion
    probe = {"at": 1760000000}
    write_savedvars(saved, [raw_scan(id="s1"), raw_scan(id="s2")], probe)
    attempts = []
    uploaded = []
    probes = []
    failed = False
    plan = load_catalog().scan_plan()

    def respond(request):
        nonlocal failed
        body = json.loads(request.content)
        if request.url.path == "/v1/scans":
            target = body["id"]
        elif request.url.path == "/v1/probes":
            target = "probe"
        else:
            return httpx.Response(
                200,
                json={"generatedAt": 1, "ackedScans": uploaded.copy(), "scanPlan": plan},
            )
        attempts.append(target)
        if target == failure and not failed:
            failed = True
            raise httpx.ConnectError("temporary outage", request=request)
        if target == "probe":
            probes.append(body)
            return httpx.Response(200, json={"status": "ok"})
        uploaded.append(target)
        return httpx.Response(200, json={"house": "us-normal-alliance", "listings": 2, "status": "created"})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.tick()  # First observation: wait for the file to hold still.
    assert attempts == []
    with pytest.raises(httpx.ConnectError):
        instance.tick()
    instance.tick()  # Unchanged file must be retried on the next poll.

    assert uploaded == ["s1", "s2"]
    assert probes == [probe]
    assert attempts.count(failure) == 2
    assert all(attempts.count(target) == 1 for target in {"s1", "s2", "probe"} - {failure})
    persisted = State.load(instance.state_file)
    assert set(persisted.uploaded) == {"s1", "s2"}
    assert persisted.probes == [probe["at"]]
    data = savedvars.load_file(instance.settings.data_lua)["CROWLEY_DATA"]
    assert data["ackedScans"] == ["s1", "s2"]
    assert data["scanPlan"] == plan
    assert "shoppingLists" not in data

    instance.tick()
    assert len(attempts) == 4  # Completed files stay processed.


def test_changed_file_waits_for_stability_after_failed_upload(companion):
    instance, saved = companion
    write_savedvars(saved, [raw_scan(id="s1")])
    attempts = []

    def respond(request):
        if request.url.path == "/v1/scans":
            attempts.append(json.loads(request.content)["id"])
            if len(attempts) == 1:
                raise httpx.ConnectError("temporary outage", request=request)
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 2, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.tick()
    with pytest.raises(httpx.ConnectError):
        instance.tick()

    write_savedvars(saved, [raw_scan(id="s1"), raw_scan(id="s2")])
    instance.tick()
    assert attempts == ["s1"]  # Changed content needs a new stability interval.
    instance.tick()
    assert attempts == ["s1", "s1", "s2"]


def test_unknown_native_marker_fixture_preserves_scan_wire_and_is_not_uploaded(companion):
    instance, saved = companion
    fixture = savedvars.load_file(FIXTURE)
    marker = fixture["CrowleyDB"]["nativeConstruction"]
    scan = fixture["CrowleyDB"]["scans"]["fixture-native-construction"]
    assert marker["state"] == "mystery"
    assert marker["extra"] == "must-not-leak"
    original_payload = scan["payload"]
    saved.write_bytes(FIXTURE.read_bytes())
    uploaded = []

    def respond(request):
        if request.url.path == "/v1/scans":
            body = json.loads(request.content)
            uploaded.append(body)
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    assert len(uploaded) == 1
    assert uploaded[0]["payload"] == original_payload
    assert "nativeConstruction" not in uploaded[0]


def test_item_reference_upload_is_remembered_and_retried_on_error(companion):
    instance, saved = companion
    reference = {
        "format": 1,
        "id": "us-normal-alliance-1760000000-Tester-0001",
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
            "clientBuild": "70450",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "startedAt": 1760000000,
        "finishedAt": 1760000000,
        "ahOpen": False,
        "items": {
            "2589": {
                "itemID": 2589,
                "status": "ok",
                "info": {
                    "quality": 1,
                    "itemLevel": 0,
                    "minLevel": 0,
                    "classID": 7,
                    "subclassID": 0,
                    "maxStack": 20,
                    "sellPrice": 3,
                    "bindType": 0,
                    "expansionID": 1,
                    "isCraftingReagent": True,
                },
                "deposit": {"kind": "unqueried", "reason": "ah_closed"},
            }
        },
    }
    write_savedvars(saved, [], reference=reference)
    calls = []

    def respond(request):
        if request.url.path == "/v1/item-reference":
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "created", "items": 1})
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    instance.once()
    assert len(calls) == 1
    assert State.load(instance.state_file).references == [reference["id"]]

    failure = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(302, text="redirect")
                if request.url.path == "/v1/item-reference"
                else httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})
            )
        ),
    )
    instance.client.close()
    instance.client = failure
    new_reference = dict(reference, id="us-normal-alliance-1760000000-Tester-0002")
    write_savedvars(saved, [], reference=new_reference)
    with pytest.raises(RuntimeError, match="server rejected item-reference"):
        instance.once()
    assert State.load(instance.state_file).references == [reference["id"]]

    successful_calls = []

    def succeed(request):
        if request.url.path == "/v1/item-reference":
            successful_calls.append(json.loads(request.content)["id"])
            return httpx.Response(200, json={"status": "created", "items": 1})
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client.close()
    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(succeed))
    instance.once()
    assert successful_calls == [new_reference["id"]]
    assert State.load(instance.state_file).references == [reference["id"], new_reference["id"]]
    # once() reprocesses the file unconditionally, so only the remembered id prevents a second upload.
    instance.once()
    assert successful_calls == [new_reference["id"]]


def test_account_upload_is_remembered_and_retried_on_error(companion):
    instance, saved = companion
    wow = WowClient(money=321)
    wow.login()
    account = savedvars.loads(wow.saved_variables())["CrowleyDB"]["account"]
    write_savedvars(saved, [], account=account)
    attempts = []
    failed = False

    def respond(request):
        nonlocal failed
        if request.url.path == "/v1/account-state":
            attempts.append(json.loads(request.content)["id"])
            if not failed:
                failed = True
                raise httpx.ConnectError("temporary outage", request=request)
            return httpx.Response(200, json={"status": "created", "items": 0})
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.ConnectError):
        instance.once()
    assert State.load(instance.state_file).accounts == []

    instance.once()
    instance.once()
    assert attempts == [account["id"], account["id"]]
    assert State.load(instance.state_file).accounts == [account["id"]]


def test_account_upload_permanent_rejection_waits_for_operator_retry(companion):
    instance, saved = companion
    wow = WowClient(money=654)
    wow.login()
    account = savedvars.loads(wow.saved_variables())["CrowleyDB"]["account"]
    write_savedvars(saved, [], account=account)
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/v1/account-state":
            attempts += 1
            if attempts == 1:
                return httpx.Response(422, text="malformed account")
            return httpx.Response(200, json={"status": "created", "items": 0})
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    assert State.load(instance.state_file).accounts == []

    instance.once()
    assert attempts == 1
    assert len(State.load(instance.state_file).rejections) == 1

    instance.retry_rejected(account["id"])
    assert attempts == 2
    assert State.load(instance.state_file).accounts == [account["id"]]


def test_queue_lua_is_written_only_when_content_changes(companion, monkeypatch):
    instance, _ = companion
    instance.settings.house_key = instance.settings.target.house
    instance.settings.character = "Tester"
    queue = build_queue(
        {
            "house": "us-normal-alliance",
            "character": "Tester",
            "generatedAt": 1_700_000_000,
            "expiresAt": 1_700_000_600,
            "actions": [{"seq": 1, "kind": "vendor_buy", "itemID": 1, "quantity": 1}],
        }
    )
    writes = []
    seen_characters = []

    def record_write(path, text):
        writes.append((path, text))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    monkeypatch.setattr("crowley.companion.sync.atomic_write", record_write)
    instance.client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: (
                seen_characters.append(request.url.params["character"]) or httpx.Response(200, json=queue)
            )
        ),
    )
    instance.write_queue()
    instance.write_queue()
    assert len(writes) == 1
    assert seen_characters == ["Tester", "Tester"]
    assert savedvars.load_file(instance.settings.queue_lua)["CROWLEY_QUEUE"] == queue


def test_unconfigured_queue_is_cleared_even_when_addon_data_fails(companion):
    instance, saved = companion
    write_savedvars(saved, [])
    stale = build_queue(
        {
            "house": "us-normal-alliance",
            "character": "Tester",
            "generatedAt": 1_700_000_000,
            "expiresAt": 1_700_000_600,
            "actions": [{"seq": 1, "kind": "vendor_buy", "itemID": 1, "quantity": 1}],
        }
    )
    instance.settings.queue_lua.write_text(render_queue_lua(stale))

    def respond(request):
        if request.url.path == "/v1/addon-data":
            raise httpx.ConnectError("temporary outage", request=request)
        raise AssertionError(f"unexpected request {request.url}")

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.ConnectError):
        instance.once()
    assert savedvars.load_file(instance.settings.queue_lua) == {"CROWLEY_QUEUE": False}


def test_execution_upload_retries_then_is_remembered(companion):
    instance, saved = companion
    queue = build_queue(
        {
            "house": "us-normal-alliance",
            "character": "Tester",
            "generatedAt": 1_700_000_000,
            "expiresAt": 1_700_000_600,
            "actions": [{"seq": 1, "kind": "vendor_buy", "itemID": 1, "quantity": 1}],
        }
    )
    execution = {
        "queueId": queue["queueId"],
        "house": queue["house"],
        "character": queue["character"],
        "results": [],
    }
    write_savedvars(saved, [], executions={queue["queueId"]: execution})
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/v1/executions":
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("temporary outage", request=request)
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.ConnectError):
        instance.once()
    instance.once()
    instance.once()
    assert attempts == 2
    assert queue["queueId"] in State.load(instance.state_file).executions


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, FailureClass.SUCCESS),
        (201, FailureClass.SUCCESS),
        (408, FailureClass.TRANSIENT),
        (429, FailureClass.TRANSIENT),
        (500, FailureClass.TRANSIENT),
        (599, FailureClass.TRANSIENT),
        (600, FailureClass.UNEXPECTED),
        (400, FailureClass.PERMANENT_CONTRACT),
        (404, FailureClass.PERMANENT_CONTRACT),
        (422, FailureClass.PERMANENT_CONTRACT),
        (301, FailureClass.UNEXPECTED),
    ],
)
def test_upload_failure_classification_table(status, expected):
    assert classify_http_status(status) is expected
    assert classify_transport_error(httpx.ConnectError("offline")) is FailureClass.TRANSIENT


def test_rejection_ledger_persists_bounded_metadata_and_evicts_only_old_entries(tmp_path):
    now = time.time()
    state = State()
    old_key = RejectionKey("Test", "WTF/Account/Test/SavedVariables/Crowley.lua", "scan", "old", "a" * 64)
    state.record_rejection(
        old_key,
        status=422,
        error_code="server-code",
        error_message="private payload must never be persisted" * 30,
        seen_at=now - (REJECTION_RETENTION_DAYS + 1) * 86400,
        configuration_digest="b" * 64,
        contract_version=1,
    )
    for index in range(MAX_REJECTIONS):
        key = RejectionKey(
            "Test",
            "WTF/Account/Test/SavedVariables/Crowley.lua",
            "scan",
            f"young-{index}",
            f"{index + 1:064x}",
        )
        state.record_rejection(
            key,
            status=422,
            error_code="code",
            error_message="message",
            seen_at=now,
            configuration_digest="c" * 64,
            contract_version=1,
        )
    path = tmp_path / "state.json"
    state.save(path)
    loaded = State.load(path)

    assert len(loaded.rejections) == MAX_REJECTIONS
    assert all(record.key.record_id != "old" for record in loaded.rejections.values())
    stored = next(record for record in loaded.rejections.values() if record.key.record_id == "young-0")
    assert len(stored.error_message) <= 256
    assert "private payload" not in stored.error_message

    extra_key = RejectionKey("Test", "WTF/Account/Test/SavedVariables/Crowley.lua", "scan", "extra", "d" * 64)
    with pytest.raises(RejectionLedgerFull, match="rejection ledger is full"):
        loaded.record_rejection(
            extra_key,
            status=422,
            error_code="code",
            error_message="message",
            seen_at=now,
            configuration_digest="c" * 64,
            contract_version=1,
        )
    assert len(loaded.rejections) == MAX_REJECTIONS


def test_retained_rejection_message_is_bounded_in_serialized_state(tmp_path):
    state = State()
    message = "server detail " * 40 + "PRIVATE_PAYLOAD"
    state.record_rejection(
        RejectionKey("Test", "WTF/Account/Test/SavedVariables/Crowley.lua", "scan", "one", "a" * 64),
        status=422,
        error_code="code",
        error_message=message,
        seen_at=time.time(),
        configuration_digest="b" * 64,
        contract_version=1,
    )
    path = tmp_path / "state.json"
    state.save(path)
    raw = path.read_bytes()
    loaded = State.load(path)
    record = next(iter(loaded.rejections.values()))
    assert len(record.error_message) == 256
    assert b"PRIVATE_PAYLOAD" not in raw


def test_state_save_preserves_history_bounds_and_upload_retention(tmp_path):
    now = time.time()
    state = State(
        uploaded={
            "old": int(now - (UPLOAD_RETENTION_DAYS + 1) * 86400),
            **{f"scan-{i}": int(now) for i in range(3)},
        },
        probes=list(range(MAX_HISTORY_ENTRIES + 3)),
        references=[f"reference-{i}" for i in range(MAX_HISTORY_ENTRIES + 3)],
        accounts=[f"account-{i}" for i in range(MAX_HISTORY_ENTRIES + 3)],
    )
    path = tmp_path / "state.json"
    state.save(path)
    loaded = State.load(path)
    assert loaded.uploaded == {"scan-0": int(now), "scan-1": int(now), "scan-2": int(now)}
    assert loaded.probes == list(range(3, MAX_HISTORY_ENTRIES + 3))
    assert loaded.references == [f"reference-{i}" for i in range(3, MAX_HISTORY_ENTRIES + 3)]
    assert loaded.accounts == [f"account-{i}" for i in range(3, MAX_HISTORY_ENTRIES + 3)]


def test_observing_rejected_record_refreshes_last_seen(tmp_path):
    now = time.time()
    key = RejectionKey("Test", "WTF/Account/Test/SavedVariables/Crowley.lua", "scan", "one", "a" * 64)
    state = State()
    state.record_rejection(
        key,
        status=422,
        error_code="code",
        error_message="message",
        seen_at=now - 1,
        configuration_digest="b" * 64,
        contract_version=1,
    )
    assert state.refresh_rejection(key, now)
    path = tmp_path / "state.json"
    state.save(path)
    refreshed = State.load(path).rejection_for(key)
    assert refreshed is not None
    assert refreshed.last_seen == pytest.approx(now)


@pytest.mark.parametrize("status", [408, 429, 500, 599])
def test_transient_http_rejection_is_retried(status, companion):
    instance, saved = companion
    write_savedvars(saved, [raw_scan(id=f"status-{status}")])
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/v1/scans":
            attempts += 1
            if attempts == 1:
                return httpx.Response(status, json={"code": "temporary", "detail": "retry"})
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    with pytest.raises(UploadResponseError) as failure:
        instance.once()
    assert failure.value.classification is FailureClass.TRANSIENT
    instance.once()
    assert attempts == 2


def test_rejection_is_retried_when_configuration_changes(companion):
    instance, saved = companion
    write_savedvars(saved, [raw_scan(id="config-change")])
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/v1/scans":
            attempts += 1
            if attempts == 1:
                return httpx.Response(422, json={"code": "invalid", "detail": "contract"})
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    instance.once()
    assert attempts == 1
    digest = instance.configuration_digest
    instance.settings = instance.settings.model_copy(update={"api_token": SecretStr("rotated-token")})
    assert instance.configuration_digest != digest
    instance.once()
    assert attempts == 2


def test_rejection_is_retried_when_contract_version_changes(companion, monkeypatch):
    from crowley.companion import sync as companion_sync

    instance, saved = companion
    write_savedvars(saved, [raw_scan(id="contract-change")])
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/v1/scans":
            attempts += 1
            if attempts == 1:
                return httpx.Response(422, json={"code": "invalid", "detail": "contract"})
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    instance.once()
    assert attempts == 1
    monkeypatch.setattr(companion_sync, "SERVER_CONTRACT_VERSION", 2)
    instance.once()
    assert attempts == 2


def test_permanent_rejection_skips_same_digest_retries_changed_digest_and_operator_retry(companion):
    instance, saved = companion
    scan = raw_scan(id="rejected")
    write_savedvars(saved, [scan])
    attempts = []

    def respond(request):
        if request.url.path == "/v1/scans":
            attempts.append(json.loads(request.content)["id"])
            if len(attempts) == 1:
                return httpx.Response(422, json={"code": "bad_scan", "detail": "invalid contract"})
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    instance.once()
    assert attempts == ["rejected"]
    assert len(State.load(instance.state_file).rejections) == 1

    changed = dict(scan)
    changed["contentChangeForRetry"] = True
    write_savedvars(saved, [changed])
    instance.once()
    assert attempts == ["rejected", "rejected"]
    assert State.load(instance.state_file).uploaded == {"rejected": pytest.approx(time.time(), abs=5)}

    # A new rejected record can be explicitly retried even when its digest is unchanged.
    another = raw_scan(id="operator")
    write_savedvars(saved, [another])
    instance.client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(422, json={"code": "bad_scan", "detail": "invalid contract"})
                if request.url.path == "/v1/scans"
                else httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})
            )
        ),
    )
    instance.once()
    calls_before_retry = len(State.load(instance.state_file).rejections)
    instance.client.close()
    instance.client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"})
                if request.url.path == "/v1/scans"
                else httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})
            )
        ),
    )
    assert calls_before_retry == 1
    instance.retry_rejected("operator")
    assert State.load(instance.state_file).uploaded["operator"] > 0


def test_permanent_rejection_does_not_block_other_records(companion):
    instance, saved = companion
    write_savedvars(saved, [raw_scan(id="bad"), raw_scan(id="good")])
    attempts = []

    def respond(request):
        if request.url.path == "/v1/scans":
            scan_id = json.loads(request.content)["id"]
            attempts.append(scan_id)
            if scan_id == "bad":
                return httpx.Response(422, json={"code": "bad_scan", "detail": "invalid contract"})
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    instance.once()
    assert attempts == ["bad", "good"]
    assert State.load(instance.state_file).uploaded == {"good": pytest.approx(time.time(), abs=5)}


def test_permanent_rejection_is_persisted_before_later_non_transient_failure(companion):
    instance, saved = companion
    write_savedvars(saved, [raw_scan(id="bad"), raw_scan(id="malformed")])
    attempts = []

    def respond(request):
        if request.url.path != "/v1/scans":
            raise AssertionError(f"unexpected request {request.url}")
        scan_id = json.loads(request.content)["id"]
        attempts.append(scan_id)
        if scan_id == "bad":
            return httpx.Response(422, json={"code": "bad_scan", "detail": "invalid contract"})
        return httpx.Response(200, json={})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match="scan response is missing key 'house'"):
        instance.once()

    rejected = State.load(instance.state_file).rejections
    assert [record.key.record_id for record in rejected.values()] == ["bad"]

    replacement = Companion(instance.settings)
    replacement.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(RuntimeError, match="scan response is missing key 'house'"):
            replacement.once()
    finally:
        replacement.client.close()
    assert attempts == ["bad", "malformed", "malformed"]


def test_companion_ledger_full_persists_first_rejection_and_leaves_signature_pending(companion):
    instance, saved = companion
    state = State()
    now = time.time()
    for index in range(MAX_REJECTIONS - 1):
        state.record_rejection(
            RejectionKey(
                "Test",
                "WTF/Account/Test/SavedVariables/Crowley.lua",
                "scan",
                f"occupied-{index}",
                f"{index + 1:064x}",
            ),
            status=422,
            error_code="occupied",
            error_message="ledger slot occupied",
            seen_at=now,
            configuration_digest="a" * 64,
            contract_version=1,
        )
    state.save(instance.state_file)
    write_savedvars(saved, [raw_scan(id="first"), raw_scan(id="second")])
    attempts = []

    def fill_ledger(request):
        if request.url.path == "/v1/addon-data":
            return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})
        if request.url.path != "/v1/scans":
            raise AssertionError(f"unexpected request {request.url}")
        scan_id = json.loads(request.content)["id"]
        attempts.append(scan_id)
        return httpx.Response(422, json={"code": "bad_scan", "detail": "invalid contract"})

    instance.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(fill_ledger))
    instance.tick()
    with pytest.raises(RejectionLedgerFull, match="rejection ledger is full"):
        instance.tick()
    assert saved not in instance._signatures
    persisted = State.load(instance.state_file)
    assert len(persisted.rejections) == MAX_REJECTIONS
    assert any(record.key.record_id == "first" for record in persisted.rejections.values())
    assert all(record.key.record_id != "second" for record in persisted.rejections.values())
    assert attempts == ["first", "second"]

    replacement_attempts = []

    def retry_second(request):
        if request.url.path == "/v1/scans":
            scan_id = json.loads(request.content)["id"]
            replacement_attempts.append(scan_id)
            assert scan_id == "second"
            return httpx.Response(
                200, json={"house": "us-normal-alliance", "listings": 1, "status": "created"}
            )
        return httpx.Response(200, json={"generatedAt": 1, "ackedScans": []})

    replacement = Companion(instance.settings)
    replacement.client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(retry_second))
    try:
        replacement.once()
    finally:
        replacement.client.close()
    assert replacement_attempts == ["second"]
    assert State.load(instance.state_file).uploaded["second"] > 0


def test_explicit_account_selection_missing_wrong_and_correct(companion):
    instance, saved = companion
    write_savedvars(saved, [])
    other = instance.settings.wow_path / "WTF/Account/Other/SavedVariables/Crowley.lua"
    other.parent.mkdir(parents=True)
    other.write_text(saved.read_text(encoding="utf-8"), encoding="utf-8")
    assert instance.settings.saved_variables_file() == saved
    assert instance.settings.saved_variables_files() == [saved]

    missing = _settings_for(
        instance.settings.wow_path.parent,
        target=instance.settings.target,
        wow_dir=instance.settings.wow_path,
        account="Missing",
    )
    with pytest.raises(FileNotFoundError, match="account directory"):
        missing.saved_variables_file()

    wrong = _settings_for(
        instance.settings.wow_path.parent,
        target=instance.settings.target,
        wow_dir=instance.settings.wow_path,
        account="Other",
    )
    assert wrong.saved_variables_file() == other

    with pytest.raises(ValidationError, match="literal account"):
        _settings_for(
            instance.settings.wow_path.parent,
            target=instance.settings.target,
            wow_dir=instance.settings.wow_path,
            account="..",
        )


def test_config_is_strict_and_requires_account_selection(companion):
    settings = companion[0].settings
    with pytest.raises(ValidationError, match="wtf_account"):
        CompanionSettings(
            target=settings.target,
            catalog=settings.catalog,
            canonical_plan=settings.canonical_plan,
            route_provenance=settings.route_provenance,
            wow_dir=settings.wow_path,
            account_dir=settings.account_dir,
            server_url=settings.server_url,
            api_token="test-token",
            saved_variables=settings.saved_variables,
            data_lua=settings.data_lua,
            queue_lua=settings.queue_lua,
            addons_dir=settings.addons_dir,
        )
    with pytest.raises(ValidationError, match="unknown"):
        CompanionSettings(
            target=settings.target,
            catalog=settings.catalog,
            canonical_plan=settings.canonical_plan,
            route_provenance=settings.route_provenance,
            wow_dir=settings.wow_path,
            account_dir=settings.account_dir,
            server_url=settings.server_url,
            api_token="test-token",
            wtf_account="Test",
            saved_variables=settings.saved_variables,
            data_lua=settings.data_lua,
            queue_lua=settings.queue_lua,
            addons_dir=settings.addons_dir,
            unknown="value",
        )


def test_state_errors_are_strict_and_legacy_state_is_upgraded(tmp_path):
    unknown_version = tmp_path / "unknown.json"
    unknown_version.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(StateError, match="unsupported companion state version 99"):
        State.load(unknown_version)

    missing_key = tmp_path / "missing.json"
    missing_key.write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(StateError, match="missing key"):
        State.load(missing_key)

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"uploaded": {"scan": int(time.time())}, "probes": []}), encoding="utf-8")
    state = State.load(legacy)
    assert state.uploaded == {"scan": pytest.approx(time.time(), abs=5)}
    assert json.loads(legacy.read_text(encoding="utf-8"))["version"] == 2

    unknown_key = tmp_path / "unknown-key.json"
    unknown_key.write_text(json.dumps({"uploaded": {}, "probes": [], "surprise": True}), encoding="utf-8")
    with pytest.raises(StateError, match="unknown key"):
        State.load(unknown_key)


def test_companion_state_contract_round_trip_and_canonical_bytes():
    state = CompanionState(
        version=STATE_VERSION,
        uploaded={"scan": 100},
        probes=(101,),
        references=("reference",),
        accounts=("account",),
        executions={"queue": "a" * 64},
        rejections={},
    )
    raw = dump_bytes(state)
    assert parse_bytes(raw) == state
    assert dump_bytes(parse_bytes(raw)) == raw


def test_companion_state_contract_refuses_unknown_keys_versions_and_legacy_shape():
    raw = dump_bytes(
        CompanionState(
            version=STATE_VERSION,
            uploaded={},
            probes=(),
            references=(),
            accounts=(),
            executions={},
            rejections={},
        )
    )
    document = json.loads(raw)
    document["unknown"] = True
    with pytest.raises(CompanionStateError, match="unknown key"):
        parse_bytes(json.dumps(document).encode())
    document = json.loads(raw)
    document["version"] = STATE_VERSION + 1
    with pytest.raises(CompanionStateError, match="unsupported companion state version"):
        parse_bytes(json.dumps(document).encode())

    legacy = b'{"uploaded":{"scan":100},"probes":[]}'
    with pytest.raises(LegacyCompanionStateError):
        parse_bytes(legacy)
    upgraded = upgrade_legacy_state_bytes(legacy)
    assert upgraded.version == STATE_VERSION
    assert dict(upgraded.uploaded) == {"scan": 100}
