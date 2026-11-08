"""Engine4 transition fences and durable session archive checks."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import crowley.session.cli as cli_module
from crowley.session import Controller, ControllerState
from crowley.session.cli import SessionIPC
from crowley.session.controller import ActionKind, ControllerStatus
from crowley.session.evidence import SweepEvidence
from crowley.session.journal import DEFAULT_STATE, Journal, JournalError
from tests.support.companion_state import write_companion_state


class _Driver:
    def __init__(self) -> None:
        self.guardian = SimpleNamespace(generation=0, revoke=self.revoke)

    def revoke(self, _reason: str) -> int:
        self.guardian.generation += 1
        return self.guardian.generation


class _InteractionDriver(_Driver):
    def __init__(self) -> None:
        super().__init__()
        self.guardian.armed = False

    def input_calibration(self, _evidence: object) -> None:
        return None

    def arm(self, _evidence: object) -> int:
        self.guardian.generation += 1
        self.guardian.armed = True
        return self.guardian.generation


def _journal(path: Path, **changes: object) -> Journal:
    value = copy.deepcopy(DEFAULT_STATE)
    value.update(changes)
    journal = Journal(path)
    journal.write(value)
    return journal


def _evidence(uid: str, started_at: int) -> SweepEvidence:
    item_evidence = {
        str(item_id): {
            "coverage": "full",
            "sourceComplete": True,
            "priced": True,
            "rowCount": 1,
            "pricedRowCount": 1,
            "ownedRows": (
                [{"itemID": item_id, "buyout": {"kind": "known", "copper": 1}}] if item_id == 1 else []
            ),
            "ownedPricedRowCount": 1 if item_id == 1 else 0,
            "category": ("recipe", "container", "equipment")[item_id % 3] if item_id < 4 else "commodity",
            "query": "queried",
        }
        for item_id in range(1, 429)
    }
    return SweepEvidence(
        uid=uid,
        house="beta-normal-alliance",
        plan_hash="plan",
        observations=428,
        attempted=428,
        full=428,
        bounded=0,
        incomplete=0,
        unqueried=0,
        priced=428,
        row_count=428,
        kinds=("commodity", "container", "equipment", "recipe"),
        query_durations=(1,),
        envelope={"startedAt": started_at, "finishedAt": started_at + 2},
        payload={},
        stable_verified=True,
        item_evidence=item_evidence,
        owned_rows=[{"itemID": 1, "buyout": {"kind": "known", "copper": 1}}],
        owned_priced_row_count=1,
    )


def _complete_cycle(controller: Controller, value: SweepEvidence, index: int) -> None:
    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot(f"old-save-{index}")
    controller.observe_frame(boot=f"new-save-{index}", sequence=1, crc32=index)
    controller.stable_saved(value)
    controller.uploaded(
        value.uid,
        accepted=True,
        upload_time=value.envelope["finishedAt"] + 1,
        data_ack_uid=value.uid,
    )
    controller.record_ack_publication(
        value.uid,
        companion_uid=value.uid,
        data_uid=value.uid,
        publication_time=value.envelope["finishedAt"] + 1,
        boot=f"new-save-{index}",
        sequence=1,
    )
    controller.ack_reload_command_sent()
    controller.expect_boot(f"old-ack-{index}", acknowledgement=True)
    controller.observe_frame(boot=f"new-ack-{index}", sequence=2, crc32=index + 10)
    controller.ack_reload_complete(
        pending_ids=set(),
        loaded_uid=value.uid,
        loaded_at=value.envelope["finishedAt"] + 2,
        boot=f"new-ack-{index}",
        sequence=2,
    )


def test_native_construction_checkpoint_is_strict_and_rebinds_frame(tmp_path: Path) -> None:
    malformed = {
        "schema": 1,
        "B0": "old",
        "expectedConstructorBoot": "native-construction:old",
        "ownerGeneration": 7,
        "kind": "preflight",
        "phase": "prepared",
        "unexpected": "must be preserved",
    }
    journal = _journal(
        tmp_path / "session.json",
        state=ControllerState.PAUSED.value,
        generation=7,
        current={"frameBoot": "old", "frameSequence": 1, "frameCrc32": 2},
        nativeConstruction=malformed,
    )
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)

    assert controller.native_construction_checkpoint_valid() is False
    assert controller.native_construction_checkpoint() is None
    controller.tick(expected_generation=7, expected_state=ControllerState.PAUSED)
    assert journal.read()["nativeConstruction"] == malformed

    valid = {
        "schema": 1,
        "B0": "old",
        "expectedConstructorBoot": "native-construction:old",
        "ownerGeneration": 7,
        "kind": "preflight",
        "phase": "prepared",
    }
    with pytest.raises(ValueError, match="malformed"):
        controller.set_native_construction_checkpoint({**valid, "extra": True})
    controller.set_native_construction_checkpoint(
        valid, expected_generation=7, expected_state=ControllerState.PAUSED
    )
    controller.rebind_native_construction_frame(
        boot="ordinary-new",
        sequence=4,
        crc32=9,
        reason="native_construction_recovery",
        expected_generation=7,
        expected_state=ControllerState.PAUSED,
    )
    assert journal.read()["current"]["frameBoot"] == "ordinary-new"
    assert journal.read()["current"]["frameSequence"] == 4
    assert journal.read()["current"]["frameCrc32"] == 9


def test_present_null_native_checkpoint_remains_invalid_after_restart(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "session.json",
        state=ControllerState.PAUSED.value,
        generation=7,
        nativeConstruction=None,
    )
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    assert controller.native_construction_checkpoint_valid() is False
    assert controller.native_construction_checkpoint() is None
    controller.tick(expected_generation=7, expected_state=ControllerState.PAUSED)
    assert "nativeConstruction" in journal.read()
    assert journal.read()["nativeConstruction"] is None


def test_native_construction_ipc_has_exact_request_shapes(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "session.json",
        state=ControllerState.PAUSED.value,
        generation=7,
    )
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    calls: list[tuple[str, dict[str, object]]] = []

    class Owner:
        def native_construction_prepare(self, **kwargs: object) -> dict[str, object]:
            calls.append(("prepare", kwargs))
            return {"handshake": {"state": "construction_prepared"}}

        def native_construction_recovery_begin(self, **kwargs: object) -> dict[str, object]:
            calls.append(("recovery", kwargs))
            return {"handshake": {"state": "recovery_required"}}

    ipc = SessionIPC(controller, Owner())
    malformed = ipc.dispatch({"command": "native_construction_prepare", "generation": 7, "unknown": "reject"})
    assert malformed == {"ok": False, "error": "native construction request fields are malformed"}
    accepted = ipc.dispatch({"command": "native_construction_prepare", "generation": 7})
    assert accepted == {"ok": True, "handshake": {"state": "construction_prepared"}}
    restart = ipc.dispatch(
        {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
    )
    assert restart == {"ok": True, "handshake": {"state": "recovery_required"}}
    assert calls == [
        ("prepare", {"expected_generation": 7}),
        ("recovery", {"expected_generation": 7, "reservation_id": ""}),
    ]


def test_native_construction_ipc_routes_all_eight_shapes(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session-all-shapes.json", state=ControllerState.PAUSED.value, generation=7)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    calls: list[tuple[str, dict[str, object]]] = []

    class Owner:
        def _reply(self, name: str, **kwargs: object) -> dict[str, object]:
            calls.append((name, kwargs))
            return {"handshake": {"state": name}}

        def native_construction_prepare(self, **kwargs: object) -> dict[str, object]:
            return self._reply("prepare", **kwargs)

        def native_construction_control_preflight(self, **kwargs: object) -> dict[str, object]:
            return self._reply("control_preflight", **kwargs)

        def native_construction_bootstrap(self, **kwargs: object) -> dict[str, object]:
            return self._reply("bootstrap", **kwargs)

        def native_construction_recheck(self, **kwargs: object) -> dict[str, object]:
            return self._reply("recheck", **kwargs)

        def native_construction_finalize_admit(self, **kwargs: object) -> dict[str, object]:
            return self._reply("finalize_admit", **kwargs)

        def native_construction_recovery_begin(self, **kwargs: object) -> dict[str, object]:
            return self._reply("recovery_begin", **kwargs)

        def native_construction_recovery_complete(self, **kwargs: object) -> dict[str, object]:
            return self._reply("recovery_complete", **kwargs)

        def native_construction_recovery_cancel(self, **kwargs: object) -> dict[str, object]:
            return self._reply("recovery_cancel", **kwargs)

    ipc = SessionIPC(controller, Owner())
    requests = [
        ({"command": "native_construction_prepare", "generation": 7}, "prepare"),
        ({"command": "native_construction_control_preflight", "generation": 7}, "control_preflight"),
        (
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": "reservation",
                "expectedConstructorBoot": "native-construction:boot",
            },
            "bootstrap",
        ),
        (
            {"command": "native_construction_recheck", "generation": 7, "reservationId": "reservation"},
            "recheck",
        ),
        (
            {
                "command": "native_construction_finalize_admit",
                "generation": 7,
                "reservationId": "reservation",
            },
            "finalize_admit",
        ),
        (
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""},
            "recovery_begin",
        ),
        (
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": "reservation",
            },
            "recovery_complete",
        ),
        (
            {
                "command": "native_construction_recovery_cancel",
                "generation": 7,
                "reservationId": "reservation",
            },
            "recovery_cancel",
        ),
    ]
    for request, expected in requests:
        malformed = ipc.dispatch({**request, "unexpected": True})
        assert malformed == {"ok": False, "error": "native construction request fields are malformed"}
        response = ipc.dispatch(request)
        assert response == {"ok": True, "handshake": {"state": expected}}
    assert [name for name, _kwargs in calls] == [expected for _request, expected in requests]


def test_native_construction_cli_argparse_roundtrips_all_eight_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: list[dict[str, object]] = []
    config = SimpleNamespace(reload_deadline_seconds=45.0, socket=tmp_path / "session.sock")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)

    def request(_socket: Path, value: dict[str, object], *, timeout: float) -> dict[str, object]:
        captured.append({**value, "_timeout": timeout})
        return {"ok": True, "accepted": value["command"]}

    monkeypatch.setattr(cli_module, "_socket_request", request)
    argv = [
        ["native-construction-prepare", "--generation", "7"],
        ["native-construction-control-preflight", "--generation", "7"],
        [
            "native-construction-bootstrap",
            "--generation",
            "7",
            "--reservation-id",
            "r",
            "--expected-constructor-boot",
            "native-construction:b",
        ],
        ["native-construction-recheck", "--generation", "7", "--reservation-id", "r"],
        ["native-construction-finalize-admit", "--generation", "7", "--reservation-id", "r"],
        ["native-construction-recovery-begin", "--generation", "7", "--reservation-id", ""],
        ["native-construction-recovery-complete", "--generation", "7", "--reservation-id", "r"],
        ["native-construction-recovery-cancel", "--generation", "7", "--reservation-id", "r"],
    ]
    for command in argv:
        assert cli_module.main(["--config", str(tmp_path / "session.toml"), *command]) == 0
        capsys.readouterr()
    assert [item["command"] for item in captured] == [
        "native_construction_prepare",
        "native_construction_control_preflight",
        "native_construction_bootstrap",
        "native_construction_recheck",
        "native_construction_finalize_admit",
        "native_construction_recovery_begin",
        "native_construction_recovery_complete",
        "native_construction_recovery_cancel",
    ]
    assert captured[2]["expectedConstructorBoot"] == "native-construction:b"
    assert captured[5]["reservationId"] == ""


def test_native_construction_ipc_rejects_all_twelve_predecessor_edges(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "session-predecessors.json", state=ControllerState.PAUSED.value, generation=7
    )
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)

    class Owner:
        def __init__(self, message: str) -> None:
            self.message = message

        def _reject(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError(self.message)

        native_construction_prepare = _reject
        native_construction_control_preflight = _reject
        native_construction_bootstrap = _reject
        native_construction_recheck = _reject
        native_construction_finalize_admit = _reject
        native_construction_recovery_begin = _reject
        native_construction_recovery_complete = _reject
        native_construction_recovery_cancel = _reject

    predecessor_cases = [
        (
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": "r",
                "expectedConstructorBoot": "native-construction:b",
            },
            "bootstrap requires a prepared reservation",
        ),
        (
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": "preflight",
                "expectedConstructorBoot": "native-construction:b",
            },
            "control preflight cannot bootstrap",
        ),
        (
            {"command": "native_construction_recheck", "generation": 7, "reservationId": "r"},
            "recheck requires an active reservation",
        ),
        (
            {"command": "native_construction_finalize_admit", "generation": 7, "reservationId": "r"},
            "finalize requires resume_ready",
        ),
        (
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""},
            "recovery requires a durable checkpoint",
        ),
        (
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": "r"},
            "recovery admission is already in flight",
        ),
        (
            {"command": "native_construction_recovery_complete", "generation": 7, "reservationId": "r"},
            "recovery complete requires a ticket",
        ),
        (
            {"command": "native_construction_recovery_cancel", "generation": 7, "reservationId": "r"},
            "recovery cancel requires a ticket",
        ),
        (
            {"command": "native_construction_recovery_complete", "generation": 7, "reservationId": "r"},
            "recovery ticket was already cancelled",
        ),
        (
            {"command": "native_construction_recovery_cancel", "generation": 7, "reservationId": "r"},
            "recovery ticket was already completed",
        ),
        (
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": "measure"},
            "only a control preflight reservation can hand off to recovery",
        ),
        (
            {"command": "native_construction_prepare", "generation": 7},
            "native construction checkpoint already exists",
        ),
    ]
    for request, message in predecessor_cases:
        response = SessionIPC(controller, Owner(message)).dispatch(request)
        assert response == {"ok": False, "error": message}


def test_stale_saved_observation_cannot_overwrite_pause(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    driver = _Driver()
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller.begin_scan(started_at=100)
    fence = controller.observation_fence()
    controller.pause("operator takeover")

    result = controller.saved(
        "stale-uid",
        expected_generation=fence.generation,
        expected_state=fence.state,
        fence=fence,
    )

    assert result.state is ControllerState.PAUSED
    assert result.last_uid == ""
    assert journal.read()["state"] == ControllerState.PAUSED.value


def test_interaction_start_uses_calibration_rearm_and_reconcile_gates(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "interaction.json")
    driver = _InteractionDriver()
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._native_calibrated = True
    controller._input_calibrated = True
    controller._input_evidence = object()

    started = controller.start("interaction")

    assert started.mode.value == "interaction"
    assert started.state is ControllerState.RECONCILE
    assert driver.guardian.armed is True
    assert started.lease_generation == driver.guardian.generation

    reconciled = controller.reconcile_interaction(
        identity_ok=True,
        world_ready=True,
        expected_generation=started.generation,
        fence=controller.observation_fence(),
    )
    assert reconciled.state is ControllerState.READY
    assert reconciled.mode.value == "interaction"


def test_interaction_start_does_not_mutate_mode_when_pending_work_requires_resume(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "interaction-pending.json", current={"pending": True, "uid": "uid-1"})
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)

    with pytest.raises(RuntimeError, match="pending scan work"):
        controller.start("interaction")

    assert controller.status().mode.value == "repeat"


def test_restart_advances_epoch_for_every_persisted_restartable_state(tmp_path: Path) -> None:
    for state in (ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED):
        journal = _journal(
            tmp_path / f"{state.value.lower()}.json",
            state=state.value,
            generation=7,
        )
        previous = Controller(journal, driver=_Driver(), recover_on_restart=False)
        stale_fence = previous.observation_fence()

        fresh = Controller(journal, driver=_Driver())

        assert fresh.status().generation == 8
        assert fresh.status().state is state
        rejected = fresh.set_action_intent("stale-ipc-work", fence=stale_fence)
        assert rejected.action_intent == ""
        assert journal.read()["generation"] == 8


def test_fence_rejects_same_state_result_after_new_intent(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    fence = controller.observation_fence()
    controller.set_action_intent(ActionKind.OPEN_AH.value)

    result = controller.record_model_proof(
        {"capturePath": "/tmp/capture", "text": "private model output"},
        fence=fence,
    )

    assert result.action_intent == ActionKind.OPEN_AH.value
    assert "modelProof" not in journal.read()["current"]["evidence"]
    assert not journal.archives("evidence")


def test_restart_uncertain_intent_resolves_without_replay_or_deadlock(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "session.json",
        state=ControllerState.SAVE_RELOAD_SENT.value,
        generation=4,
        current={
            "uid": "uid-uncertain",
            "pending": True,
            "startedAt": 100,
            "action": "save_reload",
            "actionAttempt": 7,
            "actionResult": "dispatched",
            "expectedBoot": "boot-2",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.status().state is ControllerState.PAUSED
    resumed = controller.resume()
    assert resumed.state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    unresolved = controller.reconcile_restart(
        observed_boot="boot-2",
        observed_uid="",
        scanner_phase="ready",
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )
    assert unresolved.state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="boot-2",
        observed_uid="",
        scanner_phase="ready",
        observed_scene="world",
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        stable_scan_verified=True,
        stable_files_complete=True,
        scanner_idle=True,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )
    assert resolved.state is ControllerState.BLOCKED
    assert resolved.action_intent == ""
    assert not journal.read()["restartUncertain"]
    assert journal.archives("runs")[-1]["qualification"] == "rejected"


def test_restart_observe_frame_preserves_pre_effect_boot_for_save_resolution(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "save-boot-recovery.json", state=ControllerState.WAIT_DUE.value)
    owner = Controller(journal, driver=_Driver(), recover_on_restart=False)
    owner.begin_scan(started_at=100)
    evidence = _evidence("uid-save-recovery", 100)
    owner.saved(evidence.uid)
    owner.reload_sent()
    # The daemon observes the live pre-effect frame before it asks for the
    # reload.  ``expect_boot`` then persists that same B0 at the command
    # boundary; no later frame may become the reference.
    owner.observe_frame(boot="save-before-command", sequence=1, crc32=1)
    owner.expect_boot("save-before-command")
    owner.pause("crash before save effect")

    recovered = Controller(journal, driver=_Driver())
    assert recovered.resume().state is ControllerState.RECONCILE
    fence = recovered.observation_fence()
    old_boot = recovered.reconcile_restart(
        observed_boot="save-before-command",
        observed_uid=evidence.uid,
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={evidence.uid},
        stable_sweep_ids=set(),
        stable_scan_uid=evidence.uid,
        stable_scan_evidence=evidence,
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )
    assert old_boot.state is ControllerState.RECONCILE
    assert old_boot.action_intent == ActionKind.SAVE_RELOAD.value
    observed = recovered.observe_frame(boot="save-after-restart", sequence=2, crc32=2)
    assert observed.state is ControllerState.RECONCILE
    current = journal.read()["current"]
    assert current["frameBoot"] == "save-after-restart"
    assert current["restartReferenceBoot"] == "save-before-command"
    assert current["restartObservedBoot"] == "save-after-restart"

    # Simulate a crash after the first fresh frame and before intent
    # reconciliation.  The pre-effect reference must survive the next owner.
    recovered = Controller(journal, driver=_Driver())
    assert recovered.resume().state is ControllerState.RECONCILE
    fence = recovered.observation_fence()
    resolved = recovered.reconcile_restart(
        observed_boot="save-after-restart",
        observed_uid=evidence.uid,
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={evidence.uid},
        stable_sweep_ids=set(),
        stable_scan_uid=evidence.uid,
        stable_scan_evidence=evidence,
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.NATIVE_CALIBRATION
    assert resolved.action_intent == "upload_wait"
    assert journal.read()["restartUncertain"] is False
    assert journal.read()["current"]["restartReferenceBoot"] == ""


def test_restart_observe_frame_allows_one_expected_enter_world_boot(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "enter-world-boot-recovery.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": ActionKind.ENTER_WORLD.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    observed = controller.observe_frame(boot="world-boot", sequence=1, crc32=1)
    assert observed.state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="world-boot",
        observed_scene="world",
        action_postcondition=True,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.NATIVE_CALIBRATION
    assert resolved.action_intent == ""
    assert journal.read()["restartUncertain"] is False
    assert journal.read()["current"]["restartReferenceBoot"] == ""


def test_enter_world_without_agent_boot_accepts_once_then_blocks_churn(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "enter-world-empty-boot.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": ActionKind.ENTER_WORLD.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    first = controller.observe_frame(boot="first-agent-boot", sequence=1, crc32=1)
    assert first.state is ControllerState.RECONCILE
    assert journal.read()["current"]["restartReferenceBoot"] == ""
    assert journal.read()["current"]["restartObservedBoot"] == "first-agent-boot"
    second = controller.observe_frame(boot="second-agent-boot", sequence=2, crc32=2)
    assert second.state is ControllerState.BLOCKED
    assert "boot" in second.blocker


@pytest.mark.parametrize(
    "action",
    [ActionKind.SAVE_RELOAD.value, ActionKind.ACK_RELOAD.value, ActionKind.ENTER_WORLD.value],
)
def test_restart_boot_transition_is_single_use_for_reload_intents(tmp_path: Path, action: str) -> None:
    journal = _journal(
        tmp_path / f"{action}-boot-churn.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": action,
            "actionAttempt": 3,
            "actionResult": "dispatched",
            "frameBoot": "pre-effect-boot",
            "restartReferenceBoot": "pre-effect-boot",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    first = controller.observe_frame(boot="post-effect-boot", sequence=1, crc32=1)
    assert first.state is ControllerState.RECONCILE
    second = controller.observe_frame(boot="second-post-effect-boot", sequence=2, crc32=2)
    assert second.state is ControllerState.BLOCKED
    assert "boot" in second.blocker


def test_restart_boot_transition_is_rejected_for_unrelated_intent(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "logout-boot-churn.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": ActionKind.LOGOUT.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
            "frameBoot": "pre-effect-boot",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    blocked = controller.observe_frame(boot="post-effect-boot", sequence=1, crc32=1)
    assert blocked.state is ControllerState.BLOCKED
    assert "boot" in blocked.blocker


def test_new_intent_gets_new_attempt_after_upload(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.saved("uid-1")
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    controller.stable_saved(_evidence("uid-1", 100))
    before = controller.status()
    controller.uploaded("uid-1", accepted=True, upload_time=102, data_ack_uid="uid-1")
    after = controller.status()
    assert after.state is ControllerState.ACK_RELOAD_SENT
    assert after.action_intent == "ack_reload"
    assert after.action_intent != before.action_intent
    current = journal.read()["current"]
    assert current["actionResult"] == "pending"
    assert current["actionAttempt"] > 1


def test_saved_observation_preserves_prepared_reload_intent(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "prepared-save.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.set_action_intent("save_reload")
    controller.action_result("dispatched")
    before = journal.read()["current"].copy()

    observed = controller.saved("uid-prepared")

    after = journal.read()["current"]
    assert observed.state is ControllerState.QUIET_SAVE
    assert after["action"] == "save_reload"
    assert after["actionAttempt"] == before["actionAttempt"]
    assert after["actionResult"] == "dispatched"
    assert after["deadlineAt"] == before["deadlineAt"]


def test_dispatched_scan_start_preserves_attempt_for_fresh_empty_reconcile(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "dispatched-scan.json", state=ControllerState.WAIT_DUE.value)
    owner = Controller(journal, driver=_Driver(), recover_on_restart=False)

    # This is the controller-visible public sequence used by the daemon's
    # /crowley scan command: durable preparation, dispatch, then the state
    # transition into the scanner boundary.
    owner.set_action_intent("scan", deadline_seconds=600)
    owner.action_result("preparing")
    owner.action_result("dispatched")
    before_start = journal.read()["current"].copy()
    started = owner.begin_scan(started_at=100)

    after_start = journal.read()["current"]
    assert started.state is ControllerState.SCANNING
    assert after_start["action"] == ActionKind.SCAN.value
    assert after_start["actionAttempt"] == before_start["actionAttempt"] == 1
    assert after_start["actionResult"] == "dispatched"
    assert after_start["startedAt"] == 100

    # A fresh owner must be able to resolve the already-dispatched effect from
    # ready/empty stable queues without minting another scan or replaying it.
    recovered = Controller(journal, driver=_Driver())
    assert recovered.resume().state is ControllerState.RECONCILE
    fence = recovered.observation_fence()
    resolved = recovered.reconcile_restart(
        observed_boot="scan-boot",
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.BLOCKED
    assert resolved.action_intent == ""
    assert resolved.last_uid == ""
    assert journal.read()["current"]["actionAttempt"] == before_start["actionAttempt"]
    runs = journal.archives("runs")
    assert len(runs) == 1
    assert runs[0]["qualification"] == "rejected"
    assert runs[0]["uid"] == ""
    assert runs[0]["evidence"]["attemptObservation"] == "ready_empty_stable_queues"

    # Restarting after the truthful rejection cannot replay or create a second
    # empty attempt because the dispatched intent was cleared durably.
    restarted = Controller(journal, driver=_Driver())
    assert restarted.status().action_intent == ""
    assert len(journal.archives("runs")) == 1


def test_run_and_evidence_archives_survive_bounded_checkpoint(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    first = _evidence("uid-1", 100)
    _complete_cycle(controller, first, 1)
    controller.qualify(first, started_at=100)
    controller.reopen_complete()
    controller.begin_scan(started_at=1000)
    second = _evidence("uid-2", 1000)
    _complete_cycle(controller, second, 2)
    controller.qualify(second, started_at=1000)

    archives = journal.archives()
    assert [item["uid"] for item in archives["runs"]] == ["uid-1", "uid-2"]
    assert all(
        item["ownedRows"] == [{"itemID": 1, "buyout": {"kind": "known", "copper": 1}}]
        for item in archives["runs"]
    )
    assert len(archives["cadence"]) == 2
    checkpoint_archives = journal.read()["archives"]
    assert all(isinstance(value, dict) for value in checkpoint_archives.values())
    assert checkpoint_archives["runs"]["count"] == 2
    assert list((tmp_path / ".session.json.archives" / "runs").glob("*.json"))
    assert journal.read()["qualification"]["totalAccepted"] == 2


def test_archive_retry_after_checkpoint_loss_reuses_immutable_artifact(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "retry.json")
    record = {"kind": "restart_resolution", "uid": "uid-retry", "outcome": "unresolved"}
    first = journal.append_archive("interventions", record)

    # Model a crash after artifact publication but before the live checkpoint
    # replacement.  The next append receives the same logical record.
    checkpoint = journal.read()
    checkpoint["archives"]["interventions"] = {"count": 0, "lastOrdinal": 0}
    (tmp_path / "retry.json").write_text(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    retry = journal.append_archive("interventions", record)
    assert retry["archiveOrdinal"] == first["archiveOrdinal"] == 1
    assert len(journal.archives("interventions")) == 1


def test_total_accepted_is_unbounded_while_live_rows_stay_bounded(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    # Seed a valid durable boundary rather than replaying twenty identical
    # full 428-item cycles.  The controller still executes the real 20->21
    # transition, appends the 21st archive, and trims only its live window.
    seeded = journal.read()
    seeded["qualification"] = {
        "streak": 20,
        "totalAccepted": 20,
        "cadenceSeedStart": 100,
        "accepted": [{"uid": f"uid-{index}"} for index in range(20)],
    }
    journal.write(seeded)
    for index in range(20):
        journal.append_archive("runs", {"uid": f"uid-{index}", "ordinal": index + 1})

    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    index = 20
    started = 100 + index * 900
    controller.begin_scan(started_at=started)
    value = _evidence(f"uid-{index}", started)
    _complete_cycle(controller, value, index)
    controller.qualify(value, started_at=started)

    checkpoint = journal.read()["qualification"]
    assert checkpoint["totalAccepted"] == 21
    assert len(checkpoint["accepted"]) == 20
    assert len(journal.archives("runs")) == 21


def test_upload_rejection_archives_complete_rejected_outcome(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    value = _evidence("uid-rejected", 100)
    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    controller.stable_saved(value)
    rejected = controller.uploaded(value.uid, accepted=False)

    assert rejected.state is ControllerState.BLOCKED
    assert not rejected.last_uid == ""
    assert not journal.read()["current"]["pending"]
    runs = journal.archives("runs")
    cadence = journal.archives("cadence")
    acknowledgements = journal.archives("acknowledgements")
    assert runs[-1]["qualification"] == "rejected"
    assert runs[-1]["evidence"]["fullEvidence"]["uid"] == value.uid
    assert cadence[-1]["result"] == "rejected"
    assert acknowledgements[-1]["result"] == "rejected"


def test_outcome_bundle_replays_once_after_every_publication_boundary(tmp_path: Path, monkeypatch) -> None:
    for boundary in ("runs", "evidence", "cadence", "acknowledgements", "checkpoint"):
        journal = _journal(
            tmp_path / f"outcome-{boundary}.json",
            state=ControllerState.WAIT_DUE.value,
        )
        controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
        controller.begin_scan(started_at=100)
        value = _evidence(f"uid-{boundary}", 100)
        _complete_cycle(controller, value, 1)

        original_write_archive = journal._write_archive_artifact
        original_write = journal.write
        failed = False

        def fail_publication(
            kind: str,
            ordinal: int,
            record: dict,
            *,
            target_boundary: str = boundary,
            write_archive=original_write_archive,
        ) -> None:
            nonlocal failed
            if not failed and kind == target_boundary:
                failed = True
                raise JournalError(f"injected {kind} publication failure")
            write_archive(kind, ordinal, record)

        def fail_checkpoint(value_to_write: dict, *, write_checkpoint=original_write) -> None:
            nonlocal failed
            # Qualification first persists its durable full-evidence pending
            # checkpoint. Inject the publication checkpoint failure only after
            # that deliberate staging write has succeeded.
            fail_checkpoint.calls += 1
            if fail_checkpoint.calls == 2 and not failed:
                failed = True
                raise JournalError("injected checkpoint failure")
            write_checkpoint(value_to_write)

        fail_checkpoint.calls = 0

        if boundary == "checkpoint":
            monkeypatch.setattr(journal, "write", fail_checkpoint)
        else:
            monkeypatch.setattr(journal, "_write_archive_artifact", fail_publication)
        with pytest.raises(JournalError):
            controller.qualify(value, started_at=100)
        assert failed
        monkeypatch.undo()

        recovered = Controller(journal, driver=_Driver(), recover_on_restart=False)
        assert len(recovered.journal.archives("runs")) == 1
        assert len(recovered.journal.archives("evidence")) == 1
        assert len(recovered.journal.archives("cadence")) == 1
        assert len(recovered.journal.archives("acknowledgements")) == 1
        checkpoint = recovered.journal.read()["qualification"]
        assert checkpoint["totalAccepted"] == 1
        assert checkpoint["accepted"][0]["uid"] == f"uid-{boundary}"


def test_pause_after_dispatched_reload_preserves_attempt_and_marks_uncertain(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.saved("uid-pause")
    controller.reload_sent()
    before = controller.status()
    current = journal.read()["current"]
    paused = controller.pause("operator takeover")

    assert paused.state is ControllerState.PAUSED
    assert paused.action_intent == before.action_intent == "save_reload"
    assert journal.read()["current"]["actionAttempt"] == current["actionAttempt"]
    assert journal.read()["current"]["actionResult"] == "dispatched"
    assert journal.read()["restartUncertain"]


def test_stop_before_dispatch_preserves_durable_intent_for_reconciliation(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session-preparing-stop.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.saved("uid-preparing-stop")
    controller.set_action_intent("save_reload", deadline_seconds=45)
    controller.action_result("preparing")

    stopped = controller.stop()

    assert stopped.state is ControllerState.QUIET_SAVE
    assert stopped.action_intent == "save_reload"
    assert stopped.stop_requested
    current = journal.read()["current"]
    assert current["actionResult"] == "preparing"
    assert journal.read()["restartUncertain"]


def test_stop_preserves_each_reload_intent_without_dispatching_cleanup(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.saved("uid-stop")
    controller.reload_sent()
    save_attempt = journal.read()["current"]["actionAttempt"]
    stopped = controller.stop()
    assert stopped.state is ControllerState.SAVE_RELOAD_SENT
    assert stopped.stop_requested
    assert stopped.action_intent == "save_reload"
    assert journal.read()["current"]["actionAttempt"] == save_attempt

    phases: list[tuple[str, Controller, ControllerState]] = []

    journal = _journal(tmp_path / "session-save-wait.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    controller.saved("uid-save-wait")
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    phases.append(("save_wait", controller, ControllerState.SAVE_RELOAD_WAIT))

    journal = _journal(tmp_path / "session-upload-wait.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    value = _evidence("uid-upload-wait", 100)
    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    controller.stable_saved(value)
    phases.append(("upload_wait", controller, ControllerState.UPLOAD_WAIT))

    journal = _journal(tmp_path / "session-ack-sent.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    value = _evidence("uid-ack-sent", 100)
    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    controller.stable_saved(value)
    controller.uploaded(value.uid, accepted=True, upload_time=103, data_ack_uid=value.uid)
    phases.append(("ack_sent", controller, ControllerState.ACK_RELOAD_SENT))

    journal = _journal(tmp_path / "session-ack-wait.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    value = _evidence("uid-ack-wait", 100)
    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot("old-save")
    controller.observe_frame(boot="new-save", sequence=1, crc32=1)
    controller.stable_saved(value)
    controller.uploaded(value.uid, accepted=True, upload_time=103, data_ack_uid=value.uid)
    controller.ack_reload_command_sent()
    controller.expect_boot("old-ack", acknowledgement=True)
    controller.observe_frame(boot="new-ack", sequence=2, crc32=2)
    phases.append(("ack_wait", controller, ControllerState.ACK_RELOAD_WAIT))

    for name, phase_controller, expected_state in phases:
        before = phase_controller.status()
        current_before = phase_controller.journal.read()["current"]
        stopped = phase_controller.stop()
        current_after = phase_controller.journal.read()["current"]
        assert stopped.stop_requested, name
        assert stopped.action_intent == before.action_intent, name
        assert current_after["actionAttempt"] == current_before["actionAttempt"], name
        assert current_after["actionResult"] == current_before["actionResult"], name
        assert current_after["deadlineAt"] == current_before["deadlineAt"], name
        assert stopped.state is expected_state, name


@pytest.mark.parametrize(
    ("action", "scene", "extra"),
    [
        (
            ActionKind.OPEN_AH.value,
            "auction_house",
            {"observed_ah_open": True, "action_postcondition": True},
        ),
        (
            ActionKind.DISMISS_DISCONNECT.value,
            "game_client",
            {"observed_modal": "none", "action_postcondition": True},
        ),
        (ActionKind.RECONNECT_GAME.value, "character_select", {"action_postcondition": True}),
        (ActionKind.SELECT_ARTHUR.value, "arthur_selected", {"action_postcondition": True}),
        (ActionKind.ENTER_WORLD.value, "world", {"action_postcondition": True}),
    ],
)
def test_fresh_owner_resolves_each_named_action_from_its_postcondition(
    tmp_path: Path, action: str, scene: str, extra: dict[str, object]
) -> None:
    journal = _journal(
        tmp_path / f"{action}.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "uid": "",
            "pending": False,
            "action": action,
            "actionAttempt": 3,
            "actionResult": "dispatched",
            "frameBoot": "old-enter-world" if action == ActionKind.ENTER_WORLD.value else "",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="fresh-boot" if action == ActionKind.ENTER_WORLD.value else "",
        observed_scene=scene,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
        **extra,
    )

    assert resolved.action_intent == ""
    assert resolved.state is ControllerState.NATIVE_CALIBRATION
    assert not journal.read()["restartUncertain"]
    assert journal.archives("interventions")[-1]["outcome"] == f"{action}_postcondition_observed"


def test_named_dismiss_consumes_canonical_game_client_observation(tmp_path: Path) -> None:
    from crowley.session.driver import NamedStateObservation

    journal = _journal(
        tmp_path / "canonical-dismiss.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": ActionKind.DISMISS_DISCONNECT.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
        },
    )
    observation = NamedStateObservation.from_facts(
        state="game_client",
        identity_fingerprint="trusted-client",
        target_xid=42,
        capture_id="capture-1",
        capture_sequence=1,
        facts={"scene": "game_client", "modal": "none"},
        source="native-visual",
        observed_monotonic=1.0,
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    facts = dict(observation.facts)
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_scene=facts["scene"],
        observed_modal=facts["modal"],
        action_postcondition=True,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.action_intent == ""
    assert resolved.state is ControllerState.NATIVE_CALIBRATION


@pytest.mark.parametrize(
    ("action", "scene", "extra"),
    [
        (ActionKind.OPEN_AH.value, "auction_house", {"observed_ah_open": True}),
        (ActionKind.DISMISS_DISCONNECT.value, "game_client", {}),
        (ActionKind.RECONNECT_GAME.value, "character_select", {}),
        (ActionKind.SELECT_ARTHUR.value, "arthur_selected", {}),
        (ActionKind.ENTER_WORLD.value, "world", {}),
    ],
)
def test_named_action_requires_positive_fresh_postcondition(
    tmp_path: Path, action: str, scene: str, extra: dict[str, object]
) -> None:
    journal = _journal(
        tmp_path / f"stale-{action}.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={"action": action, "actionAttempt": 3, "actionResult": "dispatched"},
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="fresh-boot" if action == ActionKind.ENTER_WORLD.value else "",
        observed_scene=scene,
        action_postcondition=False,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
        **extra,
    )

    assert resolved.state is ControllerState.RECONCILE
    assert resolved.action_intent == action
    assert journal.read()["restartUncertain"]


def test_ready_empty_dispatched_scan_is_a_truthful_rejected_attempt(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "empty-scan.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "action": ActionKind.SCAN.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
            "startedAt": 100,
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="same-boot",
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.BLOCKED
    assert resolved.action_intent == ""
    assert resolved.last_uid == ""
    assert journal.read()["qualification"]["totalAccepted"] == 0
    run = journal.archives("runs")[-1]
    assert run["qualification"] == "rejected"
    assert run["uid"] == ""
    assert run["evidence"]["attemptObservation"] == "ready_empty_stable_queues"


def test_runtime_durable_empty_scan_facts_reject_without_stable_scan_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.test_session_runtime as runtime_fixtures
    from crowley.core.luawriter import render_assignment
    from crowley.core.watchlist import load_catalog
    from tests.test_session_runtime import _runtime_daemon, _snapshot

    # Keep the real adapter/version guard while giving this fixture the
    # authentic private pinned CLI.  The helper's default intentionally
    # models the host config and may point at a newer development binary.
    pinned_cli = Path(
        "/home/dev/.cache/crowley/codex/0.162.1/node_modules/"
        "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    )
    assert pinned_cli.is_file()
    config_type = runtime_fixtures.SessionConfig

    def pinned_config(**kwargs: object):
        return config_type(**kwargs, codex_path=str(pinned_cli))

    monkeypatch.setattr(runtime_fixtures, "SessionConfig", pinned_config)

    daemon, _backend, driver, controller, config, _clock = _runtime_daemon(
        tmp_path,
        journal_changes={
            "state": ControllerState.RECONCILE.value,
            "generation": 4,
            "restartUncertain": True,
            "reconciliationRequired": True,
            "current": {
                "action": ActionKind.SCAN.value,
                "actionAttempt": 3,
                "actionResult": "dispatched",
                "startedAt": 100,
                "pending": True,
            },
        },
    )
    assert config.saved_variables is not None
    assert config.companion_state is not None
    assert config.data_lua is not None
    config.saved_variables.write_text(
        render_assignment("CrowleyDB", {"scans": {}}, "empty scan"), encoding="utf-8"
    )
    write_companion_state(config.companion_state)
    catalog = load_catalog()
    config.data_lua.write_text(
        render_assignment(
            "CROWLEY_DATA",
            {
                "generatedAt": 1000,
                "ackedScans": [],
                "scanPlan": catalog.scan_plan(),
                "referenceItemIDs": sorted(item.item_id for item in catalog.items),
            },
            "empty scan",
        ),
        encoding="utf-8",
    )
    snapshot = _snapshot(1000)
    try:
        facts = daemon._reconcile_durable_files(snapshot)
        assert facts["stable_files_complete"] is True
        assert facts["stable_scan_evidence"] is None
        assert facts["stable_scan_verified"] is False
        fence = controller.observation_fence()
        resolved = controller.reconcile_restart(
            observed_boot=snapshot["boot"],
            scanner_phase=facts["scanner_phase"],
            scanner_idle=facts["scanner_idle"],
            stable_scan_ids=facts["stable_scan_ids"],
            stable_sweep_ids=facts["stable_sweep_ids"],
            stable_scan_uid=facts["stable_scan_uid"],
            stable_scan_evidence=facts["stable_scan_evidence"],
            stable_scan_verified=facts["stable_scan_verified"],
            stable_files_complete=facts["stable_files_complete"],
            observed_scene="auction_house",
            expected_generation=fence.generation,
            expected_state=ControllerState.RECONCILE,
            fence=fence,
        )
        assert resolved.state is ControllerState.BLOCKED
        assert resolved.action_intent == ""
        runs = controller.journal.archives("runs")
        assert len(runs) == 1
        assert runs[0]["qualification"] == "rejected"
        assert runs[0]["uid"] == ""
    finally:
        daemon.request_shutdown()
        driver.close()


def _complete_scan_reconciliation(
    controller: Controller, evidence: SweepEvidence, *, observed_uid: str
) -> ControllerStatus:
    fence = controller.observation_fence()
    return controller.reconcile_restart(
        observed_boot="scan-boot",
        observed_uid=observed_uid,
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={evidence.uid},
        stable_sweep_ids=set(),
        stable_scan_uid=evidence.uid,
        stable_scan_evidence=evidence,
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )


def test_fresh_owner_resolves_first_scan_from_new_stable_uid(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "first-scan.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "uid": "",
            "pending": True,
            "startedAt": 100,
            "action": ActionKind.SCAN.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
        },
    )
    evidence = _evidence("uid-first-scan", 100)
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE

    resolved = _complete_scan_reconciliation(controller, evidence, observed_uid=evidence.uid)

    assert resolved.action_intent == ActionKind.SAVE_RELOAD.value
    assert resolved.state is ControllerState.NATIVE_CALIBRATION
    assert resolved.last_uid == evidence.uid
    assert journal.read()["current"]["uid"] == evidence.uid
    assert journal.read()["resumeState"] == ControllerState.QUIET_SAVE.value


def test_fresh_owner_resolves_next_scan_only_from_a_new_stable_uid(tmp_path: Path) -> None:
    prior_uid = "uid-prior-scan"
    journal = _journal(
        tmp_path / "next-scan.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        qualification={
            "streak": 1,
            "totalAccepted": 1,
            "cadenceSeedStart": 100,
            "accepted": [{"uid": prior_uid, "startedAt": 100}],
        },
        current={
            "uid": prior_uid,
            "pending": True,
            "startedAt": 1_000,
            "action": ActionKind.SCAN.value,
            "actionAttempt": 4,
            "actionResult": "dispatched",
        },
    )
    evidence = _evidence("uid-next-scan", 1_000)
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE

    resolved = _complete_scan_reconciliation(controller, evidence, observed_uid=evidence.uid)

    assert resolved.action_intent == ActionKind.SAVE_RELOAD.value
    assert resolved.last_uid == evidence.uid
    assert resolved.last_uid != prior_uid
    assert journal.read()["current"]["uid"] == evidence.uid


@pytest.mark.parametrize("bad_case", ["wrong_started_at", "old_uid"])
def test_scan_completion_rejects_old_or_mismatched_stable_identity(tmp_path: Path, bad_case: str) -> None:
    prior_uid = "uid-prior-scan" if bad_case == "old_uid" else ""
    started_at = 1_000 if bad_case == "old_uid" else 100
    evidence_uid = prior_uid if bad_case == "old_uid" else "uid-new-scan"
    evidence_started_at = 1_000 if bad_case == "old_uid" else 101
    journal = _journal(
        tmp_path / f"scan-{bad_case}.json",
        state=ControllerState.RECONCILE.value,
        generation=4,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "uid": prior_uid,
            "pending": True,
            "startedAt": started_at,
            "action": ActionKind.SCAN.value,
            "actionAttempt": 3,
            "actionResult": "dispatched",
        },
    )
    evidence = _evidence(evidence_uid, evidence_started_at)
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE

    resolved = _complete_scan_reconciliation(controller, evidence, observed_uid=evidence.uid)

    assert resolved.state is ControllerState.RECONCILE
    assert resolved.action_intent == ActionKind.SCAN.value
    assert journal.read()["current"]["uid"] == prior_uid


def test_completed_logout_uses_character_select_and_empty_disk_without_agent_facts(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path / "logout.json",
        state=ControllerState.RECONCILE.value,
        restartUncertain=True,
        reconciliationRequired=True,
        current={
            "uid": "uid-closeout",
            "pending": True,
            "action": "logout",
            "actionAttempt": 4,
            "actionResult": "dispatched",
        },
    )
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_scene="character_select",
        stable_files_complete=True,
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.STOPPED
    assert resolved.stop_requested
    assert resolved.action_intent == ""
    assert resolved.expected_boot == ""
    assert journal.archives("interventions")[-1]["outcome"] == "logout_completed_character_select"


def _interrupt_ack_reload(journal: Journal, evidence: SweepEvidence) -> Controller:
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=int(evidence.envelope["startedAt"]))
    controller.saved(evidence.uid)
    controller.reload_sent()
    controller.observe_frame(boot="save-before-command", sequence=1, crc32=1)
    controller.expect_boot("save-before-command")
    controller.observe_frame(boot="save-boot", sequence=2, crc32=1)
    controller.stable_saved(evidence)
    controller.uploaded(evidence.uid, accepted=True, upload_time=103, data_ack_uid=evidence.uid)
    controller.record_ack_publication(
        evidence.uid,
        companion_uid=evidence.uid,
        data_uid=evidence.uid,
        publication_time=104,
        boot="save-boot",
        sequence=2,
    )
    controller.ack_reload_command_sent()
    controller.observe_frame(boot="ack-before-command", sequence=3, crc32=2)
    controller.expect_boot("ack-before-command", acknowledgement=True)
    controller.observe_frame(boot="ack-boot", sequence=4, crc32=2)
    controller.pause("fresh owner test")
    return controller


def test_same_owner_ack_pause_after_b1_retains_original_boot(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "same-owner-ack.json", state=ControllerState.WAIT_DUE.value)
    owner = _interrupt_ack_reload(journal, _evidence("uid-same-owner-ack", 100))
    assert owner.resume().state is ControllerState.RECONCILE
    same_boot = owner.observe_frame(boot="ack-boot", sequence=5, crc32=3)
    assert same_boot.state is ControllerState.RECONCILE
    assert journal.read()["current"]["restartReferenceBoot"] == "ack-before-command"
    owner.pause("operator pause after B1")
    assert owner.resume().state is ControllerState.RECONCILE
    same_boot_after_pause = owner.observe_frame(boot="ack-boot", sequence=6, crc32=4)
    assert same_boot_after_pause.state is ControllerState.RECONCILE
    churn = owner.observe_frame(boot="ack-second-boot", sequence=7, crc32=5)
    assert churn.state is ControllerState.BLOCKED


def test_recovered_ack_reconstructs_full_evidence_and_qualifies_once(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "recovered-ack.json", state=ControllerState.WAIT_DUE.value)
    evidence = _evidence("uid-recovered", 100)
    _interrupt_ack_reload(journal, evidence)
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    observed = controller.observe_frame(boot="ack-boot", sequence=5, crc32=3)
    assert observed.state is ControllerState.RECONCILE
    # Crash after the first fresh B1 frame.  Qualification must use only the
    # durable checkpoint on the next owner, with no in-memory evidence passed
    # across the restart.
    controller = Controller(journal, driver=_Driver())
    assert controller.resume().state is ControllerState.RECONCILE
    fence = controller.observation_fence()
    resolved = controller.reconcile_restart(
        observed_boot="ack-boot",
        observed_uid=evidence.uid,
        uploaded_ids={evidence.uid},
        data_ack_ids={evidence.uid},
        loaded_ids={evidence.uid},
        pending_ids=set(),
        loaded_uid=evidence.uid,
        loaded_at=105,
        loaded_boot="ack-boot",
        loaded_sequence=5,
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={evidence.uid},
        stable_sweep_ids=set(),
        stable_scan_uid=evidence.uid,
        stable_scan_evidence=evidence,
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        stable_upload_timestamp=103,
        stable_data_ack_uid=evidence.uid,
        stable_companion_uploaded_uid=evidence.uid,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )

    assert resolved.state is ControllerState.PAUSED
    assert journal.read()["resumeState"] == ControllerState.REOPEN.value
    assert journal.read()["qualification"]["totalAccepted"] == 1
    assert len(journal.archives("runs")) == 1
    run = journal.archives("runs")[0]
    assert run["qualification"] == "accepted"
    assert (
        run["companionUploadedUID"]
        == run["dataAckUID"]
        == run["loadedUID"]
        == run["evidenceUID"]
        == evidence.uid
    )
    assert run["loadedAt"] == 105
    assert run["loadedBoot"] == "ack-boot"
    assert run["loadedSequence"] == 5

    repeated = controller.reconcile_restart(
        observed_boot="ack-boot",
        observed_scene="world",
        stable_scan_ids={evidence.uid},
        stable_sweep_ids=set(),
        expected_generation=resolved.generation,
        expected_state=resolved.state,
    )
    assert repeated.state is ControllerState.PAUSED
    assert journal.read()["qualification"]["totalAccepted"] == 1
    assert len(journal.archives("runs")) == 1
    resumed = controller.resume()
    assert resumed.state is ControllerState.NATIVE_CALIBRATION
    assert journal.read()["resumeState"] == ControllerState.REOPEN.value


def test_recovered_ack_first_pending_failure_retries_from_durable_full_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    journal = _journal(tmp_path / "recovered-pending.json", state=ControllerState.WAIT_DUE.value)
    evidence = _evidence("uid-recovered-pending", 100)
    _interrupt_ack_reload(journal, evidence)
    owner = Controller(journal, driver=_Driver())
    assert owner.resume().state is ControllerState.RECONCILE
    fence = owner.observation_fence()
    original_replace = os.replace
    failed = False

    def fail_first_pending_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal failed
        if not failed and Path(destination).parent.name == ".pending":
            failed = True
            raise OSError("first pending marker write failed")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_pending_replace)
    with pytest.raises(JournalError):
        owner.reconcile_restart(
            observed_boot="ack-boot-new",
            observed_uid=evidence.uid,
            uploaded_ids={evidence.uid},
            data_ack_ids={evidence.uid},
            loaded_ids={evidence.uid},
            pending_ids=set(),
            loaded_uid=evidence.uid,
            loaded_at=105,
            loaded_boot="ack-boot-new",
            loaded_sequence=3,
            scanner_phase="ready",
            scanner_idle=True,
            stable_scan_ids={evidence.uid},
            stable_sweep_ids=set(),
            stable_scan_uid=evidence.uid,
            stable_scan_evidence=evidence,
            stable_scan_verified=True,
            stable_files_complete=True,
            observed_scene="world",
            stable_upload_timestamp=103,
            stable_data_ack_uid=evidence.uid,
            stable_companion_uploaded_uid=evidence.uid,
            expected_generation=fence.generation,
            expected_state=ControllerState.RECONCILE,
            fence=fence,
        )
    monkeypatch.undo()

    durable = journal.read()
    assert durable["state"] == ControllerState.QUALIFY_PENDING.value
    pending = durable["qualificationPending"]
    assert len(pending["evidence"]["itemEvidence"]) == 428
    assert len(pending["evidence"]["ownedRows"]) == 1
    assert pending["metadata"]["atomicCompanionUploadedUid"] == evidence.uid
    assert not list((tmp_path / ".recovered-pending.json.archives" / ".pending").glob("*.json"))

    del owner
    recovered = Controller(journal.path, driver=_Driver())
    assert recovered.status().state is ControllerState.QUALIFY_PENDING
    retried = recovered.retry_pending_qualification()
    assert retried.state is ControllerState.PAUSED
    assert journal.read()["resumeState"] == ControllerState.REOPEN.value
    assert journal.read()["qualification"]["totalAccepted"] == 1
    assert journal.read()["qualificationPending"] is None
    assert len(journal.archives("runs")) == 1
    assert len(journal.archives("acknowledgements")) == 1
    assert journal.read()["archives"]["runs"]["count"] == 1

    paused_owner = Controller(journal.path, driver=_Driver())
    paused_owner.pause("operator takeover")
    repeated = Controller(journal.path, driver=_Driver())
    assert repeated.status().state is ControllerState.PAUSED
    assert repeated.journal.read()["resumeState"] == ControllerState.REOPEN.value
    assert repeated.journal.read()["qualification"]["totalAccepted"] == 1
    assert len(repeated.journal.archives("runs")) == 1


def test_same_owner_public_qualification_failure_preserves_staged_state_and_retries_once(
    tmp_path: Path, monkeypatch
) -> None:
    journal = _journal(tmp_path / "same-owner-outcome.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    first = _evidence("uid-same-owner-1", 100)
    _complete_cycle(controller, first, 1)

    original_replace = os.replace
    failed = False

    def fail_first_pending_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal failed
        if not failed and Path(destination).parent.name == ".pending":
            failed = True
            raise OSError("first pending marker write failed")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_pending_replace)
    with pytest.raises(JournalError):
        controller.qualify(first, started_at=100)
    monkeypatch.undo()

    assert controller.status().state is ControllerState.QUALIFY_PENDING
    assert journal.read()["state"] == ControllerState.QUALIFY_PENDING.value
    assert journal.read()["qualification"]["totalAccepted"] == 0
    blocked = controller.block("qualification publication failed")
    assert blocked.state is ControllerState.QUALIFY_PENDING
    assert journal.read()["state"] == ControllerState.QUALIFY_PENDING.value
    assert journal.read()["qualification"]["totalAccepted"] == 0

    retried = controller.retry_pending_qualification()
    assert retried.state is ControllerState.REOPEN
    assert journal.read()["qualification"]["totalAccepted"] == 1
    assert journal.read()["qualificationPending"] is None
    assert len(journal.archives("runs")) == 1
    with pytest.raises(RuntimeError, match="cannot retry qualification"):
        controller.retry_pending_qualification()

    controller.reopen_complete()
    controller.begin_scan(started_at=1_000)
    second = _evidence("uid-same-owner-2", 1_000)
    _complete_cycle(controller, second, 2)
    original_archive = journal._write_archive_artifact
    failed_artifact = False

    def fail_first_run_artifact(kind: str, ordinal: int, record: dict) -> None:
        nonlocal failed_artifact
        if not failed_artifact and kind == "runs":
            failed_artifact = True
            raise JournalError("run artifact publication failed")
        original_archive(kind, ordinal, record)

    monkeypatch.setattr(journal, "_write_archive_artifact", fail_first_run_artifact)
    with pytest.raises(JournalError):
        controller.qualify(second, started_at=1_000)
    monkeypatch.undo()
    assert controller.status().state is ControllerState.QUALIFY_PENDING
    assert journal.read()["qualification"]["totalAccepted"] == 1
    blocked_again = controller.block("second publication failed")
    assert blocked_again.state is ControllerState.QUALIFY_PENDING
    retried_again = controller.retry_pending_qualification()
    assert retried_again.state is ControllerState.REOPEN
    assert journal.read()["qualification"]["totalAccepted"] == 2
    assert len(journal.archives("runs")) == 2


def test_real_driver_qualification_repair_rearms_before_actionable_reopen(
    tmp_path: Path, monkeypatch
) -> None:
    from crowley.session.driver import RecordingBackend
    from tests.test_session import input_calibration, make_test_driver, native_calibration

    driver = make_test_driver(RecordingBackend(), lease_path=tmp_path / "desktop.lock")
    journal = Journal(tmp_path / "real-driver-qualification.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    try:
        assert controller.start().state is ControllerState.NATIVE_CALIBRATION
        controller.native_calibrated(native_calibration())
        controller.input_calibrated(evidence=input_calibration())
        assert driver.guardian.armed
        controller.reconcile(identity_ok=True)
        controller.begin_scan(started_at=100)
        evidence = _evidence("uid-real-driver-repair", 100)
        _complete_cycle(controller, evidence, 1)

        original_replace = os.replace
        failed = False

        def fail_first_pending_replace(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            nonlocal failed
            if not failed and Path(destination).parent.name == ".pending":
                failed = True
                raise OSError("first pending marker write failed")
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_first_pending_replace)
        with pytest.raises(JournalError):
            controller.qualify(evidence, started_at=100)
        monkeypatch.undo()

        assert controller.status().state is ControllerState.QUALIFY_PENDING
        assert driver.guardian.armed
        paused = controller.pause("operator takeover after publication failure")
        assert paused.state is ControllerState.PAUSED
        assert not driver.guardian.armed
        with pytest.raises(RuntimeError, match="explicit resume"):
            controller.retry_pending_qualification()

        resumed = controller.resume()
        assert resumed.state is ControllerState.RECONCILE
        assert journal.read()["resumeState"] == ControllerState.QUALIFY_PENDING.value
        assert driver.guardian.armed
        reconciled = controller.reconcile(identity_ok=True)
        assert reconciled.state is ControllerState.QUALIFY_PENDING
        assert driver.guardian.armed
        retried = controller.retry_pending_qualification()
        assert retried.state is ControllerState.REOPEN
        assert driver.guardian.armed
        assert len(journal.archives("runs")) == 1
        assert journal.read()["qualification"]["totalAccepted"] == 1
    finally:
        driver.close()


def test_rejected_public_qualification_failure_preserves_staged_state_and_retries_once(
    tmp_path: Path, monkeypatch
) -> None:
    journal = _journal(tmp_path / "rejected-outcome.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    evidence = _evidence("uid-rejected-public", 100)
    _complete_cycle(controller, evidence, 1)
    invalid = replace(evidence, item_evidence={})

    original_replace = os.replace
    failed = False

    def fail_first_pending_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal failed
        if not failed and Path(destination).parent.name == ".pending":
            failed = True
            raise OSError("first rejected pending marker write failed")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_pending_replace)
    with pytest.raises(JournalError):
        controller.qualify(invalid, started_at=100)
    monkeypatch.undo()

    assert controller.status().state is ControllerState.QUALIFY_PENDING
    assert journal.read()["qualification"]["totalAccepted"] == 0
    controller.block("rejected publication failed")
    assert journal.read()["state"] == ControllerState.QUALIFY_PENDING.value
    retried = controller.retry_pending_qualification()
    assert retried.state is ControllerState.REOPEN
    assert journal.read()["qualification"]["totalAccepted"] == 0
    assert journal.archives("runs")[0]["qualification"] == "rejected"
    assert len(journal.archives("runs")) == 1
    with pytest.raises(RuntimeError, match="cannot retry qualification"):
        controller.retry_pending_qualification()


def test_cadence_rejection_public_failure_preserves_staged_state_and_retries_once(
    tmp_path: Path, monkeypatch
) -> None:
    journal = _journal(tmp_path / "cadence-outcome.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_Driver(), recover_on_restart=False)
    controller.begin_scan(started_at=100)
    first = _evidence("uid-cadence-first", 100)
    _complete_cycle(controller, first, 1)
    assert controller.qualify(first, started_at=100).state is ControllerState.REOPEN

    controller.reopen_complete()
    controller.begin_scan(started_at=2_000)
    second = _evidence("uid-cadence-second", 2_000)
    _complete_cycle(controller, second, 2)

    original_replace = os.replace
    failed = False

    def fail_first_pending_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal failed
        if not failed and Path(destination).parent.name == ".pending":
            failed = True
            raise OSError("first cadence pending marker write failed")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_pending_replace)
    with pytest.raises(JournalError):
        controller.qualify(second, started_at=2_000)
    monkeypatch.undo()

    assert controller.status().state is ControllerState.QUALIFY_PENDING
    assert journal.read()["qualification"]["totalAccepted"] == 1
    controller.block("cadence publication failed")
    assert journal.read()["state"] == ControllerState.QUALIFY_PENDING.value
    retried = controller.retry_pending_qualification()
    assert retried.state is ControllerState.REOPEN
    assert journal.read()["qualification"]["totalAccepted"] == 1
    assert len(journal.archives("runs")) == 2
    assert journal.archives("runs")[-1]["qualification"] == "rejected"
    assert journal.archives("cadence")[-1]["reason"] == "cadence interval failed"


@pytest.mark.parametrize("bad_field", ["companion", "data", "loaded", "evidence"])
def test_recovered_ack_uid_mismatch_stays_unresolved(tmp_path: Path, bad_field: str) -> None:
    journal = _journal(tmp_path / f"mismatch-{bad_field}.json", state=ControllerState.WAIT_DUE.value)
    evidence = _evidence(f"uid-{bad_field}", 100)
    _interrupt_ack_reload(journal, evidence)
    controller = Controller(journal, driver=_Driver())
    controller.resume()
    fence = controller.observation_fence()
    wrong = f"wrong-{bad_field}"
    resolved = controller.reconcile_restart(
        observed_boot="ack-boot-new",
        observed_uid=evidence.uid,
        uploaded_ids={wrong if bad_field == "companion" else evidence.uid},
        data_ack_ids={wrong if bad_field == "data" else evidence.uid},
        loaded_ids={wrong if bad_field == "loaded" else evidence.uid},
        pending_ids=set(),
        loaded_uid=wrong if bad_field == "loaded" else evidence.uid,
        loaded_at=105,
        loaded_boot="ack-boot-new",
        loaded_sequence=3,
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={wrong if bad_field == "evidence" else evidence.uid},
        stable_sweep_ids=set(),
        stable_scan_uid=wrong if bad_field == "evidence" else evidence.uid,
        stable_scan_evidence=evidence,
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        stable_upload_timestamp=103,
        stable_data_ack_uid=wrong if bad_field == "data" else evidence.uid,
        stable_companion_uploaded_uid=wrong if bad_field == "companion" else evidence.uid,
        expected_generation=fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=fence,
    )
    assert resolved.state is ControllerState.RECONCILE
    assert journal.read()["qualification"]["totalAccepted"] == 0
    assert not journal.archives("runs")


def test_bundle_replay_uses_chronological_commit_sequence(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "chronological.json")
    first = {
        kind: {"outcomeId": "older", "value": 1}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    second = {
        kind: {"outcomeId": "newer", "value": 2}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    older_checkpoint = copy.deepcopy(DEFAULT_STATE)
    older_checkpoint["state"] = ControllerState.REOPEN.value
    newer_checkpoint = copy.deepcopy(DEFAULT_STATE)
    newer_checkpoint["state"] = ControllerState.WAIT_DUE.value
    journal.append_archive_bundle(first, checkpoint=older_checkpoint)
    journal.append_archive_bundle(second, checkpoint=newer_checkpoint)
    manifests = list((tmp_path / ".chronological.json.archives" / ".bundles").glob("*.json"))
    assert len(manifests) == 2
    # Replace the live checkpoint with an older one; startup must reconstruct
    # the newest committed state from manifests, independent of hash order.
    stale = copy.deepcopy(older_checkpoint)
    stale["commitSequence"] = 0
    (tmp_path / "chronological.json").write_text(json.dumps(stale), encoding="utf-8")
    recovered = Journal(tmp_path / "chronological.json")
    recovered.replay_archive_bundles()
    assert recovered.read()["state"] == ControllerState.WAIT_DUE.value
    assert recovered.read()["commitSequence"] == 2
    assert [item["outcomeId"] for item in recovered.archives("runs")] == ["older", "newer"]


def test_equal_commit_replay_preserves_later_pause_and_repairs_counters(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "pause-after-outcome.json")
    records = {
        kind: {"outcomeId": "pause-after-outcome", "value": 1}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    checkpoint = copy.deepcopy(DEFAULT_STATE)
    checkpoint["state"] = ControllerState.REOPEN.value
    journal.append_archive_bundle(records, checkpoint=checkpoint)

    # A later operator pause uses the same committed outcome sequence.  Write
    # a stale counter directly to model a crash after the pause checkpoint but
    # before a counter repair; replay must preserve the pause on equal seq.
    live = journal.read()
    live["state"] = ControllerState.PAUSED.value
    live["blocker"] = "operator takeover"
    live["archives"]["runs"]["count"] = 0
    live["archives"]["runs"]["lastOrdinal"] = 0
    journal.path.write_text(json.dumps(live), encoding="utf-8")

    recovered = Journal(tmp_path / "pause-after-outcome.json")
    assert recovered.replay_archive_bundles() == 1
    state = recovered.read()
    assert state["state"] == ControllerState.PAUSED.value
    assert state["blocker"] == "operator takeover"
    assert state["commitSequence"] == 1
    assert state["archives"]["runs"]["count"] == 1


def test_first_manifest_failure_leaves_pending_bundle_for_fresh_owner(tmp_path: Path, monkeypatch) -> None:
    journal = _journal(tmp_path / "pending.json")
    records = {
        kind: {"outcomeId": "pending-outcome", "value": 1}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    original = journal._write_json_artifact
    failed = False

    def fail_manifest(destination: Path, value: dict, directory: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise JournalError("first manifest write failed")
        original(destination, value, directory)

    monkeypatch.setattr(journal, "_write_json_artifact", fail_manifest)
    with pytest.raises(JournalError):
        journal.append_archive_bundle(records, checkpoint=copy.deepcopy(DEFAULT_STATE))
    assert list((tmp_path / ".pending.json.archives" / ".pending").glob("*.json"))
    monkeypatch.undo()
    recovered = Journal(tmp_path / "pending.json")
    assert recovered.replay_archive_bundles() == 1
    assert len(recovered.archives("runs")) == 1
    assert len(recovered.archives("acknowledgements")) == 1


@pytest.mark.parametrize("failure_point", ["manifest", "artifact", "checkpoint"])
def test_every_post_marker_write_failure_has_a_fresh_owner_retry_path(
    tmp_path: Path, monkeypatch, failure_point: str
) -> None:
    journal = _journal(tmp_path / f"failure-{failure_point}.json")
    records = {
        kind: {"outcomeId": f"failure-{failure_point}", "value": 1}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    if failure_point == "manifest":
        original = journal._write_json_artifact
        failed = False

        def fail_manifest(destination: Path, value: dict, directory: Path) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise JournalError("manifest write failed")
            original(destination, value, directory)

        monkeypatch.setattr(journal, "_write_json_artifact", fail_manifest)
    elif failure_point == "artifact":
        original = journal._write_archive_artifact
        failed = False

        def fail_artifact(kind: str, ordinal: int, record: dict) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise JournalError("archive artifact write failed")
            original(kind, ordinal, record)

        monkeypatch.setattr(journal, "_write_archive_artifact", fail_artifact)
    else:
        original = journal.write
        failed = False

        def fail_checkpoint(value: dict) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise JournalError("checkpoint write failed")
            original(value)

        monkeypatch.setattr(journal, "write", fail_checkpoint)

    with pytest.raises(JournalError):
        journal.append_archive_bundle(records, checkpoint=copy.deepcopy(DEFAULT_STATE))
    monkeypatch.undo()
    recovered = Journal(tmp_path / f"failure-{failure_point}.json")
    assert recovered.replay_archive_bundles() == 1
    assert len(recovered.archives("runs")) == 1
    assert recovered.read()["archives"]["runs"]["count"] == 1
    assert recovered.replay_archive_bundles() == 1
    assert recovered.read()["archives"]["runs"]["count"] == 1


def test_conflicting_retry_payload_is_rejected_without_mutating_archive(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "conflict.json")
    records = {
        kind: {"outcomeId": "conflicting", "value": 1}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    journal.append_archive_bundle(records, checkpoint=copy.deepcopy(DEFAULT_STATE))
    conflict = {
        kind: {"outcomeId": "conflicting", "value": 2}
        for kind in ("runs", "evidence", "cadence", "acknowledgements")
    }
    with pytest.raises(JournalError):
        journal.append_archive_bundle(conflict, checkpoint=copy.deepcopy(DEFAULT_STATE))
    assert journal.archives("runs")[0]["value"] == 1
    assert journal.read()["archives"]["runs"]["count"] == 1
