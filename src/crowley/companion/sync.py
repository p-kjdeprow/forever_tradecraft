"""Move data between the selected game account and the Crowley server."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import cast

import httpx

from crowley.core import companion_state, savedvars
from crowley.core.companion_state import (
    CompanionState,
    CompanionStateError,
    LegacyCompanionStateError,
    RejectionKey,
    RejectionRecord,
    dump_bytes,
    parse_bytes,
    upgrade_legacy_state_bytes,
)
from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua

from .config import CompanionSettings

log = logging.getLogger(__name__)

STATE_VERSION = companion_state.STATE_VERSION
SERVER_CONTRACT_VERSION = 1
MAX_REJECTIONS = companion_state.MAX_REJECTIONS
# A rejection is evictable only after 30 days without observing its source digest.
REJECTION_RETENTION_DAYS = companion_state.REJECTION_RETENTION_DAYS
MAX_ERROR_CODE_LENGTH = companion_state.MAX_ERROR_CODE_LENGTH
MAX_ERROR_MESSAGE_LENGTH = companion_state.MAX_ERROR_MESSAGE_LENGTH
UPLOAD_RETENTION_DAYS = 30
MAX_HISTORY_ENTRIES = 50
StateError = CompanionStateError


class RejectionLedgerFull(StateError):
    """The bounded rejection ledger cannot safely record another rejection."""


class FailureClass(Enum):
    """Classification used by the upload state machine."""

    SUCCESS = "success"
    TRANSIENT = "transient"
    PERMANENT_CONTRACT = "permanent_contract_rejection"
    UNEXPECTED = "unexpected_response"


def classify_http_status(status_code: int) -> FailureClass:
    """Classify an HTTP response without conflating retryable and permanent failures."""

    if 200 <= status_code < 300:
        return FailureClass.SUCCESS
    if status_code in (408, 429) or 500 <= status_code < 600:
        return FailureClass.TRANSIENT
    if 400 <= status_code < 500:
        return FailureClass.PERMANENT_CONTRACT
    return FailureClass.UNEXPECTED


def classify_transport_error(_: httpx.TransportError) -> FailureClass:
    """Transport failures are retryable, including timeouts and connection failures."""

    return FailureClass.TRANSIENT


def _json_digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class State:
    """Mutable companion working state backed by the shared versioned contract."""

    uploaded: dict[str, int] = field(default_factory=dict)
    probes: list[int] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    executions: dict[str, str] = field(default_factory=dict)
    rejections: dict[str, RejectionRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StateError(f"cannot read companion state {path}: {exc}") from exc
        upgraded = False
        try:
            parsed = parse_bytes(raw)
        except LegacyCompanionStateError:
            parsed = upgrade_legacy_state_bytes(raw)
            upgraded = True
        state = cls(
            uploaded=dict(parsed.uploaded),
            probes=list(parsed.probes),
            references=list(parsed.references),
            accounts=list(parsed.accounts),
            executions=dict(parsed.executions),
            rejections=dict(parsed.rejections),
        )
        if upgraded:
            state.save(path)
        return state

    def save(self, path: Path) -> None:
        self._prune(time.time())
        cutoff = time.time() - UPLOAD_RETENTION_DAYS * 86400
        self.uploaded = {
            record_id: seen_at for record_id, seen_at in self.uploaded.items() if seen_at >= cutoff
        }
        self.probes = self.probes[-MAX_HISTORY_ENTRIES:]
        self.references = self.references[-MAX_HISTORY_ENTRIES:]
        self.accounts = self.accounts[-MAX_HISTORY_ENTRIES:]
        snapshot = CompanionState(
            version=STATE_VERSION,
            uploaded=self.uploaded,
            probes=tuple(self.probes),
            references=tuple(self.references),
            accounts=tuple(self.accounts),
            executions=self.executions,
            rejections=self.rejections,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, dump_bytes(snapshot))

    def rejection_for(self, key: RejectionKey) -> RejectionRecord | None:
        return self.rejections.get(key.storage_key())

    def record_rejection(
        self,
        key: RejectionKey,
        *,
        status: int,
        error_code: str,
        error_message: str,
        seen_at: float,
        configuration_digest: str,
        contract_version: int,
    ) -> bool:
        storage_key = key.storage_key()
        current = self.rejections.get(storage_key)
        if current is not None:
            self.rejections[storage_key] = replace(
                current,
                status=status,
                error_code=error_code[:MAX_ERROR_CODE_LENGTH],
                error_message=error_message[:MAX_ERROR_MESSAGE_LENGTH],
                last_seen=seen_at,
                configuration_digest=configuration_digest,
                contract_version=contract_version,
            )
            return True
        self._prune(seen_at, limit=MAX_REJECTIONS - 1)
        if len(self.rejections) >= MAX_REJECTIONS:
            raise RejectionLedgerFull(
                f"rejection ledger is full ({MAX_REJECTIONS} recently observed entries); "
                "resolve or explicitly retry an existing rejection before continuing"
            )
        self.rejections[storage_key] = RejectionRecord(
            key=key,
            status=status,
            error_code=error_code[:MAX_ERROR_CODE_LENGTH],
            error_message=error_message[:MAX_ERROR_MESSAGE_LENGTH],
            first_seen=seen_at,
            last_seen=seen_at,
            configuration_digest=configuration_digest,
            contract_version=contract_version,
        )
        return True

    def refresh_rejection(self, key: RejectionKey, seen_at: float) -> bool:
        storage_key = key.storage_key()
        current = self.rejections.get(storage_key)
        if current is None:
            return False
        self.rejections[storage_key] = replace(current, last_seen=seen_at)
        return True

    def clear_record_rejections(self, key: RejectionKey) -> None:
        for storage_key, record in list(self.rejections.items()):
            if (
                record.key.account == key.account
                and record.key.saved_variables_file == key.saved_variables_file
                and record.key.record_kind == key.record_kind
                and record.key.record_id == key.record_id
            ):
                del self.rejections[storage_key]

    def retry_rejected(self, record_id: str | None) -> int:
        keys = [
            storage_key
            for storage_key, record in self.rejections.items()
            if record_id is None or record.key.record_id == record_id
        ]
        for storage_key in keys:
            del self.rejections[storage_key]
        return len(keys)

    def _prune(self, now: float, *, limit: int = MAX_REJECTIONS) -> None:
        cutoff = now - REJECTION_RETENTION_DAYS * 86400
        while len(self.rejections) > limit:
            eligible = [
                (record.last_seen, record.first_seen, storage_key)
                for storage_key, record in self.rejections.items()
                if record.last_seen <= cutoff
            ]
            if not eligible:
                break
            _, _, storage_key = min(eligible)
            del self.rejections[storage_key]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


@contextmanager
def state_file_lock(path: Path) -> Iterator[None]:
    """Serialize companion and CLI state updates with a sibling advisory lock.

    The daemon and ``retry-rejected`` both hold this lock across reload, mutation,
    and atomic replacement.  Consequently every writer reloads the current state
    while excluding the other process instead of overwriting its newer changes.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class UploadResponseError(RuntimeError):
    """A non-success HTTP response classified by the upload state machine."""

    def __init__(
        self,
        classification: FailureClass,
        status_code: int,
        error_code: str,
        error_message: str,
    ) -> None:
        self.classification = classification
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"HTTP {status_code} {error_code}: {error_message}")


