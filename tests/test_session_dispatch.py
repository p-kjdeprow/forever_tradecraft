"""Dispatch and admission boundary tests for native session actions."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session.calibration import OwnedGtkCalibrationWindow, OwnedWindow, XTestInputProfile
from crowley.session.driver import (
    DesktopDriver,
    DriverError,
    DriverProfile,
    MeasuredAnchor,
    RecordingBackend,
    WindowIdentity,
    X11Backend,
    XI2Observer,
)
from crowley.session.executor import PointerButton
from crowley.session.guardian import GuardianError, InputGuardian
from crowley.session.input_events import (
    RAW_MOTION_DETAIL,
    RAW_MOTION_PENDING_LIMIT,
    XI2_TIMESTAMP_MASK,
    EventClass,
    XI2Event,
)
from tests.test_session_input_tool import fixture_input_tool


@pytest.fixture(autouse=True)
def _private_pointer_click_rate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real click admission tests away from host-global rate state."""

    state_directory = tmp_path / "pointer-click-rate"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr("crowley.gui.click_gate._session_state_directory", lambda: state_directory)


def test_keyboard_profile_exactly_covers_runtime_actions_and_commands(tmp_path: Path) -> None:
    class KeymapBackend(RecordingBackend):
        def observe_keymap(self) -> dict[str, int]:
            return {
                "Return": 36,
                "Escape": 9,
                "Control_L": 37,
                "F12": 95,
                "Shift_L": 50,
                "apostrophe": 48,
                "comma": 54,
                "equal": 21,
                "minus": 20,
                "semicolon": 47,
                "0": 19,
                "1": 10,
                "2": 11,
                "9": 18,
                "slash": 47,
                "space": 65,
                **{character: index for index, character in enumerate("abcdefghijklmnopqrstuvwxyz", 10)},
            }

    driver = DesktopDriver(backend=KeymapBackend(), lease_path=tmp_path / "profile.lock")
    try:
        operations = driver.calibration_keyboard_operations()
        by_name = {(operation.operation, operation.payload): operation for operation in operations}
        runtime_vocabulary = {
            ("open_chat", ""),
            ("submit_command", ""),
            ("escape_command_buffer", ""),
            ("interact_target", ""),
            *(("type_allowlisted_command", command) for command in driver.COMMANDS),
        }
        assert set(by_name) == runtime_vocabulary
        assert len(operations) == len(runtime_vocabulary)
        assert not any("select_chat_all" in operation.operation for operation in operations)
        assert "select_chat_all" not in driver.ACTIONS
        chord = by_name[("interact_target", "")]
        assert tuple((step.phase, step.keysym) for step in chord.steps) == (
            ("press", "Control_L"),
            ("press", "F12"),
            ("release", "F12"),
            ("release", "Control_L"),
        )
        conditional = by_name[("type_allowlisted_command", driver.CONDITIONAL_INTERACTION_BINDING_COMMAND)]
        assert any(step.keysym == "comma" for step in conditional.steps)
        target = by_name[("type_allowlisted_command", driver.TARGET_AUCTIONEER_COMMAND)]
        assert (
            ("press", "Shift_L"),
            ("press", "a"),
            ("release", "a"),
            ("release", "Shift_L"),
        ) in tuple(
            tuple((step.phase, step.keysym) for step in target.steps[index : index + 4])
            for index in range(len(target.steps) - 3)
        )
        assert ("type_allowlisted_command", "/reload") in by_name
        fence = by_name[("type_allowlisted_command", "/crowley session fence")]
        assert all(step.keysym not in {"Shift_L", "Shift_R"} for step in fence.steps)
        assert not any("agent" in operation.payload for operation in operations)

        # Mirror a sealed profile made from this exact production vocabulary,
        # then ensure every operation has a guarded runtime expectation.
        keymap = KeymapBackend().observe_keymap()
        profile = tuple(
            (
                operation.operation,
                operation.payload,
                tuple(
                    (
                        step.phase,
                        step.keysym,
                        keymap[step.keysym.rsplit("+", 1)[-1]],
                        (0, 0, 0),
                        0,
                    )
                    for step in operation.steps
                ),
            )
            for operation in operations
        )
        evidence = SimpleNamespace(
            keyboard_profile=profile,
            keyboard_keymap=tuple(sorted(keymap.items())),
        )
        for operation in operations:
            kwargs = (
                {"command": operation.payload} if operation.operation == "type_allowlisted_command" else {}
            )
            action = "open_ah" if operation.operation == "interact_target" else operation.operation
            expected = driver._expected_key_operation(action, kwargs, evidence)
            expected_classes = tuple(
                event_class
                for phase, _keysym, _detail, _modifiers, _effective in next(
                    row[2] for row in profile if row[:2] == (operation.operation, operation.payload)
                )
                for event_class in (
                    ("RawKeyPress", "KeyPress") if phase == "press" else ("RawKeyRelease", "KeyRelease")
                )
            )
            expected_details = tuple(
                detail
                for _phase, _keysym, detail, _modifiers, _effective in next(
                    row[2] for row in profile if row[:2] == (operation.operation, operation.payload)
                )
                for _ in range(2)
            )
            assert expected == (
                expected_classes,
                expected_details,
                tuple((0, 0, 0) for _ in operation.steps),
            )
    finally:
        driver.close()


