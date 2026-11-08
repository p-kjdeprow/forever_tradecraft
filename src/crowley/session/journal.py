"""Small atomic, mode-0600 journal for the local repeating session."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "schema": 1,
    "state": "STOPPED",
    "mode": "repeat",
    "generation": 0,
    "leaseGeneration": 0,
    "stopRequested": False,
    "closeoutRequested": False,
    "restartRequiresResume": True,
    "reconciliationRequired": False,
    # Monotonic identity for outcome checkpoints.  Manifest filenames are
    # content hashes and therefore have no chronological meaning.
    "commitSequence": 0,
    "qualification": {
        "streak": 0,
        "totalAccepted": 0,
        "cadenceSeedStart": None,
        "accepted": [],
    },
    "blocker": "",
    "lastAction": "",
    "nativeCalibration": False,
    "inputCalibration": False,
    "calibrationEvidence": {},
    "calibrationHistory": [],
    "calibrationInvalidated": "",
    "resumeState": "STOPPED",
    "current": {
        "uid": "",
        "pending": False,
        "startedAt": 0,
        "expectedBoot": "",
        "action": "",
        "deadlineAt": 0,
        "evidence": {},
        "executor": None,
        "mailCycle": {
            "phase": "none",
            "cycleId": "",
            "ackUid": "",
            "queueId": "",
            "generation": 0,
            "leaseGeneration": 0,
            "admittedSeq": 0,
            "mailUnits": 0,
            "budget": None,
            "completedUnits": 0,
            "terminalOutcomeId": "",
            "lastExecutorSeq": 0,
            "admittedEffectiveDueAt": 0.0,
            "deadlineAt": 0.0,
            "reason": "",
        },
        "mailCycleStage": None,
    },
    "telemetry": {},
    # The live checkpoint above is intentionally bounded.  These append-only
    # archives are the durable audit trail; controller writes never trim them.
    "archives": {
        "runs": {"count": 0, "lastOrdinal": 0},
        "evidence": {"count": 0, "lastOrdinal": 0},
        "cadence": {"count": 0, "lastOrdinal": 0},
        "acknowledgements": {"count": 0, "lastOrdinal": 0},
        "interventions": {"count": 0, "lastOrdinal": 0},
        "executor": {"count": 0, "lastOrdinal": 0},
        "mail_cycle": {"count": 0, "lastOrdinal": 0},
    },
}


class JournalError(RuntimeError):
    """A journal was missing, malformed, or could not be written safely."""


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


class Journal:
    """Read and atomically replace one private JSON state file.

    The journal is intentionally independent of SavedVariables.  A controller can
    restart with a durable pause and must not infer an acknowledged scan from a
    stale in-memory addon table.
    """

    def __init__(self, path: str | os.PathLike[str], initial: dict[str, Any] | None = None):
        self.path = Path(path)
        self.initial = _copy(initial or DEFAULT_STATE)
        self.archive_path = self.path.parent / f".{self.path.name}.archives"
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    value = json.load(handle)
            except FileNotFoundError:
                return _copy(self.initial)
            except (OSError, json.JSONDecodeError) as exc:
                raise JournalError(f"cannot read journal {self.path}: {exc}") from exc
            if not isinstance(value, dict) or value.get("schema") != 1:
                raise JournalError("journal schema is not supported")
            return value

    def write(self, value: dict[str, Any]) -> None:
        with self._lock:
            if not isinstance(value, dict) or value.get("schema") != 1:
                raise JournalError("journal value must have schema 1")
            # Archive payloads live outside this checkpoint.  Keep only bounded
            # counters here so one-second status writes remain O(1) in size.
            self._normalise_archive_metadata(value)
            if self.path.exists():
                existing = self.read().get("archives", {})
                incoming = value.get("archives", {})
                if not isinstance(existing, dict) or not isinstance(incoming, dict):
                    raise JournalError("journal archives must be an object")
                for kind, prior in existing.items():
                    next_value = incoming.setdefault(kind, _copy(prior))
                    if not isinstance(prior, dict) or not isinstance(next_value, dict):
                        raise JournalError(f"journal archive {kind!r} metadata is invalid")
                    prior_count = prior.get("count", 0)
                    next_count = next_value.get("count", 0)
                    if (
                        isinstance(prior_count, bool)
                        or not isinstance(prior_count, int)
                        or isinstance(next_count, bool)
                        or not isinstance(next_count, int)
                    ):
                        raise JournalError(f"journal archive {kind!r} is immutable")
                    if next_count < prior_count:
                        # A caller may replace a stale checkpoint from an
                        # earlier process.  Retain the durable archive's
                        # monotonic counters rather than allowing that stale
                        # checkpoint to hide existing artifacts.
                        next_value["count"] = prior_count
                        next_value["lastOrdinal"] = max(
                            int(next_value.get("lastOrdinal", 0)), int(prior.get("lastOrdinal", 0))
                        )
                    elif int(next_value.get("lastOrdinal", 0)) < int(prior.get("lastOrdinal", 0)):
                        next_value["lastOrdinal"] = int(prior["lastOrdinal"])
                    if "lastArchivedAt" in prior and "lastArchivedAt" not in next_value:
                        next_value["lastArchivedAt"] = prior["lastArchivedAt"]
                prior_commit = self.read().get("commitSequence", 0)
                next_commit = value.get("commitSequence", 0)
                if (
                    isinstance(prior_commit, bool)
                    or not isinstance(prior_commit, int)
                    or prior_commit < 0
                    or isinstance(next_commit, bool)
                    or not isinstance(next_commit, int)
                    or next_commit < 0
                ):
                    raise JournalError("journal commit sequence is invalid")
                if next_commit < prior_commit:
                    value["commitSequence"] = prior_commit
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise JournalError(f"cannot write journal {self.path}: {exc}") from exc

    def update(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            value = self.read()
            change(value)
            self.write(value)
            return _copy(value)

    def append_archive(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        """Append one immutable evidence record to a durable archive.

        The controller keeps only a bounded checkpoint in its normal journal
        fields.  Archive records are copied on ingress and egress, and the
        archive lists are append-only: no live checkpoint update can evict an
        accepted run, rejection, cadence failure, acknowledgement, or human
        intervention from a previous run.
        """

        if not isinstance(kind, str) or not kind or kind in {".", ".."} or "/" in kind or "\\" in kind:
            raise JournalError("archive kind must be nonempty")
        if not isinstance(record, dict):
            raise JournalError("archive record must be an object")
        with self._lock:
            value = self.read()
            self._normalise_archive_metadata(value)
            item, changed = self._append_archive_to_value(value, kind, record)
            if changed:
                self.write(value)
            return _copy(item)

    def append_archive_bundle(
        self,
        records: dict[str, dict[str, Any]],
        *,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Publish one logical outcome and its derived archive views.

        The manifest is the durable transaction boundary.  Individual archive
        views may be interrupted by a crash or injected write failure, but the
        manifest remains and a later owner replays the exact records by their
        stable ``outcomeId``.  No outcome is marked complete before every view
        has been published and checkpointed.
        """

        if not isinstance(records, dict) or not records:
            raise JournalError("archive bundle must contain at least one record")
        if any(
            not isinstance(kind, str) or not kind or kind in {".", ".."} or "/" in kind or "\\" in kind
            for kind in records
        ):
            raise JournalError("archive bundle kinds must be nonempty strings")
        if any(not isinstance(record, dict) for record in records.values()):
            raise JournalError("archive bundle records must be objects")
        outcome_ids = {record.get("outcomeId") for record in records.values()}
        if len(outcome_ids) != 1 or not next(iter(outcome_ids)):
            raise JournalError("archive bundle records need one stable outcomeId")
        outcome_id = str(next(iter(outcome_ids)))
        manifest_dir = self.archive_path / ".bundles"
        manifest_path = manifest_dir / f"{hashlib.sha256(outcome_id.encode('utf-8')).hexdigest()}.json"
        pending_dir = self.archive_path / ".pending"
        pending_path = pending_dir / f"{hashlib.sha256(outcome_id.encode('utf-8')).hexdigest()}.json"
        with self._lock:
            value = self.read()
            self._normalise_archive_metadata(value)
            self._ensure_archive_directory(manifest_dir)
            self._ensure_archive_directory(pending_dir)
            existing_bundle: dict[str, Any] | None = None
            for existing_path in (manifest_path, pending_path):
                if not existing_path.exists():
                    continue
                try:
                    candidate = json.loads(existing_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JournalError(f"journal archive bundle is invalid: {existing_path}") from exc
                if not isinstance(candidate, dict) or candidate.get("outcomeId") != outcome_id:
                    raise JournalError(f"journal archive bundle identity is invalid: {existing_path}")
                if existing_bundle is None:
                    existing_bundle = candidate
                elif candidate != existing_bundle:
                    raise JournalError(f"archive outcome identity {outcome_id!r} has conflicting bundles")
            existing_sequence = existing_bundle.get("commitSequence") if existing_bundle else None
            if isinstance(existing_sequence, bool) or (
                existing_bundle is not None
                and (not isinstance(existing_sequence, int) or existing_sequence <= 0)
            ):
                raise JournalError(f"archive outcome identity {outcome_id!r} has invalid commit sequence")
            commit_sequence = (
                int(existing_sequence) if existing_sequence is not None else self._next_commit_sequence(value)
            )
            manifest: dict[str, Any] = {
                "schema": 1,
                "outcomeId": outcome_id,
                "commitSequence": commit_sequence,
                "records": _copy(records),
            }
            for record in manifest["records"].values():
                record.setdefault("commitSequence", commit_sequence)
            if checkpoint is not None:
                if not isinstance(checkpoint, dict) or checkpoint.get("schema") not in {None, 1}:
                    raise JournalError("archive bundle checkpoint must be a journal object")
                checkpoint_copy = _copy(checkpoint)
                checkpoint_copy["commitSequence"] = commit_sequence
                manifest["checkpoint"] = checkpoint_copy
            if existing_bundle is not None:
                # The first immutable marker fixes the transaction's
                # checkpoint.  A same-owner retry may reconstruct an
                # equivalent terminal checkpoint with a later wall-clock
                # deadline or error history; reusing the durable checkpoint
                # keeps that retry byte-identical without a second writer.
                if existing_bundle.get("records") != manifest["records"]:
                    raise JournalError(f"archive outcome identity {outcome_id!r} has conflicting records")
                if "checkpoint" in existing_bundle:
                    manifest["checkpoint"] = _copy(existing_bundle["checkpoint"])
            pending = {
                "schema": 1,
                "outcomeId": outcome_id,
                "commitSequence": commit_sequence,
                "records": _copy(manifest["records"]),
            }
            if "checkpoint" in manifest:
                pending["checkpoint"] = _copy(manifest["checkpoint"])
            if pending_path.exists():
                try:
                    existing_pending = json.loads(pending_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JournalError(f"journal pending bundle is invalid: {pending_path}") from exc
                if existing_pending != pending:
                    raise JournalError(
                        f"archive outcome identity {outcome_id!r} has conflicting pending records"
                    )
            else:
                # This marker is deliberately a separate durable boundary.  If
                # manifest creation fails, the next owner still has the exact
                # complete outcome and resulting checkpoint to retry.
                # If this first durable write fails, leave the prior live
                # checkpoint untouched.  A fresh owner can retry the same
                # qualification/publication from that checkpoint; there is no
                # second writer or alternate transaction path.
                self._write_pending_artifact(pending_path, pending, pending_dir)
            if manifest_path.exists():
                try:
                    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JournalError(
                        f"journal archive bundle manifest is invalid: {manifest_path}"
                    ) from exc
                if existing != manifest:
                    raise JournalError(f"archive outcome identity {outcome_id!r} has conflicting records")
            else:
                self._write_json_artifact(manifest_path, manifest, manifest_dir)
            # Re-read the manifest on every retry.  This is what makes a fresh
            # owner able to finish a bundle after any publication boundary.
            changed = False
            published: dict[str, dict[str, Any]] = {}
            for kind, record in manifest["records"].items():
                item, item_changed = self._append_archive_to_value(value, kind, record)
                published[kind] = item
                changed = changed or item_changed
            if isinstance(manifest.get("checkpoint"), dict):
                current_sequence = value.get("commitSequence", 0)
                incoming_sequence = manifest.get("commitSequence", 0)
                checkpoint = _copy(manifest["checkpoint"])
                current_state = value.get("state")
                if value.get("stopRequested") or current_state in {"PAUSED", "STOPPED", "BLOCKED"}:
                    for key in (
                        "state",
                        "resumeState",
                        "stopRequested",
                        "closeoutRequested",
                        "restartRequiresResume",
                        "blocker",
                        "generation",
                        "leaseGeneration",
                    ):
                        if key in value:
                            checkpoint[key] = _copy(value[key])
                if (
                    isinstance(current_sequence, int)
                    and not isinstance(current_sequence, bool)
                    and isinstance(incoming_sequence, int)
                    and not isinstance(incoming_sequence, bool)
                    and incoming_sequence > current_sequence
                ):
                    for key, item in checkpoint.items():
                        # Archive counters belong to the immutable artifacts
                        # just repaired above.  A caller-supplied checkpoint
                        # may carry stale/default metadata; copying it here
                        # would roll the counters back after a successful
                        # transaction.
                        if key == "archives":
                            continue
                        if value.get(key) != item:
                            value[key] = _copy(item)
                            changed = True
            # One checkpoint replacement covers the complete logical bundle.
            # A failure leaves the immutable manifest/artifacts for a fresh
            # owner, which retries the same records and repairs the counters.
            if changed:
                self.write(value)
            # Leaving the marker in place is intentional.  It is an applied
            # pending marker: replay remains safe if cleanup itself is
            # interrupted, and the immutable manifest is still authoritative.
            return published

    def replay_archive_bundles(self) -> int:
        """Replay every durable bundle for a fresh owner.

        Recovery is driven by the on-disk manifest, rather than by an old
        process retrying the same call.  Each manifest is immutable and the
        normal archive publication path is idempotent by outcome identity.
        """

        manifest_dir = self.archive_path / ".bundles"
        pending_dir = self.archive_path / ".pending"
        paths = [*pending_dir.glob("*.json"), *manifest_dir.glob("*.json")]
        if not paths:
            return 0
        count = 0
        seen_outcomes: set[str] = set()
        manifests: list[tuple[int, Path, dict[str, Any]]] = []
        for manifest_path in paths:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JournalError(f"journal archive bundle manifest is invalid: {manifest_path}") from exc
            if not isinstance(manifest, dict) or not isinstance(manifest.get("records"), dict):
                raise JournalError(f"journal archive bundle manifest is invalid: {manifest_path}")
            records = manifest["records"]
            outcome_id = manifest.get("outcomeId")
            if (
                not isinstance(outcome_id, str)
                or not outcome_id
                or any(
                    not isinstance(record, dict) or record.get("outcomeId") != outcome_id
                    for record in records.values()
                )
            ):
                raise JournalError(f"journal archive bundle identity is invalid: {manifest_path}")
            sequence = manifest.get("commitSequence", 0)
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise JournalError(f"journal archive bundle sequence is invalid: {manifest_path}")
            manifests.append((sequence, manifest_path, manifest))
        for _sequence, _manifest_path, manifest in sorted(
            manifests, key=lambda item: (item[0], item[1].name)
        ):
            self.append_archive_bundle(manifest["records"], checkpoint=manifest.get("checkpoint"))
            outcome_id = str(manifest["outcomeId"])
            if outcome_id not in seen_outcomes:
                seen_outcomes.add(outcome_id)
                count += 1
        return count

    def _next_commit_sequence(self, value: dict[str, Any]) -> int:
        current = value.get("commitSequence", 0)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise JournalError("journal commit sequence is invalid")
        highest = current
        for directory in (self.archive_path / ".bundles", self.archive_path / ".pending"):
            if not directory.exists():
                continue
            for path in directory.glob("*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JournalError(f"journal archive bundle is invalid: {path}") from exc
                sequence = item.get("commitSequence") if isinstance(item, dict) else None
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                    raise JournalError(f"journal archive bundle sequence is invalid: {path}")
                highest = max(highest, sequence)
        return highest + 1

    def _append_archive_to_value(
        self, value: dict[str, Any], kind: str, record: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Publish one view while deferring its checkpoint replacement."""

        if not isinstance(kind, str) or not kind or kind in {".", ".."} or "/" in kind or "\\" in kind:
            raise JournalError("archive kind must be nonempty")
        if not isinstance(record, dict):
            raise JournalError("archive record must be an object")
        archives = value["archives"]
        metadata = archives.setdefault(kind, {"count": 0, "lastOrdinal": 0})
        if not isinstance(metadata, dict):
            raise JournalError(f"journal archive {kind!r} metadata is invalid")
        count = metadata.get("count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise JournalError(f"journal archive {kind!r} count is invalid")
        item = _copy(record)
        kind_path = self.archive_path / kind
        self._ensure_archive_directory(kind_path)
        # If a process crashed after publishing an artifact but before its
        # checkpoint update, reuse that immutable artifact and repair only the
        # bounded counters on the next owner.
        highest_ordinal = count
        for artifact in sorted(kind_path.glob("*.json")):
            try:
                artifact_ordinal = int(artifact.stem)
                existing = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise JournalError(f"journal archive artifact {artifact} is invalid") from exc
            if not isinstance(existing, dict):
                raise JournalError(f"journal archive artifact {artifact} is invalid")
            highest_ordinal = max(highest_ordinal, artifact_ordinal)
            if item.get("outcomeId") and existing.get("outcomeId") == item.get("outcomeId"):
                if not self._same_archive_record(existing, item):
                    raise JournalError(
                        f"archive outcome identity {item['outcomeId']!r} has conflicting payload"
                    )
                changed = artifact_ordinal > count
                if changed:
                    metadata["count"] = artifact_ordinal
                    metadata["lastOrdinal"] = max(int(metadata.get("lastOrdinal", 0)), artifact_ordinal)
                    if "archivedAt" in existing:
                        metadata["lastArchivedAt"] = existing["archivedAt"]
                return _copy(existing), changed
            if artifact_ordinal > count and self._same_archive_record(existing, item):
                metadata["count"] = artifact_ordinal
                metadata["lastOrdinal"] = artifact_ordinal
                if "archivedAt" in existing:
                    metadata["lastArchivedAt"] = existing["archivedAt"]
                return _copy(existing), True
        ordinal = highest_ordinal + 1
        item.setdefault("archiveOrdinal", ordinal)
        item.setdefault("archivedAt", int(time.time()))
        self._write_archive_artifact(kind, ordinal, item)
        metadata["count"] = max(count, ordinal)
        metadata["lastOrdinal"] = ordinal
        metadata["lastArchivedAt"] = item["archivedAt"]
        return item, True

    @staticmethod
    def _write_json_artifact(destination: Path, value: dict[str, Any], directory: Path) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise JournalError(f"cannot write archive artifact {destination}: {exc}") from exc

    @staticmethod
    def _write_pending_artifact(destination: Path, value: dict[str, Any], directory: Path) -> None:
        """Write the pre-manifest recovery marker atomically."""

        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        parent_fd = os.open(directory.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise JournalError(f"cannot write pending archive marker {destination}: {exc}") from exc

    def _ensure_archive_directory(self, path: Path) -> None:
        created: list[Path] = []
        current = path
        while not current.exists():
            created.append(current)
            current = current.parent
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.archive_path, 0o700)
        os.chmod(path, 0o700)
        # fsync every newly created directory entry so a crash cannot leave an
        # apparently published outcome with an unlinked archive directory.
        directories = [path.parent.parent, self.archive_path, *reversed(created)]
        seen: set[Path] = set()
        for directory in directories:
            if directory in seen:
                continue
            seen.add(directory)
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
            except OSError as exc:
                raise JournalError(f"cannot open archive directory {directory}: {exc}") from exc
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    @staticmethod
    def _same_archive_record(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
        """Compare an unpublished retry without using its generated metadata."""

        def payload(value: dict[str, Any]) -> dict[str, Any]:
            return {key: child for key, child in value.items() if key not in {"archiveOrdinal", "archivedAt"}}

        return payload(existing) == payload(incoming)

    def archives(self, kind: str | None = None) -> Any:
        """Return a defensive copy of one archive or all archive lists."""

        value = self.read().get("archives", {})
        if not isinstance(value, dict):
            raise JournalError("journal archives must be an object")
        kinds = [kind] if kind is not None else sorted(value)
        result: dict[str, list[dict[str, Any]]] = {}
        for archive_kind in kinds:
            if (
                not isinstance(archive_kind, str)
                or not archive_kind
                or archive_kind in {".", ".."}
                or "/" in archive_kind
                or "\\" in archive_kind
            ):
                raise JournalError("archive kind must be nonempty")
            result[archive_kind] = self._read_archive_artifacts(archive_kind)
        return result[kind] if kind is not None else result

    @staticmethod
    def _archive_defaults() -> dict[str, dict[str, int]]:
        return {key: {"count": 0, "lastOrdinal": 0} for key in DEFAULT_STATE["archives"]}

    def _normalise_archive_metadata(self, value: dict[str, Any]) -> None:
        archives = value.setdefault("archives", self._archive_defaults())
        if not isinstance(archives, dict):
            raise JournalError("journal archives must be an object")
        # A pre-artifact checkpoint may still contain arrays.  Materialize them
        # once, then retain only bounded metadata in the live file.
        for archive_kind, entries in list(archives.items()):
            if (
                not isinstance(archive_kind, str)
                or not archive_kind
                or archive_kind in {".", ".."}
                or "/" in archive_kind
                or "\\" in archive_kind
            ):
                raise JournalError("archive kind must be nonempty")
            if isinstance(entries, list):
                # Archive arrays were never part of the released bounded
                # checkpoint contract.  Refusing them avoids a crash retry
                # race where a dynamic migration timestamp conflicts with an
                # already-published immutable artifact.
                raise JournalError(f"journal archive {archive_kind!r} uses unsupported legacy arrays")
            if not isinstance(entries, dict):
                raise JournalError(f"journal archive {archive_kind!r} metadata is invalid")
            count = entries.get("count", 0)
            ordinal = entries.get("lastOrdinal", 0)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 0
                or ordinal > count
            ):
                raise JournalError(f"journal archive {archive_kind!r} metadata is invalid")

    def _write_archive_artifact(self, kind: str, ordinal: int, record: dict[str, Any]) -> None:
        kind_path = self.archive_path / kind
        self._ensure_archive_directory(kind_path)
        destination = kind_path / f"{ordinal:020d}.json"
        if destination.exists():
            # Existing artifacts are immutable.  A byte-identical retry is
            # idempotent when publication succeeded before its checkpoint.
            try:
                if json.loads(destination.read_text(encoding="utf-8")) == record:
                    return
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise JournalError(f"journal archive {kind!r} ordinal {ordinal} is immutable")
        fd, temporary = tempfile.mkstemp(prefix=f".{ordinal:020d}.", dir=kind_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(kind_path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise JournalError(f"cannot write archive artifact {destination}: {exc}") from exc

    def _read_archive_artifacts(self, kind: str) -> list[dict[str, Any]]:
        kind_path = self.archive_path / kind
        if not kind_path.exists():
            return []
        try:
            files = sorted(kind_path.glob("*.json"))
            entries = []
            for path in files:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise JournalError(f"journal archive artifact {path} is invalid")
                entries.append(value)
            return entries
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalError(f"cannot read archive {kind!r}: {exc}") from exc


def stable_file(path: str | os.PathLike[str], *, polls: int = 2, poll_interval: float = 0.05) -> bytes:
    """Read a file only after two identical metadata polls and a stable read.

    This is the boundary used for SavedVariables and companion state.  It never
    treats an HTTP response or a log line as proof that the on-disk queue changed.
    """

    target = Path(path)
    if polls < 2:
        raise ValueError("stable_file requires at least two polls")
    if poll_interval < 0:
        raise ValueError("poll_interval must be nonnegative")
    previous: tuple[int, int] | None = None
    for index in range(polls + 2):
        stat_before = target.stat()
        marker = (stat_before.st_mtime_ns, stat_before.st_size)
        data = target.read_bytes()
        stat_after = target.stat()
        if marker == (stat_after.st_mtime_ns, stat_after.st_size) and marker == previous:
            return data
        previous = marker
        # Two reads in one scheduler turn do not establish that a companion or
        # SavedVariables writer has finished.  Keep the interval injectable so
        # unit tests remain deterministic while the daemon observes real polls.
        if index < polls + 1 and poll_interval:
            time.sleep(poll_interval)
    raise JournalError(f"file did not stabilize: {target}")


__all__ = ["DEFAULT_STATE", "Journal", "JournalError", "stable_file"]