def _error_metadata(response: httpx.Response) -> tuple[str, str]:
    fallback_code = f"http_{response.status_code}"
    fallback_message = f"HTTP status {response.status_code}"
    try:
        raw: object = response.json()
    except ValueError:
        return fallback_code, fallback_message
    if not isinstance(raw, dict):
        return fallback_code, fallback_message
    body = cast(dict[object, object], raw)
    code: str | None = None
    for key in ("code", "errorCode", "error_code"):
        candidate = body.get(key)
        if isinstance(candidate, str) and candidate:
            code = candidate[:MAX_ERROR_CODE_LENGTH]
            break
    message: str | None = None
    for key in ("message", "detail", "error"):
        candidate = body.get(key)
        if isinstance(candidate, str) and candidate:
            message = candidate[:MAX_ERROR_MESSAGE_LENGTH]
            break
    return code or fallback_code, message or fallback_message


def _is_transient_exception(error: Exception) -> bool:
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, UploadResponseError) and error.classification is FailureClass.TRANSIENT
    )


class Companion:
    def __init__(self, settings: CompanionSettings):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
            timeout=120,
        )
        if settings.state_file is None:
            raise ValueError("resolved companion state_file is required")
        self.state_file = settings.state_file
        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
        self._signatures: dict[Path, tuple[float, int]] = {}
        self._pending: dict[Path, tuple[float, int]] = {}
        self._present_ids: dict[Path, list[str]] = {}
        self._last_data_write = 0.0
        self._queue_warning_logged = False

    @property
    def configuration_digest(self) -> str:
        configuration = {
            "wow_dir": str(self.settings.wow_path),
            "server_url": self.settings.server_url.rstrip("/"),
            "wtf_account": self.settings.wtf_account,
            "house_key": self.settings.house_key,
            "character": self.settings.character,
            "api_token_sha256": hashlib.sha256(
                self.settings.api_token.get_secret_value().encode("utf-8")
            ).hexdigest(),
        }
        return _json_digest(configuration)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: object,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = self.client.request(method, endpoint, json=json_body, params=params)
        classification = classify_http_status(response.status_code)
        if classification is FailureClass.SUCCESS:
            return response
        error_code, error_message = _error_metadata(response)
        raise UploadResponseError(classification, response.status_code, error_code, error_message)

    def _source_key(self, path: Path, kind: str, record_id: str, content_digest: str) -> RejectionKey:
        try:
            relative_path = path.relative_to(self.settings.wow_path).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"SavedVariables path is outside configured wow_dir: {path}") from exc
        return RejectionKey(
            account=self.settings.wtf_account,
            saved_variables_file=relative_path,
            record_kind=kind,
            record_id=record_id,
            content_digest=content_digest,
        )

    def _is_rejected(self, key: RejectionKey) -> bool:
        record = self.state.rejection_for(key)
        if record is None:
            return False
        self.state.refresh_rejection(key, time.time())
        self.state.save(self.state_file)
        if (
            record.configuration_digest == self.configuration_digest
            and record.contract_version == SERVER_CONTRACT_VERSION
        ):
            log.error(
                "skipping permanently rejected %s %s from %s; content digest unchanged",
                key.record_kind,
                key.record_id,
                key.saved_variables_file,
            )
            return True
        return False

    def _record_permanent_rejection(self, key: RejectionKey, error: UploadResponseError) -> None:
        self.state.record_rejection(
            key,
            status=error.status_code,
            error_code=error.error_code,
            error_message=error.error_message,
            seen_at=time.time(),
            configuration_digest=self.configuration_digest,
            contract_version=SERVER_CONTRACT_VERSION,
        )
        self.state.save(self.state_file)
        log.error(
            "permanent contract rejection for %s %s from %s: HTTP %d %s: %s",
            key.record_kind,
            key.record_id,
            key.saved_variables_file,
            error.status_code,
            error.error_code,
            error.error_message,
        )

    def _submit_record(
        self,
        path: Path,
        kind: str,
        record_id: str,
        record: object,
        request: Callable[[], httpx.Response],
        on_success: Callable[[httpx.Response], None],
    ) -> bool:
        key = self._source_key(path, kind, record_id, _json_digest(record))
        if self._is_rejected(key):
            return False
        try:
            response = request()
        except UploadResponseError as exc:
            if exc.classification is FailureClass.TRANSIENT:
                raise
            if exc.classification is not FailureClass.PERMANENT_CONTRACT:
                raise RuntimeError(
                    f"server rejected {kind} {record_id}: HTTP {exc.status_code} "
                    f"{exc.error_code}: {exc.error_message}"
                ) from exc
            self._record_permanent_rejection(key, exc)
            return False
        on_success(response)
        self.state.clear_record_rejections(key)
        return True

    def changed_files(self) -> list[Path]:
        """Return the selected file after it holds still for one poll interval."""

        path = self.settings.saved_variables
        stat = path.stat()
        signature = (stat.st_mtime, stat.st_size)
        if self._signatures.get(path) == signature:
            return []
        if self._pending.get(path) == signature:
            return [path]
        self._pending[path] = signature
        return []

    def _process_file(self, path: Path) -> int:
        db = savedvars.load_file(path)["CrowleyDB"]
        self._present_ids[path] = list(db["scans"])
        uploaded = 0
        for scan_id, scan in db["scans"].items():
            if scan_id not in self.state.uploaded and self._upload_scan(path, scan_id, scan):
                uploaded += 1

        reference = db.get("reference")
        if reference and reference["id"] not in self.state.references:
            if self._upload_reference(path, reference):
                uploaded += 1

        account = db.get("account")
        if account and account["id"] not in self.state.accounts:
            if self._upload_account(path, account):
                uploaded += 1

        executions = db.get("executions", {})
        if executions == []:
            executions = {}
        if not isinstance(executions, dict):
            raise RuntimeError("CrowleyDB.executions must be an object")
        for queue_id, execution in executions.items():
            if not isinstance(execution, dict):
                raise RuntimeError(f"CrowleyDB.executions[{queue_id!r}] must be an object")
            digest = _json_digest(execution)
            if self.state.executions.get(queue_id) != digest and self._upload_execution(
                path, queue_id, execution
            ):
                self.state.executions[queue_id] = digest
                uploaded += 1

        probe = db.get("probe")
        if probe and probe["at"] not in self.state.probes:
            probe_id = str(probe["at"])
            if self._submit_record(
                path,
                "probe",
                probe_id,
                probe,
                lambda: self._request("POST", "/v1/probes", json_body=probe),
                lambda _: self.state.probes.append(probe["at"]),
            ):
                log.info("uploaded client probe from %s", time.ctime(probe["at"]))
        return uploaded

    def process_file(self, path: Path) -> int:
        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
            try:
                uploaded = self._process_file(path)
            except Exception as exc:
                if _is_transient_exception(exc):
                    self.state.save(self.state_file)
                raise
            self.state.save(self.state_file)
            return uploaded

    def _upload_scan(self, path: Path, scan_id: str, scan: dict[str, object]) -> bool:
        def accepted(response: httpx.Response) -> None:
            result = _response_object(response, "scan response")
            house = _required_string(result, "house", "scan response")
            listings = _required_value(result, "listings", "scan response")
            status = _required_string(result, "status", "scan response")
            log.info("scan %s -> %s (%s listings, %s)", scan_id, house, listings, status)
            self.state.uploaded[scan_id] = int(time.time())

        return self._submit_record(
            path,
            "scan",
            scan_id,
            scan,
            lambda: self._request("POST", "/v1/scans", json_body=scan),
            accepted,
        )

    def upload_scan(self, scan_id: str, scan: dict[str, object]) -> bool:
        return self._upload_scan(self.settings.saved_variables, scan_id, scan)

    def _upload_reference(self, path: Path, reference: dict[str, object]) -> bool:
        capture_id = _required_string(reference, "id", "item reference")

        def accepted(response: httpx.Response) -> None:
            result = _response_object(response, "item reference response")
            status = _required_string(result, "status", "item reference response")
            items = _required_value(result, "items", "item reference response")
            log.info("item reference %s -> %s (%s)", capture_id, status, items)
            self.state.references.append(capture_id)

        return self._submit_record(
            path,
            "item-reference",
            capture_id,
            reference,
            lambda: self._request("POST", "/v1/item-reference", json_body=reference),
            accepted,
        )

    def upload_reference(self, reference: dict[str, object]) -> bool:
        return self._upload_reference(self.settings.saved_variables, reference)

    def _upload_account(self, path: Path, account: dict[str, object]) -> bool:
        capture_id = _required_string(account, "id", "account state")

        def accepted(response: httpx.Response) -> None:
            result = _response_object(response, "account state response")
            status = _required_string(result, "status", "account state response")
            items = _required_value(result, "items", "account state response")
            log.info("account state %s -> %s (%s items)", capture_id, status, items)
            self.state.accounts.append(capture_id)

        return self._submit_record(
            path,
            "account-state",
            capture_id,
            account,
            lambda: self._request("POST", "/v1/account-state", json_body=account),
            accepted,
        )

    def upload_account(self, account: dict[str, object]) -> bool:
        return self._upload_account(self.settings.saved_variables, account)

    def _upload_execution(self, path: Path, queue_id: str, execution: dict[str, object]) -> bool:
        def accepted(_: httpx.Response) -> None:
            results = execution.get("results")
            if not isinstance(results, (list, dict)):
                raise RuntimeError("execution record results must be an array or object")
            log.info("uploaded execution %s (%s results)", queue_id, len(results))

        return self._submit_record(
            path,
            "execution",
            queue_id,
            execution,
            lambda: self._request("POST", "/v1/executions", json_body=execution),
            accepted,
        )

    def upload_execution(self, queue_id: str, execution: dict[str, object]) -> bool:
        return self._upload_execution(self.settings.saved_variables, queue_id, execution)

    def _write_addon_data(self) -> None:
        configured = bool(self.settings.house_key and self.settings.character)
        if not configured:
            self.write_queue()
        scan_ids = sorted({sid for ids in self._present_ids.values() for sid in ids})
        path = self.settings.saved_variables
        payload = {"scan_ids": scan_ids}
        key = self._source_key(path, "addon-data", "addon-data", _json_digest(payload))
        if self._is_rejected(key):
            return
        try:
            response = self._request("POST", "/v1/addon-data", json_body=payload)
        except UploadResponseError as exc:
            if exc.classification is not FailureClass.PERMANENT_CONTRACT:
                raise
            self._record_permanent_rejection(key, exc)
            return
        self.state.clear_record_rejections(key)
        data = response.json()
        data["ackedScans"] = sorted(data["ackedScans"], key=lambda item: item.encode())
        text = render_assignment(
            "CROWLEY_DATA",
            data,
            header="Written by the Crowley companion. Do not edit; it is overwritten.\n"
            "Loaded by the game at login or /reload.",
        )
        target = self.settings.data_lua
        atomic_write(target, text)
        self._last_data_write = time.time()
        log.info(
            "wrote %s (%d acked scans)",
            target,
            len(data["ackedScans"]),
        )
        if configured:
            self.write_queue()

    def write_queue(self) -> None:
        queue_id = "none"
        queue: dict[str, object] | None = None
        house_key = self.settings.house_key
        character = self.settings.character
        if house_key and character:
            response = self._request(
                "GET",
                f"/v1/houses/{house_key}/queue/active",
                json_body=None,
                params={"character": character},
            )
            value: object = response.json()
            if value is not None:
                queue = _mapping_for_runtime(value, "active queue response")
                queue_id = _required_string(queue, "queueId", "active queue response")
        elif not self._queue_warning_logged:
            log.warning("queue settings unset; writing Queue.lua with no queue")
            self._queue_warning_logged = True
        text = render_queue_lua(queue)
        target = self.settings.queue_lua
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current != text:
            atomic_write(target, text)
            log.info("wrote Queue.lua (%s)", queue_id)

    def write_addon_data(self) -> None:
        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
            try:
                self._write_addon_data()
            except Exception as exc:
                if _is_transient_exception(exc):
                    self.state.save(self.state_file)
                raise
            self.state.save(self.state_file)

    def tick(self) -> None:
        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
            try:
                changed = self.changed_files()
                for path in changed:
                    self._process_file(path)
                    self._signatures[path] = self._pending.pop(path)
                refresh_due = time.time() - self._last_data_write >= self.settings.refresh_minutes * 60
                if changed or refresh_due:
                    self._write_addon_data()
            except Exception as exc:
                if _is_transient_exception(exc):
                    self.state.save(self.state_file)
                raise
            self.state.save(self.state_file)

    def run(self) -> None:
        log.info("watching %s", self.settings.saved_variables)
        log.info("server %s", self.settings.server_url)
        while True:
            try:
                self.tick()
            except httpx.TransportError as exc:
                log.warning("server unreachable (%s); will retry", exc)
            except UploadResponseError as exc:
                if exc.classification is FailureClass.TRANSIENT:
                    log.warning("server temporarily rejected a request: %s", exc)
                elif exc.classification is FailureClass.PERMANENT_CONTRACT:
                    log.error("server permanently rejected a non-record request: %s", exc)
                else:
                    raise
            time.sleep(self.settings.poll_seconds)

    def once(self) -> None:
        """Process the selected file immediately, ignoring the stability wait."""

        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
            path = self.settings.saved_variables
            try:
                self._process_file(path)
                self._write_addon_data()
            except Exception as exc:
                if _is_transient_exception(exc):
                    self.state.save(self.state_file)
                raise
            self.state.save(self.state_file)

    def retry_rejected(self, record_id: str | None = None) -> int:
        """Forget matching rejection entries, then immediately process the selected file."""

        with state_file_lock(self.state_file):
            self.state = State.load(self.state_file)
            count = self.state.retry_rejected(record_id)
            path = self.settings.saved_variables
            try:
                self._process_file(path)
                self._write_addon_data()
            except Exception as exc:
                if _is_transient_exception(exc):
                    self.state.save(self.state_file)
                raise
            self.state.save(self.state_file)
            return count


def _required_value(data: Mapping[str, object], key: str, context: str) -> object:
    if key not in data:
        raise RuntimeError(f"{context} is missing key {key!r}")
    return data[key]


def _required_string(data: Mapping[str, object], key: str, context: str) -> str:
    value = _required_value(data, key, context)
    if not isinstance(value, str):
        raise RuntimeError(f"{context}.{key} must be a string")
    return value


def _response_object(response: httpx.Response, context: str) -> dict[str, object]:
    return _mapping_for_runtime(response.json(), context)


def _mapping_for_runtime(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise RuntimeError(f"{context} keys must be strings")
    return cast(dict[str, object], value)
