"""The versioned on-disk contract shared by the companion and session readers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

STATE_VERSION = 2
MAX_REJECTIONS = 256
REJECTION_RETENTION_DAYS = 30
MAX_ERROR_CODE_LENGTH = 64
MAX_ERROR_MESSAGE_LENGTH = 256


class CompanionStateError(ValueError):
    """The current companion state document is malformed."""


class LegacyCompanionStateError(CompanionStateError):
    """The document has the pre-versioned shape and needs explicit companion upgrade."""


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
            record_kind=_string(data["record_kind"], f"{context}.record_kind"),
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
    references: tuple[str, ...]
    accounts: tuple[str, ...]
    executions: Mapping[str, str]
    rejections: Mapping[str, RejectionRecord]

    def __post_init__(self) -> None:
        if self.version != STATE_VERSION:
            raise CompanionStateError(
                f"unsupported companion state version {self.version}; expected {STATE_VERSION}"
            )
        object.__setattr__(self, "uploaded", MappingProxyType(dict(self.uploaded)))
        object.__setattr__(self, "probes", tuple(self.probes))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "accounts", tuple(self.accounts))
        object.__setattr__(self, "executions", MappingProxyType(dict(self.executions)))
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


def _executions(value: object, context: str) -> dict[str, str]:
    data = _mapping(value, context)
    return {
        _string(key, f"{context} key"): _sha256(item, f"{context}[{key!r}]") for key, item in data.items()
    }


def _decode(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionStateError(f"companion state is not valid UTF-8 JSON: {exc}") from exc
    return _mapping(value, "companion state")


def _current_state(data: Mapping[str, object]) -> CompanionState:
    if "version" not in data:
        raise LegacyCompanionStateError("legacy unversioned companion state requires explicit upgrade")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise CompanionStateError("companion state version must be an integer")
    if version != STATE_VERSION:
        raise CompanionStateError(f"unsupported companion state version {version}; expected {STATE_VERSION}")
    _exact_keys(
        data,
        {"version", "uploaded", "probes", "references", "accounts", "executions", "rejections"},
        "companion state",
    )
    rejection_data = _mapping(data["rejections"], "companion state.rejections")
    if len(rejection_data) > MAX_REJECTIONS:
        raise CompanionStateError(f"companion state.rejections exceeds {MAX_REJECTIONS} entries")
    rejections: dict[str, RejectionRecord] = {}
    for storage_key, value in rejection_data.items():
        record = RejectionRecord.from_mapping(value, f"companion state.rejections[{storage_key!r}]")
        if record.key.storage_key() != storage_key:
            raise CompanionStateError(
                f"companion state.rejections[{storage_key!r}] key does not match its record"
            )
        rejections[storage_key] = record
    return CompanionState(
        version=version,
        uploaded=_uploaded(data["uploaded"], "companion state.uploaded"),
        probes=_probes(data["probes"], "companion state.probes"),
        references=_strings(data["references"], "companion state.references"),
        accounts=_strings(data["accounts"], "companion state.accounts"),
        executions=_executions(data["executions"], "companion state.executions"),
        rejections=rejections,
    )


def parse_bytes(raw: bytes) -> CompanionState:
    """Parse only the current versioned state document."""

    data = _decode(raw)
    if "version" not in data:
        raise LegacyCompanionStateError("legacy unversioned companion state requires explicit upgrade")
    return _current_state(data)


def upgrade_legacy_state_bytes(raw: bytes) -> CompanionState:
    """Explicitly upgrade the one supported legacy unversioned state shape."""

    data = _decode(raw)
    if "version" in data:
        raise CompanionStateError("legacy upgrade requires an unversioned companion state")
    unknown = sorted(set(data) - {"uploaded", "probes", "references", "accounts", "executions"})
    if unknown:
        raise CompanionStateError(f"legacy companion state has unknown key(s): {', '.join(unknown)}")
    if "uploaded" not in data or "probes" not in data:
        missing = sorted({"uploaded", "probes"} - set(data))
        raise CompanionStateError(f"legacy companion state is missing key(s): {', '.join(missing)}")
    return CompanionState(
        version=STATE_VERSION,
        uploaded=_uploaded(data["uploaded"], "legacy companion state.uploaded"),
        probes=_probes(data["probes"], "legacy companion state.probes"),
        references=_strings(data["references"], "legacy companion state.references")
        if "references" in data
        else (),
        accounts=_strings(data["accounts"], "legacy companion state.accounts") if "accounts" in data else (),
        executions=_executions(data["executions"], "legacy companion state.executions")
        if "executions" in data
        else {},
        rejections={},
    )


def dump_bytes(state: CompanionState) -> bytes:
    """Serialize current state as canonical deterministic UTF-8 JSON."""

    if state.version != STATE_VERSION:
        raise CompanionStateError(
            f"unsupported companion state version {state.version}; expected {STATE_VERSION}"
        )
    uploaded = _uploaded(dict(state.uploaded), "companion state.uploaded")
    probes = _probes(list(state.probes), "companion state.probes")
    references = _strings(list(state.references), "companion state.references")
    accounts = _strings(list(state.accounts), "companion state.accounts")
    executions = _executions(dict(state.executions), "companion state.executions")
    rejection_data: dict[str, dict[str, object]] = {}
    for storage_key, record in state.rejections.items():
        if not isinstance(storage_key, str) or not isinstance(record, RejectionRecord):
            raise CompanionStateError("companion state.rejections contains an invalid entry")
        if record.key.storage_key() != storage_key:
            raise CompanionStateError(
                f"companion state.rejections[{storage_key!r}] key does not match its record"
            )
        if (
            not isinstance(record.status, int)
            or isinstance(record.status, bool)
            or not 400 <= record.status <= 599
        ):
            raise CompanionStateError(f"companion state.rejections[{storage_key!r}].status is invalid")
        _bounded_text(
            record.error_code,
            MAX_ERROR_CODE_LENGTH,
            f"companion state.rejections[{storage_key!r}].error_code",
        )
        _bounded_text(
            record.error_message,
            MAX_ERROR_MESSAGE_LENGTH,
            f"companion state.rejections[{storage_key!r}].error_message",
        )
        _timestamp(record.first_seen, f"companion state.rejections[{storage_key!r}].first_seen")
        _timestamp(record.last_seen, f"companion state.rejections[{storage_key!r}].last_seen")
        _sha256(
            record.configuration_digest,
            f"companion state.rejections[{storage_key!r}].configuration_digest",
        )
        _positive_int(
            record.contract_version,
            f"companion state.rejections[{storage_key!r}].contract_version",
        )
        rejection_data[storage_key] = record.to_mapping()
    if len(rejection_data) > MAX_REJECTIONS:
        raise CompanionStateError(f"companion state.rejections exceeds {MAX_REJECTIONS} entries")
    value = {
        "version": STATE_VERSION,
        "uploaded": uploaded,
        "probes": list(probes),
        "references": list(references),
        "accounts": list(accounts),
        "executions": executions,
        "rejections": rejection_data,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
