"""The versioned on-disk contract used by the companion upload state machine."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

STATE_VERSION = 3
_PREVIOUS_STATE_VERSION = 2
_RETAINED_RECORD_KINDS = frozenset({"scan", "probe", "addon-data"})
MAX_REJECTIONS = 256
REJECTION_RETENTION_DAYS = 30
MAX_ERROR_CODE_LENGTH = 64
MAX_ERROR_MESSAGE_LENGTH = 256


class CompanionStateError(ValueError):
    """The current companion state document is malformed."""


class PreviousCompanionStateError(CompanionStateError):
    """The document uses the previous versioned shape and needs explicit upgrade."""


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CompanionStateError(f"{context} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise CompanionStateError(f"{context} keys must be strings")
    return cast(dict[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], context: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        raise CompanionStateError(f"{context} has unknown key(s): {', '.join(unknown)}")
    missing = sorted(expected - set(value))
    if missing:
        raise CompanionStateError(f"{context} is missing key(s): {', '.join(missing)}")


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CompanionStateError(f"{context} must be a non-empty string")
    return value


def _sha256(value: object, context: str) -> str:
    result = _string(value, context)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise CompanionStateError(f"{context} must be a lowercase SHA-256 digest")
    return result


def _positive_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CompanionStateError(f"{context} must be a positive integer")
    return value


def _timestamp(value: object, context: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise CompanionStateError(f"{context} must be a positive number")
    return float(value)


def _bounded_text(value: object, limit: int, context: str) -> str:
    text = _string(value, context)
    if len(text) > limit:
        raise CompanionStateError(f"{context} exceeds {limit} characters")
    return text


def _record_kind(value: object, context: str, *, previous: bool = False) -> str:
    result = _string(value, context)
    if previous:
        return result
    if result not in _RETAINED_RECORD_KINDS:
        raise CompanionStateError(f"{context} is not a retained companion record kind")
    return result


@dataclass(frozen=True)
class RejectionKey:
    account: str
    saved_variables_file: str
    record_kind: str
    record_id: str
    content_digest: str

    def storage_key(self) -> str:
        return json.dumps(
            [
                self.account,
                self.saved_variables_file,
                self.record_kind,
                self.record_id,
                self.content_digest,
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class RejectionRecord:
    key: RejectionKey
    status: int
    error_code: str
    error_message: str
    first_seen: float
    last_seen: float
    configuration_digest: str
    contract_version: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "account": self.key.account,
            "saved_variables_file": self.key.saved_variables_file,
            "record_kind": self.key.record_kind,
            "record_id": self.key.record_id,
            "content_digest": self.key.content_digest,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "configuration_digest": self.configuration_digest,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_mapping(cls, value: object, context: str) -> RejectionRecord:
        data = _mapping(value, context)
        _exact_keys(
            data,
            {
                "account",
                "saved_variables_file",
                "record_kind",
                "record_id",
                "content_digest",
                "status",
                "error_code",
                "error_message",
                "first_seen",
                "last_seen",
                "configuration_digest",
                "contract_version",
            },
            context,
        )
        key = RejectionKey(
            account=_string(data["account"], f"{context}.account"),
            saved_variables_file=_string(data["saved_variables_file"], f"{context}.saved_variables_file"),
            record_kind=_record_kind(data["record_kind"], f"{context}.record_kind"),
            record_id=_string(data["record_id"], f"{context}.record_id"),
            content_digest=_sha256(data["content_digest"], f"{context}.content_digest"),
        )
        status = data["status"]
        if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
            raise CompanionStateError(f"{context}.status must be an HTTP error status")
        return cls(
            key=key,
            status=status,
            error_code=_bounded_text(data["error_code"], MAX_ERROR_CODE_LENGTH, f"{context}.error_code"),
            error_message=_bounded_text(
                data["error_message"], MAX_ERROR_MESSAGE_LENGTH, f"{context}.error_message"
            ),
            first_seen=_timestamp(data["first_seen"], f"{context}.first_seen"),
            last_seen=_timestamp(data["last_seen"], f"{context}.last_seen"),
            configuration_digest=_sha256(data["configuration_digest"], f"{context}.configuration_digest"),
            contract_version=_positive_int(data["contract_version"], f"{context}.contract_version"),
        )


@dataclass(frozen=True)
class CompanionState:
    version: int
    uploaded: Mapping[str, int]
    probes: tuple[int, ...]
    rejections: Mapping[str, RejectionRecord]

    def __post_init__(self) -> None:
        if self.version != STATE_VERSION:
            raise CompanionStateError(
                f"unsupported companion state version {self.version}; expected {STATE_VERSION}"
            )
        object.__setattr__(self, "uploaded", MappingProxyType(dict(self.uploaded)))
        object.__setattr__(self, "probes", tuple(self.probes))
        object.__setattr__(self, "rejections", MappingProxyType(dict(self.rejections)))


def _uploaded(value: object, context: str) -> dict[str, int]:
    data = _mapping(value, context)
    return {
        _string(key, f"{context} key"): _positive_int(item, f"{context}[{key!r}]")
        for key, item in data.items()
    }


def _probes(value: object, context: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise CompanionStateError(f"{context} must be an array")
    return tuple(_positive_int(item, f"{context}[{index}]") for index, item in enumerate(value))


def _strings(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CompanionStateError(f"{context} must be an array")
    return tuple(_string(item, f"{context}[{index}]") for index, item in enumerate(value))


def _decode(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionStateError(f"companion state is not valid UTF-8 JSON: {exc}") from exc
    return _mapping(value, "companion state")


def _current_state(data: Mapping[str, object]) -> CompanionState:
    if "version" not in data:
        raise CompanionStateError("companion state is missing version")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise CompanionStateError("companion state version must be an integer")
    if version == _PREVIOUS_STATE_VERSION:
        raise PreviousCompanionStateError(
            f"companion state version {version} requires explicit upgrade to {STATE_VERSION}"
        )
    if version != STATE_VERSION:
        raise CompanionStateError(f"unsupported companion state version {version}; expected {STATE_VERSION}")
    _exact_keys(data, {"version", "uploaded", "probes", "rejections"}, "companion state")
    return CompanionState(
        version=version,
        uploaded=_uploaded(data["uploaded"], "companion state.uploaded"),
        probes=_probes(data["probes"], "companion state.probes"),
        rejections=_rejections(data["rejections"], "companion state.rejections"),
    )


def _rejections(value: object, context: str, *, previous: bool = False) -> dict[str, RejectionRecord]:
    rejection_data = _mapping(value, context)
    if len(rejection_data) > MAX_REJECTIONS:
        raise CompanionStateError(f"{context} exceeds {MAX_REJECTIONS} entries")
    rejections: dict[str, RejectionRecord] = {}
    for storage_key, raw_record in rejection_data.items():
        if previous:
            record = _previous_rejection_record(raw_record, f"{context}[{storage_key!r}]")
        else:
            record = RejectionRecord.from_mapping(raw_record, f"{context}[{storage_key!r}]")
        if record.key.storage_key() != storage_key:
            raise CompanionStateError(f"{context}[{storage_key!r}] key does not match its record")
        if previous and record.key.record_kind not in _RETAINED_RECORD_KINDS:
            continue
        rejections[storage_key] = record
    return rejections


def _previous_rejection_record(value: object, context: str) -> RejectionRecord:
    """Validate a version-2 rejection before migrating or dropping its record kind."""

    data = _mapping(value, context)
    _exact_keys(
        data,
        {
            "account",
            "saved_variables_file",
            "record_kind",
            "record_id",
            "content_digest",
            "status",
            "error_code",
            "error_message",
            "first_seen",
            "last_seen",
            "configuration_digest",
            "contract_version",
        },
        context,
    )
    status = data["status"]
    if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
        raise CompanionStateError(f"{context}.status must be an HTTP error status")
    return RejectionRecord(
        key=RejectionKey(
            account=_string(data["account"], f"{context}.account"),
            saved_variables_file=_string(data["saved_variables_file"], f"{context}.saved_variables_file"),
            record_kind=_record_kind(data["record_kind"], f"{context}.record_kind", previous=True),
            record_id=_string(data["record_id"], f"{context}.record_id"),
            content_digest=_sha256(data["content_digest"], f"{context}.content_digest"),
        ),
        status=status,
        error_code=_bounded_text(data["error_code"], MAX_ERROR_CODE_LENGTH, f"{context}.error_code"),
        error_message=_bounded_text(
            data["error_message"], MAX_ERROR_MESSAGE_LENGTH, f"{context}.error_message"
        ),
        first_seen=_timestamp(data["first_seen"], f"{context}.first_seen"),
        last_seen=_timestamp(data["last_seen"], f"{context}.last_seen"),
        configuration_digest=_sha256(data["configuration_digest"], f"{context}.configuration_digest"),
        contract_version=_positive_int(data["contract_version"], f"{context}.contract_version"),
    )


def parse_bytes(raw: bytes) -> CompanionState:
    """Parse only the current versioned state document."""

    data = _decode(raw)
    if "version" not in data:
        raise CompanionStateError("companion state is missing version")
    return _current_state(data)


def upgrade_previous_state_bytes(raw: bytes) -> CompanionState:
    """Upgrade the version-2 operational state and discard retired workflow cache entries."""

    data = _decode(raw)
    if "version" not in data or data["version"] != _PREVIOUS_STATE_VERSION:
        raise CompanionStateError("version-2 upgrade requires companion state version 2")
    _exact_keys(
        data,
        {"version", "uploaded", "probes", "references", "accounts", "executions", "rejections"},
        "version-2 companion state",
    )
    _strings(data["references"], "version-2 companion state.references")
    _strings(data["accounts"], "version-2 companion state.accounts")
    executions = _mapping(data["executions"], "version-2 companion state.executions")
    for key, value in executions.items():
        _string(key, "version-2 companion state.executions key")
        _sha256(value, f"version-2 companion state.executions[{key!r}]")
    _probes(data["probes"], "version-2 companion state.probes")
    return CompanionState(
        version=STATE_VERSION,
        uploaded=_uploaded(data["uploaded"], "version-2 companion state.uploaded"),
        probes=_probes(data["probes"], "version-2 companion state.probes"),
        rejections=_rejections(data["rejections"], "version-2 companion state.rejections", previous=True),
    )


def dump_bytes(state: CompanionState) -> bytes:
    """Serialize current state as canonical deterministic UTF-8 JSON."""

    if state.version != STATE_VERSION:
        raise CompanionStateError(
            f"unsupported companion state version {state.version}; expected {STATE_VERSION}"
        )
    uploaded = _uploaded(dict(state.uploaded), "companion state.uploaded")
    probes = _probes(list(state.probes), "companion state.probes")
    rejection_data = _rejections(
        {key: record.to_mapping() for key, record in state.rejections.items()}, "companion state.rejections"
    )
    if len(rejection_data) > MAX_REJECTIONS:
        raise CompanionStateError(f"companion state.rejections exceeds {MAX_REJECTIONS} entries")
    value = {
        "version": STATE_VERSION,
        "uploaded": uploaded,
        "probes": list(probes),
        "rejections": {key: record.to_mapping() for key, record in rejection_data.items()},
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
