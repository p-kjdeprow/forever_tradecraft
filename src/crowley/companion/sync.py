"""Moves data between the game folder and the server.

WoW only writes SavedVariables on /reload, logout or exit, and only reads addon files at
login or /reload, so this is a polling loop over files rather than anything event-driven.
Polling also works on Wine prefixes and network mounts where inotify is unreliable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from crowley.core import savedvars
from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua

from .config import CompanionSettings, state_path

log = logging.getLogger(__name__)


def _json_digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class State:
    uploaded: dict[str, int] = field(default_factory=dict)  # scan id -> upload time
    probes: list[int] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    executions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():  # first run
            return cls()
        data = json.loads(path.read_text())
        return cls(
            uploaded=data["uploaded"],
            probes=data["probes"],
            references=data.get("references", []),
            accounts=data.get("accounts", []),
            executions=data.get("executions", {}),
        )

    def save(self, path: Path) -> None:
        # Uploaded ids only matter while the scan may still be in SavedVariables.
        cutoff = time.time() - 30 * 86400
        self.uploaded = {k: v for k, v in self.uploaded.items() if v >= cutoff}
        self.probes = self.probes[-50:]
        self.references = self.references[-50:]
        self.accounts = self.accounts[-50:]
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps(
                {
                    "uploaded": self.uploaded,
                    "probes": self.probes,
                    "references": self.references,
                    "accounts": self.accounts,
                    "executions": self.executions,
                }
            ),
        )


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Companion:
    def __init__(self, settings: CompanionSettings):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.api_token.get_secret_value()}"},
            timeout=120,
        )
        self.state_file = state_path()
        self.state = State.load(self.state_file)
        self._signatures: dict[Path, tuple[float, int]] = {}
        self._pending: dict[Path, tuple[float, int]] = {}
        self._present_ids: dict[Path, list[str]] = {}
        self._last_data_write = 0.0
        self._queue_warning_logged = False

    # -- reading the game's files -------------------------------------------------------

    def changed_files(self) -> list[Path]:
        """Files whose (mtime, size) changed and then held still for one poll interval,
        so we never parse a file the client is halfway through writing."""
        ready = []
        for path in self.settings.saved_variables_files():
            stat = path.stat()
            sig = (stat.st_mtime, stat.st_size)
            if self._signatures.get(path) == sig:
                continue
            if self._pending.get(path) == sig:
                ready.append(path)
            else:
                self._pending[path] = sig
        return ready

    def process_file(self, path: Path) -> int:
        db = savedvars.load_file(path)["CrowleyDB"]
        self._present_ids[path] = list(db["scans"])

        uploaded = 0
        for scan_id, scan in db["scans"].items():
            if scan_id not in self.state.uploaded:
                self.upload_scan(scan_id, scan)
                uploaded += 1

        reference = db.get("reference")
        if reference and reference["id"] not in self.state.references:
            self.upload_reference(reference)
            uploaded += 1

        account = db.get("account")
        if account and account["id"] not in self.state.accounts:
            self.upload_account(account)
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
            if self.state.executions.get(queue_id) != digest:
                self.upload_execution(queue_id, execution)
                self.state.executions[queue_id] = digest
                uploaded += 1

        # Only present after /crowley probe has been run.
        probe = db.get("probe")
        if probe and probe["at"] not in self.state.probes:
            self.client.post("/v1/probes", json=probe).raise_for_status()
            self.state.probes.append(probe["at"])
            log.info("uploaded client probe from %s", time.ctime(probe["at"]))

        self.state.save(self.state_file)
        return uploaded

    def upload_scan(self, scan_id: str, scan: dict[str, Any]) -> None:
        response = self.client.post("/v1/scans", json=scan)
        if response.is_error:
            raise RuntimeError(f"server rejected scan {scan_id}: {response.status_code} {response.text}")
        result = response.json()
        log.info(
            "scan %s -> %s (%s listings, %s)", scan_id, result["house"], result["listings"], result["status"]
        )
        self.state.uploaded[scan_id] = int(time.time())

    def upload_reference(self, reference: dict[str, Any]) -> None:
        capture_id = reference["id"]
        response = self.client.post("/v1/item-reference", json=reference)
        if not response.is_success:
            raise RuntimeError(
                f"server rejected item reference {capture_id}: {response.status_code} {response.text}"
            )
        result = response.json()
        log.info(
            "item reference %s -> %s (%s)",
            capture_id,
            result.get("status", "ok"),
            result.get("items", "unknown"),
        )
        self.state.references.append(capture_id)

    def upload_account(self, account: dict[str, Any]) -> None:
        capture_id = account["id"]
        response = self.client.post("/v1/account-state", json=account)
        if not response.is_success:
            raise RuntimeError(
                f"server rejected account state {capture_id}: {response.status_code} {response.text}"
            )
        result = response.json()
        log.info(
            "account state %s -> %s (%s items)",
            capture_id,
            result.get("status", "ok"),
            result.get("items", "unknown"),
        )
        self.state.accounts.append(capture_id)

    def upload_execution(self, queue_id: str, execution: dict[str, Any]) -> None:
        response = self.client.post("/v1/executions", json=execution)
        if not response.is_success:
            raise RuntimeError(
                f"server rejected execution {queue_id}: {response.status_code} {response.text}"
            )
        log.info("uploaded execution %s (%s results)", queue_id, len(execution.get("results", [])))

    # -- writing back into the game -----------------------------------------------------

    def write_addon_data(self) -> None:
        # A missing queue identity is a local, deterministic no-queue state.  Publish it
        # before contacting the server so an outage cannot leave an old Queue.lua armed.
        configured = bool(self.settings.house_key and self.settings.character)
        if not configured:
            self.write_queue()
        scan_ids = sorted({sid for ids in self._present_ids.values() for sid in ids})
        response = self.client.post("/v1/addon-data", json={"scan_ids": scan_ids})
        response.raise_for_status()
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
            "wrote %s (%d acked scans, %d shopping lists)",
            target,
            len(data["ackedScans"]),
            len(data["shoppingLists"]),
        )
        if configured:
            self.write_queue()

    def write_queue(self) -> None:
        queue_id = "none"
        queue: dict[str, Any] | None = None
        if self.settings.house_key and self.settings.character:
            response = self.client.get(
                f"/v1/houses/{self.settings.house_key}/queue/active",
                params={"character": self.settings.character},
            )
            response.raise_for_status()
            queue = response.json()
            if queue is not None:
                queue_id = queue["queueId"]
        else:
            if not self._queue_warning_logged:
                log.warning("queue settings unset; writing Queue.lua with no queue")
                self._queue_warning_logged = True
        text = render_queue_lua(queue)
        target = self.settings.queue_lua
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current != text:
            atomic_write(target, text)
            log.info("wrote Queue.lua (%s)", queue_id)

    # -- loop ---------------------------------------------------------------------------

    def tick(self) -> None:
        changed = self.changed_files()
        for path in changed:
            self.process_file(path)
            # Commit the observed signature only after uploads and state saving succeed.
            # A transport failure leaves the stable file pending for the next poll.
            self._signatures[path] = self._pending.pop(path)
        refresh_due = time.time() - self._last_data_write >= self.settings.refresh_minutes * 60
        if changed or refresh_due:
            self.write_addon_data()

    def run(self) -> None:
        log.info("watching %s", self.settings.wow_path / "WTF" / "Account")
        log.info("server %s", self.settings.server_url)
        while True:
            try:
                self.tick()
            except httpx.TransportError as exc:
                log.warning("server unreachable (%s); will retry", exc)
            time.sleep(self.settings.poll_seconds)

    def once(self) -> None:
        """Process every file immediately, ignoring the stability wait."""
        for path in self.settings.saved_variables_files():
            self.process_file(path)
        self.write_addon_data()