def test_runtime_evidence_keyboard_dispatch_guards_shifted_text_punctuation_and_ctrl_f12(
    tmp_path: Path,
) -> None:
    """The passive-runtime profile and Guardian agree on physical key transitions."""

    from crowley.session.startup import StartupEnvironment, mint_runtime_evidence

    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        "World of Warcraft",
        "wowb.exe",
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "runtime-keyboard-test-start",
        ":dispatch",
        "runtime-keyboard-test-identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    hierarchy = (
        (2, 3, "master", "pointer:Virtual core pointer"),
        (3, 2, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (6, 2, "slave", "pointer:xwayland-pointer:17"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
        (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
        (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
    )
    keymap = {
        "Control_L": 37,
        "F12": 96,
        "Shift_L": 50,
        "Return": 36,
        "Escape": 9,
        "a": 38,
        "b": 56,
        "c": 54,
        "d": 40,
        "e": 26,
        "f": 41,
        "g": 42,
        "h": 43,
        "i": 31,
        "j": 44,
        "k": 45,
        "l": 46,
        "m": 58,
        "n": 57,
        "o": 32,
        "p": 33,
        "q": 24,
        "r": 27,
        "s": 39,
        "t": 28,
        "u": 30,
        "v": 55,
        "w": 25,
        "x": 53,
        "y": 29,
        "z": 52,
        "0": 19,
        "1": 10,
        "2": 11,
        "9": 18,
        "comma": 59,
        "apostrophe": 48,
        "equal": 21,
        "minus": 20,
        "semicolon": 47,
        "slash": 61,
        "space": 65,
    }
    levels_by_code: dict[int, list[str]] = {}
    for symbol, code in keymap.items():
        levels_by_code.setdefault(code, []).append(symbol)
        if len(symbol) == 1 and symbol.isascii() and symbol.isalpha() and symbol.islower():
            levels_by_code[code].append(symbol.upper())
    levels = {code: tuple(sorted(set(symbols))) for code, symbols in levels_by_code.items()}
    profile = XTestInputProfile(
        4,
        2,
        "pointer:Virtual core XTEST pointer",
        3,
        ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
        1,
        1,
        (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        0x374,
        (0, 0, 1920, 1080),
        hierarchy,
    )
    backend = X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "private-Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "runtime-keyboard-tool"),
    )
    driver = DesktopDriver(
        profile=backend.profile,
        backend=backend,
        lease_path=tmp_path / "runtime-keyboard.lock",
    )
    keyboard_profile = DesktopDriver._runtime_keyboard_profile(
        tuple(driver.COMMANDS), levels, keymap, (0x10, 0, 0), (1, 4)
    )
    environment = StartupEnvironment(
        display=driver.profile.display,
        hierarchy=hierarchy,
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=profile,
        keymap_levels=tuple(sorted(levels.items())),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version=backend.input_tool.version,
        input_tool_sha256=backend.input_tool.digest,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-dispatch",
        readiness_sequence=77,
        environment=environment,
        identity=identity,
        keyboard_keymap=tuple(sorted(keymap.items())),
        native_modifiers=(0x10, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=keyboard_profile,
    )
    backend.resolve_identity = lambda require_foreground=True, **_kwargs: identity  # type: ignore[method-assign]
    backend._is_owned_client_target = lambda xid, _identity: xid in identity.target_event_xids  # type: ignore[method-assign]
    backend._check_published_tool = lambda **_kwargs: None  # type: ignore[method-assign]
    backend.activate_generation = lambda _generation: None  # type: ignore[method-assign]
    backend.observe_keymap = lambda **_kwargs: keymap  # type: ignore[method-assign]
    backend.observe_keymap_levels = lambda **_kwargs: levels  # type: ignore[method-assign]
    backend.observe_native_input_state = lambda _root: ((0x10, 0, 0), (), ())  # type: ignore[method-assign]
    driver.input_hierarchy = lambda **_kwargs: hierarchy  # type: ignore[method-assign]
    driver.xtest_input_profile = lambda _hierarchy=None, **_kwargs: profile  # type: ignore[method-assign]

    class RuntimeMonitor:
        alive = True
        failure_diagnostic = None

        def __init__(self) -> None:
            self._events: list[XI2Event] = []

        def events(self) -> list[XI2Event]:
            return list(self._events)

        def push(self, event: XI2Event) -> None:
            self._events.append(event)
            driver._external_input(event)

        def wait_for_event(self, predicate, *, timeout: float) -> None:
            del timeout
            assert predicate(self.events()), "the runtime Guardian did not complete the emitted key sequence"

        def stop(self) -> str:
            self.alive = False
            return ""

    monitor = RuntimeMonitor()
    driver._monitor = monitor  # type: ignore[assignment]
    driver._monitor_calibration = False
    generation = driver.arm(evidence)
    emitted_sequences: list[tuple[tuple[str, str, int], ...]] = []
    modifier_masks = {"Shift_L": 1, "Control_L": 4}
    next_time = 1

    def emit_physical_sequence(name: str, **kwargs: object) -> None:
        nonlocal next_time
        if name == "type_allowlisted_command":
            command = str(kwargs["command"])
            physical_steps: list[tuple[str, str]] = []
            for char in command:
                mapped = X11Backend._keysym_for_char(char)
                assert mapped is not None
                parts = mapped.split("+")
                if len(parts) == 1:
                    physical_steps.extend((("press", parts[0]), ("release", parts[0])))
                else:
                    assert parts[0] == "Shift_L"
                    physical_steps.extend(
                        (
                            ("press", parts[0]),
                            ("press", parts[1]),
                            ("release", parts[1]),
                            ("release", parts[0]),
                        )
                    )
        elif name == "open_ah":
            physical_steps = [
                ("press", "Control_L"),
                ("press", "F12"),
                ("release", "F12"),
                ("release", "Control_L"),
            ]
        else:
            raise AssertionError(f"unexpected keyboard action {name}")
        measured_keymap = kwargs["_keyboard_keymap"]
        assert isinstance(measured_keymap, dict)
        transitions = tuple((phase, keysym, measured_keymap[keysym]) for phase, keysym in physical_steps)
        emitted_sequences.append(transitions)
        locked, latched, base = evidence.native_modifiers
        for phase, keysym, detail in transitions:
            raw_class = EventClass.RAW_KEY_PRESS if phase == "press" else EventClass.RAW_KEY_RELEASE
            delivered_class = EventClass.KEY_PRESS if phase == "press" else EventClass.KEY_RELEASE
            event_type = 13 if phase == "press" else 14
            monitor.push(
                XI2Event(
                    type_number=event_type,
                    event_class=raw_class,
                    master_device=3,
                    source_device=5,
                    timestamp_ms=next_time,
                    detail=detail,
                    root_xid=None,
                    event_xid=None,
                    child_xid=None,
                )
            )
            current = (locked, latched, base)
            monitor.push(
                XI2Event(
                    type_number=2 if phase == "press" else 3,
                    event_class=delivered_class,
                    master_device=5,
                    source_device=5,
                    timestamp_ms=next_time,
                    detail=detail,
                    root_xid=0x374,
                    event_xid=identity.child_xid,
                    child_xid=identity.child_xid,
                    root_xy=(30.0, 40.0),
                    event_xy=(30.0, 40.0),
                    modifiers=current,
                    modifier_effective=locked | latched | base,
                )
            )
            next_time += 1
            if keysym in modifier_masks:
                bit = modifier_masks[keysym]
                base = base | bit if phase == "press" else base & ~bit

    backend.action = emit_physical_sequence  # type: ignore[method-assign]
    try:
        driver.run(
            "type_allowlisted_command",
            generation,
            command=driver.TARGET_AUCTIONEER_COMMAND,
            precondition=lambda _identity: True,
        )
        driver.run(
            "type_allowlisted_command",
            generation,
            command=driver.CONDITIONAL_INTERACTION_BINDING_COMMAND,
            precondition=lambda _identity: True,
        )
        driver.run(
            "open_ah",
            generation,
            precondition=lambda _identity: True,
            postcondition=lambda _identity: True,
        )
        assert driver.guardian.armed
        assert not driver.guardian.held
        assert not driver._held_input
        assert len(emitted_sequences) == 3
        assert any(step[:2] == ("press", "Shift_L") for step in emitted_sequences[0])
        assert any(step[:2] == ("press", "Shift_L") for step in emitted_sequences[1])
        assert any(step[:2] == ("press", "comma") for step in emitted_sequences[1])
        assert emitted_sequences[2] == (
            ("press", "Control_L", 37),
            ("press", "F12", 96),
            ("release", "F12", 96),
            ("release", "Control_L", 37),
        )
        assert len(monitor.events()) == sum(len(sequence) for sequence in emitted_sequences) * 2
    finally:
        driver.close()


def test_guardian_pins_modifier_state_for_each_keyboard_transition(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "keyboard-modifiers.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    generation = guardian.arm(Evidence())
    action = guardian.begin_action(
        generation,
        target_xid=0x600119,
        source_device=5,
        master_device=3,
        event_classes={"RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"},
        required_sequence=("RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"),
        expected_details=(37, 37, 38, 38),
        expected_modifier_sequence=((0, 0, 0), (0x10, 0, 0)),
        modifier_group_size=2,
    )

    def event(
        event_class: str,
        detail: int,
        timestamp: int,
        *,
        raw: bool,
        modifiers: tuple[int, int, int] | None,
        effective: int | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            event_class=event_class,
            source_device=5,
            master_device=3,
            target_xid=None if raw else 0x600119,
            child_xid=None if raw else 0x600119,
            detail=detail,
            timestamp_ms=timestamp,
            modifiers=modifiers,
            modifier_effective=effective,
            is_raw=raw,
        )

    try:
        assert guardian.observe(event("RawKeyPress", 37, 1, raw=True, modifiers=None, effective=None))
        assert guardian.observe(event("KeyPress", 37, 2, raw=False, modifiers=(0, 0, 0), effective=0))
        assert guardian.observe(event("RawKeyRelease", 38, 3, raw=True, modifiers=None, effective=None))
        assert guardian.observe(event("KeyRelease", 38, 4, raw=False, modifiers=(0x10, 0, 0), effective=0x10))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()


def _x11(tmp_path: Path) -> X11Backend:
    return X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )


def test_xi2_observer_rejects_stalled_wrapper_and_wrong_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import crowley.session.driver as driver_module

    observer = XI2Observer(display=":dispatch", xauthority=str(tmp_path / "Xauthority"))
    monkeypatch.setattr(driver_module, "_resolve_wrapped_executable", lambda _command: "/bin/true")
    monkeypatch.setattr(driver_module, "_EXEC_ADMISSION_WRAPPER", "import time; time.sleep(30)")
    observer.start()
    try:
        with pytest.raises(DriverError, match="wrapper"):
            observer.verify_exec_identity()
    finally:
        observer.stop()

    wrong = XI2Observer(display=":dispatch", xauthority=str(tmp_path / "Xauthority"))
    expected = os.path.realpath("/bin/true")
    running = os.path.realpath("/bin/sleep")
    assert expected != running
    monkeypatch.setattr(driver_module, "_resolve_wrapped_executable", lambda _command: expected)
    monkeypatch.setattr(
        driver_module,
        "_EXEC_ADMISSION_WRAPPER",
        "import os; os.execv('/bin/sleep', ['/bin/sleep', '30'])",
    )
    wrong.start()
    try:
        with pytest.raises(DriverError, match="wrapper"):
            wrong.verify_exec_identity()
        assert wrong.process is not None and wrong.process.poll() is None
    finally:
        wrong.stop()
    assert wrong.process is None


def test_exec_wrapper_real_true_returns_after_registration(tmp_path: Path) -> None:
    backend = _x11(tmp_path)
    try:
        result = backend._run(["/bin/true"], timeout=2)
        assert result.returncode == 0
        assert backend.active_process_groups == frozenset()
        assert not backend._active_processes
    finally:
        backend.release_held_input()


def test_exec_wrapper_real_true_uses_current_guardian_generation(tmp_path: Path) -> None:
    backend = _x11(tmp_path)

    class Evidence:
        is_live = True
        fingerprint = "owned"

    guardian = InputGuardian(
        tmp_path / "current-generation.lock",
        parent_pid=os.getppid(),
        release_input=backend.release_held_input,
    )
    registered: list[tuple[int | None, int]] = []

    def register(generation: int | None, pgid: int) -> None:
        registered.append((generation, pgid))
        guardian.register_process_group(pgid, generation)

    backend.set_admission(guardian.dispatch_admission)
    backend.set_process_group_callbacks(register, guardian.unregister_process_group)
    try:
        generation = guardian.arm(Evidence())
        backend.activate_generation(generation)
        result = backend._run(["/bin/true"], generation=generation, timeout=2)
        assert result.returncode == 0
        assert registered and registered[0][0] == generation
        assert backend.active_process_groups == frozenset()
        assert guardian.active_process_groups == frozenset()
    finally:
        guardian.close()
        backend.release_held_input()


def test_exec_wrapper_real_process_is_killed_and_not_orphaned(tmp_path: Path) -> None:
    backend = _x11(tmp_path)
    result: dict[str, BaseException] = {}

    def run() -> None:
        try:
            backend._run(["/bin/sleep", "20"], timeout=5)
        except BaseException as exc:  # the test asserts the bounded cancellation result
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.005)
        groups = backend.active_process_groups
        assert len(groups) == 1
        group = next(iter(groups))
        backend.release_held_input()
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), DriverError)
        assert backend.active_process_groups == frozenset()
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)
    finally:
        backend.release_held_input()
        thread.join(2)


def test_mutating_backend_calls_require_explicit_capability(tmp_path: Path) -> None:
    backend = _x11(tmp_path)
    with pytest.raises(DriverError, match="capability"):
        backend._run(["xdotool", "key", "F13"], timeout=2)
    with pytest.raises(DriverError, match="capability"):
        backend._run(["xdotool", "type", "unsafe"], timeout=2)
    with pytest.raises(DriverError, match="generation"):
        backend.action("focus_game")


@pytest.mark.parametrize(
    "argv",
    [
        ["xdotool", "click", "1"],
        ["xdotool", "mousedown", "1"],
        ["xdotool", "mouseup", "1"],
        ["xdotool", "mousemove_relative", "1", "1"],
        ["xdotool", "exec", "true"],
        ["xdotool", "mousemove", "10", "10"],
    ],
)
def test_removed_raw_input_argv_forms_are_refused_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    backend = _x11(tmp_path)
    spawned = False

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("removed input argv form reached subprocess spawning")

    monkeypatch.setattr("crowley.session.driver.subprocess.Popen", unexpected_spawn)
    with pytest.raises(DriverError, match="not allowlisted|broker-admitted"):
        backend._run(argv, generation=1, held_buttons={1})
    assert not spawned


def test_schema_qualified_single_step_pointer_motion_is_refused_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _x11(tmp_path)
    spawned = False

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("single-step pointer motion reached subprocess spawning")

    monkeypatch.setattr("crowley.session.driver.subprocess.Popen", unexpected_spawn)
    with pytest.raises(DriverError, match="not allowlisted"):
        backend._run(
            ["xdotool", "mousemove", "10", "10"],
            generation=1,
            input_schema="pointer-motion",
            expected_argv=("xdotool", "mousemove", "10", "10"),
        )
    assert not spawned


@pytest.mark.parametrize(
    ("argv", "input_schema", "held_buttons"),
    [
        (
            [
                "xdotool",
                "sleep",
                "0.010",
                "mousemove",
                "10",
                "10",
                "sleep",
                "0.010",
                "mousemove",
                "10",
                "10",
            ],
            "pointer-motion",
            set(),
        ),
        (
            [
                "xdotool",
                "mousedown",
                "3",
                "sleep",
                "0.050",
                "mousemove",
                "20",
                "20",
                "sleep",
                "0.020",
                "mouseup",
                "3",
            ],
            "camera-drag",
            {3},
        ),
    ],
    ids=("repeated-pointer-destination", "one-step-camera-drag"),
)
def test_human_motion_requires_two_distinct_destinations_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    input_schema: str,
    held_buttons: set[int],
) -> None:
    backend = _x11(tmp_path)
    spawned = False

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("insufficient human motion reached subprocess spawning")

    monkeypatch.setattr("crowley.session.driver.subprocess.Popen", unexpected_spawn)
    with pytest.raises(DriverError, match="not allowlisted"):
        backend._run(
            argv,
            generation=1,
            input_schema=input_schema,
            expected_argv=tuple(argv),
            held_buttons=held_buttons,
        )
    assert not spawned


def test_real_admission_publishes_only_current_holds(tmp_path: Path) -> None:
    backend = _x11(tmp_path)

    class Evidence:
        is_live = True
        fingerprint = "owned"

    guardian = InputGuardian(
        tmp_path / "holds.lock",
        parent_pid=os.getppid(),
        release_input=backend.release_held_input,
    )
    published: list[tuple[set[str], set[int]]] = []
    backend.set_admission(guardian.dispatch_admission)
    backend.set_hold_publisher(lambda _authority, keys, buttons: published.append((keys, buttons)))
    backend.set_process_group_callbacks(
        lambda generation, pgid: guardian.register_process_group(pgid, generation),
        guardian.unregister_process_group,
    )
    try:
        generation = guardian.arm(Evidence())
        backend.activate_generation(generation)
        result = backend._run(
            ["/bin/true"],
            generation=generation,
            held_keys={"ctrl"},
            held_buttons={1},
            timeout=2,
        )
        assert result.returncode == 0
        assert published[0] == ({"ctrl"}, {1})
        assert published[-1] == (set(), set())
        assert guardian.held is False
    finally:
        guardian.close()
        backend.release_held_input()


def test_cancelled_real_action_retains_current_hold_for_release(tmp_path: Path) -> None:
    backend = _x11(tmp_path)

    class Evidence:
        is_live = True
        fingerprint = "owned"

    guardian = InputGuardian(
        tmp_path / "held-cancel.lock",
        parent_pid=os.getppid(),
        release_input=backend.release_held_input,
    )
    backend._release_command = lambda _args: True
    backend.set_admission(guardian.dispatch_admission)
    backend.set_hold_publisher(
        lambda authority, keys, buttons: guardian.publish_current_holds(authority, keys, buttons)
    )
    backend.set_process_group_callbacks(
        lambda generation, pgid: guardian.register_process_group(pgid, generation),
        guardian.unregister_process_group,
    )
    result: dict[str, BaseException] = {}
    try:
        generation = guardian.arm(Evidence())
        backend.activate_generation(generation)
        guardian.begin_action(
            generation,
            target_xid=0x101,
            source_device=4,
            master_device=2,
            event_classes=set(),
            held_keys={"ctrl"},
        )

        def run() -> None:
            try:
                backend._run(
                    ["/bin/sleep", "20"],
                    generation=generation,
                    held_keys={"ctrl"},
                    timeout=5,
                )
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 2
        while not backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.005)
        assert backend.active_process_groups
        guardian.revoke("cancel held action")
        thread.join(2)
        assert isinstance(result.get("error"), DriverError)
        assert not guardian.held
        assert not backend._held_keys
        assert not backend.active_process_groups
    finally:
        guardian.close()
        backend.release_held_input()


def test_measured_anchor_rejects_deleted_retained_capture(tmp_path: Path) -> None:
    capture = tmp_path / "anchor.png"
    capture.write_bytes(b"retained-capture")
    capture_digest = hashlib.sha256(capture.read_bytes()).hexdigest()
    anchor = MeasuredAnchor.from_capture(
        action="open_ah",
        x=100,
        y=100,
        identity_fingerprint="identity",
        target_xid=0x600119,
        capture_id=capture_digest,
        capture_sequence=1,
        source="native-visual",
        observed_monotonic=1.0,
        capture_monotonic=1.0,
        scene="world",
        bounds=(80, 48, 256, 256),
        target_ancestry=(0x600119, 0x600118),
        capture_path=str(capture.resolve()),
        capture_sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
        generation=1,
        facts=(
            ("action", "open_ah"),
            ("bounds", "80,48,256,256"),
            ("capture", capture_digest),
            ("capturePath", str(capture.resolve())),
            ("captureSha256", hashlib.sha256(capture.read_bytes()).hexdigest()),
            ("control", "auctioneer_buckler"),
            ("generation", "1"),
            ("scene", "world"),
            ("target", str(0x600119)),
            ("targetAncestry", "6291705,6291736"),
        ),
    )
    anchor.verify_seal()
    capture.unlink()
    with pytest.raises(DriverError, match="capture"):
        anchor.verify_seal()


def test_old_generation_cannot_revive_when_new_generation_is_armed(tmp_path: Path) -> None:
    backend = _x11(tmp_path)

    class Evidence:
        is_live = True
        fingerprint = "owned"

    guardian = InputGuardian(
        tmp_path / "generation.lock",
        parent_pid=os.getppid(),
        release_input=backend.release_held_input,
    )
    backend.set_admission(guardian.dispatch_admission)
    backend.set_process_group_callbacks(
        lambda generation, pgid: guardian.register_process_group(pgid, generation),
        guardian.unregister_process_group,
    )
    result: dict[str, BaseException] = {}
    try:
        first = guardian.arm(Evidence())
        backend.activate_generation(first)

        def run_old() -> None:
            try:
                backend._run(["/bin/sleep", "20"], generation=first, timeout=5)
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=run_old)
        thread.start()
        deadline = time.monotonic() + 2
        while not backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.005)
        assert backend.active_process_groups
        guardian.revoke("human takeover")
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), DriverError)
        assert backend.active_process_groups == frozenset()
        assert guardian.active_process_groups == frozenset()

        # Re-arm only after the first action's physical process group has
        # actually disappeared.  Then issue a real late request with the old
        # generation; it must be rejected by the new lease fence before any
        # process is spawned.
        second = guardian.arm(Evidence())
        backend.activate_generation(second)
        late_result: dict[str, BaseException] = {}

        def run_late_old() -> None:
            try:
                backend._run(["/bin/sleep", "20"], generation=first, timeout=5)
            except BaseException as exc:
                late_result["error"] = exc

        late_thread = threading.Thread(target=run_late_old)
        late_thread.start()
        late_thread.join(2)
        assert not late_thread.is_alive()
        assert isinstance(late_result.get("error"), DriverError)
        assert backend.active_process_groups == frozenset()
    finally:
        try:
            guardian.close()
        finally:
            backend.release_held_input()


def test_real_old_calibration_ticket_cannot_spawn_after_revoke(tmp_path: Path) -> None:
    from crowley.session.driver import DesktopDriver

    backend = _x11(tmp_path)
    driver = DesktopDriver(
        profile=backend.profile,
        backend=backend,
        lease_path=tmp_path / "calibration-revoke.lock",
    )
    target = OwnedWindow(
        os.getpid(),
        0x600119,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({0x600119}),
        "/usr/bin/python3",
        "target-start",
        0x600118,
        (0, 0, 500, 260),
        frozenset({0x600118, 0x600119}),
    )
    result: dict[str, BaseException] = {}
    try:
        driver.begin_calibration_operation()
        ticket = driver._issue_calibration_ticket(target)

        def run_old_ticket() -> None:
            try:
                backend._run(["/bin/sleep", "20"], calibration_ticket=ticket, timeout=5)
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=run_old_ticket)
        thread.start()
        deadline = time.monotonic() + 2
        while not backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.005)
        groups = backend.active_process_groups
        assert len(groups) == 1
        driver.revoke("human takeover")
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), DriverError)
        assert backend.active_process_groups == frozenset()
        assert driver.guardian.active_process_groups == frozenset()
        for group in groups:
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)
    finally:
        driver.close()


def test_calibration_admission_rejects_epoch_changed_after_begin(tmp_path: Path) -> None:
    driver = DesktopDriver(
        profile=DriverProfile(display=":epoch-admission", xauthority=str(tmp_path / "Xauthority")),
        backend=RecordingBackend(),
        lease_path=tmp_path / "epoch-admission.lock",
    )
    release_calls: list[bool] = []
    try:
        driver.begin_calibration_operation()
        driver.guardian.cancel_calibration_operation()
        with pytest.raises(DriverError, match="epoch"):
            driver.admit_calibration_target_process(500_101, lambda: release_calls.append(True))
        assert not release_calls
        assert not driver.guardian.active_process_groups
    finally:
        driver.close()


def test_calibration_ticket_rejects_epoch_changed_after_admission(tmp_path: Path) -> None:
    driver = DesktopDriver(
        profile=DriverProfile(display=":epoch-ticket", xauthority=str(tmp_path / "Xauthority")),
        backend=RecordingBackend(),
        lease_path=tmp_path / "epoch-ticket.lock",
    )
    target = OwnedWindow(
        os.getpid(),
        0x600119,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({0x600119}),
        "/usr/bin/python3",
        "target-start",
        0x600118,
        (0, 0, 500, 260),
        frozenset({0x600118, 0x600119}),
    )
    try:
        driver.begin_calibration_operation()
        driver.admit_calibration_target_process(500_102, lambda: None)()
        driver.guardian.cancel_calibration_operation()
        with pytest.raises(DriverError, match="epoch"):
            driver.issue_calibration_ticket(target)
    finally:
        driver.close()


def test_calibration_ticket_runs_before_arm_and_is_burned_on_revoke(tmp_path: Path) -> None:
    backend = RecordingBackend()
    from crowley.session.driver import DesktopDriver

    driver = DesktopDriver(
        profile=DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        backend=backend,
        lease_path=tmp_path / "lease.lock",
    )
    target = OwnedWindow(
        os.getpid(),
        0x600119,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({0x600119}),
        "/usr/bin/python3",
        "target-start",
        0x600118,
        (0, 0, 500, 260),
        frozenset({0x600118, 0x600119}),
    )
    try:
        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.1, calibration_ticket=ticket)
        timer = next(iter(driver._calibration_timers.values()))
        timer.cancel()
        timer.function(*timer.args)
        assert any(event["name"] == "calibration_focus" for event in backend.events)

        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.1, calibration_ticket=ticket)
        stale_timer = next(iter(driver._calibration_timers.values()))
        driver.revoke("human takeover")
        stale_timer.function(*stale_timer.args)
        assert driver.calibration_executions == 0
        assert driver.calibration_rejected_count >= 1
        assert [event for event in backend.events if event["name"] == "calibration_focus"]
    finally:
        driver.close()


def test_calibration_retry_gets_a_fresh_epoch(tmp_path: Path) -> None:
    backend = RecordingBackend()
    from crowley.session.driver import DesktopDriver

    driver = DesktopDriver(
        profile=DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        backend=backend,
        lease_path=tmp_path / "retry.lock",
    )
    target = OwnedWindow(
        os.getpid(),
        0x600119,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({0x600119}),
        "/usr/bin/python3",
        "target-start",
        0x600118,
        (0, 0, 500, 260),
        frozenset({0x600118, 0x600119}),
    )
    try:
        driver.begin_calibration_operation()
        old_ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.1, calibration_ticket=old_ticket)
        old_timer = next(iter(driver._calibration_timers.values()))
        driver.revoke("human takeover")
        driver.begin_calibration_operation()
        new_ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.1, calibration_ticket=new_ticket)
        new_timer = next(timer for timer in driver._calibration_timers.values() if timer is not old_timer)
        old_timer.function(*old_timer.args)
        new_timer.cancel()
        new_timer.function(*new_timer.args)
        assert driver.calibration_executions == 1
    finally:
        driver.close()


def test_calibration_ticket_binds_certified_managed_frame_targets(tmp_path: Path) -> None:
    backend = RecordingBackend()
    from crowley.session.driver import DesktopDriver

    driver = DesktopDriver(
        profile=DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        backend=backend,
        lease_path=tmp_path / "certificate.lock",
    )
    target = OwnedWindow(
        os.getpid(),
        0x600119,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({0x600119}),
        "/usr/bin/python3",
        "target-start",
        0x600118,
        (0, 0, 500, 260),
        frozenset({0x600118, 0x600119}),
    )
    try:
        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.1, calibration_ticket=ticket)
        ticket = driver._calibration_ticket
        assert ticket is not None
        assert ticket.target_xids == frozenset({0x600118, 0x600119})
        assert ticket.managed_frame_xid == 0x600118
        assert ticket.contains(0x600118)
        assert ticket.contains(0x600119)
        assert not ticket.contains(0x374)

        for event_targets in (
            frozenset({0x374, 0x600119}),  # root substituted for the frame
            frozenset({0x600117, 0x600119}),  # unrelated frame substituted
            frozenset({0x600118, 0x600119, 0x600117}),  # unrelated sibling added
        ):
            tampered = replace(target, certified_event_target_xids=event_targets)
            with pytest.raises(DriverError, match="certificate"):
                driver._verify_calibration_target(ticket, tampered)
    finally:
        driver.close()


def test_guardian_requires_pinned_detail_and_delivered_modifiers(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "guardian.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class Event:
        event_class = "KeyPress"
        source_device = 5
        master_device = 5
        target_xid = 0x600119
        detail = 38
        timestamp_ms = 1
        modifiers = (0x10, 0, 0)
        modifier_effective = 0x10
        is_raw = False

    try:
        generation = guardian.arm(Evidence())
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=5,
            master_device=5,
            event_classes={"KeyPress"},
            required_sequence=("KeyPress",),
            expected_details=(38,),
            expected_modifiers=(0x10, 0, 0),
        )
        assert guardian.observe(Event())
        guardian.finish_action(generation)

        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=5,
            master_device=5,
            event_classes={"KeyPress"},
            required_sequence=("KeyPress",),
            expected_details=(38,),
            expected_modifiers=(0, 0, 0),
        )
        bad = Event()
        bad.detail = 999
        assert not guardian.observe(bad)
        assert not guardian.armed
    finally:
        guardian.close()


def test_guardian_accepts_only_sealed_controlled_motion_echo(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "echo.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class Event:
        event_class = "Motion"
        source_device = 7
        master_device = 7
        target_xid = 0x600119
        detail = None
        timestamp_ms = 11
        modifiers = (0x10, 0, 0)
        modifier_effective = 0x10
        is_raw = False
        root_xy = (10.0, 20.0)
        event_xy = (1.0, 2.0)

    try:
        generation = guardian.arm(Evidence())
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            target_ancestor=lambda xid: xid == 0x600119,
            optional_classes={"Motion"},
            expected_modifiers=(0x10, 0, 0),
            echo_proof=(("MotionEcho", 11, 7, 7, 0x600119, (10.0, 20.0), (1.0, 2.0), 9, 4, 0x600119),),
        )
        current = Event()
        current.source_device = 4
        current.master_device = 4
        current.timestamp_ms = 9
        current.root_xy = (10.0, 20.0)
        current.event_xy = (1.0, 2.0)
        assert guardian.observe(current)
        assert guardian.observe(Event())
        guardian.finish_action(generation)

        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            target_ancestor=lambda xid: xid == 0x600119,
            optional_classes={"Motion"},
            expected_modifiers=(0x10, 0, 0),
            echo_proof=(("MotionEcho", 11, 7, 7, 0x600119, (10.0, 20.0), (1.0, 2.0), 9, 4, 0x600119),),
        )
        bad = Event()
        bad.timestamp_ms = 5000
        assert not guardian.observe(bad)
        assert not guardian.armed
    finally:
        guardian.close()


@pytest.mark.parametrize(
    ("primary_timestamp", "forwarded_timestamp", "accepted"),
    (
        (XI2_TIMESTAMP_MASK, 0, True),
        (100, 350, True),
        (100, 351, False),
        (100, 50, False),
    ),
)
def test_guardian_controlled_echo_uses_forward_card32_250ms_boundary(
    tmp_path: Path, primary_timestamp: int, forwarded_timestamp: int, accepted: bool
) -> None:
    guardian = InputGuardian(
        tmp_path / f"echo-boundary-{primary_timestamp}-{forwarded_timestamp}.lock",
        parent_pid=os.getppid(),
    )

    class Evidence:
        is_live = True
        fingerprint = "owned-echo-boundary"

    target = 0x600119
    point = (10.0, 20.0)
    event_point = (1.0, 2.0)
    modifiers = (0x10, 0, 0)

    def event(source: int, master: int, timestamp: int) -> SimpleNamespace:
        return SimpleNamespace(
            event_class="Motion",
            source_device=source,
            master_device=master,
            target_xid=target,
            child_xid=target,
            detail=None,
            timestamp_ms=timestamp,
            modifiers=modifiers,
            modifier_effective=0x10,
            is_raw=False,
            root_xy=point,
            event_xy=event_point,
        )

    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=target,
            source_device=4,
            master_device=2,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            optional_classes={"Motion"},
            expected_modifiers=modifiers,
            echo_proof=(
                (
                    "MotionEcho",
                    forwarded_timestamp,
                    7,
                    7,
                    target,
                    point,
                    event_point,
                    primary_timestamp,
                    4,
                    target,
                ),
            ),
        )
        assert guardian.observe(event(4, 4, primary_timestamp))
        if accepted:
            assert guardian.observe(event(7, 7, forwarded_timestamp))
            assert action.complete
            guardian.finish_action(generation, action)
        else:
            assert not guardian.observe(event(7, 7, forwarded_timestamp))
            assert not guardian.armed
    finally:
        guardian.close()


@pytest.mark.parametrize(
    ("timestamp_index", "invalid_timestamp"),
    (
        (1, True),
        (1, -1),
        (1, XI2_TIMESTAMP_MASK + 1),
        (7, True),
        (7, -1),
        (7, XI2_TIMESTAMP_MASK + 1),
    ),
)
def test_guardian_rejects_invalid_sealed_echo_timestamps(
    tmp_path: Path, timestamp_index: int, invalid_timestamp: object
) -> None:
    guardian = InputGuardian(
        tmp_path / f"echo-invalid-{timestamp_index}-{invalid_timestamp!s}.lock", parent_pid=os.getppid()
    )

    class Evidence:
        is_live = True
        fingerprint = "owned-invalid-echo"

    echo = [
        "MotionEcho",
        5,
        7,
        7,
        0x600119,
        (10.0, 20.0),
        (1.0, 2.0),
        4,
        4,
        0x600119,
    ]
    echo[timestamp_index] = invalid_timestamp
    try:
        generation = guardian.arm(Evidence())
        with pytest.raises(GuardianError, match="controlled echo proof"):
            guardian.begin_action(
                generation,
                target_xid=0x600119,
                source_device=4,
                master_device=2,
                event_classes={"Motion"},
                device_by_class={"Motion": 4},
                optional_classes={"Motion"},
                expected_modifiers=(0x10, 0, 0),
                echo_proof=(tuple(echo),),
            )
    finally:
        guardian.close()


def test_guardian_raw_motion_never_completes_an_action(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "raw-motion.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class Event:
        event_class = "RawMotion"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = RAW_MOTION_DETAIL
        timestamp_ms = 11
        modifiers = None
        modifier_effective = None
        is_raw = True
        root_xy = None
        event_xy = None

    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion"},
            device_by_class={"RawMotion": 2},
            optional_classes={"RawMotion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(Event())
        assert not action.complete
        with pytest.raises(GuardianError, match="event trace is incomplete"):
            guardian.finish_action(generation, action)
        assert not guardian.armed
    finally:
        guardian.close()


def test_guardian_pairs_raw_motion_with_same_timestamp_delivered_motion(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "raw-motion-pair.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class Event:
        target_xid = 0x600119
        detail = None
        modifiers = (0x10, 0, 0)
        modifier_effective = 0x10
        root_xy = (10.0, 20.0)
        event_xy = (1.0, 2.0)
        is_raw = False

    class RawEvent(Event):
        event_class = "RawMotion"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = RAW_MOTION_DETAIL
        modifiers = None
        modifier_effective = None
        root_xy = None
        event_xy = None
        is_raw = True
        timestamp_ms = 11

    class MotionEvent(Event):
        event_class = "Motion"
        source_device = 4
        master_device = 4
        timestamp_ms = 11

    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            target_ancestor=lambda xid: xid == 0x600119,
            optional_classes={"RawMotion", "Motion"},
            expected_modifiers=(0x10, 0, 0),
            expected_motion_trace=((10, 20, 1, 2),),
        )
        assert guardian.observe(RawEvent())
        assert not action.complete
        assert guardian.observe(MotionEvent())
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()


def test_guardian_raw_motion_pairing_is_forward_one_to_one_and_bounded(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "raw-motion-bounded.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class RawEvent:
        event_class = "RawMotion"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = RAW_MOTION_DETAIL
        modifiers = None
        modifier_effective = None
        is_raw = True
        root_xy = None
        event_xy = None

        def __init__(self, timestamp_ms: int) -> None:
            self.timestamp_ms = timestamp_ms

    class MotionEvent:
        event_class = "Motion"
        source_device = 4
        master_device = 4
        target_xid = 0x600119
        detail = None
        modifiers = (0, 0, 0)
        modifier_effective = 0
        is_raw = False
        root_xy = (10.0, 20.0)
        event_xy = (1.0, 2.0)

        def __init__(self, timestamp_ms: int) -> None:
            self.timestamp_ms = timestamp_ms

    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(RawEvent(12))
        assert guardian.observe(MotionEvent(11))
        assert not action.complete
        assert guardian.observe(MotionEvent(12))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()


def test_guardian_retains_forward_arrival_clock_after_lagged_raw_pairs(tmp_path: Path) -> None:
    class Evidence:
        is_live = True
        fingerprint = "owned-raw-lag"

    class RawEvent:
        event_class = "RawMotion"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = RAW_MOTION_DETAIL
        modifiers = None
        modifier_effective = None
        is_raw = True
        root_xy = None
        event_xy = None

        def __init__(self, timestamp_ms: int) -> None:
            self.timestamp_ms = timestamp_ms

    class MotionEvent:
        event_class = "Motion"
        source_device = 4
        master_device = 4
        target_xid = 0x600119
        child_xid = 0x600119
        detail = None
        modifiers = (0, 0, 0)
        modifier_effective = 0
        is_raw = False
        root_xy = (10.0, 20.0)
        event_xy = (1.0, 2.0)

        def __init__(self, timestamp_ms: int) -> None:
            self.timestamp_ms = timestamp_ms

    class DeviceChange:
        event_class = "DeviceChanged"
        source_device = 4
        master_device = 2
        target_xid = None
        child_xid = None
        detail = None
        modifiers = None
        modifier_effective = None
        is_raw = False
        root_xy = None
        event_xy = None

        def __init__(self, timestamp_ms: int) -> None:
            self.timestamp_ms = timestamp_ms

    def begin(guardian: InputGuardian, generation: int):
        return guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion", "DeviceChanged"},
            device_by_class={"RawMotion": 2, "Motion": 4, "DeviceChanged": 2},
            optional_classes={"RawMotion", "Motion", "DeviceChanged"},
            require_motion_before_button=False,
        )

    guardian = InputGuardian(tmp_path / "raw-motion-lag.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        action = begin(guardian, generation)
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(RawEvent(12))
        assert guardian.observe(MotionEvent(11))
        assert guardian.observe(MotionEvent(12))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-lag-wrap.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        action = begin(guardian, generation)
        assert guardian.observe(RawEvent(XI2_TIMESTAMP_MASK))
        assert guardian.observe(RawEvent(0))
        assert guardian.observe(MotionEvent(XI2_TIMESTAMP_MASK))
        assert guardian.observe(MotionEvent(0))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-lag-retention.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        begin(guardian, generation)
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(RawEvent(12))
        assert guardian.observe(MotionEvent(11))
        assert not guardian.observe(DeviceChange(11))
        assert not guardian.armed
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-wrap.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(RawEvent(XI2_TIMESTAMP_MASK))
        assert guardian.observe(MotionEvent(XI2_TIMESTAMP_MASK))
        assert guardian.observe(RawEvent(0))
        assert guardian.observe(MotionEvent(0))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-backward.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(MotionEvent(10))
        assert not guardian.observe(RawEvent(9))
        assert not guardian.armed
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-duplicate.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(MotionEvent(11))
        assert not action.complete
        with pytest.raises(GuardianError, match="incomplete"):
            guardian.finish_action(generation, action)
        assert not guardian.armed
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-forward-equal.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(MotionEvent(11))
        assert guardian.observe(RawEvent(11))
        assert not action.complete
        with pytest.raises(GuardianError, match="incomplete"):
            guardian.finish_action(generation, action)
        assert not guardian.armed

        # The second following primary consumes the second equal-time raw
        # record, proving pairing is forward and one-to-one.
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            require_motion_before_button=False,
        )
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(MotionEvent(11))
        assert guardian.observe(RawEvent(11))
        assert guardian.observe(MotionEvent(11))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "raw-motion-overflow.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(Evidence())
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion"},
            device_by_class={"RawMotion": 2},
            optional_classes={"RawMotion"},
            require_motion_before_button=False,
        )
        for timestamp in range(RAW_MOTION_PENDING_LIMIT):
            assert guardian.observe(RawEvent(timestamp + 1))
        assert not guardian.observe(RawEvent(RAW_MOTION_PENDING_LIMIT + 1))
        assert not guardian.armed
    finally:
        guardian.close()


@pytest.mark.parametrize("variant", ("source", "master", "time", "target", "human", "idle", "post-release"))
def test_guardian_rejects_unpaired_or_unowned_raw_motion(tmp_path: Path, variant: str) -> None:
    guardian = InputGuardian(tmp_path / f"raw-motion-{variant}.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned"

    class Event:
        event_class = "RawMotion"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = RAW_MOTION_DETAIL
        timestamp_ms = 11
        modifiers = None
        modifier_effective = None
        is_raw = True
        root_xy = None
        event_xy = None

    class Release:
        event_class = "RawButtonRelease"
        source_device = 4
        master_device = 2
        target_xid = None
        detail = 1
        timestamp_ms = 10
        modifiers = None
        modifier_effective = None
        is_raw = True
        root_xy = None
        event_xy = None

    try:
        generation = guardian.arm(Evidence())
        if variant == "idle":
            assert not guardian.observe(Event())
            return
        action_classes = {"RawMotion"}
        required_sequence: tuple[str, ...] = ()
        optional_classes = {"RawMotion"}
        device_by_class = {"RawMotion": 2}
        if variant == "time":
            action_classes.add("Motion")
            optional_classes.add("Motion")
            device_by_class["Motion"] = 4
        if variant == "post-release":
            action_classes.add("RawButtonRelease")
            required_sequence = ("RawButtonRelease",)
            optional_classes.add("RawButtonRelease")
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes=action_classes,
            device_by_class={**device_by_class, "RawButtonRelease": 2},
            required_sequence=required_sequence,
            optional_classes=optional_classes,
            detail=1 if variant == "post-release" else None,
            require_motion_before_button=False,
        )
        if variant == "post-release":
            assert guardian.observe(Release())
        event = Event()
        if variant == "source" or variant == "human":
            event.source_device = 9
        if variant == "master":
            event.master_device = 4
        if variant == "target":
            event.target_xid = 0x600119
        if variant == "time":
            assert guardian.observe(event)
        else:
            assert not guardian.observe(event)
        if variant == "time":
            motion = SimpleNamespace(
                event_class="Motion",
                source_device=4,
                master_device=4,
                target_xid=0x600119,
                detail=None,
                timestamp_ms=12,
                modifiers=(0, 0, 0),
                modifier_effective=0,
                is_raw=False,
                root_xy=(10.0, 20.0),
                event_xy=(1.0, 2.0),
            )
            assert not guardian.observe(motion)
        assert not guardian.armed
    finally:
        guardian.close()


def test_production_pointer_dispatch_pairs_current_motion_with_forwarded_echo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A seeded native gesture admits only its current source-4/7 Motion pairs."""

    from crowley.session import driver as driver_module
    from crowley.session.driver import DesktopDriver, WindowIdentity
    from crowley.session.executor import BindingMetadata, Point, PointerButton, PointerTarget, Rect
    from crowley.session.pointer_execution import plan_human_pointer
    from crowley.session.startup import StartupEnvironment, mint_runtime_evidence

    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "game-start",
        ":test",
        "game-test-identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    backend.resolve_identity = lambda require_foreground=True: identity  # type: ignore[method-assign]
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "production-echo.lock")

    modifiers = (0x10, 0, 0)
    hierarchy = (
        (2, 3, "master", "pointer:Virtual core pointer"),
        (3, 2, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (6, 2, "slave", "pointer:xwayland-pointer:17"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
        (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
        (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
    )
    xtest_profile = XTestInputProfile(
        4,
        2,
        "pointer:Virtual core XTEST pointer",
        3,
        ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
        1,
        1,
        (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        0x374,
        (0, 0, 1920, 1080),
        hierarchy,
    )
    environment = StartupEnvironment(
        display=profile.display,
        hierarchy=hierarchy,
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=xtest_profile,
        keymap_levels=((38, ("a", "A")),),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version="private-test-input-tool",
        input_tool_sha256="c" * 64,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-runtime",
        readiness_sequence=12,
        environment=environment,
        identity=identity,
        keyboard_keymap=(("a", 38),),
        native_modifiers=modifiers,
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=(),
    )
    assert evidence.forwarded_echo_sources == frozenset({7})
    assert evidence.synthetic_pointer_master == 2
    assert evidence.forwarded_echo_sources != frozenset({evidence.synthetic_pointer_master})
    backend.xtest_input_profile = lambda _hierarchy, **_kwargs: evidence.xtest_profile  # type: ignore[method-assign]

    def driver_input_hierarchy(**_kwargs: object):
        return evidence.xinput_hierarchy

    driver.input_hierarchy = driver_input_hierarchy  # type: ignore[method-assign]
    driver._input_evidence = evidence

    seed = 0x51A7

    def seeded_plan(*args: object, **kwargs: object):
        kwargs["seed"] = seed
        return plan_human_pointer(*args, **kwargs)

    monkeypatch.setattr(driver_module, "plan_human_pointer", seeded_plan)
    anchor = MeasuredAnchor(
        action="logout",
        x=30,
        y=40,
        identity_fingerprint=identity.fingerprint,
        target_xid=identity.child_xid,
        capture_id="pointer-dispatch-capture",
        capture_sequence=1,
        source="native-measured",
        observed_monotonic=time.monotonic(),
        capture_monotonic=time.monotonic(),
        generation=1,
        bounds=(26, 36, 12, 12),
        target_ancestry=(identity.child_xid,),
    ).with_hash()

    binding = BindingMetadata(
        identity_fingerprint=identity.fingerprint,
        capture_id=anchor.capture_id,
        capture_sequence=anchor.capture_sequence,
        generation=1,
        target_xid=anchor.target_xid,
        target_ancestry=anchor.target_ancestry,
        source=anchor.source,
    )
    precomputed_plan = plan_human_pointer(
        Point(10, 20),
        PointerTarget("world", Rect(*anchor.bounds), binding, padding=1),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=seed,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )
    planned_trace: list[tuple[int, int, int, int]] = []
    prior = precomputed_plan.trajectory.samples[0].point
    for sample in precomputed_plan.trajectory.samples[1:]:
        if sample.point == prior:
            continue
        root = (
            identity.child_geometry[0] + int(sample.point.x),
            identity.child_geometry[1] + int(sample.point.y),
        )
        planned_trace.append((*root, *root))
        prior = sample.point
    assert planned_trace

    def measure(action: str, *, generation: int | None = None) -> object:
        assert action == "logout"
        assert generation == 1
        driver._measured_anchors[action] = anchor
        return anchor

    driver.measure_pointer_anchor = measure  # type: ignore[method-assign]
    backend.consume_measured_anchor = lambda action, record: None  # type: ignore[method-assign]

    class Monitor:
        def wait_for_event(self, predicate: object, *, timeout: float) -> None:
            del timeout
            assert predicate([])

        def stop(self) -> str:
            return ""

    driver._monitor = Monitor()  # type: ignore[assignment]
    driver.backend.pointer_position = lambda **_kwargs: (10, 20, identity.child_xid)  # type: ignore[method-assign]
    driver.backend._is_owned_client_target = lambda xid, current: xid in current.target_event_xids  # type: ignore[method-assign]
    backend.pointer_position = lambda **_kwargs: (10, 20, identity.child_xid)  # type: ignore[method-assign]
    backend._is_owned_client_target = lambda xid, current: xid in current.target_event_xids  # type: ignore[method-assign]

    def action(name: str, *, generation: int, **kwargs: object) -> None:
        assert name == "logout" and generation == 1
        gesture = kwargs.get("_gesture_plan")
        assert gesture is not None and gesture.seed == seed
        assert gesture.trajectory == precomputed_plan.trajectory
        assert gesture.endpoint == precomputed_plan.endpoint

        def event(
            event_class: EventClass,
            source: int,
            master: int,
            timestamp: int,
            *,
            detail: int | None,
            target: int | None,
            point: tuple[float, float] | None = None,
            raw: bool = False,
        ) -> XI2Event:
            return XI2Event(
                event_class=event_class,
                type_number={
                    EventClass.RAW_MOTION: 17,
                    EventClass.MOTION: 6,
                    EventClass.RAW_BUTTON_PRESS: 15,
                    EventClass.BUTTON_PRESS: 4,
                    EventClass.RAW_BUTTON_RELEASE: 16,
                    EventClass.BUTTON_RELEASE: 5,
                }[event_class],
                master_device=master,
                source_device=source,
                timestamp_ms=timestamp,
                detail=detail,
                root_xid=None if raw else evidence.xtest_profile.root_xid,
                event_xid=None if raw else evidence.xtest_profile.root_xid,
                child_xid=target,
                root_xy=point,
                event_xy=point,
                modifiers=None if raw else modifiers,
                modifier_effective=None if raw else 0x10,
                valuators=(() if point is None else ((0, point[0], None), (1, point[1], None))),
            )

        records: list[XI2Event] = []
        timestamp = 100
        for root_x, root_y, _event_x, _event_y in planned_trace:
            point = (float(root_x), float(root_y))
            records.append(
                event(
                    EventClass.RAW_MOTION,
                    4,
                    2,
                    timestamp,
                    detail=RAW_MOTION_DETAIL,
                    target=None,
                    point=None,
                    raw=True,
                )
            )
            # Raw XI2 motion carries endpoint values through valuators, while
            # the primary/forwarded pair carries the canonical root coords.
            records[-1] = replace(
                records[-1],
                valuators=((0, float(root_x), None), (1, float(root_y), None)),
            )
            records.append(
                event(
                    EventClass.MOTION,
                    4,
                    4,
                    timestamp,
                    detail=RAW_MOTION_DETAIL,
                    target=identity.child_xid,
                    point=point,
                )
            )
            records.append(
                event(
                    EventClass.MOTION,
                    7,
                    7,
                    timestamp + 1,
                    detail=RAW_MOTION_DETAIL,
                    target=identity.child_xid,
                    point=point,
                )
            )
            timestamp += 2
        endpoint = (
            float(identity.child_geometry[0] + int(gesture.endpoint.x)),
            float(identity.child_geometry[1] + int(gesture.endpoint.y)),
        )
        records.extend(
            (
                event(EventClass.RAW_BUTTON_PRESS, 4, 2, timestamp, detail=1, target=None, raw=True),
                event(
                    EventClass.BUTTON_PRESS,
                    4,
                    4,
                    timestamp,
                    detail=1,
                    target=identity.child_xid,
                    point=endpoint,
                ),
                event(EventClass.RAW_BUTTON_RELEASE, 4, 2, timestamp + 1, detail=1, target=None, raw=True),
                event(
                    EventClass.BUTTON_RELEASE,
                    4,
                    4,
                    timestamp + 1,
                    detail=1,
                    target=identity.child_xid,
                    point=endpoint,
                ),
            )
        )
        # Keep this assertion close to the production admission shape: Motion
        # stays on source 4 and raw button transitions use its measured master.
        for index, record in enumerate(records):
            assert driver.guardian.observe(record), (
                index,
                driver.guardian.revoked_reason,
                driver.guardian.action,
            )
        assert driver.guardian.action is not None
        assert driver.guardian.action.echo_sources == frozenset({7})
        assert driver.guardian.action.motion_index == len(planned_trace)
        assert driver.guardian.action.echo_index == len(planned_trace)

    backend.action = action  # type: ignore[method-assign]
    try:
        generation = driver.guardian.arm(evidence)
        driver.run(
            "logout",
            generation,
            precondition=lambda _identity: True,
            postcondition=lambda _identity: True,
        )
        assert driver.guardian.armed
        driver.guardian.begin_action(
            generation,
            target_xid=identity.child_xid,
            source_device=evidence.synthetic_pointer_source,
            master_device=evidence.synthetic_pointer_master,
            event_classes={EventClass.MOTION.value},
            device_by_class={EventClass.MOTION.value: evidence.synthetic_pointer_source},
            optional_classes={EventClass.MOTION.value},
            echo_sources=evidence.forwarded_echo_sources,
        )
        assert not driver.guardian.observe(
            XI2Event(
                event_class=EventClass.MOTION,
                type_number=6,
                master_device=7,
                source_device=7,
                timestamp_ms=50_000,
                detail=RAW_MOTION_DETAIL,
                root_xid=0x374,
                event_xid=0x374,
                child_xid=identity.child_xid,
                root_xy=(40.0, 50.0),
                event_xy=(40.0, 50.0),
                modifiers=modifiers,
                modifier_effective=0x10,
            )
        )
        assert not driver.guardian.armed
        assert "unattributed XI2 event" in driver.guardian.revoked_reason
    finally:
        driver.close()


def test_world_pointer_probe_completes_with_forwarded_echo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """World probing admits the same source-4/7 motion topology as normal pointers."""

    from crowley.session import driver as driver_module
    from crowley.session.executor import BindingMetadata, Point, PointerTarget, Rect
    from crowley.session.pointer_execution import plan_human_pointer
    from crowley.session.startup import StartupEnvironment, mint_runtime_evidence

    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "world-tool"))
    identity = WindowIdentity(
        0x710100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x710101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x710100,
        "game-start",
        ":test",
        "world-pointer-identity",
        0x1,
        0x710100,
        frozenset({0x710100, 0x710101}),
    )
    backend.resolve_identity = lambda require_foreground=True, **_kwargs: identity  # type: ignore[method-assign]
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "world-pointer.lock")

    modifiers = (0x10, 0, 0)
    hierarchy = (
        (2, 3, "master", "pointer:Virtual core pointer"),
        (3, 2, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
    )
    xtest_profile = XTestInputProfile(
        4,
        2,
        "pointer:Virtual core XTEST pointer",
        3,
        ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
        1,
        1,
        (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        0x374,
        (0, 0, 1920, 1080),
        hierarchy,
    )
    environment = StartupEnvironment(
        display=profile.display,
        hierarchy=hierarchy,
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=xtest_profile,
        keymap_levels=((38, ("a", "A")),),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version="private-test-input-tool",
        input_tool_sha256="c" * 64,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-world-pointer",
        readiness_sequence=12,
        environment=environment,
        identity=identity,
        keyboard_keymap=(("a", 38),),
        native_modifiers=modifiers,
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=(),
    )
    backend.xtest_input_profile = lambda _hierarchy, **_kwargs: evidence.xtest_profile  # type: ignore[method-assign]
    driver.input_hierarchy = lambda **_kwargs: evidence.xinput_hierarchy  # type: ignore[method-assign]
    driver._input_evidence = evidence

    seed = 0x72A1
    start = Point(10, 20)
    target = Point(100, 120)
    binding = BindingMetadata(
        identity_fingerprint=identity.fingerprint,
        capture_id=f"world-pointer:1:{identity.fingerprint}",
        capture_sequence=1,
        generation=1,
        target_xid=identity.child_xid,
        target_ancestry=(identity.child_xid,),
        source="native-telemetry",
    )
    planned = plan_human_pointer(
        start,
        PointerTarget("world-hover", Rect(int(target.x), int(target.y), 1, 1), binding, padding=0),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=seed,
        button=None,
        profile=driver.pointer_motion_profile,
    )
    trace: list[tuple[int, int, int, int]] = []
    prior = planned.trajectory.samples[0].point
    for sample in planned.trajectory.samples[1:]:
        if sample.point == prior:
            continue
        root = (int(sample.point.x), int(sample.point.y))
        trace.append((*root, *root))
        prior = sample.point
    assert trace
    monkeypatch.setattr(driver_module.secrets, "randbits", lambda _bits: seed)

    records: list[XI2Event] = []
    origin = (10.0, 20.0)
    for timestamp, (root_x, root_y, _event_x, _event_y) in enumerate(trace, 100):
        point = (float(root_x), float(root_y))
        records.extend(
            (
                XI2Event(
                    event_class=EventClass.RAW_MOTION,
                    type_number=17,
                    master_device=2,
                    source_device=4,
                    timestamp_ms=timestamp,
                    detail=RAW_MOTION_DETAIL,
                    root_xid=None,
                    event_xid=None,
                    child_xid=None,
                    root_xy=None,
                    event_xy=None,
                    modifiers=None,
                    modifier_effective=None,
                    valuators=((0, point[0], None), (1, point[1], None)),
                ),
                XI2Event(
                    event_class=EventClass.MOTION,
                    type_number=6,
                    master_device=4,
                    source_device=4,
                    timestamp_ms=timestamp,
                    detail=RAW_MOTION_DETAIL,
                    root_xid=xtest_profile.root_xid,
                    event_xid=xtest_profile.root_xid,
                    child_xid=identity.child_xid,
                    root_xy=origin,
                    event_xy=origin,
                    modifiers=modifiers,
                    modifier_effective=0x10,
                    valuators=((0, point[0], None), (1, point[1], None)),
                ),
                XI2Event(
                    event_class=EventClass.MOTION,
                    type_number=6,
                    master_device=7,
                    source_device=7,
                    timestamp_ms=timestamp + 1,
                    detail=RAW_MOTION_DETAIL,
                    root_xid=xtest_profile.root_xid,
                    event_xid=xtest_profile.root_xid,
                    child_xid=identity.child_xid,
                    root_xy=point,
                    event_xy=point,
                    modifiers=modifiers,
                    modifier_effective=0x10,
                    valuators=((0, point[0], None), (1, point[1], None)),
                ),
            )
        )
        origin = point

    pointer = [(10, 20, identity.child_xid)]
    endpoint = (int(planned.endpoint.x), int(planned.endpoint.y), identity.child_xid)

    def run(_argv: list[str], **_kwargs: object) -> None:
        for record in records:
            assert driver.guardian.observe(record), driver.guardian.revoked_reason
        pointer[0] = endpoint

    backend._run = run  # type: ignore[method-assign]
    backend.pointer_position = lambda **_kwargs: pointer[0]  # type: ignore[method-assign]
    backend._is_owned_client_target = (  # type: ignore[method-assign]
        lambda xid, current: xid in current.target_event_xids
    )

    class Monitor:
        def wait_for_event(self, predicate: object, *, timeout: float) -> None:
            del timeout
            assert predicate([])

        def stop(self) -> str:
            return ""

    driver._monitor = Monitor()  # type: ignore[assignment]
    try:
        generation = driver.guardian.arm(evidence)
        gesture = driver.run_pointer_motion(target, generation=generation)
        assert gesture.trajectory.endpoint == planned.trajectory.endpoint
        assert driver.guardian.armed
        assert driver.guardian.action is None
    finally:
        driver.close()


def test_production_camera_drag_entrypoint_seals_press_motion_release_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.executor import (
        BindingMetadata,
        Point,
        Rect,
        plan_camera_drag,
    )

    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = RecordingBackend()
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "camera-entry.lock")
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "game-start",
        ":test",
        "camera-entry-identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    binding = BindingMetadata(
        identity_fingerprint=identity.fingerprint,
        capture_id="camera-capture",
        capture_sequence=1,
        generation=1,
        target_xid=identity.child_xid,
        target_ancestry=(identity.child_xid,),
        source="camera",
    )
    plan = plan_camera_drag(Point(100, 100), Point(110, 120), Rect(0, 0, 1920, 1080), binding)
    captured: dict[str, object] = {}
    call_order: list[str] = []

    backend.pointer_position = lambda **_kwargs: (100, 100, identity.child_xid)  # type: ignore[method-assign]

    trace: list[tuple[int, int, int, int]] = []
    prior = plan.samples[0].point
    for sample in plan.samples[1:]:
        if sample.point == prior:
            continue
        trace.append(
            (
                identity.child_geometry[0] + int(sample.point.x),
                identity.child_geometry[1] + int(sample.point.y),
                int(sample.point.x),
                int(sample.point.y),
            )
        )
        prior = sample.point
    echo_proof = tuple(
        (
            "MotionEcho",
            101 + index,
            7,
            7,
            identity.child_xid,
            (float(root_x), float(root_y)),
            (float(event_x), float(event_y)),
            100 + index,
            4,
            identity.child_xid,
        )
        for index, (root_x, root_y, event_x, event_y) in enumerate(trace)
    )
    evidence = SimpleNamespace(
        is_live=True,
        fingerprint="camera-entry-evidence",
        synthetic_echo_proof=echo_proof,
    )

    def input_context(
        *_args: object, **_kwargs: object
    ) -> tuple[object, object, int, int, tuple[int, int, int], object]:
        return identity, evidence, 4, 2, (0x10, 0, 0), ()

    monkeypatch.setattr(driver, "_motion_input_context", input_context)
    driver._is_owned_client_target = lambda xid, _identity: xid == identity.child_xid  # type: ignore[method-assign]
    real_begin_action = driver.guardian.begin_action

    def begin_action(generation: int, **kwargs: object):
        call_order.append("begin")
        captured.update(kwargs)
        return real_begin_action(generation, **kwargs)

    driver.guardian.begin_action = begin_action  # type: ignore[method-assign]

    real_dispatch_admission = driver.guardian.dispatch_admission

    def dispatch_admission(generation: int, callback: object):
        call_order.append("dispatch")
        return real_dispatch_admission(generation, callback)

    driver.guardian.dispatch_admission = dispatch_admission  # type: ignore[method-assign]

    real_check = driver.guardian.check

    def check(generation: int) -> None:
        call_order.append("check")
        real_check(generation)

    driver.guardian.check = check  # type: ignore[method-assign]

    real_finish_action = driver.guardian.finish_action

    def finish_action(generation: int, action: object | None = None) -> None:
        call_order.append("finish")
        assert driver.guardian.action is not None
        real_finish_action(generation, action)
        assert driver.guardian.action is None

    driver.guardian.finish_action = finish_action  # type: ignore[method-assign]

    def run_chain(chain: list[str], **kwargs: object) -> SimpleNamespace:
        captured["chain"] = list(chain)
        captured["runKwargs"] = kwargs
        driver.input_broker.redeem_button_emission_claim(
            kwargs["button_emission_claim"],
            emitted_chain=tuple(chain),
            button=PointerButton.RIGHT,
            action_generation=kwargs["emission_action_generation"],
            lease_generation=kwargs["emission_lease_generation"],
        )
        action = driver.guardian.action
        assert action is not None

        def event(
            event_class: str,
            source: int,
            master: int,
            timestamp: int,
            *,
            target: int | None,
            detail: int | None = None,
            raw: bool = False,
            root_xy: tuple[float, float] | None = None,
            event_xy: tuple[float, float] | None = None,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                event_class=event_class,
                source_device=source,
                master_device=master,
                target_xid=target,
                child_xid=target,
                detail=detail,
                timestamp_ms=timestamp,
                modifiers=None if raw else (0x10, 0, 0),
                modifier_effective=None if raw else 0x10,
                is_raw=raw,
                root_xy=root_xy,
                event_xy=event_xy,
            )

        records = [
            event(EventClass.RAW_BUTTON_PRESS.value, 4, 2, 99, target=None, detail=3, raw=True),
            event(
                EventClass.BUTTON_PRESS.value,
                4,
                4,
                99,
                target=identity.child_xid,
                detail=3,
                root_xy=(100.0, 100.0),
                event_xy=(100.0, 100.0),
            ),
        ]
        for index, (root_x, root_y, event_x, event_y) in enumerate(action.expected_motion_trace):
            records.extend(
                (
                    event(
                        EventClass.RAW_MOTION.value,
                        4,
                        2,
                        100 + index,
                        target=None,
                        detail=RAW_MOTION_DETAIL,
                        raw=True,
                    ),
                    event(
                        EventClass.MOTION.value,
                        4,
                        4,
                        100 + index,
                        target=identity.child_xid,
                        root_xy=(float(root_x), float(root_y)),
                        event_xy=(float(event_x), float(event_y)),
                    ),
                    event(
                        EventClass.MOTION.value,
                        7,
                        7,
                        101 + index,
                        target=identity.child_xid,
                        root_xy=(float(root_x), float(root_y)),
                        event_xy=(float(event_x), float(event_y)),
                    ),
                )
            )
        release_timestamp = 100 + len(action.expected_motion_trace) + 2
        records.extend(
            (
                event(
                    EventClass.RAW_BUTTON_RELEASE.value,
                    4,
                    2,
                    release_timestamp,
                    target=None,
                    detail=3,
                    raw=True,
                ),
                event(
                    EventClass.BUTTON_RELEASE.value,
                    4,
                    4,
                    release_timestamp,
                    target=identity.child_xid,
                    detail=3,
                    root_xy=(110.0, 120.0),
                    event_xy=(110.0, 120.0),
                ),
            )
        )
        for index, record in enumerate(records):
            assert driver.guardian.observe(record), (
                index,
                driver.guardian.revoked_reason,
                record,
            )
        assert action.complete
        return SimpleNamespace(returncode=0, args=list(chain), stdout="", stderr="")

    backend._run = run_chain  # type: ignore[attr-defined]

    real_close = driver.close

    def close() -> None:
        assert driver.guardian.action is None
        call_order.append("close")
        real_close()

    driver.close = close  # type: ignore[method-assign]

    class Monitor:
        def wait_for_event(self, predicate: object, *, timeout: float) -> None:
            del timeout
            assert predicate([])

        def stop(self) -> str:
            return ""

    driver._monitor = Monitor()  # type: ignore[assignment]
    original_mint = driver.input_broker.mint_admitted_gesture

    def record_generations(**kwargs: object):
        captured["actionGeneration"] = kwargs["action_generation"]
        captured["leaseGeneration"] = kwargs["lease_generation"]
        return original_mint(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(driver.input_broker, "mint_admitted_gesture", record_generations)
    try:
        assert driver.guardian.arm(evidence) == 1
        driver.run_camera_drag(plan, generation=1, action_generation=7)
    finally:
        driver.close()

    assert captured["detail"] == 3
    assert captured["expected_modifiers"] == (0x10, 0, 0)
    assert captured["expected_press_coordinates"] == (100.0, 100.0)
    assert captured["expected_release_coordinates"] == (110.0, 120.0)
    assert captured["expected_motion_trace"] == tuple(trace)
    assert captured["echo_proof"] == echo_proof
    assert "RawMotion" in captured["optional_classes"]
    assert captured["required_sequence"] == (
        "RawButtonPress",
        "ButtonPress",
        "RawButtonRelease",
        "ButtonRelease",
    )
    chain = captured["chain"]
    assert chain[0:3] == ["xdotool", "mousedown", "3"]
    assert chain[-2:] == ["mouseup", "3"]
    assert captured["runKwargs"]["input_schema"] == "camera-drag"
    assert captured["runKwargs"]["expected_argv"] == tuple(chain)
    assert captured["actionGeneration"] == 7
    assert captured["leaseGeneration"] == 1
    assert call_order == ["begin", "dispatch", "check", "finish", "close"]


@pytest.mark.parametrize("button", (1, 3))
def test_guardian_pointer_actions_accept_raw_motion_zero_detail_and_controlled_echo(
    tmp_path: Path, button: int
) -> None:
    guardian = InputGuardian(tmp_path / f"pointer-raw-detail-{button}.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned-pointer"

    target = 0x600119
    point = (10.0, 20.0)
    event_point = (1.0, 2.0)
    modifiers = (0x10, 0, 0)

    def event(
        event_class: str,
        timestamp: int,
        *,
        source: int,
        master: int,
        raw: bool,
        detail: int | None,
        event_target: int | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            event_class=event_class,
            source_device=source,
            master_device=master,
            target_xid=event_target,
            child_xid=event_target,
            detail=detail,
            timestamp_ms=timestamp,
            modifiers=None if raw else modifiers,
            modifier_effective=None if raw else 0x10,
            is_raw=raw,
            root_xy=None if raw else point,
            event_xy=None if raw else event_point,
        )

    echo = ("MotionEcho", 11, 7, 7, target, point, event_point, 10, 4, target)
    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=target,
            source_device=4,
            master_device=2,
            event_classes={
                "RawButtonPress",
                "ButtonPress",
                "RawButtonRelease",
                "ButtonRelease",
                "RawMotion",
                "Motion",
            },
            detail=button,
            device_by_class={
                "RawButtonPress": 2,
                "ButtonPress": 4,
                "RawButtonRelease": 2,
                "ButtonRelease": 4,
                "RawMotion": 2,
                "Motion": 4,
            },
            target_ancestor=lambda xid: xid == target,
            required_sequence=("RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"),
            optional_classes={"RawMotion", "Motion"},
            expected_modifiers=modifiers,
            expected_details=(button,) * 4,
            expected_motion_trace=((10, 20, 1, 2),),
            echo_proof=(echo,),
            require_motion_before_release=True,
        )
        records = (
            event("RawMotion", 10, source=4, master=2, raw=True, detail=RAW_MOTION_DETAIL, event_target=None),
            event("Motion", 10, source=4, master=4, raw=False, detail=0, event_target=target),
            event("Motion", 11, source=7, master=7, raw=False, detail=0, event_target=target),
            event("RawButtonPress", 12, source=4, master=2, raw=True, detail=button, event_target=None),
            event("ButtonPress", 12, source=4, master=4, raw=False, detail=button, event_target=target),
            event("RawButtonRelease", 13, source=4, master=2, raw=True, detail=button, event_target=None),
            event("ButtonRelease", 13, source=4, master=4, raw=False, detail=button, event_target=target),
        )
        for record in records:
            assert guardian.observe(record), (record.event_class, guardian.revoked_reason)
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()


def test_guardian_canonical_echo_requires_sealed_root_and_event_ids(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "canonical-echo-root-event.lock", parent_pid=os.getppid())
    target = 0x600119
    try:
        generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="canonical-echo"))
        action = guardian.begin_action(
            generation,
            target_xid=target,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((30, 40, 30, 40),),
            initial_origin=(10, 20, 10, 20),
            expected_root_xid=0x374,
            expected_event_xid=0x374,
            expected_child_xids={target},
            canonical_motion=True,
            echo_proof=(("MotionEcho", 11, 7, 7, target, (30.0, 40.0), (30.0, 40.0), 10, 4, target),),
            require_motion_before_button=False,
        )
        raw = XI2Event(
            17,
            EventClass.RAW_MOTION,
            2,
            4,
            10,
            0,
            None,
            None,
            None,
            valuators=((0, 30.0, 30.0), (1, 40.0, 40.0)),
        )
        primary = XI2Event(
            6,
            EventClass.MOTION,
            4,
            4,
            10,
            0,
            0x374,
            0x374,
            target,
            (10.0, 20.0),
            (10.0, 20.0),
            (0, 0, 0),
            modifier_effective=0,
            valuators=((0, 30.0, 30.0), (1, 40.0, 40.0)),
        )
        wrong_ids = XI2Event(
            6,
            EventClass.MOTION,
            7,
            7,
            11,
            0,
            0x375,
            0x375,
            target,
            (30.0, 40.0),
            (30.0, 40.0),
            (0, 0, 0),
            modifier_effective=0,
        )
        assert guardian.observe(raw)
        assert guardian.observe(primary)
        assert not guardian.observe(wrong_ids)
        assert not guardian.armed
        assert action is not guardian.action
    finally:
        guardian.close()


def test_guardian_canonical_motion_uses_certified_frame_targets_only(tmp_path: Path) -> None:
    """A certified managed-frame child is accepted; an unrelated child revokes."""

    target = 0x600119
    frame = 0x600118
    unrelated = 0x700101

    def motion_pair(timestamp: int, origin: tuple[int, int], endpoint: tuple[int, int], child: int):
        valuators = ((0, float(endpoint[0]), float(endpoint[0])), (1, float(endpoint[1]), float(endpoint[1])))
        return (
            XI2Event(
                17,
                EventClass.RAW_MOTION,
                2,
                4,
                timestamp,
                0,
                None,
                None,
                None,
                valuators=valuators,
            ),
            XI2Event(
                6,
                EventClass.MOTION,
                4,
                4,
                timestamp,
                0,
                0x374,
                0x374,
                child,
                (float(origin[0]), float(origin[1])),
                (float(origin[0]), float(origin[1])),
                (0, 0, 0),
                modifier_effective=0,
                valuators=valuators,
            ),
        )

    def begin(guardian: InputGuardian):
        generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="certified-targets"))
        action = guardian.begin_action(
            generation,
            target_xid=target,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            target_ancestor=lambda xid: xid in {target, frame},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((30, 40, 30, 40), (50, 60, 50, 60)),
            initial_origin=(10, 20, 10, 20),
            expected_root_xid=0x374,
            expected_event_xid=0x374,
            expected_child_xids={target, frame},
            canonical_motion=True,
            require_motion_before_button=False,
        )
        return generation, action

    guardian = InputGuardian(tmp_path / "certified-frame.lock", parent_pid=os.getppid())
    try:
        generation, action = begin(guardian)
        first_raw, first_primary = motion_pair(10, (10, 20), (30, 40), frame)
        assert guardian.observe(first_raw)
        assert guardian.observe(first_primary)
        assert action.last_accepted_endpoint == (30, 40, 30, 40)
    finally:
        guardian.close()

    guardian = InputGuardian(tmp_path / "unrelated-frame.lock", parent_pid=os.getppid())
    try:
        _generation, _action = begin(guardian)
        first_raw, first_primary = motion_pair(10, (10, 20), (30, 40), target)
        second_raw, second_primary = motion_pair(11, (30, 40), (50, 60), unrelated)
        assert guardian.observe(first_raw)
        assert guardian.observe(first_primary)
        assert guardian.observe(second_raw)
        assert not guardian.observe(second_primary)
        assert not guardian.armed
    finally:
        guardian.close()


def test_guardian_rejects_raw_motion_after_release_for_pointer_detail(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "pointer-raw-post-release.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned-pointer-post-release"

    try:
        generation = guardian.arm(Evidence())
        guardian.begin_action(
            generation,
            target_xid=0x600119,
            source_device=4,
            master_device=2,
            event_classes={"RawButtonRelease", "RawMotion"},
            detail=1,
            device_by_class={"RawButtonRelease": 2, "RawMotion": 2},
            required_sequence=("RawButtonRelease",),
            optional_classes={"RawMotion"},
        )
        release = SimpleNamespace(
            event_class="RawButtonRelease",
            source_device=4,
            master_device=2,
            target_xid=None,
            child_xid=None,
            detail=1,
            timestamp_ms=20,
            modifiers=None,
            modifier_effective=None,
            is_raw=True,
            root_xy=None,
            event_xy=None,
        )
        raw_motion = SimpleNamespace(
            event_class="RawMotion",
            source_device=4,
            master_device=2,
            target_xid=None,
            child_xid=None,
            detail=RAW_MOTION_DETAIL,
            timestamp_ms=21,
            modifiers=None,
            modifier_effective=None,
            is_raw=True,
            root_xy=None,
            event_xy=None,
        )
        assert guardian.observe(release)
        assert not guardian.observe(raw_motion)
        assert not guardian.armed
    finally:
        guardian.close()


def test_guardian_pairs_fifo_raw_motion_around_delayed_forwarded_echo(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "pointer-raw-delayed-echo.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "owned-pointer-delayed-echo"

    target = 0x600119
    point = (10.0, 20.0)
    event_point = (1.0, 2.0)
    modifiers = (0, 0, 0)

    def motion(source: int, master: int, timestamp: int) -> SimpleNamespace:
        return SimpleNamespace(
            event_class="Motion",
            source_device=source,
            master_device=master,
            target_xid=target,
            child_xid=target,
            detail=0,
            timestamp_ms=timestamp,
            modifiers=modifiers,
            modifier_effective=0,
            is_raw=False,
            root_xy=point,
            event_xy=event_point,
        )

    def raw(timestamp: int) -> SimpleNamespace:
        return SimpleNamespace(
            event_class="RawMotion",
            source_device=4,
            master_device=2,
            target_xid=None,
            child_xid=None,
            detail=RAW_MOTION_DETAIL,
            timestamp_ms=timestamp,
            modifiers=None,
            modifier_effective=None,
            is_raw=True,
            root_xy=None,
            event_xy=None,
        )

    echo_proof = (
        ("MotionEcho", 10, 7, 7, target, point, event_point, 10, 4, target),
        ("MotionEcho", 11, 7, 7, target, point, event_point, 11, 4, target),
    )
    try:
        generation = guardian.arm(Evidence())
        action = guardian.begin_action(
            generation,
            target_xid=target,
            source_device=4,
            master_device=2,
            event_classes={"RawMotion", "Motion"},
            device_by_class={"RawMotion": 2, "Motion": 4},
            optional_classes={"RawMotion", "Motion"},
            expected_modifiers=modifiers,
            expected_motion_trace=((10, 20, 1, 2), (10, 20, 1, 2)),
            echo_proof=echo_proof,
            require_motion_before_button=False,
        )
        assert guardian.observe(motion(4, 4, 10))
        assert guardian.observe(raw(11))
        # A forwarded echo for the earlier primary must not consume or reject
        # the pending raw record for the later primary.
        assert guardian.observe(motion(7, 7, 10))
        assert guardian.observe(motion(4, 4, 11))
        assert guardian.observe(motion(7, 7, 11))
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()
